import time
import json
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

# استيراد الدوال المشتركة من ملف المرافق
from utils import (
    init_db, check_db_connection, initialize_binance_client,
    fetch_historical_data, calculate_rsi_indicator, calculate_atr_indicator,
    get_btc_trend_4h, fetch_recent_volume, get_crypto_symbols,
    save_ml_model_to_db, convert_np_values, logger,
    RSI_PERIOD, VOLUME_LOOKBACK_CANDLES, RSI_MOMENTUM_LOOKBACK_CANDLES,
    BASE_ML_MODEL_NAME, ML_TARGET_LOOKAHEAD_CANDLES, TELEGRAM_TOKEN, CHAT_ID
)

# استيراد مكتبات تعلم الآلة
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, classification_report
from sklearn.preprocessing import StandardScaler
import requests # لإرسال رسائل تيليجرام

# ---------------------- ثوابت خاصة بالتدريب ----------------------
TRAINING_INTERVAL_DAYS: int = 7 # تدريب النموذج كل 7 أيام
TRAINING_LOOKBACK_DAYS: int = 60 # زيادة أيام البحث عن البيانات للتدريب


# ---------------------- ML Model Training Functions ----------------------

def prepare_data_for_ml(symbol: str, interval: str, lookback_days: int) -> Optional[pd.DataFrame]:
    """
    Fetches historical data, calculates features (volume, RSI momentum),
    and defines the target variable for ML training.
    """
    logger.info(f"ℹ️ [ML Data Prep] تجهيز البيانات لتدريب نموذج ML لـ {symbol}...")
    df = fetch_historical_data(symbol, interval, lookback_days)

    if df is None or df.empty:
        logger.warning(f"⚠️ [ML Data Prep] لا توجد بيانات كافية لـ {symbol} لتجهيزها للتدريب.")
        return None

    # حساب المؤشرات
    df = calculate_rsi_indicator(df, RSI_PERIOD)
    df['volume_15m_avg'] = df['volume'].rolling(window=VOLUME_LOOKBACK_CANDLES, min_periods=1).mean()

    df['rsi_momentum_bullish'] = 0
    if len(df) >= RSI_MOMENTUM_LOOKBACK_CANDLES + 1:
        for i in range(RSI_MOMENTUM_LOOKBACK_CANDLES, len(df)):
            rsi_slice = df['rsi'].iloc[i - RSI_MOMENTUM_LOOKBACK_CANDLES : i + 1]
            if not rsi_slice.isnull().any() and np.all(np.diff(rsi_slice) > 0) and rsi_slice.iloc[-1] > 50:
                df.loc[df.index[i], 'rsi_momentum_bullish'] = 1

    # إضافة اتجاه البيتكوين كميزة (ترميز يدوي)
    btc_trend = get_btc_trend_4h()
    df['btc_trend_encoded'] = 0 # محايد أو غير معروف
    if "صعود" in btc_trend:
        df['btc_trend_encoded'] = 1
    elif "هبوط" in btc_trend:
        df['btc_trend_encoded'] = -1

    # تعريف المتغير الهدف (target_movement): هل سيرتفع السعر بعد N شمعة؟
    # إذا ارتفع سعر الإغلاق بعد ML_TARGET_LOOKAHEAD_CANDLES شمعة، الهدف هو 1، وإلا فهو 0.
    df['future_close'] = df['close'].shift(-ML_TARGET_LOOKAHEAD_CANDLES)
    df['target_movement'] = (df['future_close'] > df['close']).astype(int)

    # إزالة الصفوف التي تحتوي على قيم NaN بعد حساب المؤشرات والهدف
    feature_cols = [
        'volume_15m_avg',
        'rsi_momentum_bullish',
        'btc_trend_encoded'
    ]
    df.dropna(subset=feature_cols + ['target_movement'], inplace=True)

    if df.empty:
        logger.warning(f"⚠️ [ML Data Prep] DataFrame فارغ بعد إزالة قيم NaN لـ {symbol}.")
        return None

    logger.info(f"✅ [ML Data Prep] تم تجهيز {len(df)} صفًا لتدريب نموذج ML لـ {symbol}.")
    return df

def train_ml_model(symbol: str, df: pd.DataFrame) -> Optional[Tuple[Any, Dict[str, Any]]]:
    """
    Trains an ML model (DecisionTreeClassifier) for a given symbol.
    Returns the trained model and its metrics.
    """
    logger.info(f"ℹ️ [ML Training] بدء تدريب نموذج ML لـ {symbol}...")

    feature_cols = [
        'volume_15m_avg',
        'rsi_momentum_bullish',
        'btc_trend_encoded'
    ]
    target_col = 'target_movement'

    X = df[feature_cols]
    y = df[target_col]

    if len(X) < 20: # الحد الأدنى لعدد العينات للتدريب
        logger.warning(f"⚠️ [ML Training] بيانات غير كافية لتدريب النموذج لـ {symbol} ({len(X)} عينة).")
        return None, None

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    # مقياس الميزات
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    model = DecisionTreeClassifier(random_state=42, max_depth=5) # يمكن تعديل المعلمات
    model.fit(X_train_scaled, y_train)

    y_pred = model.predict(X_test_scaled)
    accuracy = accuracy_score(y_test, y_pred)
    report = classification_report(y_test, y_pred, output_dict=True)

    metrics = {
        'accuracy': accuracy,
        'classification_report': report,
        'train_samples': len(X_train),
        'test_samples': len(X_test),
        'feature_columns': feature_cols
    }

    logger.info(f"✅ [ML Training] تم تدريب النموذج لـ {symbol}. الدقة: {accuracy:.4f}")
    logger.debug(f"📊 [ML Training] تقرير التصنيف لـ {symbol}:\n{json.dumps(metrics['classification_report'], indent=2)}")

    # إضافة الـ scaler كخاصية للنموذج ليتم حفظه معه
    model.scaler = scaler
    return model, metrics

