import time
import os
import json
import logging
import requests
import numpy as np
import pandas as pd
import psycopg2
import pickle
from psycopg2 import sql, OperationalError, InterfaceError
from psycopg2.extras import RealDictCursor
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from datetime import datetime, timedelta
from decouple import config
from typing import List, Dict, Optional, Any, Tuple

# استيراد مكتبات Flask والخيوط
from flask import Flask, request, Response
from threading import Thread

# ---------------------- إعداد التسجيل ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('ml_model_trainer.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('MLTrainer')

# ---------------------- تحميل المتغيرات البيئية ----------------------
try:
    API_KEY: str = config('BINANCE_API_KEY')
    API_SECRET: str = config('BINANCE_API_SECRET')
    DB_URL: str = config('DATABASE_URL')
    WEBHOOK_URL: Optional[str] = config('WEBHOOK_URL', default=None) # إضافة WEBHOOK_URL
except Exception as e:
     logger.critical(f"❌ فشل في تحميل المتغيرات البيئية الأساسية: {e}")
     exit(1)

logger.info(f"Binance API Key: {'Available' if API_KEY else 'Not available'}")
logger.info(f"Database URL: {'Available' if DB_URL else 'Not available'}")
logger.info(f"Webhook URL: {WEBHOOK_URL if WEBHOOK_URL else 'Not specified'} (Flask will always run for Render)")


# ---------------------- إعداد الثوابت والمتغيرات العامة ----------------------
SIGNAL_GENERATION_TIMEFRAME: str = '15m'
DATA_LOOKBACK_DAYS_FOR_TRAINING: int = 90 # 3 أشهر من البيانات
ML_MODEL_NAME: str = 'DecisionTree_Scalping_V1'

# Indicator Parameters (نسخ من c4.py لضمان الاتساق)
RSI_PERIOD: int = 9
EMA_SHORT_PERIOD: int = 8
EMA_LONG_PERIOD: int = 21
VWMA_PERIOD: int = 15
ENTRY_ATR_PERIOD: int = 10
BOLLINGER_WINDOW: int = 20
BOLLINGER_STD_DEV: int = 2
MACD_FAST: int = 9
MACD_SLOW: int = 18
MACD_SIGNAL: int = 9
ADX_PERIOD: int = 10
SUPERTREND_PERIOD: int = 10
SUPERTREND_MULTIPLIER: float = 2.5

# Global variables
conn: Optional[psycopg2.extensions.connection] = None
cur: Optional[psycopg2.extensions.cursor] = None
client: Optional[Client] = None

# متغيرات لتتبع حالة التدريب
# تم تعريفها هنا كمتغيرات عامة
training_status: str = "Idle"
last_training_time: Optional[datetime] = None
last_training_metrics: Dict[str, Any] = {}
training_error: Optional[str] = None


# ---------------------- Binance Client Setup ----------------------
try:
    logger.info("ℹ️ [Binance] تهيئة عميل Binance...")
    client = Client(API_KEY, API_SECRET)
    client.ping()
    server_time = client.get_server_time()
    logger.info(f"✅ [Binance] تم تهيئة عميل Binance. وقت الخادم: {datetime.fromtimestamp(server_time['serverTime']/1000)}")
except BinanceRequestException as req_err:
     logger.critical(f"❌ [Binance] خطأ في طلب Binance (مشكلة في الشبكة أو الطلب): {req_err}")
     exit(1)
except BinanceAPIException as api_err:
     logger.critical(f"❌ [Binance] خطأ في واجهة برمجة تطبيقات Binance (مفاتيح غير صالحة أو مشكلة في الخادم): {api_err}")
     exit(1)
except Exception as e:
    logger.critical(f"❌ [Binance] فشل غير متوقع في تهيئة عميل Binance: {e}")
    exit(1)

