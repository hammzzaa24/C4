import os
import gc
import pickle
import logging
import warnings
import pandas as pd
import numpy as np
import requests
import json
from decouple import config
from binance.client import Client
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone
from backtesting import Backtest, Strategy
from tqdm import tqdm
import threading
from flask import Flask

# --- إعداد خادم الويب ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Backtester service is running.", 200

# --- تجاهل التحذيرات غير الهامة ---
warnings.simplefilter(action='ignore', category=FutureWarning)
pd.options.mode.chained_assignment = None

# ---------------------- إعداد نظام التسجيل (Logging) ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('backtester_v7_with_ichimoku.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('Backtester_V7_With_Ichimoku')

# ---------------------- إعداد متغيرات البيئة والثوابت ----------------------
try:
    API_KEY = config('BINANCE_API_KEY')
    API_SECRET = config('BINANCE_API_SECRET')
    DB_URL = config('DATABASE_URL')
    TELEGRAM_TOKEN = config('TELEGRAM_BOT_TOKEN', default=None)
    CHAT_ID = config('TELEGRAM_CHAT_ID', default=None)
except Exception as e:
    logger.critical(f"❌ فشل حرج في تحميل متغيرات البيئة: {e}")
    exit(1)

# --- إعدادات الاختبار الخلفي ---
INITIAL_CASH = 100.0
TRADE_AMOUNT_USDT = 10.0
FEE = 0.001
SLIPPAGE = 0.0005
COMMISSION = FEE + SLIPPAGE
BACKTEST_PERIOD_DAYS = 30
OUT_OF_SAMPLE_OFFSET_DAYS = 126

# --- ثوابت الاستراتيجية والنموذج ---
BASE_ML_MODEL_NAME = 'LightGBM_Scalping_V7_With_Ichimoku'
MODEL_FOLDER = 'V7'
SIGNAL_GENERATION_TIMEFRAME = '15m'
HIGHER_TIMEFRAME = '4h'
BTC_SYMBOL = 'BTCUSDT'
MODEL_CONFIDENCE_THRESHOLD = 0.70
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 2.0

# --- إعدادات المؤشرات ---
ADX_PERIOD, BBANDS_PERIOD, RSI_PERIOD = 14, 20, 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
ATR_PERIOD, EMA_SLOW_PERIOD, EMA_FAST_PERIOD = 14, 200, 50
BTC_CORR_PERIOD, STOCH_RSI_PERIOD, STOCH_K, STOCH_D, REL_VOL_PERIOD = 30, 14, 3, 3, 30

# متغيرات الاتصال العامة
conn = None
client = None

# ---------------------- دوال قاعدة البيانات ----------------------
def init_db():
    global conn
    try:
        conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
        conn.autocommit = False
        logger.info("✅ [DB] تم تهيئة الاتصال بقاعدة البيانات بنجاح.")
        create_backtest_results_table()
    except Exception as e:
        logger.critical(f"❌ [DB] فشل الاتصال بقاعدة البيانات: {e}")
        conn = None