def send_telegram_message(target_chat_id: str, text: str, parse_mode: str = 'Markdown') -> None:
    """Sends a message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': str(target_chat_id),
        'text': text,
        'parse_mode': parse_mode
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"✅ [Telegram] تم إرسال رسالة بنجاح إلى {target_chat_id}.")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Telegram] فشل إرسال الرسالة إلى {target_chat_id}: {e}")

# ---------------------- Main Execution Block ----------------------
if __name__ == "__main__":
    logger.info("🚀 بدء سكريبت تدريب نماذج تعلم الآلة...")
    logger.info(f"الوقت المحلي: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | وقت UTC: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        # تهيئة عميل Binance وقاعدة البيانات
        initialize_binance_client()
        init_db()

        symbols_to_train = get_crypto_symbols()
        if not symbols_to_train:
            logger.critical("❌ [Main] لا توجد رموز صالحة للتدريب. خروج.")
            exit(1)

        total_trained_models = 0
        total_skipped_models = 0
        training_results = []

        for symbol in symbols_to_train:
            logger.info(f"✨ [Main] بدء تدريب النموذج لـ {symbol}...")
            try:
                df_ml = prepare_data_for_ml(symbol, '5m', TRAINING_LOOKBACK_DAYS) # استخدام 5m كإطار زمني للتدريب
                if df_ml is None:
                    logger.warning(f"⚠️ [Main] تخطي تدريب {symbol}: لا توجد بيانات كافية أو تم إزالة جميع الصفوف بعد المعالجة.")
                    total_skipped_models += 1
                    training_results.append(f"❌ `{symbol}`: تم التخطي (بيانات غير كافية)")
                    continue

                model, metrics = train_ml_model(symbol, df_ml)
                if model and metrics:
                    if save_ml_model_to_db(symbol, model, metrics):
                        total_trained_models += 1
                        training_results.append(f"✅ `{symbol}`: تم التدريب بنجاح (دقة: {metrics['accuracy']:.2f})")
                    else:
                        logger.error(f"❌ [Main] فشل حفظ النموذج لـ {symbol} في قاعدة البيانات.")
                        total_skipped_models += 1
                        training_results.append(f"❌ `{symbol}`: فشل الحفظ في DB")
                else:
                    logger.warning(f"⚠️ [Main] تخطي تدريب {symbol}: فشل التدريب أو لا توجد مقاييس.")
                    total_skipped_models += 1
                    training_results.append(f"❌ `{symbol}`: فشل التدريب")

            except Exception as e:
                logger.error(f"❌ [Main] خطأ فادح أثناء تدريب النموذج لـ {symbol}: {e}", exc_info=True)
                total_skipped_models += 1
                training_results.append(f"❌ `{symbol}`: خطأ غير متوقع")
            time.sleep(1) # تأخير بسيط بين تدريب كل نموذج

        final_message = (
            f"📊 *تقرير تدريب نماذج تعلم الآلة:*\n"
            f"——————————————\n"
            f"✅ تم تدريب النماذج: *{total_trained_models}*\n"
            f"❌ تم تخطي النماذج: *{total_skipped_models}*\n"
            f"——————————————\n"
            f"التفاصيل:\n" + "\n".join(training_results) +
            f"\n——————————————\n"
            f"🕰️ _اكتمل في: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_"
        )
        send_telegram_message(CHAT_ID, final_message)
        logger.info("✅ [Main] اكتملت عملية تدريب النماذج لجميع الأزواج.")

    except Exception as startup_err:
        logger.critical(f"❌ [Main] حدث خطأ فادح أثناء بدء التشغيل أو في حلقة التدريب الرئيسية: {startup_err}", exc_info=True)
        send_telegram_message(CHAT_ID, f"❌ *خطأ فادح في سكريبت تدريب النماذج:*\n`{str(startup_err)}`")
    finally:
        logger.info("🛑 [Main] يتم إيقاف تشغيل سكريبت التدريب...")
        if check_db_connection() and utils.conn: # استخدام utils.conn للوصول إلى الاتصال العام
            try:
                utils.conn.close()
                logger.info("✅ [DB] تم إغلاق اتصال قاعدة البيانات.")
            except Exception as close_err:
                logger.error(f"⚠️ [DB] خطأ في إغلاق اتصال قاعدة البيانات: {close_err}")
        logger.info("👋 [Main] تم إيقاف سكريبت تدريب نماذج تعلم الآلة.")
        # os._exit(0) # لا تستخدم exit في بيئة الإنتاج إلا إذا كنت متأكدًا