# ---------------------- Database Connection Setup (نسخ من c4.py) ----------------------
def init_db(retries: int = 5, delay: int = 5) -> None:
    """Initializes database connection and creates tables if they don't exist."""
    global conn, cur
    logger.info("[DB] بدء تهيئة قاعدة البيانات...")
    for attempt in range(retries):
        try:
            logger.info(f"[DB] محاولة الاتصال بقاعدة البيانات (المحاولة {attempt + 1}/{retries})...")
            conn = psycopg2.connect(DB_URL, connect_timeout=10, cursor_factory=RealDictCursor)
            conn.autocommit = False
            cur = conn.cursor()
            logger.info("✅ [DB] تم الاتصال بقاعدة البيانات بنجاح.")

            # --- Create or update signals table (Modified schema) ---
            logger.info("[DB] التحقق من/إنشاء جدول 'signals'...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id SERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    entry_price DOUBLE PRECISION NOT NULL,
                    initial_target DOUBLE PRECISION NOT NULL,
                    current_target DOUBLE PRECISION NOT NULL,
                    r2_score DOUBLE PRECISION,
                    volume_15m DOUBLE PRECISION,
                    achieved_target BOOLEAN DEFAULT FALSE,
                    closing_price DOUBLE PRECISION,
                    closed_at TIMESTAMP,
                    sent_at TIMESTAMP DEFAULT NOW(),
                    entry_time TIMESTAMP DEFAULT NOW(),
                    time_to_target INTERVAL,
                    profit_percentage DOUBLE PRECISION,
                    strategy_name TEXT,
                    signal_details JSONB
                );""")
            conn.commit()
            logger.info("✅ [DB] جدول 'signals' موجود أو تم إنشاؤه.")

            # --- Create ml_models table (NEW) ---
            logger.info("[DB] التحقق من/إنشاء جدول 'ml_models'...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ml_models (
                    id SERIAL PRIMARY KEY,
                    model_name TEXT NOT NULL UNIQUE,
                    model_data BYTEA NOT NULL,
                    trained_at TIMESTAMP DEFAULT NOW(),
                    metrics JSONB
                );""")
            conn.commit()
            logger.info("✅ [DB] جدول 'ml_models' موجود أو تم إنشاؤه.")

            # --- Create market_dominance table (if it doesn't exist) ---
            logger.info("[DB] التحقق من/إنشاء جدول 'market_dominance'...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS market_dominance (
                    id SERIAL PRIMARY KEY,
                    recorded_at TIMESTAMP DEFAULT NOW(),
                    btc_dominance DOUBLE PRECISION,
                    eth_dominance DOUBLE PRECISION
                );
            """)
            conn.commit()
            logger.info("✅ [DB] جدول 'market_dominance' موجود أو تم إنشاؤه.")

            logger.info("✅ [DB] تم تهيئة قاعدة البيانات بنجاح.")
            return

        except OperationalError as op_err:
            logger.error(f"❌ [DB] خطأ تشغيلي في الاتصال (المحاولة {attempt + 1}): {op_err}")
            if conn: conn.rollback()
            if attempt == retries - 1:
                 logger.critical("❌ [DB] فشلت جميع محاولات الاتصال بقاعدة البيانات.")
                 raise op_err
            time.sleep(delay)
        except Exception as e:
            logger.critical(f"❌ [DB] فشل غير متوقع في تهيئة قاعدة البيانات (المحاولة {attempt + 1}): {e}", exc_info=True)
            if conn: conn.rollback()
            if attempt == retries - 1:
                 logger.critical("❌ [DB] فشلت جميع محاولات الاتصال بقاعدة البيانات.")
                 raise e
            time.sleep(delay)

    logger.critical("❌ [DB] فشل الاتصال بقاعدة البيانات بعد عدة محاولات.")
    exit(1)


def check_db_connection() -> bool:
    """Checks database connection status and re-initializes if necessary."""
    global conn, cur
    try:
        if conn is None or conn.closed != 0:
            logger.warning("⚠️ [DB] الاتصال مغلق أو غير موجود. إعادة التهيئة...")
            init_db()
            return True
        else:
             with conn.cursor() as check_cur:
                  check_cur.execute("SELECT 1;")
                  check_cur.fetchone()
             return True
    except (OperationalError, InterfaceError) as e:
        logger.error(f"❌ [DB] فقدان الاتصال بقاعدة البيانات ({e}). إعادة التهيئة...")
        try:
             init_db()
             return True
        except Exception as recon_err:
            logger.error(f"❌ [DB] فشلت محاولة إعادة الاتصال بعد فقدان الاتصال: {recon_err}")
            return False
    except Exception as e:
        logger.error(f"❌ [DB] خطأ غير متوقع أثناء التحقق من الاتصال: {e}", exc_info=True)
        try:
            init_db()
            return True
        except Exception as recon_err:
             logger.error(f"❌ [DB] فشلت محاولة إعادة الاتصال بعد خطأ غير متوقع: {recon_err}")
             return False

def convert_np_values(obj: Any) -> Any:
    """Converts NumPy data types to native Python types for JSON and DB compatibility."""
    if isinstance(obj, dict):
        return {k: convert_np_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_np_values(item) for item in obj]
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, (np.integer, np.int_)):
        return int(obj)
    elif isinstance(obj, (np.floating, np.float64)):
        return float(obj)
    elif isinstance(obj, (np.bool_)):
        return bool(obj)
    elif pd.isna(obj):
        return None
    else:
        return obj

def get_crypto_symbols(filename: str = 'crypto_list.txt') -> List[str]:
    """
    Reads the list of currency symbols from a text file, then validates them
    as valid USDT pairs available for Spot trading on Binance. (نسخ من c4.py)
    """
    raw_symbols: List[str] = []
    logger.info(f"ℹ️ [Data] قراءة قائمة الرموز من الملف '{filename}'...")
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(script_dir, filename)

        if not os.path.exists(file_path):
            file_path = os.path.abspath(filename)
            if not os.path.exists(file_path):
                 logger.error(f"❌ [Data] الملف '{filename}' غير موجود في دليل السكربت أو الدليل الحالي.")
                 return []
            else:
                 logger.warning(f"⚠️ [Data] الملف '{filename}' غير موجود في دليل السكربت. استخدام الملف في الدليل الحالي: '{file_path}'")

        with open(file_path, 'r', encoding='utf-8') as f:
            raw_symbols = [f"{line.strip().upper().replace('USDT', '')}USDT"
                           for line in f if line.strip() and not line.startswith('#')]
        raw_symbols = sorted(list(set(raw_symbols)))
        logger.info(f"ℹ️ [Data] تم قراءة {len(raw_symbols)} رمزًا مبدئيًا من '{file_path}'.")

    except FileNotFoundError:
         logger.error(f"❌ [Data] الملف '{filename}' غير موجود.")
         return []
    except Exception as e:
        logger.error(f"❌ [Data] خطأ في قراءة الملف '{filename}': {e}", exc_info=True)
        return []

    if not raw_symbols:
         logger.warning("⚠️ [Data] قائمة الرموز الأولية فارغة.")
         return []

    if not client:
        logger.error("❌ [Data Validation] عميل Binance غير مهيأ. لا يمكن التحقق من الرموز.")
        return raw_symbols

    try:
        logger.info("ℹ️ [Data Validation] التحقق من الرموز وحالة التداول من Binance API...")
        exchange_info = client.get_exchange_info()
        valid_trading_usdt_symbols = {
            s['symbol'] for s in exchange_info['symbols']
            if s.get('quoteAsset') == 'USDT' and
               s.get('status') == 'TRADING' and
               s.get('isSpotTradingAllowed') is True
        }
        logger.info(f"ℹ️ [Data Validation] تم العثور على {len(valid_trading_usdt_symbols)} زوج تداول USDT صالح في Spot على Binance.")
        validated_symbols = [symbol for symbol in raw_symbols if symbol in valid_trading_usdt_symbols]

        removed_count = len(raw_symbols) - len(validated_symbols)
        if removed_count > 0:
            removed_symbols = set(raw_symbols) - set(validated_symbols)
            logger.warning(f"⚠️ [Data Validation] تم إزالة {removed_count} رمز تداول USDT غير صالح أو غير متاح من القائمة: {', '.join(removed_symbols)}")

        logger.info(f"✅ [Data Validation] تم التحقق من الرموز. استخدام {len(validated_symbols)} رمزًا صالحًا.")
        return validated_symbols

    except (BinanceAPIException, BinanceRequestException) as binance_err:
         logger.error(f"❌ [Data Validation] خطأ في Binance API أو الشبكة أثناء التحقق من الرموز: {binance_err}")
         logger.warning("⚠️ [Data Validation] استخدام القائمة الأولية من الملف بدون التحقق من Binance.")
         return raw_symbols
    except Exception as api_err:
         logger.error(f"❌ [Data Validation] خطأ غير متوقع أثناء التحقق من رموز Binance: {api_err}", exc_info=True)
         logger.warning("⚠️ [Data Validation] استخدام القائمة الأولية من الملف بدون التحقق من Binance.")
         return raw_symbols


def fetch_historical_data(symbol: str, interval: str, days: int) -> Optional[pd.DataFrame]:
    """
    Fetches historical candlestick data from Binance for a specified number of days.
    This function relies on python-binance's get_historical_klines to handle
    internal pagination for large data ranges.
    """
    if not client:
        logger.error("❌ [Data] عميل Binance غير مهيأ لجلب البيانات.")
        return None
    try:
        # Calculate the start date for the entire data range needed
        start_dt = datetime.utcnow() - timedelta(days=days + 1)
        start_str_overall = start_dt.strftime("%Y-%m-%d %H:%M:%S")

        logger.debug(f"ℹ️ [Data] جلب بيانات {interval} لـ {symbol} من {start_str_overall} حتى الآن...")

        # Call get_historical_klines for the entire period.
        # The python-binance library is designed to handle internal pagination
        # if the requested range exceeds the API's single-request limit (e.g., 1000 klines).
        klines = client.get_historical_klines(symbol, interval, start_str_overall)

        if not klines:
            logger.warning(f"⚠️ [Data] لا توجد بيانات تاريخية ({interval}) لـ {symbol} للفترة المطلوبة.")
            return None

        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore'
        ])

        numeric_cols = ['open', 'high', 'low', 'close', 'volume']
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df = df[numeric_cols]
        initial_len = len(df)
        df.dropna(subset=numeric_cols, inplace=True)

        if len(df) < initial_len:
            logger.debug(f"ℹ️ [Data] {symbol}: تم إسقاط {initial_len - len(df)} صفًا بسبب قيم NaN في بيانات OHLCV.")

        if df.empty:
            logger.warning(f"⚠️ [Data] DataFrame لـ {symbol} فارغ بعد إزالة قيم NaN الأساسية.")
            return None

        # Sort by index (timestamp) to ensure chronological order
        df.sort_index(inplace=True)

        logger.debug(f"✅ [Data] تم جلب ومعالجة {len(df)} شمعة تاريخية ({interval}) لـ {symbol}.")
        return df

    except BinanceAPIException as api_err:
         logger.error(f"❌ [Data] خطأ في Binance API أثناء جلب البيانات لـ {symbol}: {api_err}")
         return None
    except BinanceRequestException as req_err:
         logger.error(f"❌ [Data] خطأ في الطلب أو الشبكة أثناء جلب البيانات لـ {symbol}: {req_err}")
         return None
    except Exception as e:
        logger.error(f"❌ [Data] خطأ غير متوقع أثناء جلب البيانات التاريخية لـ {symbol}: {e}", exc_info=True)
        return None

# ---------------------- Technical Indicator Functions (نسخ من c4.py) ----------------------
def calculate_ema(series: pd.Series, span: int) -> pd.Series:
    """Calculates Exponential Moving Average (EMA)."""
    if series is None or series.isnull().all() or len(series) < span:
        return pd.Series(index=series.index if series is not None else None, dtype=float)
    return series.ewm(span=span, adjust=False).mean()

def calculate_vwma(df: pd.DataFrame, period: int) -> pd.Series:
    """Calculates Volume Weighted Moving Average (VWMA)."""
    df_calc = df.copy()
    required_cols = ['close', 'volume']
    if not all(col in df_calc.columns for col in required_cols) or df_calc[required_cols].isnull().all().any():
        logger.warning("⚠️ [Indicator VWMA] أعمدة 'close' أو 'volume' مفقودة أو فارغة.")
        return pd.Series(index=df_calc.index if df_calc is not None else None, dtype=float)
    if len(df_calc) < period:
        logger.warning(f"⚠️ [Indicator VWMA] بيانات غير كافية ({len(df_calc)} < {period}) لحساب VWMA.")
        return pd.Series(index=df_calc.index if df_calc is not None else None, dtype=float)

    df_calc['price_volume'] = df_calc['close'] * df_calc['volume']
    rolling_price_volume_sum = df_calc['price_volume'].rolling(window=period, min_periods=period).sum()
    rolling_volume_sum = df_calc['volume'].rolling(window=period, min_periods=period).sum()
    vwma = rolling_price_volume_sum / rolling_volume_sum.replace(0, np.nan)
    df_calc.drop(columns=['price_volume'], inplace=True, errors='ignore')
    return vwma

def calculate_rsi_indicator(df: pd.DataFrame, period: int = RSI_PERIOD) -> pd.DataFrame:
    """Calculates Relative Strength Index (RSI)."""
    df = df.copy()
    if 'close' not in df.columns or df['close'].isnull().all():
        logger.warning("⚠️ [Indicator RSI] عمود 'close' مفقود أو فارغ.")
        df['rsi'] = np.nan
        return df
    if len(df) < period:
        logger.warning(f"⚠️ [Indicator RSI] بيانات غير كافية ({len(df)} < {period}) لحساب RSI.")
        df['rsi'] = np.nan
        return df

    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    rsi_series = 100 - (100 / (1 + rs))
    df['rsi'] = rsi_series.ffill().fillna(50)

    return df

def calculate_atr_indicator(df: pd.DataFrame, period: int = ENTRY_ATR_PERIOD) -> pd.DataFrame:
    """Calculates Average True Range (ATR)."""
    df = df.copy()
    required_cols = ['high', 'low', 'close']
    if not all(col in df.columns for col in required_cols) or df[required_cols].isnull().all().any():
        logger.warning("⚠️ [Indicator ATR] أعمدة 'high', 'low', 'close' مفقودة أو فارغة.")
        df['atr'] = np.nan
        return df
    if len(df) < period + 1:
        logger.warning(f"⚠️ [Indicator ATR] بيانات غير كافية ({len(df)} < {period + 1}) لحساب ATR.")
        df['atr'] = np.nan
        return df

    high_low = df['high'] - df['low']
    high_close_prev = (df['high'] - df['close'].shift(1)).abs()
    low_close_prev = (df['low'] - df['close'].shift(1)).abs()

    tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1, skipna=False)

    df['atr'] = tr.ewm(span=period, adjust=False).mean()
    return df

def calculate_bollinger_bands(df: pd.DataFrame, window: int = BOLLINGER_WINDOW, num_std: int = BOLLINGER_STD_DEV) -> pd.DataFrame:
    """Calculates Bollinger Bands."""
    df = df.copy()
    if 'close' not in df.columns or df['close'].isnull().all():
        logger.warning("⚠️ [Indicator BB] عمود 'close' مفقود أو فارغ.")
        df['bb_middle'] = np.nan
        df['bb_upper'] = np.nan
        df['bb_lower'] = np.nan
        return df
    if len(df) < window:
         logger.warning(f"⚠️ [Indicator BB] بيانات غير كافية ({len(df)} < {window}) لحساب BB.")
         df['bb_middle'] = np.nan
         df['bb_upper'] = np.nan
         df['bb_lower'] = np.nan
         return df

    df['bb_middle'] = df['close'].rolling(window=window).mean()
    df['bb_std'] = df['close'].rolling(window=window).std()
    df['bb_upper'] = df['bb_middle'] + num_std * df['bb_std']
    df['bb_lower'] = df['bb_middle'] - num_std * df['bb_std']
    return df

def calculate_macd(df: pd.DataFrame, fast: int = MACD_FAST, slow: int = MACD_SLOW, signal: int = MACD_SIGNAL) -> pd.DataFrame:
    """Calculates MACD, Signal Line, and Histogram."""
    df = df.copy()
    if 'close' not in df.columns or df['close'].isnull().all():
        logger.warning("⚠️ [Indicator MACD] عمود 'close' مفقود أو فارغ.")
        df['macd'] = np.nan
        df['macd_signal'] = np.nan
        df['macd_hist'] = np.nan
        return df
    min_len = max(fast, slow, signal)
    if len(df) < min_len:
        logger.warning(f"⚠️ [Indicator MACD] بيانات غير كافية ({len(df)} < {min_len}) لحساب MACD.")
        df['macd'] = np.nan
        df['macd_signal'] = np.nan
        df['macd_hist'] = np.nan
        return df

    ema_fast = calculate_ema(df['close'], fast)
    ema_slow = calculate_ema(df['close'], slow)
    df['macd'] = ema_fast - ema_slow
    df['macd_signal'] = calculate_ema(df['macd'], signal)
    df['macd_hist'] = df['macd'] - df['macd_signal']
    return df

def calculate_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.DataFrame:
    """Calculates ADX, DI+ and DI-."""
    df_calc = df.copy()
    required_cols = ['high', 'low', 'close']
    if not all(col in df_calc.columns for col in required_cols) or df_calc[required_cols].isnull().all().any():
        logger.warning("⚠️ [Indicator ADX] أعمدة 'high', 'low', 'close' مفقودة أو فارغة.")
        df_calc['adx'] = np.nan
        df_calc['di_plus'] = np.nan
        df_calc['di_minus'] = np.nan
        return df_calc
    if len(df_calc) < period * 2:
        logger.warning(f"⚠️ [Indicator ADX] بيانات غير كافية ({len(df_calc)} < {period * 2}) لحساب ADX.")
        df_calc['adx'] = np.nan
        df_calc['di_plus'] = np.nan
        df_calc['di_minus'] = np.nan
        return df_calc

    df_calc['high-low'] = df_calc['high'] - df_calc['low']
    df_calc['high-prev_close'] = abs(df_calc['high'] - df_calc['close'].shift(1))
    df_calc['low-prev_close'] = abs(df_calc['low'] - df_calc['close'].shift(1))
    df_calc['tr'] = df_calc[['high-low', 'high-prev_close', 'low-prev_close']].max(axis=1, skipna=False)

    df_calc['up_move'] = df_calc['high'] - df_calc['high'].shift(1)
    df_calc['down_move'] = df_calc['low'].shift(1) - df_calc['low']
    df_calc['+dm'] = np.where((df_calc['up_move'] > df_calc['down_move']) & (df_calc['up_move'] > 0), df_calc['up_move'], 0)
    df_calc['-dm'] = np.where((df_calc['down_move'] > df_calc['up_move']) & (df_calc['down_move'] > 0), df_calc['down_move'], 0)

    alpha = 1 / period
    df_calc['tr_smooth'] = df_calc['tr'].ewm(alpha=alpha, adjust=False).mean()
    df_calc['+dm_smooth'] = df_calc['+dm'].ewm(alpha=alpha, adjust=False).mean()
    df_calc['di_minus_smooth'] = df_calc['-dm'].ewm(alpha=alpha, adjust=False).mean()

    df_calc['di_plus'] = np.where(df_calc['tr_smooth'] > 0, 100 * (df_calc['+dm_smooth'] / df_calc['tr_smooth']), 0)
    df_calc['di_minus'] = np.where(df_calc['tr_smooth'] > 0, 100 * (df_calc['di_minus_smooth'] / df_calc['tr_smooth']), 0)

    di_sum = df_calc['di_plus'] + df_calc['di_minus']
    df_calc['dx'] = np.where(di_sum > 0, 100 * abs(df_calc['di_plus'] - df_calc['di_minus']) / di_sum, 0)

    df_calc['adx'] = df_calc['dx'].ewm(alpha=alpha, adjust=False).mean()

    return df_calc[['adx', 'di_plus', 'di_minus']]

def calculate_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates Volume Weighted Average Price (VWAP) - Resets daily. (نسخ من c4.py)"""
    df = df.copy()
    required_cols = ['high', 'low', 'close', 'volume']
    if not all(col in df.columns for col in required_cols) or df[required_cols].isnull().all().any():
        logger.warning("⚠️ [Indicator VWAP] أعمدة 'high', 'low', 'close' أو 'volume' مفقودة أو فارغة.")
        df['vwap'] = np.nan
        return df
    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index)
            logger.warning("⚠️ [Indicator VWAP] تم تحويل الفهرس إلى DatetimeIndex.")
        except Exception:
            logger.error("❌ [Indicator VWAP] فشل تحويل الفهرس إلى DatetimeIndex، لا يمكن حساب VWAP اليومي.")
            df['vwap'] = np.nan
            return df
    if df.index.tz is not None:
        df.index = df.index.tz_convert('UTC')
        logger.debug("ℹ️ [Indicator VWAP] تم تحويل الفهرس إلى UTC لإعادة الضبط اليومي.")
    else:
        df.index = df.index.tz_localize('UTC')
        logger.debug("ℹ️ [Indicator VWAP] تم توطين الفهرس إلى UTC لإعادة الضبط اليومي.")


    df['date'] = df.index.date
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
    df['tp_vol'] = df['typical_price'] * df['volume']

    try:
        df['cum_tp_vol'] = df.groupby('date')['tp_vol'].cumsum()
        df['cum_volume'] = df.groupby('date')['volume'].cumsum()
    except KeyError as e:
        logger.error(f"❌ [Indicator VWAP] خطأ في تجميع البيانات حسب التاريخ: {e}. قد يكون الفهرس غير صحيح.")
        df['vwap'] = np.nan
        df.drop(columns=['date', 'typical_price', 'tp_vol', 'cum_tp_vol', 'cum_volume'], inplace=True, errors='ignore')
        return df
    except Exception as e:
         logger.error(f"❌ [Indicator VWAP] خطأ غير متوقع في حساب VWAP: {e}", exc_info=True)
         df['vwap'] = np.nan
         df.drop(columns=['date', 'typical_price', 'tp_vol', 'cum_tp_vol', 'cum_volume'], inplace=True, errors='ignore')
         return df


    df['vwap'] = np.where(df['cum_volume'] > 0, df['cum_tp_vol'] / df['cum_volume'], np.nan)

    df['vwap'] = df['vwap'].bfill()

    df.drop(columns=['date', 'typical_price', 'tp_vol', 'cum_tp_vol', 'cum_volume'], inplace=True, errors='ignore')
    return df