def create_backtest_results_table():
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS backtest_results (
                    id SERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    run_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    stats JSONB NOT NULL,
                    duration_days INT,
                    return_pct NUMERIC,
                    win_rate_pct NUMERIC,
                    profit_factor NUMERIC,
                    num_trades INT
                );
            """)
        conn.commit()
        logger.info("✅ [DB] تم التأكد من وجود جدول 'backtest_results'.")
    except Exception as e:
        logger.error(f"❌ [DB] فشل في إنشاء جدول 'backtest_results': {e}")
        conn.rollback()

# --- ✨ جديد: دالة مساعدة لتنظيف قيم NaN ---
def replace_nan_with_none(obj):
    """
    Recursively traverses a dictionary or list and replaces float NaN with None.
    """
    if isinstance(obj, dict):
        return {k: replace_nan_with_none(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [replace_nan_with_none(elem) for elem in obj]
    # Check for float NaN values
    elif isinstance(obj, float) and np.isnan(obj):
        return None
    return obj

def save_backtest_results(symbol, strategy_name, run_timestamp, stats):
    if not conn:
        logger.error("❌ [DB Save] لا يمكن حفظ النتائج، لا يوجد اتصال بقاعدة البيانات.")
        return

    # --- ✨ تصحيح: تنظيف قيم NaN قبل تحويلها إلى JSON ---
    cleaned_stats = replace_nan_with_none(stats)

    duration_obj = cleaned_stats.get('Duration')
    duration_days = duration_obj.days if pd.notna(duration_obj) and duration_obj is not None else 0
    
    return_pct = cleaned_stats.get('Return [%]') or 0.0
    win_rate_pct = cleaned_stats.get('Win Rate [%]') or 0.0
    
    profit_factor = cleaned_stats.get('Profit Factor')
    # If profit_factor is None (was NaN) or inf, set to 0 for DB
    if profit_factor is None or (isinstance(profit_factor, float) and np.isinf(profit_factor)):
        profit_factor = 0.0

    num_trades = cleaned_stats.get('# Trades') or 0

    # Use the cleaned dictionary for JSON conversion
    stats_json = json.dumps(cleaned_stats, default=str)

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO backtest_results
                (symbol, strategy_name, run_timestamp, stats, duration_days, return_pct, win_rate_pct, profit_factor, num_trades)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (symbol, strategy_name, run_timestamp, stats_json, duration_days, return_pct, win_rate_pct, profit_factor, num_trades))
        conn.commit()
        logger.info(f"✅ [DB Save] تم حفظ نتائج الاختبار الخلفي للعملة {symbol} بنجاح.")
    except Exception as e:
        logger.error(f"❌ [DB Save] فشل في حفظ نتائج {symbol}: {e}")
        conn.rollback()

def load_ml_model_bundle_from_folder(symbol: str) -> dict | None:
    model_name = f"{BASE_ML_MODEL_NAME}_{symbol}"
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        model_path = os.path.join(script_dir, MODEL_FOLDER, f"{model_name}.pkl")
        if not os.path.exists(model_path):
            logger.warning(f"⚠️ [ML Model File] Model file not found for {symbol} at {model_path}")
            return None
        with open(model_path, 'rb') as f:
            model_bundle = pickle.load(f)
        if 'model' in model_bundle and 'scaler' in model_bundle and 'feature_names' in model_bundle:
            logger.info(f"✅ [ML Model File] Successfully loaded model bundle for {symbol} from local file.")
            return model_bundle
        logger.error(f"❌ [ML Model File] Model bundle for {symbol} is incomplete.")
        return None
    except Exception as e:
        logger.error(f"❌ [ML Model File] Error loading model bundle from file for {symbol}: {e}")
        return None

def fetch_historical_data(symbol: str, interval: str, days: int, out_of_sample_period_days: int = 0) -> pd.DataFrame | None:
    global client
    if not client:
        try:
            client = Client(API_KEY, API_SECRET)
        except Exception as e:
            logger.error(f"❌ [Binance] Failed to initialize Binance client: {e}")
            return None
    try:
        now = datetime.now(timezone.utc)
        end_dt = now - timedelta(days=out_of_sample_period_days)
        start_dt = end_dt - timedelta(days=days)
        klines = client.get_historical_klines(symbol, interval, start_dt.strftime("%Y-%m-%d %H:%M:%S"), end_dt.strftime("%Y-%m-%d %H:%M:%S"))
        if not klines: return None
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume', 'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore'])
        df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].apply(pd.to_numeric)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.set_index('timestamp', inplace=True)
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)
        return df.dropna()
    except Exception as e:
        logger.error(f"❌ [Data] Error fetching historical data for {symbol}: {e}")
        return None

def create_all_features(df: pd.DataFrame, btc_df: pd.DataFrame) -> pd.DataFrame:
    df_calc = df.copy()
    high_low = df_calc['High'] - df_calc['Low']
    high_close = (df_calc['High'] - df_calc['Close'].shift()).abs()
    low_close = (df_calc['Low'] - df_calc['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df_calc['atr'] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
    up_move = df_calc['High'].diff()
    down_move = -df_calc['Low'].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df_calc.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df_calc.index)
    plus_di = 100 * plus_dm.ewm(span=ADX_PERIOD, adjust=False).mean() / df_calc['atr']
    minus_di = 100 * minus_dm.ewm(span=ADX_PERIOD, adjust=False).mean() / df_calc['atr']
    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, 1e-9))
    df_calc['adx'] = dx.ewm(span=ADX_PERIOD, adjust=False).mean()
    delta = df_calc['Close'].diff()
    gain = delta.clip(lower=0).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    df_calc['rsi'] = 100 - (100 / (1 + (gain / loss.replace(0, 1e-9))))
    return df_calc

def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("⚠️ [Telegram] Token or Chat ID not configured. Skipping message.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': str(CHAT_ID), 'text': text, 'parse_mode': 'Markdown'}
    try:
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()
        logger.info("✅ [Telegram] Summary report sent successfully.")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Telegram] Failed to send message: {e}")

# ---------------------- فئة استراتيجية Backtesting.py ----------------------
class MLStrategy(Strategy):
    ml_model = None
    scaler = None
    feature_names = None

    def init(self):
        pass

    def next(self):
        if self.position: return

        try:
            current_index = self.data.index[-1]
            if not all(feature in self.data.df.columns for feature in self.feature_names):
                logger.warning(f"Missing one or more feature columns at index {current_index}. Skipping.")
                return
            
            features = self.data.df.loc[current_index, self.feature_names]
            if features.isnull().any(): return
        except (KeyError, IndexError):
            return

        features_df = pd.DataFrame([features])
        features_scaled_np = self.scaler.transform(features_df)
        features_scaled_df = pd.DataFrame(features_scaled_np, columns=self.feature_names)
        
        prediction = self.ml_model.predict(features_scaled_df)[0]
        prediction_proba = self.ml_model.predict_proba(features_scaled_df)[0]
        
        try:
            class_1_index = list(self.ml_model.classes_).index(1)
            prob_for_class_1 = prediction_proba[class_1_index]
        except ValueError:
            return

        if prediction == 1 and prob_for_class_1 >= MODEL_CONFIDENCE_THRESHOLD:
            current_atr = self.data.atr[-1]
            if pd.isna(current_atr) or current_atr == 0: return

            required_cash = TRADE_AMOUNT_USDT * (1 + COMMISSION)
            if self.equity < required_cash: return

            current_price = self.data.Close[-1]
            size_as_fraction = TRADE_AMOUNT_USDT / self.equity

            if size_as_fraction > 0:
                stop_loss_price = current_price - (current_atr * ATR_SL_MULTIPLIER)
                take_profit_price = current_price + (current_atr * ATR_TP_MULTIPLIER)
                self.buy(size=size_as_fraction, sl=stop_loss_price, tp=take_profit_price)

def generate_report_from_db(run_timestamp):
    if not conn:
        logger.error("❌ [Report] Cannot generate report, no database connection.")
        return

    logger.info("📊 [Report] Generating final report from saved results...")
    try:
        query = """
            SELECT
                COUNT(*) AS total_symbols,
                SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) AS profitable_symbols,
                SUM(num_trades) AS total_trades,
                AVG(win_rate_pct) AS avg_win_rate,
                AVG(profit_factor) FILTER (WHERE profit_factor > 0 AND profit_factor != 'Infinity'::numeric) AS avg_profit_factor,
                SUM(return_pct) AS total_return_pct,
                AVG(return_pct) AS avg_return_pct
            FROM backtest_results
            WHERE run_timestamp = %s;
        """
        with conn.cursor() as cur:
            cur.execute(query, (run_timestamp,))
            summary = cur.fetchone()
        conn.commit()

        if not summary or summary['total_symbols'] == 0:
            logger.warning("⚠️ [Report] No results found for the current backtest run.")
            send_telegram_message("🏁 Backtest finished but no valid results were found to generate a report.")
            return

        report_title = f"📊 *ملخص الاختبار الخلفي - {BASE_ML_MODEL_NAME}*"
        report_date = f"*{run_timestamp.strftime('%Y-%m-%d %H:%M:%S %Z')}*"
        
        total_symbols = summary.get('total_symbols', 0)
        profitable_symbols = summary.get('profitable_symbols', 0)
        
        report_body = (
            f"----------------------------------------\n"
            f"▫️ *إجمالي الرموز المختبرة:* `{total_symbols}`\n"
            f"📈 *رموز رابحة:* `{int(profitable_symbols)}`\n"
            f"📉 *رموز خاسرة:* `{int(total_symbols - profitable_symbols)}`\n"
            f"🔄 *إجمالي الصفقات:* `{int(summary.get('total_trades', 0))}`\n"
            f"----------------------------------------\n"
            f"🎯 *متوسط نسبة الربح:* `{summary.get('avg_win_rate', 0):.2f}%`\n"
            f"💰 *متوسط عامل الربح:* `{summary.get('avg_profit_factor', 0):.2f}`\n"
            f"📈 *متوسط العائد لكل رمز:* `{summary.get('avg_return_pct', 0):.2f}%`\n"
            f"📊 *إجمالي العائد (مجموع):* `{summary.get('total_return_pct', 0):.2f}%`\n"
            f"----------------------------------------"
        )
        
        final_report = f"{report_title}\n{report_date}\n\n{report_body}"
        send_telegram_message(final_report)

    except Exception as e:
        logger.error(f"❌ [Report] Failed to generate report from database: {e}")
        if conn: conn.rollback()
        send_telegram_message("❌ An error occurred while generating the backtest report.")

# ---------------------- كتلة التنفيذ الرئيسية ----------------------
def run_backtest():
    global conn
    logger.info(f"🚀 Starting advanced backtest for strategy {BASE_ML_MODEL_NAME}...")
    
    init_db()
    if not conn:
        logger.critical("❌ Cannot run backtest without a database connection.")
        send_telegram_message("❌ Backtest failed: Could not connect to the database.")
        return

    run_timestamp = datetime.now(timezone.utc)

    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(script_dir, 'crypto_list.txt')
        with open(file_path, 'r', encoding='utf-8') as f:
            symbols_to_test = [line.strip().upper() + "USDT" for line in f if line.strip() and not line.startswith('#')]
    except FileNotFoundError:
        logger.error("❌ 'crypto_list.txt' file not found."); return

    btc_df_full = fetch_historical_data(BTC_SYMBOL, SIGNAL_GENERATION_TIMEFRAME, BACKTEST_PERIOD_DAYS + 10, out_of_sample_period_days=OUT_OF_SAMPLE_OFFSET_DAYS)
    if btc_df_full is None:
        logger.critical("❌ Failed to fetch BTC data. Cannot proceed."); return
    btc_df_full['btc_returns'] = btc_df_full['Close'].pct_change()
    
    for symbol in tqdm(symbols_to_test, desc="Backtesting Symbols"):
        logger.info(f"\n--- ⏳ Processing symbol: {symbol} ---")
        
        model_bundle = load_ml_model_bundle_from_folder(symbol)

        if not model_bundle:
            logger.warning(f"⚠️ Skipping {symbol}: Model not found."); continue
        
        df_15m = fetch_historical_data(symbol, SIGNAL_GENERATION_TIMEFRAME, BACKTEST_PERIOD_DAYS, out_of_sample_period_days=OUT_OF_SAMPLE_OFFSET_DAYS)
        if df_15m is None or df_15m.empty:
            logger.warning(f"⚠️ Skipping {symbol}: Insufficient historical data."); continue
            
        data = create_all_features(df_15m, btc_df_full)
        data.replace([np.inf, -np.inf], np.nan, inplace=True)
        data.dropna(inplace=True)
        
        if data.empty:
            logger.warning(f"⚠️ Skipping {symbol}: DataFrame is empty after feature engineering."); continue

        try:
            for feature in model_bundle['feature_names']:
                if feature not in data.columns:
                    data[feature] = 0
            
            bt = Backtest(data, MLStrategy, cash=INITIAL_CASH, commission=COMMISSION, exclusive_orders=True)
            stats = bt.run(
                ml_model=model_bundle['model'],
                scaler=model_bundle['scaler'],
                feature_names=model_bundle['feature_names']
            )
            
            save_backtest_results(symbol, BASE_ML_MODEL_NAME, run_timestamp, stats.to_dict())

        except Exception as e:
            logger.error(f"❌ [Backtest Run] Backtest failed for symbol {symbol}: {e}", exc_info=True)
        
        del data, df_15m, model_bundle
        gc.collect()
        logger.info(f"🧠 [Memory] Memory freed after processing {symbol}.")

    generate_report_from_db(run_timestamp)
        
    if conn:
        conn.close()
    logger.info("✅ Backtest thread finished.")


if __name__ == "__main__":
    backtest_thread = threading.Thread(target=run_backtest, name="run_backtest", daemon=True)
    backtest_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