def calculate_obv(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates On-Balance Volume (OBV). (نسخ من c4.py)"""
    df = df.copy()
    required_cols = ['close', 'volume']
    if not all(col in df.columns for col in required_cols) or df[required_cols].isnull().all().any():
        logger.warning("⚠️ [Indicator OBV] أعمدة 'close' أو 'volume' مفقودة أو فارغة.")
        df['obv'] = np.nan
        return df
    if not pd.api.types.is_numeric_dtype(df['close']) or not pd.api.types.is_numeric_dtype(df['volume']):
        logger.warning("⚠️ [Indicator OBV] أعمدة 'close' أو 'volume' ليست رقمية.")
        df['obv'] = np.nan
        return df

    obv = np.zeros(len(df), dtype=np.float64)
    close = df['close'].values
    volume = df['volume'].values

    close_diff = df['close'].diff().values

    for i in range(1, len(df)):
        if np.isnan(close[i]) or np.isnan(volume[i]) or np.isnan(close_diff[i]):
            obv[i] = obv[i-1]
            continue

        if close_diff[i] > 0:
            obv[i] = obv[i-1] + volume[i]
        elif close_diff[i] < 0:
             obv[i] = obv[i-1] - volume[i]
        else:
             obv[i] = obv[i-1]

    df['obv'] = obv
    return df

def calculate_supertrend(df: pd.DataFrame, period: int = SUPERTREND_PERIOD, multiplier: float = SUPERTREND_MULTIPLIER) -> pd.DataFrame:
    """Calculates the SuperTrend indicator. (نسخ من c4.py)"""
    df_st = df.copy()
    required_cols = ['high', 'low', 'close']
    if not all(col in df_st.columns for col in required_cols) or df_st[required_cols].isnull().all().any():
        logger.warning("⚠️ [Indicator SuperTrend] أعمدة 'high', 'low', 'close' مفقودة أو فارغة.")
        df_st['supertrend'] = np.nan
        df_st['supertrend_trend'] = 0
        return df_st

    df_st = calculate_atr_indicator(df_st, period=SUPERTREND_PERIOD)


    if 'atr' not in df_st.columns or df_st['atr'].isnull().all():
         logger.warning("⚠️ [Indicator SuperTrend] لا يمكن حساب SuperTrend بسبب قيم ATR غير صالحة أو مفقودة.")
         df_st['supertrend'] = np.nan
         df_st['supertrend_trend'] = 0
         return df_st
    if len(df_st) < SUPERTREND_PERIOD:
        logger.warning(f"⚠️ [Indicator SuperTrend] بيانات غير كافية ({len(df_st)} < {SUPERTREND_PERIOD}) لحساب SuperTrend.")
        df_st['supertrend'] = np.nan
        df_st['supertrend_trend'] = 0
        return df_st

    hl2 = (df_st['high'] + df_st['low']) / 2
    df_st['basic_ub'] = hl2 + multiplier * df_st['atr']
    df_st['basic_lb'] = hl2 - multiplier * df_st['atr']

    df_st['final_ub'] = 0.0
    df_st['final_lb'] = 0.0
    df_st['supertrend'] = np.nan
    df_st['supertrend_trend'] = 0

    close = df_st['close'].values
    basic_ub = df_st['basic_ub'].values
    basic_lb = df_st['basic_lb'].values
    final_ub = df_st['final_ub'].values
    final_lb = df_st['final_lb'].values
    st = df_st['supertrend'].values
    st_trend = df_st['supertrend_trend'].values

    for i in range(1, len(df_st)):
        if pd.isna(basic_ub[i]) or pd.isna(basic_lb[i]) or pd.isna(close[i]):
            final_ub[i] = final_ub[i-1]
            final_lb[i] = final_lb[i-1]
            st[i] = st[i-1]
            st_trend[i] = st_trend[i-1]
            continue

        if basic_ub[i] < final_ub[i-1] or close[i-1] > final_ub[i-1]:
            final_ub[i] = basic_ub[i]
        else:
            final_ub[i] = final_ub[i-1]

        if basic_lb[i] > final_lb[i-1] or close[i-1] < final_lb[i-1]:
            final_lb[i] = basic_lb[i]
        else:
            final_lb[i] = final_lb[i-1]

        if st_trend[i-1] == -1:
            if close[i] <= final_ub[i]:
                st[i] = final_ub[i]
                st_trend[i] = -1
            else:
                st[i] = final_lb[i]
                st_trend[i] = 1
        elif st_trend[i-1] == 1:
            if close[i] >= final_lb[i]:
                st[i] = final_lb[i]
                st_trend[i] = 1
            else:
                st[i] = final_ub[i]
                st_trend[i] = -1
        else:
             if close[i] > final_ub[i]:
                 st[i] = final_lb[i]
                 st_trend[i] = 1
             elif close[i] < final_ub[i]:
                  st[i] = final_ub[i]
                  st_trend[i] = -1
             else:
                  st[i] = np.nan
                  st_trend[i] = 0


    df_st['final_ub'] = final_ub
    df_st['final_lb'] = final_lb
    df_st['supertrend'] = st
    df_st['supertrend_trend'] = st_trend

    df_st.drop(columns=['basic_ub', 'basic_lb', 'final_ub', 'final_lb'], inplace=True, errors='ignore')

    return df_st

def is_hammer(row: pd.Series) -> int:
    """Checks for Hammer pattern (bullish signal). (نسخ من c4.py)"""
    o, h, l, c = row.get('open'), row.get('high'), row.get('low'), row.get('close')
    if pd.isna([o, h, l, c]).any(): return 0
    body = abs(c - o)
    candle_range = h - l
    if candle_range == 0: return 0
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    is_small_body = body < (candle_range * 0.35)
    is_long_lower_shadow = lower_shadow >= 1.8 * body if body > 0 else lower_shadow > candle_range * 0.6
    is_small_upper_shadow = upper_shadow <= body * 0.6 if body > 0 else upper_shadow < candle_range * 0.15
    return 100 if is_small_body and is_long_lower_shadow and is_small_upper_shadow else 0

def is_shooting_star(row: pd.Series) -> int:
    """Checks for Shooting Star pattern (bearish signal). (نسخ من c4.py)"""
    o, h, l, c = row.get('open'), row.get('high'), row.get('low'), row.get('close')
    if pd.isna([o, h, l, c]).any(): return 0
    body = abs(c - o)
    candle_range = h - l
    if candle_range == 0: return 0
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    is_small_body = body < (candle_range * 0.35)
    is_long_upper_shadow = upper_shadow >= 1.8 * body if body > 0 else upper_shadow > candle_range * 0.6
    is_small_lower_shadow = lower_shadow <= body * 0.6 if body > 0 else upper_shadow < candle_range * 0.15
    return -100 if is_small_body and is_long_upper_shadow and is_small_lower_shadow else 0

def is_doji(row: pd.Series) -> int:
    """Checks for Doji pattern (uncertainty). (نسخ من c4.py)"""
    o, h, l, c = row.get('open'), row.get('high'), row.get('low'), row.get('close')
    if pd.isna([o, h, l, c]).any(): return 0
    candle_range = h - l
    if candle_range == 0: return 0
    return 100 if abs(c - o) <= (candle_range * 0.1) else 0

def compute_engulfing(df: pd.DataFrame, idx: int) -> int:
    """Checks for Bullish or Bearish Engulfing pattern. (نسخ من c4.py)"""
    if idx == 0: return 0
    prev = df.iloc[idx - 1]
    curr = df.iloc[idx]
    if pd.isna([prev['close'], prev['open'], curr['close'], curr['open']]).any():
        return 0
    if abs(prev['close'] - prev['open']) < (prev['high'] - prev['low']) * 0.1:
        return 0

    is_bullish = (prev['close'] < prev['open'] and curr['close'] > curr['open'] and
                  curr['open'] <= prev['close'] and curr['close'] >= prev['open'])
    is_bearish = (prev['close'] > prev['open'] and curr['close'] < curr['open'] and
                  curr['open'] >= prev['close'] and curr['close'] <= prev['open'])

    if is_bullish: return 100
    if is_bearish: return -100
    return 0

def detect_candlestick_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Adds candlestick pattern signals to the DataFrame. (نسخ من c4.py)"""
    df = df.copy()
    logger.debug("ℹ️ [Indicators] كشف أنماط الشموع...")
    df['Hammer'] = df.apply(is_hammer, axis=1)
    df['ShootingStar'] = df.apply(is_shooting_star, axis=1)
    df['Doji'] = df.apply(is_doji, axis=1)
    engulfing_values = [compute_engulfing(df, i) for i in range(len(df))]
    df['Engulfing'] = engulfing_values
    df['BullishCandleSignal'] = df.apply(lambda row: 1 if (row['Hammer'] == 100 or row['Engulfing'] == 100) else 0, axis=1)
    df['BearishCandleSignal'] = df.apply(lambda row: 1 if (row['ShootingStar'] == -100 or row['Engulfing'] == -100) else 0, axis=1)
    logger.debug("✅ [Indicators] تم كشف أنماط الشموع.")
    return df


# ---------------------- وظائف تدريب وحفظ النموذج ----------------------
from sklearn.model_selection import train_test_split
from sklearn.tree import DecisionTreeClassifier
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
from sklearn.preprocessing import StandardScaler

def prepare_data_for_ml(df: pd.DataFrame, target_period: int = 5) -> Optional[pd.DataFrame]:
    """
    يجهز البيانات لتدريب نموذج التعلم الآلي.
    يضيف المؤشرات ويزيل الصفوف التي تحتوي على قيم NaN.
    يضيف عمود الهدف 'target' الذي يشير إلى ما إذا كان السعر سيرتفع في الشموع القادمة.
    """
    logger.info(f"ℹ️ [ML Prep] تجهيز البيانات لنموذج التعلم الآلي...")

    min_len_required = max(EMA_SHORT_PERIOD, EMA_LONG_PERIOD, VWMA_PERIOD, RSI_PERIOD, ENTRY_ATR_PERIOD, BOLLINGER_WINDOW, MACD_SLOW, ADX_PERIOD*2, SUPERTREND_PERIOD) + target_period + 5

    if len(df) < min_len_required:
        logger.warning(f"⚠️ [ML Prep] DataFrame قصير جدًا ({len(df)} < {min_len_required}) لتجهيز البيانات.")
        return None

    df_calc = df.copy()

    # حساب جميع المؤشرات
    df_calc = calculate_atr_indicator(df_calc, ENTRY_ATR_PERIOD)
    df_calc = calculate_supertrend(df_calc, SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER)
    df_calc[f'ema_{EMA_SHORT_PERIOD}'] = calculate_ema(df_calc['close'], EMA_SHORT_PERIOD)
    df_calc[f'ema_{EMA_LONG_PERIOD}'] = calculate_ema(df_calc['close'], EMA_LONG_PERIOD)
    df_calc['vwma'] = calculate_vwma(df_calc, VWMA_PERIOD)
    df_calc = calculate_rsi_indicator(df_calc, RSI_PERIOD)
    df_calc = calculate_bollinger_bands(df_calc, BOLLINGER_WINDOW, BOLLINGER_STD_DEV)
    df_calc = calculate_macd(df_calc, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    adx_df = calculate_adx(df_calc, ADX_PERIOD)
    df_calc = df_calc.join(adx_df)
    df_calc = calculate_vwap(df_calc)
    df_calc = calculate_obv(df_calc)
    df_calc = detect_candlestick_patterns(df_calc)

    # تعريف أعمدة الميزات التي سيستخدمها النموذج
    feature_columns = [
        f'ema_{EMA_SHORT_PERIOD}', f'ema_{EMA_LONG_PERIOD}', 'vwma',
        'rsi', 'atr', 'bb_upper', 'bb_lower', 'bb_middle',
        'macd', 'macd_signal', 'macd_hist',
        'adx', 'di_plus', 'di_minus', 'vwap', 'obv',
        'supertrend', 'supertrend_trend',
        'BullishCandleSignal', 'BearishCandleSignal' # إضافة أنماط الشموع كميزات
    ]

    # التأكد من وجود جميع أعمدة الميزات وتحويلها إلى أرقام
    for col in feature_columns:
        if col not in df_calc.columns:
            logger.warning(f"⚠️ [ML Prep] عمود الميزة المفقود: {col}. سيتم إضافته كـ NaN.")
            df_calc[col] = np.nan
        else:
            df_calc[col] = pd.to_numeric(df_calc[col], errors='coerce')

    # إنشاء عمود الهدف: هل السعر سيصعد بنسبة معينة في الشموع القادمة؟
    # على سبيل المثال، إذا كان السعر سيصعد بنسبة 0.5% خلال الشموع الخمس القادمة
    price_change_threshold = 0.005 # 0.5%
    # Ensure 'close' column is numeric before shifting
    df_calc['close'] = pd.to_numeric(df_calc['close'], errors='coerce')
    df_calc['future_max_close'] = df_calc['close'].shift(-target_period).rolling(window=target_period, min_periods=1).max()
    # Corrected target calculation: check if future max close is significantly higher than current close
    df_calc['target'] = ((df_calc['future_max_close'] / df_calc['close']) - 1 > price_change_threshold).astype(int)


    # إسقاط الصفوف التي تحتوي على قيم NaN بعد حساب المؤشرات والهدف
    initial_len = len(df_calc)
    df_cleaned = df_calc.dropna(subset=feature_columns + ['target']).copy()
    dropped_count = initial_len - len(df_cleaned)

    if dropped_count > 0:
        logger.info(f"ℹ️ [ML Prep] تم إسقاط {dropped_count} صفًا بسبب قيم NaN بعد حساب المؤشرات والهدف.")
    if df_cleaned.empty:
        logger.warning(f"⚠️ [ML Prep] DataFrame فارغ بعد إزالة قيم NaN لتجهيز ML.")
        return None

    logger.info(f"✅ [ML Prep] تم تجهيز البيانات بنجاح. عدد الصفوف: {len(df_cleaned)}")
    return df_cleaned[feature_columns + ['target']]


def train_and_evaluate_model(data: pd.DataFrame) -> Tuple[Any, Dict[str, Any]]:
    """
    يقوم بتدريب نموذج Decision Tree ويقيم أداءه.
    """
    logger.info("ℹ️ [ML Train] بدء تدريب وتقييم النموذج...")

    if data.empty:
        logger.error("❌ [ML Train] DataFrame فارغ للتدريب.")
        return None, {}

    X = data.drop('target', axis=1)
    y = data['target']

    if X.empty or y.empty:
        logger.error("❌ [ML Train] ميزات أو أهداف فارغة للتدريب.")
        return None, {}

    # تقسيم البيانات إلى مجموعات تدريب واختبار
    # استخدام stratify=y لضمان توزيع متوازن للفئات في مجموعات التدريب والاختبار
    try:
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    except ValueError as ve:
        logger.warning(f"⚠️ [ML Train] لا يمكن استخدام stratify بسبب وجود فئة واحدة في الهدف: {ve}. سيتم المتابعة بدون stratify.")
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)


    # التحجيم (Scaling) للميزات (مهم لبعض النماذج، وليس بالضرورة لـ Decision Tree ولكن ممارسة جيدة)
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    # تدريب نموذج Decision Tree Classifier
    model = DecisionTreeClassifier(random_state=42, max_depth=10) # يمكن تعديل المعلمات
    model.fit(X_train_scaled, y_train)
    logger.info("✅ [ML Train] تم تدريب النموذج بنجاح.")

    # التقييم
    y_pred = model.predict(X_test_scaled)
    accuracy = accuracy_score(y_test, y_pred)
    precision = precision_score(y_test, y_pred, zero_division=0)
    recall = recall_score(y_test, y_pred, zero_division=0)
    f1 = f1_score(y_test, y_pred, zero_division=0)

    metrics = {
        'accuracy': accuracy,
        'precision': precision,
        'recall': recall,
        'f1_score': f1,
        'num_samples_trained': len(X_train),
        'num_samples_tested': len(X_test),
        'feature_names': X.columns.tolist() # حفظ أسماء الميزات لضمان التناسق عند التحميل
    }

    logger.info(f"📊 [ML Train] مقاييس أداء النموذج:")
    logger.info(f"  - الدقة (Accuracy): {accuracy:.4f}")
    logger.info(f"  - الدقة (Precision): {precision:.4f}")
    logger.info(f"  - الاستدعاء (Recall): {recall:.4f}")
    logger.info(f"  - مقياس F1: {f1:.4f}")

    return model, metrics

def save_ml_model_to_db(model: Any, model_name: str, metrics: Dict[str, Any]) -> bool:
    """
    يحفظ النموذج المدرب وبياناته الوصفية (المقاييس) في قاعدة البيانات.
    """
    if not check_db_connection() or not conn:
        logger.error("❌ [DB Save] لا يمكن حفظ نموذج ML بسبب مشكلة في اتصال قاعدة البيانات.")
        return False

    logger.info(f"ℹ️ [DB Save] محاولة حفظ نموذج ML '{model_name}' في قاعدة البيانات...")
    try:
        # تسلسل النموذج باستخدام pickle
        model_binary = pickle.dumps(model)

        # تحويل المقاييس إلى JSONB
        metrics_json = json.dumps(convert_np_values(metrics))

        with conn.cursor() as db_cur:
            # التحقق مما إذا كان النموذج موجودًا بالفعل (للتحديث أو الإدراج)
            db_cur.execute("SELECT id FROM ml_models WHERE model_name = %s;", (model_name,))
            existing_model = db_cur.fetchone()

            if existing_model:
                # تحديث النموذج الموجود
                update_query = sql.SQL("""
                    UPDATE ml_models
                    SET model_data = %s, trained_at = NOW(), metrics = %s
                    WHERE id = %s;
                """)
                db_cur.execute(update_query, (model_binary, metrics_json, existing_model['id']))
                logger.info(f"✅ [DB Save] تم تحديث نموذج ML '{model_name}' في قاعدة البيانات بنجاح.")
            else:
                # إدراج نموذج جديد
                insert_query = sql.SQL("""
                    INSERT INTO ml_models (model_name, model_data, trained_at, metrics)
                    VALUES (%s, %s, NOW(), %s);
                """)
                db_cur.execute(insert_query, (model_name, model_binary, metrics_json))
                logger.info(f"✅ [DB Save] تم حفظ نموذج ML '{model_name}' جديد في قاعدة البيانات بنجاح.")
        conn.commit()
        return True
    except psycopg2.Error as db_err:
        logger.error(f"❌ [DB Save] خطأ في قاعدة البيانات أثناء حفظ نموذج ML: {db_err}")
        if conn: conn.rollback()
        return False
    except pickle.PicklingError as pickle_err:
        logger.error(f"❌ [DB Save] خطأ في تسلسل نموذج ML: {pickle_err}", exc_info=True)
        if conn: conn.rollback()
        return False
    except Exception as e:
        logger.error(f"❌ [DB Save] خطأ غير متوقع أثناء حفظ نموذج ML: {e}", exc_info=True)
        if conn: conn.rollback()
        return False

def cleanup_resources() -> None:
    """Closes used resources like the database connection."""
    global conn
    logger.info("ℹ️ [Cleanup] إغلاق الموارد...")
    if conn:
        try:
            conn.close()
            logger.info("✅ [DB] تم إغلاق اتصال قاعدة البيانات.")
        except Exception as close_err:
            logger.error(f"⚠️ [DB] خطأ في إغلاق اتصال قاعدة البيانات: {close_err}")
    logger.info("✅ [Cleanup] اكتمل تنظيف الموارد.")


# ---------------------- Flask Service ----------------------
app = Flask(__name__)

@app.route('/')
def home() -> Response:
    """Simple home page to show the bot is running."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status_message = (
        f"🤖 *ML Trainer Service Status:*\n"
        f"- Current Time: {now}\n"
        f"- Training Status: *{training_status}*\n"
    )
    if last_training_time:
        status_message += f"- Last Training Time: {last_training_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    if last_training_metrics:
        status_message += f"- Last Training Metrics (Accuracy): {last_training_metrics.get('accuracy', 'N/A'):.4f}\n"
    if training_error:
        status_message += f"- Last Error: {training_error}\n"

    return Response(status_message, status=200, mimetype='text/plain')

@app.route('/favicon.ico')
def favicon() -> Response:
    """Handles favicon request to avoid 404 errors in logs."""
    return Response(status=204)

def run_flask_service() -> None:
    """Runs the Flask application."""
    host = "0.0.0.0"
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"ℹ️ [Flask] بدء تطبيق Flask على {host}:{port}...")
    try:
        from waitress import serve
        logger.info("✅ [Flask] استخدام خادم 'waitress'.")
        serve(app, host=host, port=port, threads=6)
    except ImportError:
        logger.warning("⚠️ [Flask] 'waitress' غير مثبت. الرجوع إلى خادم تطوير Flask (لا يوصى به للإنتاج).")
        try:
            app.run(host=host, port=port)
        except Exception as flask_run_err:
            logger.critical(f"❌ [Flask] فشل بدء خادم التطوير: {flask_run_err}", exc_info=True)
    except Exception as serve_err:
        logger.critical(f"❌ [Flask] فشل بدء الخادم (waitress؟): {serve_err}", exc_info=True)


# ---------------------- نقطة الدخول الرئيسية ----------------------
if __name__ == "__main__":
    logger.info("🚀 بدء سكريبت تدريب نموذج التعلم الآلي...")
    logger.info(f"الوقت المحلي: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | وقت UTC: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")

    flask_thread: Optional[Thread] = None

    try:
        # 1. بدء خدمة Flask في خيط منفصل أولاً
        # هذا يضمن أن الخدمة ستكون متاحة للاستجابة لطلبات Uptime Monitor
        # بينما يتم تنفيذ عملية التدريب في الخيط الرئيسي.
        flask_thread = Thread(target=run_flask_service, daemon=False, name="FlaskServiceThread")
        flask_thread.start()
        logger.info("✅ [Main] تم بدء خدمة Flask.")
        time.sleep(2) # إعطاء بعض الوقت لـ Flask للبدء

        # 2. تهيئة قاعدة البيانات
        init_db()

        # 3. جلب قائمة الرموز
        symbols = get_crypto_symbols()
        if not symbols:
            logger.critical("❌ [Main] لا توجد رموز صالحة للتدريب. يرجى التحقق من 'crypto_list.txt'.")
            # لا حاجة لـ 'global' هنا لأننا نقوم بالتعيين مباشرة لمتغير عام
            training_status = "Failed: No valid symbols"
            exit(1)

        # لا حاجة لـ 'global' هنا لأننا نقوم بالتعيين مباشرة لمتغيرات عامة
        training_status = "In Progress: Fetching Data"
        training_error = None # Reset error

        all_data_for_training = pd.DataFrame()
        logger.info(f"ℹ️ [Main] جلب بيانات تاريخية لـ {len(symbols)} رمزًا للتدريب...")

        # 4. جلب البيانات التاريخية لجميع الرموز ودمجها
        for symbol in symbols:
            logger.info(f"⏳ [Main] جلب البيانات لـ {symbol}...")
            df_hist = fetch_historical_data(symbol, interval=SIGNAL_GENERATION_TIMEFRAME, days=DATA_LOOKBACK_DAYS_FOR_TRAINING)
            if df_hist is not None and not df_hist.empty:
                df_hist['symbol'] = symbol # إضافة عمود الرمز لتحديد مصدر البيانات
                all_data_for_training = pd.concat([all_data_for_training, df_hist])
                logger.info(f"✅ [Main] تم جلب {len(df_hist)} شمعة لـ {symbol}.")
            else:
                logger.warning(f"⚠️ [Main] لم يتمكن من جلب بيانات كافية لـ {symbol}. تخطي.")
            time.sleep(0.1) # تأخير لتجنب حدود معدل API

        if all_data_for_training.empty:
            logger.critical("❌ [Main] لم يتم جلب أي بيانات كافية للتدريب من أي رمز. لا يمكن المتابعة.")
            training_status = "Failed: No sufficient data"
            exit(1)

        logger.info(f"✅ [Main] تم جلب إجمالي {len(all_data_for_training)} صفًا من البيانات الخام لجميع الرموز.")

        # 5. تجهيز البيانات لنموذج التعلم الآلي
        training_status = "In Progress: Preparing Data"
        processed_dfs = []
        for symbol in symbols:
            symbol_data = all_data_for_training[all_data_for_training['symbol'] == symbol].copy()
            if not symbol_data.empty:
                df_processed = prepare_data_for_ml(symbol_data.drop(columns=['symbol']))
                if df_processed is not None and not df_processed.empty:
                    processed_dfs.append(df_processed)
            else:
                logger.warning(f"⚠️ [Main] لا توجد بيانات خام لـ {symbol} بعد الدمج الأولي.")

        if not processed_dfs:
            logger.critical("❌ [Main] لا توجد بيانات جاهزة للتدريب بعد المعالجة المسبقة للمؤشرات.")
            training_status = "Failed: No processed data"
            exit(1)

        final_training_data = pd.concat(processed_dfs).reset_index(drop=True)
        logger.info(f"✅ [Main] تم تجهيز إجمالي {len(final_training_data)} صفًا من البيانات للتدريب.")

        if final_training_data.empty:
            logger.critical("❌ [Main] DataFrame التدريب النهائي فارغ. لا يمكن تدريب النموذج.")
            training_status = "Failed: Empty training data"
            exit(1)

        # 6. تدريب وتقييم النموذج
        training_status = "In Progress: Training Model"
        trained_model, model_metrics = train_and_evaluate_model(final_training_data)

        if trained_model is None:
            logger.critical("❌ [Main] فشل تدريب النموذج. لا يمكن حفظه.")
            training_status = "Failed: Model training failed"
            exit(1)

        # 7. حفظ النموذج في قاعدة البيانات
        training_status = "In Progress: Saving Model"
        if save_ml_model_to_db(trained_model, ML_MODEL_NAME, model_metrics):
            logger.info(f"✅ [Main] تم حفظ النموذج '{ML_MODEL_NAME}' بنجاح في قاعدة البيانات.")
            training_status = "Completed Successfully"
            last_training_time = datetime.now()
            last_training_metrics = model_metrics
        else:
            logger.error(f"❌ [Main] فشل حفظ النموذج '{ML_MODEL_NAME}' في قاعدة البيانات.")
            training_status = "Completed with Errors: Model save failed"
            training_error = "Model save failed"

        # انتظر خيط Flask لإنهاء (مما يبقي البرنامج قيد التشغيل)
        if flask_thread:
            flask_thread.join()

    except Exception as e:
        logger.critical(f"❌ [Main] حدث خطأ فادح أثناء تشغيل سكريبت التدريب: {e}", exc_info=True)
        # لا حاجة لـ 'global' هنا لأننا نقوم بالتعيين مباشرة لمتغيرات عامة
        training_status = "Failed: Unhandled exception"
        training_error = str(e)
    finally:
        logger.info("🛑 [Main] يتم إيقاف تشغيل سكريبت التدريب...")
        cleanup_resources()
        logger.info("👋 [Main] تم إيقاف سكريبت تدريب نموذج التعلم الآلي.")
        # os._exit(0) # لا تستخدم os._exit(0) هنا إذا كنت تريد أن يبقى Flask يعمل

