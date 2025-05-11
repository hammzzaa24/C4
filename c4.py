import time
import os
import json
import logging
import requests
import numpy as np
import pandas as pd
import psycopg2
from psycopg2 import sql, OperationalError, InterfaceError # لاستخدام استعلامات آمنة وأخطاء محددة
from psycopg2.extras import RealDictCursor # للحصول على النتائج كقواميس
from binance.client import Client
from binance import ThreadedWebsocketManager
from binance.exceptions import BinanceAPIException, BinanceRequestException # أخطاء Binance المحددة
from flask import Flask, request, Response
from threading import Thread
from datetime import datetime, timedelta
from decouple import config
from typing import List, Dict, Optional, Tuple, Any, Union # لإضافة Type Hinting

# ---------------------- إعداد التسجيل ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', # إضافة اسم المسجل
    handlers=[
        logging.FileHandler('crypto_bot_momentum_growth.log', encoding='utf-8'), # اسم ملف تسجيل جديد
        logging.StreamHandler()
    ]
)
# استخدام اسم محدد للمسجل بدلاً من الجذر
logger = logging.getLogger('MomentumGrowthBot') # اسم مسجل جديد

# ---------------------- تحميل المتغيرات البيئية ----------------------
try:
    API_KEY: str = config('BINANCE_API_KEY')
    API_SECRET: str = config('BINANCE_API_SECRET')
    TELEGRAM_TOKEN: str = config('TELEGRAM_BOT_TOKEN')
    CHAT_ID: str = config('TELEGRAM_CHAT_ID')
    DB_URL: str = config('DATABASE_URL')
    # استخدام قيمة افتراضية None إذا لم يكن المتغير موجودًا
    WEBHOOK_URL: Optional[str] = config('WEBHOOK_URL', default=None)
except Exception as e:
     logger.critical(f"❌ Failed to load essential environment variables: {e}")
     exit(1) # استخدام رمز خروج غير صفري للإشارة إلى خطأ

logger.info(f"Binance API Key: {'Available' if API_KEY else 'Not available'}")
logger.info(f"Telegram Token: {TELEGRAM_TOKEN[:10]}...{'*' * (len(TELEGRAM_TOKEN)-10)}")
logger.info(f"Telegram Chat ID: {CHAT_ID}")
logger.info(f"Database URL: {'Available' if DB_URL else 'Not available'}")
logger.info(f"Webhook URL: {WEBHOOK_URL if WEBHOOK_URL else 'Not specified'}")

# ---------------------- إعداد الثوابت والمتغيرات العامة (معدلة لاستراتيجية النمو) ----------------------
TRADE_VALUE: float = 15.0         # Default trade value in USDT (Increased slightly)
MAX_OPEN_TRADES: int = 7          # Maximum number of open trades simultaneously (Increased)
SIGNAL_GENERATION_TIMEFRAME: str = '5m' # Timeframe for signal generation
SIGNAL_GENERATION_LOOKBACK_DAYS: int = 5 # Slightly increased historical data lookback
SIGNAL_TRACKING_TIMEFRAME: str = '5m' # Timeframe for signal tracking and stop loss updates
SIGNAL_TRACKING_LOOKBACK_DAYS: int = 1   # Reduced historical data lookback in days for signal tracking

# =============================================================================
# --- Indicator Parameters (Adjusted for Momentum Growth Scalping) ---
# These values are tuned for capturing strong momentum on 5m
# =============================================================================
RSI_PERIOD: int = 9          # RSI Period (Faster reaction)
RSI_OVERSOLD: int = 30        # Oversold threshold
RSI_OVERBOUGHT: int = 70      # Overbought threshold
EMA_SHORT_PERIOD: int = 8      # Short EMA period
EMA_LONG_PERIOD: int = 21       # Long EMA period
VWMA_PERIOD: int = 15           # VWMA Period
SWING_ORDER: int = 3          # Order for swing point detection (for Elliott/MACD swings - less critical for this strategy)
FIB_LEVELS_TO_CHECK: List[float] = [0.382, 0.5, 0.618] # Less relevant
FIB_TOLERANCE: float = 0.005 # Less relevant
LOOKBACK_FOR_SWINGS: int = 50 # Less relevant
ENTRY_ATR_PERIOD: int = 10     # ATR Period for initial stop loss and trailing stop calculation
ENTRY_ATR_MULTIPLIER: float = 1.8 # ATR Multiplier for initial stop loss (Adjusted)
BOLLINGER_WINDOW: int = 20     # Bollinger Bands Window
BOLLINGER_STD_DEV: int = 2       # Bollinger Bands Standard Deviation
MACD_FAST: int = 9            # MACD Fast Period
MACD_SLOW: int = 18            # MACD Slow Period
MACD_SIGNAL: int = 9             # MACD Signal Line Period
ADX_PERIOD: int = 10            # ADX Period
SUPERTREND_PERIOD: int = 10     # SuperTrend Period
SUPERTREND_MULTIPLIER: float = 2.5 # SuperTrend Multiplier (Adjusted)

# Momentum Indicator Parameters
STOCH_K_PERIOD: int = 14       # Stochastic %K Period
STOCH_D_PERIOD: int = 3        # Stochastic %D Period
STOCH_SMOOTH_K: int = 3        # Stochastic Smoothing for %K
STOCH_OVERBOUGHT: int = 80     # Stochastic Overbought Threshold
STOCH_OVERSOLD: int = 20       # Stochastic Oversold Threshold
WILLIAMS_R_PERIOD: int = 14    # Williams %R Period
WILLIAMS_R_OVERBOUGHT: int = -20 # Williams %R Overbought Threshold
WILLIAMS_R_OVERSOLD: int = -80 # Williams %R Oversold Threshold
# -----------------------------------------

# Strategy Specific Parameters (Momentum Growth Scalper)
MIN_PROFIT_FOR_GROWTH_PCT: float = 0.015 # Minimum profit percentage (1.5%) to trigger trailing stop and allow growth
MAX_INITIAL_LOSS_PCT: float = 0.02 # Maximum allowed initial loss percentage (2%)
TRAILING_STOP_ATR_MULTIPLIER: float = 2.5 # ATR Multiplier for trailing stop (Wider for growth)
TRAILING_STOP_MOVE_INCREMENT_PCT: float = 0.001 # Price increase percentage to move trailing stop (0.1%)

# Additional Signal Conditions
MIN_VOLUME_15M_USDT: float = 300000.0 # Minimum liquidity in the last 15 minutes in USDT (Increased)

# Parameters for Entry Logic Lookback
RECENT_EMA_CROSS_LOOKBACK: int = 3 # Check for EMA cross within the last X candles
MIN_ADX_TREND_STRENGTH: int = 25 # Increased minimum ADX for stronger trend confirmation
MACD_HIST_INCREASE_CANDLES: int = 3 # Check if MACD histogram is increasing over the last X candles
OBV_INCREASE_CANDLES: int = 3 # Check if OBV is increasing over the last X candles
RECENT_STOCH_CROSS_LOOKBACK: int = 3 # Check for Stochastic cross within the last X candles
# =============================================================================
# --- End Indicator Parameters ---
# =============================================================================


# Global variables (will be initialized later)
conn: Optional[psycopg2.extensions.connection] = None
cur: Optional[psycopg2.extensions.cursor] = None
client: Optional[Client] = None
ticker_data: Dict[str, float] = {} # Dictionary to store the latest closing prices for symbols

# ---------------------- Binance Client Setup ----------------------
try:
    logger.info("ℹ️ [Binance] Initializing Binance client...")
    client = Client(API_KEY, API_SECRET)
    client.ping() # Check connection and keys validity
    server_time = client.get_server_time()
    logger.info(f"✅ [Binance] Binance client initialized. Server time: {datetime.fromtimestamp(server_time['serverTime']/1000)}")
except BinanceRequestException as req_err:
     logger.critical(f"❌ [Binance] Binance request error (network or request issue): {req_err}")
     exit(1)
except BinanceAPIException as api_err:
     logger.critical(f"❌ [Binance] Binance API error (invalid keys or server issue): {api_err}")
     exit(1)
except Exception as e:
    logger.critical(f"❌ [Binance] Unexpected failure initializing Binance client: {e}")
    exit(1)

# ---------------------- Additional Indicator Functions (Keep as is) ----------------------
# Keeping the existing indicator calculation functions as they are used by the new strategy.
# (calculate_ema, calculate_vwma, get_btc_trend_4h, fetch_historical_data, calculate_rsi_indicator,
# calculate_atr_indicator, calculate_bollinger_bands, calculate_macd, calculate_adx,
# calculate_vwap, calculate_obv, calculate_supertrend, calculate_stochastic, calculate_williams_r)
# Assuming these functions are correctly implemented in the original script.
# Re-including them here for completeness in the new code block.

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
        logger.warning("⚠️ [Indicator VWMA] 'close' or 'volume' columns missing or empty.")
        return pd.Series(index=df_calc.index if df_calc is not None else None, dtype=float)
    if len(df_calc) < period:
        logger.warning(f"⚠️ [Indicator VWMA] Insufficient data ({len(df_calc)} < {period}) to calculate VWMA.")
        return pd.Series(index=df_calc.index if df_calc is not None else None, dtype=float)

    df_calc['price_volume'] = df_calc['close'] * df_calc['volume']
    rolling_price_volume_sum = df_calc['price_volume'].rolling(window=period, min_periods=period).sum()
    rolling_volume_sum = df_calc['volume'].rolling(window=period, min_periods=period).sum()
    vwma = rolling_price_volume_sum / rolling_volume_sum.replace(0, np.nan)
    df_calc.drop(columns=['price_volume'], inplace=True, errors='ignore')
    return vwma

def get_btc_trend_4h() -> str:
    """Calculates Bitcoin trend on 4-hour timeframe using EMA20 and EMA50."""
    logger.debug("ℹ️ [Indicators] Calculating Bitcoin 4-hour trend...")
    try:
        df = fetch_historical_data("BTCUSDT", interval=Client.KLINE_INTERVAL_4HOUR, days=10)
        if df is None or df.empty or len(df) < 50 + 1:
            logger.warning("⚠️ [Indicators] Insufficient BTC/USDT 4H data to calculate trend.")
            return "N/A (Insufficient Data)"

        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df.dropna(subset=['close'], inplace=True)
        if len(df) < 50:
             logger.warning("⚠️ [Indicators] Insufficient BTC/USDT 4H data after removing NaNs.")
             return "N/A (Insufficient Data)"

        ema20 = calculate_ema(df['close'], 20).iloc[-1]
        ema50 = calculate_ema(df['close'], 50).iloc[-1]
        current_close = df['close'].iloc[-1]

        if pd.isna(ema20) or pd.isna(ema50) or pd.isna(current_close):
            logger.warning("⚠️ [Indicators] BTC EMA or current price values are NaN.")
            return "N/A (Calculation Error)"

        diff_ema20_pct = abs(current_close - ema20) / current_close if current_close > 0 else 0

        if current_close > ema20 > ema50:
            trend = "صعود 📈" # Uptrend
        elif current_close < ema20 < ema50:
            trend = "هبوط 📉" # Downtrend
        elif diff_ema20_pct < 0.005: # Less than 0.5% difference, considered stable
            trend = "استقرار 🔄" # Sideways
        else: # Crossover or unclear divergence
            trend = "تذبذب 🔀" # Volatile

        logger.debug(f"✅ [Indicators] Bitcoin 4H Trend: {trend}")
        return trend
    except Exception as e:
        logger.error(f"❌ [Indicators] Error calculating Bitcoin 4-hour trend: {e}", exc_info=True)
        return "N/A (Error)"

def fetch_historical_data(symbol: str, interval: str, days: int) -> Optional[pd.DataFrame]:
    """Fetches historical candlestick data from Binance."""
    if not client:
        logger.error("❌ [Data] Binance client not initialized for data fetching.")
        return None
    try:
        start_dt = datetime.utcnow() - timedelta(days=days + 1) # Add an extra day as buffer
        start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        logger.debug(f"ℹ️ [Data] Fetching {interval} data for {symbol} since {start_str} (limit 1000 candles)...")

        klines = client.get_historical_klines(symbol, interval, start_str, limit=1000)

        if not klines:
            logger.warning(f"⚠️ [Data] No historical data ({interval}) for {symbol} for the requested period.")
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
            logger.debug(f"ℹ️ [Data] {symbol}: Dropped {initial_len - len(df)} rows due to NaN in OHLCV data.")

        if df.empty:
            logger.warning(f"⚠️ [Data] DataFrame for {symbol} is empty after removing essential NaNs.")
            return None

        logger.debug(f"✅ [Data] Fetched and processed {len(df)} historical candles ({interval}) for {symbol}.")
        return df

    except BinanceAPIException as api_err:
         logger.error(f"❌ [Data] Binance API error fetching data for {symbol}: {api_err}")
         return None
    except BinanceRequestException as req_err:
         logger.error(f"❌ [Data] Request or network error fetching data for {symbol}: {req_err}")
         return None
    except Exception as e:
        logger.error(f"❌ [Data] Unexpected error fetching historical data for {symbol}: {e}", exc_info=True)
        return None

def calculate_rsi_indicator(df: pd.DataFrame, period: int = RSI_PERIOD) -> pd.DataFrame:
    """Calculates Relative Strength Index (RSI)."""
    df = df.copy()
    if 'close' not in df.columns or df['close'].isnull().all():
        logger.warning("⚠️ [Indicator RSI] 'close' column missing or empty.")
        df['rsi'] = np.nan
        return df
    if len(df) < period:
        logger.warning(f"⚠️ [Indicator RSI] Insufficient data ({len(df)} < {period}) to calculate RSI.")
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
        logger.warning("⚠️ [Indicator ATR] 'high', 'low', 'close' columns missing or empty.")
        df['atr'] = np.nan
        return df
    if len(df) < period + 1:
        logger.warning(f"⚠️ [Indicator ATR] Insufficient data ({len(df)} < {period + 1}) to calculate ATR.")
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
        logger.warning("⚠️ [Indicator BB] 'close' column missing or empty.")
        df['bb_middle'] = np.nan
        df['bb_upper'] = np.nan
        df['bb_lower'] = np.nan
        return df
    if len(df) < window:
         logger.warning(f"⚠️ [Indicator BB] Insufficient data ({len(df)} < {window}) to calculate BB.")
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
        logger.warning("⚠️ [Indicator MACD] 'close' column missing or empty.")
        df['macd'] = np.nan
        df['macd_signal'] = np.nan
        df['macd_hist'] = np.nan
        return df
    min_len = max(fast, slow, signal)
    if len(df) < min_len:
        logger.warning(f"⚠️ [Indicator MACD] Insufficient data ({len(df)} < {min_len}) to calculate MACD.")
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
        logger.warning("⚠️ [Indicator ADX] 'high', 'low', 'close' columns missing or empty.")
        df_calc['adx'] = np.nan
        df_calc['di_plus'] = np.nan
        df_calc['di_minus'] = np.nan
        return df_calc
    if len(df_calc) < period * 2:
        logger.warning(f"⚠️ [Indicator ADX] Insufficient data ({len(df_calc)} < {period * 2}) to calculate ADX.")
        df_calc['adx'] = np.nan
        df_calc['di_plus'] = np.nan
        df_calc['di_minus'] = np.nan
        return df_calc

    df_calc['high-low'] = df_calc['high'] - df_calc['low']
    df_calc['high-prev_close'] = abs(df_calc['high'] - df_calc['close'].shift(1))
    df_calc['low-prev_close'] = abs(df_calc['low'].shift(1) - df_calc['low'])
    df_calc['tr'] = df_calc[['high-low', 'high-prev_close', 'low-prev_close']].max(axis=1, skipna=False)

    df_calc['up_move'] = df_calc['high'] - df_calc['high'].shift(1)
    df_calc['down_move'] = df_calc['low'].shift(1) - df_calc['low']
    df_calc['+dm'] = np.where((df_calc['up_move'] > df_calc['down_move']) & (df_calc['up_move'] > 0), df_calc['up_move'], 0)
    df_calc['-dm'] = np.where((df_calc['down_move'] > df_calc['up_move']) & (df_calc['down_move'] > 0), df_calc['down_move'], 0)

    alpha = 1 / period
    df_calc['tr_smooth'] = df_calc['tr'].ewm(alpha=alpha, adjust=False).mean()
    df_calc['+dm_smooth'] = df_calc['+dm'].ewm(alpha=alpha, adjust=False).mean()
    df_calc['-dm_smooth'] = df_calc['-dm'].ewm(alpha=alpha, adjust=False).mean()

    df_calc['di_plus'] = np.where(df_calc['tr_smooth'] > 0, 100 * (df_calc['+dm_smooth'] / df_calc['tr_smooth']), 0)
    df_calc['di_minus'] = np.where(df_calc['tr_smooth'] > 0, 100 * (df_calc['-dm_smooth'] / df_calc['tr_smooth']), 0)

    di_sum = df_calc['di_plus'] + df_calc['di_minus']
    df_calc['dx'] = np.where(di_sum > 0, 100 * abs(df_calc['di_plus'] - df_calc['di_minus']) / di_sum, 0)

    df_calc['adx'] = df_calc['dx'].ewm(alpha=alpha, adjust=False).mean()

    return df_calc[['adx', 'di_plus', 'di_minus']]


def calculate_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates Volume Weighted Average Price (VWAP) - Resets daily."""
    df = df.copy()
    required_cols = ['high', 'low', 'close', 'volume']
    if not all(col in df.columns for col in required_cols) or df[required_cols].isnull().all().any():
        logger.warning("⚠️ [Indicator VWAP] 'high', 'low', 'close' or 'volume' columns missing or empty.")
        df['vwap'] = np.nan
        return df
    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index)
            logger.warning("⚠️ [Indicator VWAP] Index converted to DatetimeIndex.")
        except Exception:
            logger.error("❌ [Indicator VWAP] Failed to convert index to DatetimeIndex, cannot calculate daily VWAP.")
            df['vwap'] = np.nan
            df.drop(columns=['date', 'typical_price', 'tp_vol', 'cum_tp_vol', 'cum_volume'], inplace=True, errors='ignore')
            return df

    df['date'] = df.index.date
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
    df['tp_vol'] = df['typical_price'] * df['volume']

    try:
        df['cum_tp_vol'] = df.groupby('date')['tp_vol'].cumsum()
        df['cum_volume'] = df.groupby('date')['volume'].cumsum()
    except KeyError as e:
        logger.error(f"❌ [Indicator VWAP] Error grouping data by date: {e}. Index might be incorrect.")
        df['vwap'] = np.nan
        df.drop(columns=['date', 'typical_price', 'tp_vol', 'cum_tp_vol', 'cum_volume'], inplace=True, errors='ignore')
        return df
    except Exception as e:
         logger.error(f"❌ [Indicator VWAP] Unexpected error in VWAP calculation: {e}", exc_info=True)
         df['vwap'] = np.nan
         df.drop(columns=['date', 'typical_price', 'tp_vol', 'cum_tp_vol', 'cum_volume'], inplace=True, errors='ignore')
         return df

    df['vwap'] = np.where(df['cum_volume'] > 0, df['cum_tp_vol'] / df['cum_volume'], np.nan)
    df['vwap'] = df['vwap'].bfill()
    df.drop(columns=['date', 'typical_price', 'tp_vol', 'cum_tp_vol', 'cum_volume'], inplace=True, errors='ignore')
    return df


def calculate_obv(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates On-Balance Volume (OBV)."""
    df = df.copy()
    required_cols = ['close', 'volume']
    if not all(col in df.columns for col in required_cols) or df[required_cols].isnull().all().any():
        logger.warning("⚠️ [Indicator OBV] 'close' or 'volume' columns missing or empty.")
        df['obv'] = np.nan
        return df
    if not pd.api.types.is_numeric_dtype(df['close']) or not pd.api.types.is_numeric_dtype(df['volume']):
        logger.warning("⚠️ [Indicator OBV] 'close' or 'volume' columns are not numeric.")
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
    """Calculates the SuperTrend indicator."""
    df_st = df.copy()
    required_cols = ['high', 'low', 'close']
    if not all(col in df_st.columns for col in required_cols) or df_st[required_cols].isnull().all().any():
        logger.warning("⚠️ [Indicator SuperTrend] 'high', 'low', 'close' columns missing or empty.")
        df_st['supertrend'] = np.nan
        df_st['supertrend_trend'] = 0 # 0: unknown, 1: uptrend, -1: downtrend
        return df_st

    if 'atr' not in df_st.columns or df_st['atr'].isnull().all():
        logger.debug(f"ℹ️ [Indicator SuperTrend] Calculating ATR (period={period}) for SuperTrend...")
        df_st = calculate_atr_indicator(df_st, period=period)

    if 'atr' not in df_st.columns or df_st['atr'].isnull().all():
         logger.warning("⚠️ [Indicator SuperTrend] Cannot calculate SuperTrend due to missing valid ATR values.")
         df_st['supertrend'] = np.nan
         df_st['supertrend_trend'] = 0
         return df_st
    if len(df_st) < period:
        logger.warning(f"⚠️ [Indicator SuperTrend] Insufficient data ({len(df_st)} < {period}) to calculate SuperTrend.")
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
             elif close[i] < final_lb[i]:
                  st[i] = final_ub[i]
                  st_trend[i] = -1
             else:
                  if close[i] > basic_ub[i]:
                      st[i] = basic_lb[i]
                      st_trend[i] = 1
                  elif close[i] < basic_lb[i]:
                      st[i] = basic_ub[i]
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

def calculate_stochastic(df: pd.DataFrame, k_period: int = STOCH_K_PERIOD, d_period: int = STOCH_D_PERIOD, smooth_k: int = STOCH_SMOOTH_K) -> pd.DataFrame:
    """Calculates Stochastic Oscillator (%K and %D)."""
    df_stoch = df.copy()
    required_cols = ['high', 'low', 'close']
    if not all(col in df_stoch.columns for col in required_cols) or df_stoch[required_cols].isnull().all().any():
        logger.warning("⚠️ [Indicator Stochastic] 'high', 'low', 'close' columns missing or empty.")
        df_stoch['stoch_k'] = np.nan
        df_stoch['stoch_d'] = np.nan
        return df_stoch
    min_len = max(k_period, d_period, smooth_k)
    if len(df_stoch) < min_len:
        logger.warning(f"⚠️ [Indicator Stochastic] Insufficient data ({len(df_stoch)} < {min_len}) to calculate Stochastic.")
        df_stoch['stoch_k'] = np.nan
        df_stoch['stoch_d'] = np.nan
        return df_stoch

    lowest_low = df_stoch['low'].rolling(window=k_period).min()
    highest_high = df_stoch['high'].rolling(window=k_period).max()
    range_hl = highest_high - lowest_low
    df_stoch['stoch_k_raw'] = ((df_stoch['close'] - lowest_low) / range_hl) * 100
    df_stoch['stoch_k_raw'] = df_stoch['stoch_k_raw'].replace([np.inf, -np.inf], np.nan)

    df_stoch['stoch_k'] = df_stoch['stoch_k_raw'].rolling(window=smooth_k).mean()
    df_stoch['stoch_d'] = df_stoch['stoch_k'].rolling(window=d_period).mean()
    df_stoch.drop(columns=['stoch_k_raw'], inplace=True, errors='ignore')

    return df_stoch[['stoch_k', 'stoch_d']]

def calculate_williams_r(df: pd.DataFrame, period: int = WILLIAMS_R_PERIOD) -> pd.DataFrame:
    """Calculates Williams %R."""
    df_wr = df.copy()
    required_cols = ['high', 'low', 'close']
    if not all(col in df_wr.columns for col in required_cols) or df_wr[required_cols].isnull().all().any():
        logger.warning("⚠️ [Indicator WilliamsR] 'high', 'low', 'close' columns missing or empty.")
        df_wr['williams_r'] = np.nan
        return df_wr
    if len(df_wr) < period:
        logger.warning(f"⚠️ [Indicator WilliamsR] Insufficient data ({len(df_wr)} < {period}) to calculate Williams %R.")
        df_wr['williams_r'] = np.nan
        return df_wr

    highest_high = df_wr['high'].rolling(window=period).max()
    lowest_low = df_wr['low'].rolling(window=period).min()
    range_hl = highest_high - lowest_low
    df_wr['williams_r'] = ((highest_high - df_wr['close']) / range_hl) * -100
    df_wr['williams_r'] = df_wr['williams_r'].replace([np.inf, -np.inf], np.nan)

    return df_wr[['williams_r']]

# ---------------------- Candlestick Patterns (Keep as is) ----------------------
# Keeping the existing candlestick pattern functions.
# (is_hammer, is_shooting_star, is_doji, compute_engulfing, detect_candlestick_patterns)
# Assuming these functions are correctly implemented in the original script.
# Re-including them here for completeness in the new code block.

def is_hammer(row: pd.Series) -> int:
    """Checks for Hammer pattern (bullish signal)."""
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
    return 1 if is_small_body and is_long_lower_shadow and is_small_upper_shadow else 0 # Return 1 for bullish pattern

def is_shooting_star(row: pd.Series) -> int:
    """Checks for Shooting Star pattern (bearish signal)."""
    o, h, l, c = row.get('open'), row.get('high'), row.get('low'), row.get('close')
    if pd.isna([o, h, l, c]).any(): return 0
    body = abs(c - o)
    candle_range = h - l
    if candle_range == 0: return 0
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    is_small_body = body < (candle_range * 0.35)
    is_long_upper_shadow = upper_shadow >= 1.8 * body if body > 0 else upper_shadow > candle_range * 0.6
    is_small_lower_shadow = lower_shadow <= body * 0.6 if body > 0 else lower_shadow < candle_range * 0.15
    return -1 if is_small_body and is_long_upper_shadow and is_small_lower_shadow else 0 # Return -1 for bearish pattern

def is_doji(row: pd.Series) -> int:
    """Checks for Doji pattern (uncertainty)."""
    o, h, l, c = row.get('open'), row.get('high'), row.get('low'), row.get('close')
    if pd.isna([o, h, l, c]).any(): return 0
    candle_range = h - l
    if candle_range == 0: return 0
    return 1 if abs(c - o) <= (candle_range * 0.1) else 0 # Return 1 for doji

def compute_engulfing(df: pd.DataFrame, idx: int) -> int:
    """Checks for Bullish or Bearish Engulfing pattern."""
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

    if is_bullish: return 1
    if is_bearish: return -1
    return 0

def detect_candlestick_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Adds candlestick pattern signals to the DataFrame."""
    df = df.copy()
    logger.debug("ℹ️ [Indicators] Detecting candlestick patterns...")
    df['Hammer'] = df.apply(is_hammer, axis=1)
    df['ShootingStar'] = df.apply(is_shooting_star, axis=1)
    df['Doji'] = df.apply(is_doji, axis=1)
    engulfing_values = [compute_engulfing(df, i) for i in range(len(df))]
    df['Engulfing'] = engulfing_values

    df['BullishCandleSignal'] = df.apply(lambda row: 1 if (row['Hammer'] == 1 or row['Engulfing'] == 1) else 0, axis=1)
    df['BearishCandleSignal'] = df.apply(lambda row: 1 if (row['ShootingStar'] == -1 or row['Engulfing'] == -1) else 0, axis=1)

    logger.debug("✅ [Indicators] Candlestick patterns detected.")
    return df

# ---------------------- Other Helper Functions (Elliott, Swings, Volume - Keep as is) ----------------------
# Keeping the existing helper functions.
# (detect_swings, detect_elliott_waves, fetch_recent_volume)
# Assuming these functions are correctly implemented in the original script.
# Re-including them here for completeness in the new code block.

def detect_swings(prices: np.ndarray, order: int = SWING_ORDER) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Detects swing points (peaks and troughs) in a time series (numpy array)."""
    n = len(prices)
    if n < 2 * order + 1: return [], []

    maxima_indices = []
    minima_indices = []

    for i in range(order, n - order):
        window = prices[i - order : i + order + 1]
        center_val = prices[i]

        if np.isnan(window).any(): continue

        is_max = np.all(center_val >= window)
        is_min = np.all(center_val <= window)
        is_unique_max = is_max and (np.sum(window == center_val) == 1)
        is_unique_min = is_min and (np.sum(window == center_val) == 1)

        if is_unique_max:
            if not maxima_indices or i > maxima_indices[-1] + order:
                 maxima_indices.append(i)
        elif is_unique_min:
            if not minima_indices or i > minima_indices[-1] + order:
                minima_indices.append(i)

    maxima = [(idx, prices[idx]) for idx in maxima_indices]
    minima = [(idx, prices[idx]) for idx in minima_indices]
    return maxima, minima

def detect_elliott_waves(df: pd.DataFrame, order: int = SWING_ORDER) -> List[Dict[str, Any]]:
    """Simple attempt to identify Elliott Waves based on MACD histogram swings."""
    if 'macd_hist' not in df.columns or df['macd_hist'].isnull().all():
        logger.warning("⚠️ [Elliott] 'macd_hist' column missing or empty for Elliott Wave calculation.")
        return []

    macd_values = df['macd_hist'].dropna().values
    if len(macd_values) < 2 * order + 1:
         logger.warning("⚠️ [Elliott] Insufficient MACD hist data after removing NaNs.")
         return []

    maxima, minima = detect_swings(macd_values, order=order)

    df_nonan_macd = df['macd_hist'].dropna()
    all_swings = sorted(
        [(df_nonan_macd.index[idx], val, 'max') for idx, val in maxima] +
        [(df_nonan_macd.index[idx], val, 'min') for idx, val in minima],
        key=lambda x: x[0]
    )

    waves = []
    wave_number = 1
    for timestamp, val, typ in all_swings:
        wave_type = "Impulse" if (typ == 'max' and val > 0) or (typ == 'min' and val >= 0) else "Correction"
        waves.append({
            "wave": wave_number,
            "timestamp": str(timestamp),
            "macd_hist_value": float(val),
            "swing_type": typ,
            "classified_type": wave_type
        })
        wave_number += 1
    return waves


def fetch_recent_volume(symbol: str) -> float:
    """Fetches the trading volume in USDT for the last 15 minutes for the specified symbol."""
    if not client:
         logger.error(f"❌ [Data Volume] Binance client not initialized to fetch volume for {symbol}.")
         return 0.0
    try:
        logger.debug(f"ℹ️ [Data Volume] Fetching 15-minute volume for {symbol}...")
        klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=15)
        if not klines or len(klines) < 15:
             logger.warning(f"⚠️ [Data Volume] Insufficient 1m data (less than 15 candles) for {symbol}.")
             return 0.0

        volume_usdt = sum(float(k[7]) for k in klines if len(k) > 7 and k[7])
        logger.debug(f"✅ [Data Volume] Last 15 minutes liquidity for {symbol}: {volume_usdt:.2f} USDT")
        return volume_usdt
    except (BinanceAPIException, BinanceRequestException) as binance_err:
         logger.error(f"❌ [Data Volume] Binance API or network error while fetching volume for {symbol}: {binance_err}")
         return 0.0
    except Exception as e:
        logger.error(f"❌ [Data Volume] Unexpected error fetching volume for {symbol}: {e}", exc_info=True)
        return 0.0

# ---------------------- Database Connection Setup (Keep as is) ----------------------
# Keeping the existing database functions.
# (init_db, check_db_connection, convert_np_values)
# Assuming these functions are correctly implemented in the original script.
# Re-including them here for completeness in the new code block.

def init_db(retries: int = 5, delay: int = 5) -> None:
    """ Initializes database connection and creates tables if they don't exist. """
    global conn, cur
    logger.info("[DB] Starting database initialization...")
    for attempt in range(retries):
        try:
            logger.info(f"[DB] Attempting to connect to database (Attempt {attempt + 1}/{retries})...")
            conn = psycopg2.connect(DB_URL, connect_timeout=10, cursor_factory=RealDictCursor)
            conn.autocommit = False
            cur = conn.cursor()
            logger.info("✅ [DB] Successfully connected to database.")

            logger.info("[DB] Checking/Creating 'signals' table...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id SERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    entry_price DOUBLE PRECISION NOT NULL,
                    initial_target DOUBLE PRECISION NOT NULL,
                    initial_stop_loss DOUBLE PRECISION NOT NULL,
                    current_target DOUBLE PRECISION NOT NULL,
                    current_stop_loss DOUBLE PRECISION NOT NULL,
                    r2_score DOUBLE PRECISION, -- Now represents the weighted signal score (can be kept or repurposed)
                    volume_15m DOUBLE PRECISION,
                    achieved_target BOOLEAN DEFAULT FALSE,
                    hit_stop_loss BOOLEAN DEFAULT FALSE,
                    closing_price DOUBLE PRECISION,
                    closed_at TIMESTAMP,
                    sent_at TIMESTAMP DEFAULT NOW(),
                    profit_percentage DOUBLE PRECISION,
                    profitable_stop_loss BOOLEAN DEFAULT FALSE,
                    is_trailing_active BOOLEAN DEFAULT FALSE,
                    strategy_name TEXT,
                    signal_details JSONB,
                    last_trailing_update_price DOUBLE PRECISION
                );""")
            conn.commit()
            logger.info("✅ [DB] 'signals' table exists or was created.")

            required_columns = {
                "symbol", "entry_price", "initial_target", "initial_stop_loss",
                "current_target", "current_stop_loss", "r2_score", "volume_15m",
                "achieved_target", "hit_stop_loss", "closing_price", "closed_at",
                "sent_at", "profit_percentage", "profitable_stop_loss",
                "is_trailing_active", "strategy_name", "signal_details",
                "last_trailing_update_price"
            }
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'signals' AND table_schema = 'public';")
            existing_columns = {row['column_name'] for row in cur.fetchall()}
            missing_columns = required_columns - existing_columns

            if missing_columns:
                logger.warning(f"⚠️ [DB] Following columns are missing in 'signals' table: {missing_columns}. Please add them manually if needed.")
            else:
                logger.info("✅ [DB] All required columns exist in 'signals' table.")

            logger.info("[DB] Checking/Creating 'market_dominance' table...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS market_dominance (
                    id SERIAL PRIMARY KEY,
                    recorded_at TIMESTAMP DEFAULT NOW(),
                    btc_dominance DOUBLE PRECISION,
                    eth_dominance DOUBLE PRECISION
                );
            """)
            conn.commit()
            logger.info("✅ [DB] 'market_dominance' table exists or was created.")

            logger.info("✅ [DB] Database initialization successful.")
            return

        except OperationalError as op_err:
            logger.error(f"❌ [DB] Operational error connecting (Attempt {attempt + 1}): {op_err}")
            if conn: conn.rollback()
            if attempt == retries - 1:
                 logger.critical("❌ [DB] All database connection attempts failed.")
                 raise op_err
            time.sleep(delay)
        except Exception as e:
            logger.critical(f"❌ [DB] Unexpected failure initializing database (Attempt {attempt + 1}): {e}", exc_info=True)
            if conn: conn.rollback()
            if attempt == retries - 1:
                 logger.critical("❌ [DB] All database connection attempts failed.")
                 raise e
            time.sleep(delay)

    logger.critical("❌ [DB] Database connection failed after multiple attempts.")
    exit(1)


def check_db_connection() -> bool:
    """Checks database connection status and re-initializes if necessary."""
    global conn, cur
    try:
        if conn is None or conn.closed != 0:
            logger.warning("⚠️ [DB] Connection closed or not found. Re-initializing...")
            init_db()
            return True
        else:
             with conn.cursor() as check_cur:
                  check_cur.execute("SELECT 1;")
                  check_cur.fetchone()
             return True
    except (OperationalError, InterfaceError) as e:
        logger.error(f"❌ [DB] Database connection lost ({e}). Re-initializing...")
        try:
             init_db()
             return True
        except Exception as recon_err:
            logger.error(f"❌ [DB] Reconnection attempt failed after connection loss: {recon_err}")
            return False
    except Exception as e:
        logger.error(f"❌ [DB] Unexpected error during connection check: {e}", exc_info=True)
        try:
            init_db()
            return True
        except Exception as recon_err:
             logger.error(f"❌ [DB] Reconnection attempt failed after unexpected error: {recon_err}")
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

# ---------------------- Reading and Validating Symbols List (Keep as is) ----------------------
# Keeping the existing symbol validation function.
# (get_crypto_symbols)
# Assuming this function is correctly implemented in the original script.
# Re-including them here for completeness in the new code block.

def get_crypto_symbols(filename: str = 'crypto_list.txt') -> List[str]:
    """
    Reads the list of currency symbols from a text file, then validates them
    as valid USDT pairs available for Spot trading on Binance.
    """
    raw_symbols: List[str] = []
    logger.info(f"ℹ️ [Data] Reading symbols list from file '{filename}'...")
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(script_dir, filename)

        if not os.path.exists(file_path):
            file_path = os.path.abspath(filename)
            if not os.path.exists(file_path):
                 logger.error(f"❌ [Data] File '{filename}' not found in script directory or current directory.")
                 return []
            else:
                 logger.warning(f"⚠️ [Data] File '{filename}' not found in script directory. Using file in current directory: '{file_path}'")

        with open(file_path, 'r', encoding='utf-8') as f:
            raw_symbols = [f"{line.strip().upper().replace('USDT', '')}USDT"
                           for line in f if line.strip() and not line.startswith('#')]
        raw_symbols = sorted(list(set(raw_symbols)))
        logger.info(f"ℹ️ [Data] Read {len(raw_symbols)} initial symbols from '{file_path}'.")

    except FileNotFoundError:
         logger.error(f"❌ [Data] File '{filename}' not found.")
         return []
    except Exception as e:
        logger.error(f"❌ [Data] Error reading file '{filename}': {e}", exc_info=True)
        return []

    if not raw_symbols:
         logger.warning("⚠️ [Data] Initial symbols list is empty.")
         return []

    if not client:
        logger.error("❌ [Data Validation] Binance client not initialized. Cannot validate symbols.")
        return raw_symbols

    try:
        logger.info("ℹ️ [Data Validation] Validating symbols and trading status from Binance API...")
        exchange_info = client.get_exchange_info()
        valid_trading_usdt_symbols = {
            s['symbol'] for s in exchange_info['symbols']
            if s.get('quoteAsset') == 'USDT' and
               s.get('status') == 'TRADING' and
               s.get('isSpotTradingAllowed') is True
        }
        logger.info(f"ℹ️ [Data Validation] Found {len(valid_trading_usdt_symbols)} valid USDT Spot trading pairs on Binance.")

        validated_symbols = [symbol for symbol in raw_symbols if symbol in valid_trading_usdt_symbols]

        removed_count = len(raw_symbols) - len(validated_symbols)
        if removed_count > 0:
            removed_symbols = set(raw_symbols) - set(validated_symbols)
            logger.warning(f"⚠️ [Data Validation] Removed {removed_count} invalid or unavailable USDT Spot trading symbols from the list: {', '.join(removed_symbols)}")

        logger.info(f"✅ [Data Validation] Symbols validated. Using {len(validated_symbols)} valid symbols.")
        return validated_symbols

    except (BinanceAPIException, BinanceRequestException) as binance_err:
         logger.error(f"❌ [Data Validation] Binance API or network error while validating symbols: {binance_err}")
         logger.warning("⚠️ [Data Validation] Using initial list from file without Binance validation.")
         return raw_symbols
    except Exception as api_err:
         logger.error(f"❌ [Data Validation] Unexpected error while validating Binance symbols: {api_err}", exc_info=True)
         logger.warning("⚠️ [Data Validation] Using initial list from file without Binance validation.")
         return raw_symbols


# ---------------------- WebSocket Management for Ticker Prices (Keep as is) ----------------------
# Keeping the existing WebSocket functions.
# (handle_ticker_message, run_ticker_socket_manager)
# Assuming these functions are correctly implemented in the original script.
# Re-including them here for completeness in the new code block.

def handle_ticker_message(msg: Union[List[Dict[str, Any]], Dict[str, Any]]) -> None:
    """Handles incoming WebSocket messages for mini-ticker prices."""
    global ticker_data
    try:
        if isinstance(msg, list):
            for ticker_item in msg:
                symbol = ticker_item.get('s')
                price_str = ticker_item.get('c')
                if symbol and 'USDT' in symbol and price_str:
                    try:
                        ticker_data[symbol] = float(price_str)
                    except ValueError:
                         logger.warning(f"⚠️ [WS] Invalid price value for symbol {symbol}: '{price_str}'")
        elif isinstance(msg, dict):
             if msg.get('e') == 'error':
                 logger.error(f"❌ [WS] Error message from WebSocket: {msg.get('m', 'No error details')}")
             elif msg.get('stream') and msg.get('data'):
                 for ticker_item in msg.get('data', []):
                    symbol = ticker_item.get('s')
                    price_str = ticker_item.get('c')
                    if symbol and 'USDT' in symbol and price_str:
                        try:
                            ticker_data[symbol] = float(price_str)
                        except ValueError:
                             logger.warning(f"⚠️ [WS] Invalid price value for symbol {symbol} in combined stream: '{price_str}'")
        else:
             logger.warning(f"⚠️ [WS] Received WebSocket message with unexpected format: {type(msg)}")

    except Exception as e:
        logger.error(f"❌ [WS] Error processing ticker message: {e}", exc_info=True)


def run_ticker_socket_manager() -> None:
    """Runs and manages the WebSocket connection for mini-ticker."""
    while True:
        try:
            logger.info("ℹ️ [WS] Starting WebSocket Manager for Ticker prices...")
            twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
            twm.start()

            stream_name = twm.start_miniticker_socket(callback=handle_ticker_message)
            logger.info(f"✅ [WS] WebSocket stream started: {stream_name}")

            twm.join()
            logger.warning("⚠️ [WS] WebSocket Manager stopped. Restarting...")

        except Exception as e:
            logger.error(f"❌ [WS] Fatal error in WebSocket Manager: {e}. Restarting in 15 seconds...", exc_info=True)

        time.sleep(15)

# ---------------------- Telegram Functions (Adjusted for new strategy) ----------------------
# Keeping the existing Telegram functions, but modifying send_telegram_alert.
# (send_telegram_message, send_tracking_notification)
# Assuming these functions are correctly implemented in the original script.
# Re-including them here for completeness in the new code block.

def send_telegram_message(target_chat_id: str, text: str, reply_markup: Optional[Dict] = None, parse_mode: str = 'Markdown', disable_web_page_preview: bool = True, timeout: int = 20) -> Optional[Dict]:
    """Sends a message via Telegram Bot API with improved error handling."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': str(target_chat_id),
        'text': text,
        'parse_mode': parse_mode,
        'disable_web_page_preview': disable_web_page_preview
    }
    if reply_markup:
        try:
            payload['reply_markup'] = json.dumps(convert_np_values(reply_markup))
        except (TypeError, ValueError) as json_err:
             logger.error(f"❌ [Telegram] Failed to convert reply_markup to JSON: {json_err} - Markup: {reply_markup}")
             return None

    logger.debug(f"ℹ️ [Telegram] Sending message to {target_chat_id}...")
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        logger.info(f"✅ [Telegram] Message sent successfully to {target_chat_id}.")
        return response.json()
    except requests.exceptions.Timeout:
         logger.error(f"❌ [Telegram] Failed to send message to {target_chat_id} (Timeout).")
         return None
    except requests.exceptions.HTTPError as http_err:
        logger.error(f"❌ [Telegram] Failed to send message to {target_chat_id} (HTTP Error: {http_err.response.status_code}).")
        try:
            error_details = http_err.response.json()
            logger.error(f"❌ [Telegram] API error details: {error_details}")
        except json.JSONDecodeError:
            logger.error(f"❌ [Telegram] Could not decode error response: {http_err.response.text}")
        return None
    except requests.exceptions.RequestException as req_err:
        logger.error(f"❌ [Telegram] Failed to send message to {target_chat_id} (Request Error): {req_err}")
        return None
    except Exception as e:
         logger.error(f"❌ [Telegram] Unexpected error sending message: {e}", exc_info=True)
         return None

def send_tracking_notification(details: Dict[str, Any]) -> None:
    """Formats and sends enhanced Telegram notifications for tracking events in Arabic."""
    symbol = details.get('symbol', 'N/A')
    signal_id = details.get('id', 'N/A')
    notification_type = details.get('type', 'unknown')
    message = ""
    safe_symbol = symbol.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')
    closing_price = details.get('closing_price', 0.0)
    profit_pct = details.get('profit_pct', 0.0)
    current_price = details.get('current_price', 0.0)
    atr_value = details.get('atr_value', 0.0)
    new_stop_loss = details.get('new_stop_loss', 0.0)
    old_stop_loss = details.get('old_stop_loss', 0.0)
    new_target = details.get('new_target', 0.0)
    old_target = details.get('old_target', 0.0)


    logger.debug(f"ℹ️ [Notification] Formatting tracking notification: ID={signal_id}, Type={notification_type}, Symbol={symbol}")

    if notification_type == 'target_hit':
        message = (
            f"✅ *تم الوصول إلى الهدف الأولي (ID: {signal_id})*\n" # Modified text
            f"——————————————\n"
            f"🪙 **الزوج:** `{safe_symbol}`\n"
            f"🎯 **سعر الإغلاق (الهدف):** `${closing_price:,.8g}`\n"
            f"💰 **الربح المحقق حتى الآن:** {profit_pct:+.2f}%\n" # Modified text
            f"➡️ *تم تفعيل وقف الخسارة المتحرك للسماح بنمو الربح.*" # Added text
        )
    elif notification_type == 'stop_loss_hit':
        sl_type_msg_ar = "بربح ✅" if details.get('profitable_sl', False) else "بخسارة ❌"
        message = (
            f"🛑 *تم ضرب وقف الخسارة (ID: {signal_id})*\n"
            f"——————————————\n"
            f"🪙 **الزوج:** `{safe_symbol}`\n"
            f"🚫 **سعر الإغلاق (الوقف):** `${closing_price:,.8g}`\n"
            f"📉 **النتيجة:** {profit_pct:.2f}% ({sl_type_msg_ar})"
        )
    elif notification_type == 'trailing_activated':
        activation_profit_pct = details.get('activation_profit_pct', MIN_PROFIT_FOR_GROWTH_PCT * 100) # Use new constant
        message = (
            f"⬆️ *تم تفعيل وقف الخسارة المتحرك (ID: {signal_id})*\n"
            f"——————————————\n"
            f"🪙 **الزوج:** `{safe_symbol}`\n"
            f"📈 **السعر الحالي (عند التفعيل):** `${current_price:,.8g}` (الربح > {activation_profit_pct:.1f}%)\n"
            f"📊 **قيمة ATR ({ENTRY_ATR_PERIOD}):** `{atr_value:,.8g}` (المضاعف: {TRAILING_STOP_ATR_MULTIPLIER})\n"
            f"🛡️ **وقف الخسارة الجديد:** `${new_stop_loss:,.8g}`\n"
            f"✨ *الآن يمكن للصفقة تحقيق أرباح أكبر مع تحرك السعر للأعلى.*" # Added text
        )
    elif notification_type == 'trailing_updated':
        trigger_price_increase_pct = details.get('trigger_price_increase_pct', TRAILING_STOP_MOVE_INCREMENT_PCT * 100)
        message = (
            f"➡️ *تم تحديث وقف الخسارة المتحرك (ID: {signal_id})*\n"
            f"——————————————\n"
            f"🪙 **الزوج:** `{safe_symbol}`\n"
            f"📈 **السعر الحالي (عند التحديث):** `${current_price:,.8g}` (+{trigger_price_increase_pct:.1f}% منذ آخر تحديث)\n"
            f"📊 **قيمة ATR ({ENTRY_ATR_PERIOD}):** `{atr_value:,.8g}` (المضاعف: {TRAILING_STOP_ATR_MULTIPLIER})\n"
            f"🔒 **الوقف السابق:** `${old_stop_loss:,.8g}`\n"
            f"🛡️ **وقف الخسارة الجديد:** `${new_stop_loss:,.8g}`"
        )
    elif notification_type == 'target_and_sl_updated': # This type might be less relevant with the new strategy focus on trailing stop
        message = (
            f"🔄 *تم تحديث الهدف ووقف الخسارة بناءً على تحليل جديد (ID: {signal_id})*\n"
            f"——————————————\n"
            f"🪙 **الزوج:** `{safe_symbol}`\n"
            f"📈 **السعر الحالي:** `${current_price:,.8g}`\n"
            f"🎯 **الهدف السابق:** `${old_target:,.8g}`\n"
            f"🎯 **الهدف الجديد:** `${new_target:,.8g}`\n"
            f"🔒 **الوقف السابق:** `${old_stop_loss:,.8g}`\n"
            f"🛡️ **وقف الخسارة الجديد:** `${new_stop_loss:,.8g}`"
        )
    else:
        logger.warning(f"⚠️ [Notification] Unknown notification type: {notification_type} for details: {details}")
        return

    if message:
        send_telegram_message(CHAT_ID, message, parse_mode='Markdown')

def send_telegram_alert(signal_data: Dict[str, Any], timeframe: str) -> None:
    """Formats and sends a new trading signal alert to Telegram in Arabic."""
    logger.debug(f"ℹ️ [Telegram Alert] Formatting and sending alert for signal: {signal_data.get('symbol', 'N/A')}")
    try:
        entry_price = float(signal_data['entry_price'])
        initial_target = float(signal_data['initial_target']) # Initial target as the first milestone
        initial_stop_loss = float(signal_data['initial_stop_loss'])
        symbol = signal_data['symbol']
        strategy_name = signal_data.get('strategy_name', 'N/A')
        volume_15m = signal_data.get('volume_15m', 0.0)
        trade_value_signal = signal_data.get('trade_value', TRADE_VALUE)
        signal_details = signal_data.get('signal_details', {})

        initial_profit_pct = ((initial_target / entry_price) - 1) * 100 if entry_price > 0 else 0
        initial_loss_pct = ((initial_stop_loss / entry_price) - 1) * 100 if entry_price > 0 else 0
        # Calculate potential profit/loss based on initial levels and trade value
        potential_initial_profit_usdt = trade_value_signal * (initial_profit_pct / 100)
        potential_initial_loss_usdt = abs(trade_value_signal * (initial_loss_pct / 100))


        timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        safe_symbol = symbol.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')

        fear_greed = get_fear_greed_index()
        btc_trend = get_btc_trend_4h()

        # Build the message in Arabic
        message = (
            f"💡 *إشارة تداول جديدة ({strategy_name.replace('_', ' ').title()})* 💡\n" # Strategy name in title case
            f"——————————————\n"
            f"🪙 **الزوج:** `{safe_symbol}`\n"
            f"📈 **نوع الإشارة:** شراء (طويل)\n"
            f"🕰️ **الإطار الزمني:** {timeframe}\n"
            f"💧 **السيولة (15 دقيقة):** {volume_15m:,.0f} USDT\n"
            f"——————————————\n"
            f"➡️ **سعر الدخول المقترح:** `${entry_price:,.8g}`\n"
            f"🎯 **الهدف الأولي:** `${initial_target:,.8g}` (≈ {initial_profit_pct:+.2f}%)\n" # Display initial target and its percentage
            f"🛑 **وقف الخسارة الأولي:** `${initial_stop_loss:,.8g}` (≈ {initial_loss_pct:.2f}%)\n" # Display initial stop loss and its percentage
            f"——————————————\n"
            f"✅ *الشروط الإلزامية المحققة:*\n"
            # List mandatory conditions and their status from signal_details
            f"  - تقاطع المتوسطات الأسيّة (حديث): {signal_details.get('EMA_Cross', 'N/A')}\n"
            f"  - سوبر ترند: {signal_details.get('SuperTrend', 'N/A')}\n"
            f"  - ماكد: {signal_details.get('MACD', 'N/A')}\n"
            f"  - مؤشر الاتجاه (ADX/DI): {signal_details.get('ADX/DI', 'N/A')}\n"
            f"  - المتوسط الوزني للحجم (VWMA): {signal_details.get('VWMA_Mandatory', 'N/A')}\n"
            f"  - تقاطع Stochastic صعودي (حديث) وليس تشبع شراء: {signal_details.get('Stoch_Cross_Not_Overbought', 'N/A')}\n" # Updated text
            f"  - Williams %R ليس في تشبع شراء: {signal_details.get('Williams_R_Not_Overbought', 'N/A')}\n" # Updated text
            f"  - نمط شمعة صعودي مؤكد: {signal_details.get('Bullish_Candle_Confirmed', 'N/A')}\n" # Updated text
            f"  - حجم التوازن (OBV) يتزايد مؤخراً: {signal_details.get('OBV_Increasing_Recent', 'N/A')}\n"
            f"  - السيولة كافية: {signal_details.get('Volume_Check', 'N/A')}\n" # Added Volume check status
            f"  - اتجاه البيتكوين: {signal_details.get('BTC_Trend', 'N/A')}\n" # Added BTC Trend status
            f"——————————————\n"
            f"✨ *ملاحظة:* عند الوصول للهدف الأولي، سيتم تفعيل وقف الخسارة المتحرك للسماح بنمو الربح.\n" # Added explanation
            f"——————————————\n"
            f"😨/🤑 **مؤشر الخوف والجشع:** {fear_greed}\n"
            f"⏰ {timestamp_str}"
        )

        reply_markup = {
            "inline_keyboard": [
                [{"text": "📊 عرض تقرير الأداء", "callback_data": "get_report"}]
            ]
        }

        send_telegram_message(CHAT_ID, message, reply_markup=reply_markup, parse_mode='Markdown')

    except KeyError as ke:
        logger.error(f"❌ [Telegram Alert] Signal data incomplete for symbol {signal_data.get('symbol', 'N/A')}: Missing key {ke}", exc_info=True)
    except Exception as e:
        logger.error(f"❌ [Telegram Alert] Failed to send signal alert for symbol {signal_data.get('symbol', 'N/A')}: {e}", exc_info=True)

# ---------------------- Database Functions (Insert and Update) (Keep as is) ----------------------
# Keeping the existing database insert function.
# (insert_signal_into_db)
# Assuming this function is correctly implemented in the original script.
# Re-including them here for completeness in the new code block.

def insert_signal_into_db(signal: Dict[str, Any]) -> bool:
    """Inserts a new signal into the signals table."""
    if not check_db_connection() or not conn:
        logger.error(f"❌ [DB Insert] Failed to insert signal {signal.get('symbol', 'N/A')} due to DB connection issue.")
        return False

    symbol = signal.get('symbol', 'N/A')
    logger.debug(f"ℹ️ [DB Insert] Attempting to insert signal for {symbol}...")
    try:
        signal_prepared = convert_np_values(signal)
        signal_details_json = json.dumps(signal_prepared.get('signal_details', {}))

        with conn.cursor() as cur_ins:
            insert_query = sql.SQL("""
                INSERT INTO signals
                 (symbol, entry_price, initial_target, initial_stop_loss, current_target, current_stop_loss,
                 r2_score, strategy_name, signal_details, last_trailing_update_price, volume_15m)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """)
            cur_ins.execute(insert_query, (
                signal_prepared['symbol'],
                signal_prepared['entry_price'],
                signal_prepared['initial_target'],
                signal_prepared['initial_stop_loss'],
                signal_prepared['current_target'], # current_target starts as initial_target
                signal_prepared['current_stop_loss'], # current_stop_loss starts as initial_stop_loss
                signal_prepared.get('r2_score'), # Can be 0 or used for a different purpose in this strategy
                signal_prepared.get('strategy_name', 'unknown'),
                signal_details_json,
                None, # last_trailing_update_price starts as None
                signal_prepared.get('volume_15m')
            ))
        conn.commit()
        logger.info(f"✅ [DB Insert] Signal for {symbol} inserted into database.")
        return True
    except psycopg2.Error as db_err:
        logger.error(f"❌ [DB Insert] Database error inserting signal for {symbol}: {db_err}")
        if conn: conn.rollback()
        return False
    except (TypeError, ValueError) as convert_err:
         logger.error(f"❌ [DB Insert] Error converting signal data before insertion for {symbol}: {convert_err} - Signal Data: {signal}")
         if conn: conn.rollback()
         return False
    except Exception as e:
        logger.error(f"❌ [DB Insert] Unexpected error inserting signal for {symbol}: {e}", exc_info=True)
        if conn: conn.rollback()
        return False

# ---------------------- Trading Strategy (Momentum Growth Scalper) -------------------

class MomentumGrowthScalper: # New strategy class name
    """Encapsulates the trading strategy logic and associated indicators with strict mandatory conditions and dynamic target/trailing stop."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        # Required columns for indicator calculation
        self.required_cols_indicators = [
            'open', 'high', 'low', 'close', 'volume',
            f'ema_{EMA_SHORT_PERIOD}', f'ema_{EMA_LONG_PERIOD}', 'vwma',
            'rsi', 'atr', 'bb_upper', 'bb_lower', 'bb_middle',
            'macd', 'macd_signal', 'macd_hist',
            'adx', 'di_plus', 'di_minus',
            'vwap', 'obv', 'supertrend', 'supertrend_trend',
            'BullishCandleSignal', 'BearishCandleSignal',
            'stoch_k', 'stoch_d',
            'williams_r'
        ]
        # Required columns for buy signal generation
        self.required_cols_buy_signal = [
            'close', 'high', 'low', # Need high/low for candle patterns and ATR
            f'ema_{EMA_SHORT_PERIOD}', f'ema_{EMA_LONG_PERIOD}', 'vwma',
            'rsi', 'atr',
            'macd', 'macd_signal', 'macd_hist',
            'supertrend_trend', 'adx', 'di_plus', 'di_minus', 'vwap', 'bb_upper',
            'BullishCandleSignal', 'obv',
            'stoch_k', 'stoch_d',
            'williams_r'
        ]

        # =====================================================================
        # --- Mandatory Entry Conditions (All must be met) ---
        # Stricter conditions for high-probability entries
        # =====================================================================
        self.essential_conditions = [
            'ema_cross_bullish_recent',
            'supertrend_up',
            'macd_positive_and_increasing', # Combined MACD conditions
            'adx_strong_trending_bullish',
            'above_vwma',
            'stochastic_bullish_cross_recent_and_not_overbought', # Combined Stoch conditions
            'williams_r_not_overbought', # Williams %R not in overbought
            'bullish_candle_confirmation', # Bullish candle pattern
            'obv_increasing_recent', # OBV increasing
            'volume_check', # Sufficient volume
            'btc_trend_ok' # Bitcoin trend not bearish
        ]
        # =====================================================================


    def populate_indicators(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """Calculates all required indicators for the strategy."""
        logger.debug(f"ℹ️ [Strategy {self.symbol}] Calculating indicators...")
        min_len_required = max(
            EMA_SHORT_PERIOD, EMA_LONG_PERIOD, VWMA_PERIOD, RSI_PERIOD,
            ENTRY_ATR_PERIOD, BOLLINGER_WINDOW, MACD_SLOW, ADX_PERIOD*2,
            SUPERTREND_PERIOD, RECENT_EMA_CROSS_LOOKBACK, MACD_HIST_INCREASE_CANDLES,
            OBV_INCREASE_CANDLES, STOCH_K_PERIOD, STOCH_D_PERIOD, STOCH_SMOOTH_K,
            WILLIAMS_R_PERIOD, RECENT_STOCH_CROSS_LOOKBACK
        ) + 5

        if len(df) < min_len_required:
            logger.warning(f"⚠️ [Strategy {self.symbol}] DataFrame too short ({len(df)} < {min_len_required}) to calculate indicators.")
            return None

        try:
            df_calc = df.copy()
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
            stoch_df = calculate_stochastic(df_calc, STOCH_K_PERIOD, STOCH_D_PERIOD, STOCH_SMOOTH_K)
            df_calc = df_calc.join(stoch_df)
            williams_r_df = calculate_williams_r(df_calc, WILLIAMS_R_PERIOD)
            df_calc = df_calc.join(williams_r_df)

            required_cols_indicators_adjusted = [
                'open', 'high', 'low', 'close', 'volume',
                f'ema_{EMA_SHORT_PERIOD}', f'ema_{EMA_LONG_PERIOD}', 'vwma',
                'rsi', 'atr', 'bb_upper', 'bb_lower', 'bb_middle',
                'macd', 'macd_signal', 'macd_hist',
                'adx', 'di_plus', 'di_minus',
                'vwap', 'obv', 'supertrend', 'supertrend_trend',
                'BullishCandleSignal', 'BearishCandleSignal',
                'stoch_k', 'stoch_d', 'williams_r'
            ]
            missing_cols = [col for col in required_cols_indicators_adjusted if col not in df_calc.columns]
            if missing_cols:
                 logger.error(f"❌ [Strategy {self.symbol}] Required indicator columns missing after calculation: {missing_cols}")
                 logger.debug(f"Columns present: {df_calc.columns.tolist()}")
                 return None

            initial_len = len(df_calc)
            df_cleaned = df_calc.dropna(subset=required_cols_indicators_adjusted).copy()
            dropped_count = initial_len - len(df_cleaned)

            if dropped_count > 0:
                 logger.debug(f"ℹ️ [Strategy {self.symbol}] Dropped {dropped_count} rows due to NaN in indicators.")
            if df_cleaned.empty:
                logger.warning(f"⚠️ [Strategy {self.symbol}] DataFrame is empty after removing indicator NaNs.")
                return None

            latest = df_cleaned.iloc[-1]
            logger.debug(f"✅ [Strategy {self.symbol}] Indicators calculated. Latest Close: {latest.get('close', np.nan):.4f}, EMA{EMA_SHORT_PERIOD}: {latest.get(f'ema_{EMA_SHORT_PERIOD}', np.nan):.4f}, EMA{EMA_LONG_PERIOD}: {latest.get(f'ema_{EMA_LONG_PERIOD}', np.nan):.4f}, Stoch K: {latest.get('stoch_k', np.nan):.2f}, Stoch D: {latest.get('stoch_d', np.nan):.2f}, Williams %R: {latest.get('williams_r', np.nan):.2f}")
            return df_cleaned

        except KeyError as ke:
             logger.error(f"❌ [Strategy {self.symbol}] Error: Required column not found during indicator calculation: {ke}", exc_info=True)
             return None
        except Exception as e:
            logger.error(f"❌ [Strategy {self.symbol}] Unexpected error during indicator calculation: {e}", exc_info=True)
            return None


    def generate_buy_signal(self, df_processed: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generates a buy signal based on strict mandatory conditions for Momentum Growth Scalper.
        Calculates initial target (min 1.5%) and stop loss (ATR based, max 2% loss).
        """
        logger.debug(f"ℹ️ [Strategy {self.symbol}] Generating buy signal...")

        min_signal_data_len = max(
            RECENT_EMA_CROSS_LOOKBACK, MACD_HIST_INCREASE_CANDLES,
            OBV_INCREASE_CANDLES, RECENT_STOCH_CROSS_LOOKBACK
        ) + 1
        if df_processed is None or df_processed.empty or len(df_processed) < min_signal_data_len:
            logger.warning(f"⚠️ [Strategy {self.symbol}] DataFrame is empty or too short (<{min_signal_data_len}), cannot generate signal.")
            return None

        required_cols_buy_signal_adjusted = [
            'close', 'high', 'low',
            f'ema_{EMA_SHORT_PERIOD}', f'ema_{EMA_LONG_PERIOD}', 'vwma',
            'rsi', 'atr',
            'macd', 'macd_signal', 'macd_hist',
            'supertrend_trend', 'adx', 'di_plus', 'di_minus', 'vwap', 'bb_upper',
            'BullishCandleSignal', 'obv',
            'stoch_k', 'stoch_d', 'williams_r'
        ]
        missing_cols = [col for col in required_cols_buy_signal_adjusted if col not in df_processed.columns]
        if missing_cols:
            logger.warning(f"⚠️ [Strategy {self.symbol}] DataFrame missing required columns for signal: {missing_cols}.")
            return None

        last_row = df_processed.iloc[-1]
        recent_df = df_processed.iloc[-min_signal_data_len:]

        if recent_df[required_cols_buy_signal_adjusted].isnull().values.any():
             logger.warning(f"⚠️ [Strategy {self.symbol}] Recent data contains NaN in required signal columns. Cannot generate signal.")
             return None

        # --- Check Mandatory Conditions ---
        essential_passed = True
        failed_essential_conditions = []
        signal_details = {}

        # Condition 1: Bitcoin trend is not bearish
        btc_trend = get_btc_trend_4h()
        if "هبوط" in btc_trend:
            essential_passed = False
            failed_essential_conditions.append('Bitcoin Trend is Bearish')
            signal_details['BTC_Trend'] = f'Failed: Bearish ({btc_trend})'
        else:
             signal_details['BTC_Trend'] = f'Passed: Not Bearish ({btc_trend})'


        # Condition 2: Positive EMA Cross condition (Must be recent)
        ema_cross_bullish_recent = False
        if len(recent_df) >= RECENT_EMA_CROSS_LOOKBACK + 1:
             ema_short_slice = recent_df[f'ema_{EMA_SHORT_PERIOD}'].iloc[-RECENT_EMA_CROSS_LOOKBACK-1:]
             ema_long_slice = recent_df[f'ema_{EMA_LONG_PERIOD}'].iloc[-RECENT_EMA_CROSS_LOOKBACK-1:]
             if not ema_short_slice.isnull().any() and not ema_long_slice.isnull().any():
                for i in range(1, RECENT_EMA_CROSS_LOOKBACK + 1):
                     if ema_short_slice.iloc[-i] > ema_long_slice.iloc[-i] and ema_short_slice.iloc[-i-1] <= ema_long_slice.iloc[-i-1]:
                          ema_cross_bullish_recent = True
                          break
        if not ema_cross_bullish_recent:
            essential_passed = False
            failed_essential_conditions.append(f'Recent EMA Cross (Bullish) in last {RECENT_EMA_CROSS_LOOKBACK} candles')
            signal_details['EMA_Cross'] = f'Failed: No recent bullish cross in last {RECENT_EMA_CROSS_LOOKBACK} candles'
        else:
             signal_details['EMA_Cross'] = f'Passed: Recent bullish cross detected'

        # Condition 3: SuperTrend condition: Price closes above SuperTrend and SuperTrend trend is up
        if not (pd.notna(last_row['supertrend']) and last_row['close'] > last_row['supertrend'] and last_row['supertrend_trend'] == 1):
             essential_passed = False
             failed_essential_conditions.append('SuperTrend (Up Trend & Price Above)')
             detail_st = f'ST:{last_row.get("supertrend", np.nan):.4f}, Trend:{last_row.get("supertrend_trend", 0)}'
             signal_details['SuperTrend'] = f'Failed: Not Up Trend or Price Not Above ({detail_st})'
        else:
            signal_details['SuperTrend'] = f'Passed: Up Trend & Price Above'

        # Condition 4: MACD condition (Positive histogram AND increasing)
        macd_hist_increasing = False
        if len(recent_df) >= MACD_HIST_INCREASE_CANDLES + 1:
             macd_hist_slice = recent_df['macd_hist'].iloc[-MACD_HIST_INCREASE_CANDLES-1:]
             if not macd_hist_slice.isnull().any() and np.all(np.diff(macd_hist_slice) > 0):
                  macd_hist_increasing = True
        if not (pd.notna(last_row['macd_hist']) and last_row['macd_hist'] > 0 and macd_hist_increasing):
             essential_passed = False
             failed_essential_conditions.append(f'MACD (Hist Positive AND Increasing over last {MACD_HIST_INCREASE_CANDLES} candles)')
             detail_macd = f'Hist: {last_row.get("macd_hist", np.nan):.4f}, Increasing: {macd_hist_increasing}'
             signal_details['MACD'] = f'Failed: Not Positive Hist OR Not Increasing ({detail_macd})'
        else:
             signal_details['MACD'] = f'Passed: Hist Positive and Increasing'


        # Condition 5: Stronger ADX and DI+ above DI- condition (ADX threshold increased)
        if not (pd.notna(last_row['adx']) and pd.notna(last_row['di_plus']) and pd.notna(last_row['di_minus']) and last_row['adx'] > MIN_ADX_TREND_STRENGTH and last_row['di_plus'] > last_row['di_minus']):
             essential_passed = False
             failed_essential_conditions.append(f'ADX/DI (Strong Trending Bullish, ADX > {MIN_ADX_TREND_STRENGTH})')
             detail_adx = f'ADX:{last_row.get("adx", np.nan):.1f}, DI+:{last_row.get("di_plus", np.nan):.1f}, DI-:{last_row.get("di_minus", np.nan):.1f}'
             signal_details['ADX/DI'] = f'Failed: Not Strong Trending Bullish (ADX <= {MIN_ADX_TREND_STRENGTH} or DI+ <= DI-) ({detail_adx})'
        else:
             signal_details['ADX/DI'] = f'Passed: Strong Trending Bullish (ADX:{last_row["adx"]:.1f}, DI+>DI-)'

        # Condition 6: VWMA condition: Price closes above the VWMA
        if not (pd.notna(last_row['vwma']) and last_row['close'] > last_row['vwma']):
             essential_passed = False
             failed_essential_conditions.append('Above VWMA')
             detail_vwma = f'Close:{last_row.get("close", np.nan):.4f}, VWMA:{last_row.get("vwma", np.nan):.4f}'
             signal_details['VWMA_Mandatory'] = f'Failed: Not Closed Above VWMA ({detail_vwma})'
        else:
             signal_details['VWMA_Mandatory'] = f'Passed: Closed Above VWMA'

        # Condition 7: Recent Stochastic Bullish Cross AND not overbought
        stoch_bullish_cross_recent = False
        if len(recent_df) >= RECENT_STOCH_CROSS_LOOKBACK + 1:
             stoch_k_slice = recent_df['stoch_k'].iloc[-RECENT_STOCH_CROSS_LOOKBACK-1:]
             stoch_d_slice = recent_df['stoch_d'].iloc[-RECENT_STOCH_CROSS_LOOKBACK-1:]
             if not stoch_k_slice.isnull().any() and not stoch_d_slice.isnull().any():
                for i in range(1, RECENT_STOCH_CROSS_LOOKBACK + 1):
                     if stoch_k_slice.iloc[-i] > stoch_d_slice.iloc[-i] and stoch_k_slice.iloc[-i-1] <= stoch_d_slice.iloc[-i-1]:
                          stoch_bullish_cross_recent = True
                          break
        if not (stoch_bullish_cross_recent and pd.notna(last_row['stoch_k']) and last_row['stoch_k'] < STOCH_OVERBOUGHT):
             essential_passed = False
             failed_essential_conditions.append(f'Recent Stochastic Bullish Cross in last {RECENT_STOCH_CROSS_LOOKBACK} candles AND not overbought')
             detail_stoch = f'Cross Recent: {stoch_bullish_cross_recent}, Stoch K: {last_row.get("stoch_k", np.nan):.2f}'
             signal_details['Stoch_Cross_Not_Overbought'] = f'Failed: No recent cross OR is overbought ({detail_stoch})'
        else:
             signal_details['Stoch_Cross_Not_Overbought'] = f'Passed: Recent bullish cross and not overbought'

        # Condition 8: Williams %R not in overbought zone
        if not (pd.notna(last_row['williams_r']) and last_row['williams_r'] > WILLIAMS_R_OVERBOUGHT): # Note: Williams %R is negative, so > -20 means not overbought
             essential_passed = False
             failed_essential_conditions.append(f'Williams %R not overbought (> {WILLIAMS_R_OVERBOUGHT})')
             detail_wr = f'Williams %R: {last_row.get("williams_r", np.nan):.2f}'
             signal_details['Williams_R_Not_Overbought'] = f'Failed: Is overbought ({detail_wr})'
        else:
             signal_details['Williams_R_Not_Overbought'] = f'Passed: Not overbought'

        # Condition 9: Bullish candlestick pattern confirmation (Hammer or Engulfing)
        if not (last_row.get('BullishCandleSignal', 0) == 1):
             essential_passed = False
             failed_essential_conditions.append('Bullish Candlestick Pattern Confirmation')
             signal_details['Bullish_Candle_Confirmed'] = f'Failed: No bullish pattern detected'
        else:
             signal_details['Bullish_Candle_Confirmed'] = f'Passed: Bullish pattern detected'

        # Condition 10: OBV is increasing over the last X candles
        obv_increasing_recent = False
        if len(recent_df) >= OBV_INCREASE_CANDLES + 1:
             obv_slice = recent_df['obv'].iloc[-OBV_INCREASE_CANDLES-1:]
             if not obv_slice.isnull().any() and np.all(np.diff(obv_slice) > 0):
                  obv_increasing_recent = True
        if not obv_increasing_recent:
             essential_passed = False
             failed_essential_conditions.append(f'OBV increasing over last {OBV_INCREASE_CANDLES} candles')
             signal_details['OBV_Increasing_Recent'] = f'Failed: OBV not increasing over last {OBV_INCREASE_CANDLES} candles'
        else:
             signal_details['OBV_Increasing_Recent'] = f'Passed: OBV increasing'

        # Condition 11: Check trading volume (liquidity)
        volume_recent = fetch_recent_volume(self.symbol)
        if volume_recent < MIN_VOLUME_15M_USDT:
            essential_passed = False
            failed_essential_conditions.append(f'Insufficient Liquidity (< {MIN_VOLUME_15M_USDT:,.0f} USDT)')
            signal_details['Volume_Check'] = f'Failed: Liquidity ({volume_recent:,.0f}) < Threshold ({MIN_VOLUME_15M_USDT:,.0f})'
        else:
             signal_details['Volume_Check'] = f'Passed: Liquidity ({volume_recent:,.0f}) >= Threshold ({MIN_VOLUME_15M_USDT:,.0f})'


        # If any mandatory condition failed, reject the signal immediately
        if not essential_passed:
            logger.debug(f"ℹ️ [Strategy {self.symbol}] Mandatory conditions failed: {', '.join(failed_essential_conditions)}. Signal rejected.")
            return None

        # --- Calculate Initial Target and Stop Loss ---
        current_price = last_row['close']
        current_atr = last_row.get('atr')

        if pd.isna(current_atr) or current_atr <= 0:
             logger.warning(f"⚠️ [Strategy {self.symbol}] Invalid ATR value ({current_atr}) for calculating target and stop loss. Signal rejected.")
             return None

        # Initial Target: Minimum 1.5% profit
        initial_target_price = current_price * (1 + MIN_PROFIT_FOR_GROWTH_PCT)

        # Initial Stop Loss: ATR based, but not more than MAX_INITIAL_LOSS_PCT below entry
        atr_stop_loss_price = current_price - (ENTRY_ATR_MULTIPLIER * current_atr)
        max_loss_stop_loss_price = current_price * (1 - MAX_INITIAL_LOSS_PCT)

        # Take the higher of the ATR stop loss and the maximum allowed loss stop loss (closer to entry)
        initial_stop_loss_price = max(atr_stop_loss_price, max_loss_stop_loss_price)

        # Ensure stop loss is not zero or negative and is below the entry price
        if initial_stop_loss_price <= 0 or initial_stop_loss_price >= current_price:
            # Fallback to a very small percentage if calculation is invalid
            fallback_sl_price = current_price * 0.99 # 1% below entry as a strict minimum fallback
            initial_stop_loss_price = max(fallback_sl_price, current_price * 0.0005) # Ensure not too close to zero
            logger.warning(f"⚠️ [Strategy {self.symbol}] Calculated initial stop loss ({initial_stop_loss_price:.8g}) is invalid or above entry price. Adjusted to {initial_stop_loss_price:.8f}")
            signal_details['Warning_Initial_SL'] = f'Initial SL adjusted (was invalid, set to {initial_stop_loss_price:.8f})'


        # Compile final signal data
        signal_output = {
            'symbol': self.symbol,
            'entry_price': float(f"{current_price:.8g}"),
            'initial_target': float(f"{initial_target_price:.8g}"),
            'initial_stop_loss': float(f"{initial_stop_loss_price:.8g}"),
            'current_target': float(f"{initial_target_price:.8g}"), # Current target starts as initial target
            'current_stop_loss': float(f"{initial_stop_loss_price:.8g}"), # Current stop loss starts as initial stop loss
            'r2_score': 0.0, # Not using score in this strategy, set to 0
            'strategy_name': 'MomentumGrowthScalper', # New strategy name
            'signal_details': signal_details,
            'volume_15m': volume_recent,
            'trade_value': TRADE_VALUE,
            'total_possible_score': 0.0 # Not using score
        }

        logger.info(f"✅ [Strategy {self.symbol}] Confirmed buy signal. Price: {current_price:.6f}, Initial Target: {initial_target_price:.6f}, Initial SL: {initial_stop_loss_price:.6f}, ATR: {current_atr:.6f}, Volume: {volume_recent:,.0f}")
        return signal_output


# ---------------------- Open Signal Tracking Function (Adjusted) ----------------------
def track_signals() -> None:
    """Tracks open signals, checks targets and stop losses, and applies trailing stop."""
    logger.info("ℹ️ [Tracker] Starting open signal tracking process...")
    while True:
        active_signals_summary: List[str] = []
        processed_in_cycle = 0
        try:
            if not check_db_connection() or not conn:
                logger.warning("⚠️ [Tracker] Skipping tracking cycle due to DB connection issue.")
                time.sleep(15)
                continue

            with conn.cursor() as track_cur:
                 track_cur.execute("""
                    SELECT id, symbol, entry_price, initial_target, current_target, current_stop_loss,
                           is_trailing_active, last_trailing_update_price
                    FROM signals
                    WHERE achieved_target = FALSE AND hit_stop_loss = FALSE;
                """)
                 open_signals: List[Dict] = track_cur.fetchall()

            if not open_signals:
                time.sleep(10)
                continue

            logger.debug(f"ℹ️ [Tracker] Tracking {len(open_signals)} open signals...")

            for signal_row in open_signals:
                signal_id = signal_row['id']
                symbol = signal_row['symbol']
                processed_in_cycle += 1
                update_executed = False

                try:
                    entry_price = float(signal_row['entry_price'])
                    initial_target = float(signal_row['initial_target'])
                    current_target = float(signal_row['current_target']) # This will be the initial target until trade closes
                    current_stop_loss = float(signal_row['current_stop_loss'])
                    is_trailing_active = signal_row['is_trailing_active']
                    last_update_px = signal_row['last_trailing_update_price']
                    last_trailing_update_price = float(last_update_px) if last_update_px is not None else None

                    current_price = ticker_data.get(symbol)

                    if current_price is None:
                         logger.warning(f"⚠️ [Tracker] {symbol}(ID:{signal_id}): Current price not available in Ticker data.")
                         continue

                    active_signals_summary.append(f"{symbol}({signal_id}): P={current_price:.4f} T={current_target:.4f} SL={current_stop_loss:.4f} Trail={'On' if is_trailing_active else 'Off'}")

                    update_query: Optional[sql.SQL] = None
                    update_params: Tuple = ()
                    log_message: Optional[str] = None
                    notification_details: Dict[str, Any] = {'symbol': symbol, 'id': signal_id, 'current_price': current_price}

                    # --- Check for Exit Conditions ---

                    # 1. Check for Stop Loss Hit (Highest priority exit)
                    if current_price <= current_stop_loss:
                        loss_pct = ((current_stop_loss / entry_price) - 1) * 100 if entry_price > 0 else 0
                        profitable_sl = current_stop_loss > entry_price
                        sl_type_msg = "at a profit ✅" if profitable_sl else "at a loss ❌"
                        update_query = sql.SQL("UPDATE signals SET hit_stop_loss = TRUE, closing_price = %s, closed_at = NOW(), profit_percentage = %s, profitable_stop_loss = %s WHERE id = %s;")
                        update_params = (current_stop_loss, loss_pct, profitable_sl, signal_id)
                        log_message = f"🔻 [Tracker] {symbol}(ID:{signal_id}): Stop Loss hit ({sl_type_msg}) at {current_stop_loss:.8g} (Percentage: {loss_pct:.2f}%)."
                        notification_details.update({'type': 'stop_loss_hit', 'closing_price': current_stop_loss, 'profit_pct': loss_pct, 'profitable_sl': profitable_sl})
                        update_executed = True

                    # 2. Check for Initial Target Hit (Trigger for Trailing Stop)
                    # Only check if SL was not hit and trailing is not already active
                    elif not is_trailing_active and current_price >= initial_target:
                        logger.info(f"ℹ️ [Tracker] {symbol}(ID:{signal_id}): Price {current_price:.8g} reached initial target {initial_target:.8g}. Activating trailing stop...")
                        # Fetch recent data for current ATR calculation
                        df_atr = fetch_historical_data(symbol, interval=SIGNAL_TRACKING_TIMEFRAME, days=SIGNAL_TRACKING_LOOKBACK_DAYS)
                        if df_atr is not None and not df_atr.empty:
                            df_atr = calculate_atr_indicator(df_atr, period=ENTRY_ATR_PERIOD)
                            if not df_atr.empty and 'atr' in df_atr.columns and pd.notna(df_atr['atr'].iloc[-1]):
                                current_atr_val = df_atr['atr'].iloc[-1]
                                if current_atr_val > 0:
                                     # Calculate the *initial* trailing stop based on the *current* price
                                     new_stop_loss_calc = current_price - (TRAILING_STOP_ATR_MULTIPLIER * current_atr_val)
                                     # Ensure the new trailing stop is at least the entry price
                                     new_stop_loss = max(new_stop_loss_calc, entry_price)

                                     # Update DB to activate trailing stop and set the first trailing stop loss
                                     update_query = sql.SQL("UPDATE signals SET is_trailing_active = TRUE, current_stop_loss = %s, last_trailing_update_price = %s WHERE id = %s;")
                                     update_params = (new_stop_loss, current_price, signal_id) # Store current_price as the price when trailing was last updated
                                     log_message = f"⬆️✅ [Tracker] {symbol}(ID:{signal_id}): Trailing stop activated. Price={current_price:.8g}, ATR={current_atr_val:.8g}. New Stop: {new_stop_loss:.8g}"
                                     # Calculate profit percentage AT THE INITIAL TARGET HIT for notification
                                     profit_pct_at_target = ((initial_target / entry_price) - 1) * 100 if entry_price > 0 else 0
                                     notification_details.update({
                                         'type': 'target_hit', # Use 'target_hit' type for notification
                                         'closing_price': initial_target, # Use initial target price for notification display
                                         'profit_pct': profit_pct_at_target,
                                         'current_price': current_price, # Include current price at activation
                                         'atr_value': current_atr_val,
                                         'new_stop_loss': new_stop_loss
                                     })
                                     update_executed = True
                                else: logger.warning(f"⚠️ [Tracker] {symbol}(ID:{signal_id}): Invalid ATR value ({current_atr_val}) for trailing stop activation.")
                            else: logger.warning(f"⚠️ [Tracker] {symbol}(ID:{signal_id}): Cannot calculate ATR for trailing stop activation.")
                        else: logger.warning(f"⚠️ [Tracker] {symbol}(ID:{signal_id}): Cannot fetch data to calculate ATR for trailing stop activation.")


                    # 3. Update Trailing Stop (If trailing is already active)
                    # Only check if SL was not hit and trailing IS active
                    elif is_trailing_active and last_trailing_update_price is not None:
                        update_threshold_price = last_trailing_update_price * (1 + TRAILING_STOP_MOVE_INCREMENT_PCT)
                        if current_price >= update_threshold_price:
                            logger.info(f"ℹ️ [Tracker] {symbol}(ID:{signal_id}): Price {current_price:.8g} reached trailing update threshold ({update_threshold_price:.8g}). Fetching ATR...")
                            df_recent = fetch_historical_data(symbol, interval=SIGNAL_TRACKING_TIMEFRAME, days=SIGNAL_TRACKING_LOOKBACK_DAYS)
                            if df_recent is not None and not df_recent.empty:
                                df_recent = calculate_atr_indicator(df_recent, period=ENTRY_ATR_PERIOD)
                                if not df_recent.empty and 'atr' in df_recent.columns and pd.notna(df_recent['atr'].iloc[-1]):
                                     current_atr_val_update = df_recent['atr'].iloc[-1]
                                     if current_atr_val_update > 0:
                                         # Calculate potential new stop loss based on current price and ATR
                                         potential_new_stop_loss = current_price - (TRAILING_STOP_ATR_MULTIPLIER * current_atr_val_update)
                                         # Ensure the new stop loss is higher than the current stop loss
                                         if potential_new_stop_loss > current_stop_loss:
                                            new_stop_loss_update = potential_new_stop_loss
                                            update_query = sql.SQL("UPDATE signals SET current_stop_loss = %s, last_trailing_update_price = %s WHERE id = %s;")
                                            update_params = (new_stop_loss_update, current_price, signal_id) # Update last trailing update price
                                            log_message = f"➡️🔼 [Tracker] {symbol}(ID:{signal_id}): Trailing stop updated. Price={current_price:.8g}, ATR={current_atr_val_update:.8g}. Old={current_stop_loss:.8g}, New: {new_stop_loss_update:.8g}"
                                            notification_details.update({'type': 'trailing_updated', 'current_price': current_price, 'atr_value': current_atr_val_update, 'old_stop_loss': current_stop_loss, 'new_stop_loss': new_stop_loss_update, 'trigger_price_increase_pct': TRAILING_STOP_MOVE_INCREMENT_PCT * 100})
                                            update_executed = True
                                         else:
                                             logger.debug(f"ℹ️ [Tracker] {symbol}(ID:{signal_id}): Calculated trailing stop ({potential_new_stop_loss:.8g}) is not higher than current ({current_stop_loss:.8g}). Not updating.")
                                     else: logger.warning(f"⚠️ [Tracker] {symbol}(ID:{signal_id}): Invalid ATR value ({current_atr_val_update}) for update.")
                                else: logger.warning(f"⚠️ [Tracker] {symbol}(ID:{signal_id}): Cannot calculate ATR for update.")
                            else: logger.warning(f"⚠️ [Tracker] {symbol}(ID:{signal_id}): Cannot fetch data to calculate ATR for update.")
                        else:
                             logger.debug(f"ℹ️ [Tracker] {symbol}(ID:{signal_id}): Price {current_price:.8g} not yet reached trailing update threshold ({update_threshold_price:.8g}).")


                    # --- Execute Database Update and Send Notification ---
                    if update_executed and update_query:
                        try:
                             with conn.cursor() as update_cur:
                                  update_cur.execute(update_query, update_params)
                             conn.commit()
                             if log_message: logger.info(log_message)
                             if notification_details.get('type'):
                                send_tracking_notification(notification_details)
                        except psycopg2.Error as db_err:
                            logger.error(f"❌ [Tracker] {symbol}(ID:{signal_id}): DB error during update: {db_err}")
                            if conn: conn.rollback()
                        except Exception as exec_err:
                            logger.error(f"❌ [Tracker] {symbol}(ID:{signal_id}): Unexpected error during update execution/notification: {exec_err}", exc_info=True)
                            if conn: conn.rollback()

                except (TypeError, ValueError) as convert_err:
                    logger.error(f"❌ [Tracker] {symbol}(ID:{signal_id}): Error converting initial signal values: {convert_err}")
                    continue
                except Exception as inner_loop_err:
                     logger.error(f"❌ [Tracker] {symbol}(ID:{signal_id}): Unexpected error processing signal: {inner_loop_err}", exc_info=True)
                     continue

            if active_signals_summary:
                logger.debug(f"ℹ️ [Tracker] End of cycle status ({processed_in_cycle} processed): {'; '.join(active_signals_summary)}")

            time.sleep(3)

        except psycopg2.Error as db_cycle_err:
             logger.error(f"❌ [Tracker] Database error in main tracking cycle: {db_cycle_err}. Attempting to reconnect...")
             if conn: conn.rollback()
             time.sleep(30)
             check_db_connection()
        except Exception as cycle_err:
            logger.error(f"❌ [Tracker] Unexpected error in signal tracking cycle: {cycle_err}", exc_info=True)
            time.sleep(30)


# ---------------------- Flask Service (Optional for Webhook) (Keep as is) ----------------------
# Keeping the existing Flask functions.
# (app, home, favicon, webhook, handle_status_command, run_flask)
# Assuming these functions are correctly implemented in the original script.
# Re-including them here for completeness in the new code block.

app = Flask(__name__)

@app.route('/')
def home() -> Response:
    """Simple home page to show the bot is running."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # Check if threads are initialized before accessing is_alive()
    ws_alive = 'ws_thread' in globals() and ws_thread is not None and ws_thread.is_alive()
    tracker_alive = 'tracker_thread' in globals() and tracker_thread is not None and tracker_thread.is_alive()
    flask_alive = 'flask_thread' in globals() and flask_thread is not None and flask_thread.is_alive() if WEBHOOK_URL else True # Assume Flask is 'alive' if not configured
    status = "running" if ws_alive and tracker_alive and flask_alive else "partially running"
    return Response(f"📈 Crypto Signal Bot ({status}) - Last Check: {now}", status=200, mimetype='text/plain')

@app.route('/favicon.ico')
def favicon() -> Response:
    """Handles favicon request to avoid 404 errors in logs."""
    return Response(status=204)

@app.route('/webhook', methods=['POST'])
def webhook() -> Tuple[str, int]:
    """Handles incoming requests from Telegram (like button presses and commands)."""
    if not request.is_json:
        logger.warning("⚠️ [Flask] Received non-JSON webhook request.")
        return "Invalid request format", 400

    try:
        data = request.get_json()
        logger.debug(f"ℹ️ [Flask] Received webhook data: {json.dumps(data)[:200]}...")

        if 'callback_query' in data:
            callback_query = data['callback_query']
            callback_id = callback_query['id']
            callback_data = callback_query.get('data')
            message_info = callback_query.get('message')
            if not message_info or not callback_data:
                 logger.warning(f"⚠️ [Flask] Callback query (ID: {callback_id}) missing message or data.")
                 try:
                     ack_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
                     requests.post(ack_url, json={'callback_query_id': callback_id}, timeout=5)
                 except Exception as ack_err:
                     logger.warning(f"⚠️ [Flask] Failed to acknowledge invalid callback query {callback_id}: {ack_err}")
                 return "OK", 200

            chat_id_callback = message_info.get('chat', {}).get('id')
            if not chat_id_callback:
                 logger.warning(f"⚠️ [Flask] Callback query (ID: {callback_id}) missing chat ID.")
                 try:
                     ack_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
                     requests.post(ack_url, json={'callback_query_id': callback_id}, timeout=5)
                 except Exception as ack_err:
                     logger.warning(f"⚠️ [Flask] Failed to acknowledge invalid callback query {callback_id}: {ack_err}")
                 return "OK", 200

            message_id = message_info['message_id']
            user_info = callback_query.get('from', {})
            user_id = user_info.get('id')
            username = user_info.get('username', 'N/A')

            logger.info(f"ℹ️ [Flask] Received callback query: Data='{callback_data}', User={username}({user_id}), Chat={chat_id_callback}")

            try:
                ack_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
                requests.post(ack_url, json={'callback_query_id': callback_id}, timeout=5)
            except Exception as ack_err:
                 logger.warning(f"⚠️ [Flask] Failed to acknowledge callback query {callback_id}: {ack_err}")

            if callback_data == "get_report":
                report_thread = Thread(target=lambda: send_telegram_message(chat_id_callback, generate_performance_report(), parse_mode='Markdown'))
                report_thread.start()
            else:
                logger.warning(f"⚠️ [Flask] Received unhandled callback data: '{callback_data}'")

        elif 'message' in data:
            message_data = data['message']
            chat_info = message_data.get('chat')
            user_info = message_data.get('from', {})
            text_msg = message_data.get('text', '').strip()

            if not chat_info or not text_msg:
                 logger.debug("ℹ️ [Flask] Received message without chat info or text.")
                 return "OK", 200

            chat_id_msg = chat_info['id']
            user_id = user_info.get('id')
            username = user_info.get('username', 'N/A')

            logger.info(f"ℹ️ [Flask] Received message: Text='{text_msg}', User={username}({user_id}), Chat={chat_id_msg}")

            if text_msg.lower() == '/report':
                 report_thread = Thread(target=lambda: send_telegram_message(chat_id_msg, generate_performance_report(), parse_mode='Markdown'))
                 report_thread.start()
            elif text_msg.lower() == '/status':
                 status_thread = Thread(target=handle_status_command, args=(chat_id_msg,))
                 status_thread.start()

        else:
            logger.debug("ℹ️ [Flask] Received webhook data without 'callback_query' or 'message'.")

        return "OK", 200
    except Exception as e:
         logger.error(f"❌ [Flask] Error processing webhook: {e}", exc_info=True)
         return "Internal Server Error", 500

def handle_status_command(chat_id_msg: int) -> None:
    """Separate function to handle /status command to avoid blocking the Webhook."""
    logger.info(f"ℹ️ [Flask Status] Handling /status command for chat {chat_id_msg}")
    status_msg = "⏳ جلب الحالة..."
    msg_sent = send_telegram_message(chat_id_msg, status_msg)
    if not (msg_sent and msg_sent.get('ok')):
         logger.error(f"❌ [Flask Status] Failed to send initial status message to {chat_id_msg}")
         return

    message_id_to_edit = msg_sent['result']['message_id']
    try:
        open_count = 0
        if check_db_connection() and conn:
            with conn.cursor() as status_cur:
                status_cur.execute("SELECT COUNT(*) AS count FROM signals WHERE achieved_target = FALSE AND hit_stop_loss = FALSE;")
                open_count = (status_cur.fetchone() or {}).get('count', 0)

        ws_status = 'نشط ✅' if 'ws_thread' in globals() and ws_thread is not None and ws_thread.is_alive() else 'غير نشط ❌'
        tracker_status = 'نشط ✅' if 'tracker_thread' in globals() and tracker_thread is not None and tracker_thread.is_alive() else 'غير نشط ❌'
        final_status_msg = (
            f"🤖 *حالة البوت:*\n"
            f"- تتبع الأسعار (WS): {ws_status}\n"
            f"- تتبع الإشارات: {tracker_status}\n"
            f"- الإشارات النشطة: *{open_count}* / {MAX_OPEN_TRADES}\n"
            f"- وقت الخادم الحالي: {datetime.now().strftime('%H:%M:%S')}"
        )
        edit_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
        edit_payload = {
            'chat_id': chat_id_msg,
             'message_id': message_id_to_edit,
            'text': final_status_msg,
            'parse_mode': 'Markdown'
        }
        response = requests.post(edit_url, json=edit_payload, timeout=10)
        response.raise_for_status()
        logger.info(f"✅ [Flask Status] Status updated for chat {chat_id_msg}")

    except Exception as status_err:
        logger.error(f"❌ [Flask Status] Error getting/editing status details for chat {chat_id_msg}: {status_err}", exc_info=True)
        send_telegram_message(chat_id_msg, "❌ حدث خطأ أثناء جلب تفاصيل الحالة.")


def run_flask() -> None:
    """Runs the Flask application to listen for the Webhook using a production server if available."""
    if not WEBHOOK_URL:
        logger.info("ℹ️ [Flask] Webhook URL not configured. Flask server will not start.")
        return

    host = "0.0.0.0"
    port = int(config('PORT', default=10000))
    logger.info(f"ℹ️ [Flask] Starting Flask app on {host}:{port}...")
    try:
        from waitress import serve
        logger.info("✅ [Flask] Using 'waitress' server.")
        serve(app, host=host, port=port, threads=6)
    except ImportError:
         logger.warning("⚠️ [Flask] 'waitress' not installed. Falling back to Flask development server (NOT recommended for production).")
         try:
             app.run(host=host, port=port)
         except Exception as flask_run_err:
              logger.critical(f"❌ [Flask] Failed to start development server: {flask_run_err}", exc_info=True)
    except Exception as serve_err:
         logger.critical(f"❌ [Flask] Failed to start server (waitress?): {serve_err}", exc_info=True)


# ---------------------- Main Loop and Check Function (Adjusted) ----------------------
def main_loop() -> None:
    """Main loop to scan pairs and generate signals using the Momentum Growth Scalper strategy."""
    symbols_to_scan = get_crypto_symbols()
    if not symbols_to_scan:
        logger.critical("❌ [Main] No valid symbols loaded or validated. Cannot proceed.")
        return

    logger.info(f"✅ [Main] Loaded {len(symbols_to_scan)} valid symbols for scanning.")
    last_full_scan_time = time.time()

    while True:
        try:
            scan_start_time = time.time()
            logger.info("+" + "-"*60 + "+")
            logger.info(f"🔄 [Main] Starting Market Scan Cycle - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info("+" + "-"*60 + "+")

            if not check_db_connection() or not conn:
                logger.error("❌ [Main] Skipping scan cycle due to database connection failure.")
                time.sleep(60)
                continue

            open_count = 0
            try:
                 with conn.cursor() as cur_check:
                    cur_check.execute("SELECT COUNT(*) AS count FROM signals WHERE achieved_target = FALSE AND hit_stop_loss = FALSE;")
                    open_count = (cur_check.fetchone() or {}).get('count', 0)
            except psycopg2.Error as db_err:
                 logger.error(f"❌ [Main] DB error checking open signal count: {db_err}. Skipping cycle.")
                 if conn: conn.rollback()
                 time.sleep(60)
                 continue

            logger.info(f"ℹ️ [Main] Currently Open Signals: {open_count} / {MAX_OPEN_TRADES}")
            if open_count >= MAX_OPEN_TRADES:
                logger.info(f"⚠️ [Main] Maximum number of open signals reached. Waiting...")
                time.sleep(60)
                continue

            processed_in_loop = 0
            signals_generated_in_loop = 0
            slots_available = MAX_OPEN_TRADES - open_count

            for symbol in symbols_to_scan:
                 if slots_available <= 0:
                      logger.info(f"ℹ️ [Main] Maximum limit ({MAX_OPEN_TRADES}) reached during scan. Stopping symbol scan for this cycle.")
                      break

                 processed_in_loop += 1
                 logger.debug(f"🔍 [Main] Scanning {symbol} ({processed_in_loop}/{len(symbols_to_scan)})...")

                 try:
                    with conn.cursor() as symbol_cur:
                        symbol_cur.execute("SELECT 1 FROM signals WHERE symbol = %s AND achieved_target = FALSE AND hit_stop_loss = FALSE LIMIT 1;", (symbol,))
                        if symbol_cur.fetchone():
                            continue

                    # Use the new strategy class
                    strategy = MomentumGrowthScalper(symbol)
                    df_hist = fetch_historical_data(symbol, interval=SIGNAL_GENERATION_TIMEFRAME, days=SIGNAL_GENERATION_LOOKBACK_DAYS)
                    if df_hist is None or df_hist.empty:
                        continue

                    df_indicators = strategy.populate_indicators(df_hist)
                    if df_indicators is None:
                        continue

                    potential_signal = strategy.generate_buy_signal(df_indicators)

                    if potential_signal:
                        logger.info(f"✨ [Main] Potential signal found for {symbol}! Final check and insertion...")
                        with conn.cursor() as final_check_cur:
                             final_check_cur.execute("SELECT COUNT(*) AS count FROM signals WHERE achieved_target = FALSE AND hit_stop_loss = FALSE;")
                             final_open_count = (final_check_cur.fetchone() or {}).get('count', 0)

                             if final_open_count < MAX_OPEN_TRADES:
                                 if insert_signal_into_db(potential_signal):
                                     send_telegram_alert(potential_signal, SIGNAL_GENERATION_TIMEFRAME)
                                     signals_generated_in_loop += 1
                                     slots_available -= 1
                                     time.sleep(2) # Small delay after sending alert
                                 else:
                                     logger.error(f"❌ [Main] Failed to insert signal for {symbol} into database.")
                             else:
                                 logger.warning(f"⚠️ [Main] Maximum limit ({final_open_count}) reached before inserting signal for {symbol}. Signal ignored.")
                                 break

                 except psycopg2.Error as db_loop_err:
                      logger.error(f"❌ [Main] DB error processing symbol {symbol}: {db_loop_err}. Moving to next...")
                      if conn: conn.rollback()
                      continue
                 except Exception as symbol_proc_err:
                      logger.error(f"❌ [Main] General error processing symbol {symbol}: {symbol_proc_err}", exc_info=True)
                      continue

                 time.sleep(0.3) # Small delay between processing symbols

            scan_duration = time.time() - scan_start_time
            logger.info(f"🏁 [Main] Scan cycle finished. Signals generated: {signals_generated_in_loop}. Scan duration: {scan_duration:.2f} seconds.")
            # Adjust wait time for 5m timeframe scan (e.g., scan every 1 minute)
            wait_time = max(15, 60 - scan_duration) # Wait 1 minute total or at least 15 seconds
            logger.info(f"⏳ [Main] Waiting {wait_time:.1f} seconds for the next cycle...")
            time.sleep(wait_time)

        except KeyboardInterrupt:
             logger.info("🛑 [Main] Stop requested (KeyboardInterrupt). Shutting down...")
             break
        except psycopg2.Error as db_main_err:
             logger.error(f"❌ [Main] Fatal database error in main loop: {db_main_err}. Attempting to reconnect...")
             if conn: conn.rollback()
             time.sleep(60)
             try:
                 init_db()
             except Exception as recon_err:
                 logger.critical(f"❌ [Main] Failed to reconnect to database: {recon_err}. Exiting...")
                 break
        except Exception as main_err:
            logger.error(f"❌ [Main] Unexpected error in main loop: {main_err}", exc_info=True)
            logger.info("ℹ️ [Main] Waiting 120 seconds before retrying...")
            time.sleep(120)

def cleanup_resources() -> None:
    """Closes used resources like the database connection."""
    global conn
    logger.info("ℹ️ [Cleanup] Closing resources...")
    if conn:
        try:
            conn.close()
            logger.info("✅ [DB] Database connection closed.")
        except Exception as close_err:
            logger.error(f"⚠️ [DB] Error closing database connection: {close_err}")
    logger.info("✅ [Cleanup] Resource cleanup complete.")


# ---------------------- Main Entry Point ----------------------
if __name__ == "__main__":
    logger.info("🚀 Starting trading signal bot...")
    logger.info(f"Local Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | UTC Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")

    ws_thread: Optional[Thread] = None
    tracker_thread: Optional[Thread] = None
    flask_thread: Optional[Thread] = None

    try:
        init_db()

        ws_thread = Thread(target=run_ticker_socket_manager, daemon=True, name="WebSocketThread")
        ws_thread.start()
        logger.info("✅ [Main] WebSocket Ticker thread started.")
        logger.info("ℹ️ [Main] Waiting 5 seconds for WebSocket initialization...")
        time.sleep(5)
        if not ticker_data:
             logger.warning("⚠️ [Main] No initial data received from WebSocket after 5 seconds.")
        else:
             logger.info(f"✅ [Main] Received initial data from WebSocket for {len(ticker_data)} symbols.")

        tracker_thread = Thread(target=track_signals, daemon=True, name="TrackerThread")
        tracker_thread.start()
        logger.info("✅ [Main] Signal Tracker thread started.")

        if WEBHOOK_URL:
            flask_thread = Thread(target=run_flask, daemon=True, name="FlaskThread")
            flask_thread.start()
            logger.info("✅ [Main] Flask Webhook thread started.")
        else:
             logger.info("ℹ️ [Main] Webhook URL not configured, Flask server will not start.")

        main_loop()

    except Exception as startup_err:
        logger.critical(f"❌ [Main] A fatal error occurred during startup or in the main loop: {startup_err}", exc_info=True)
    finally:
        logger.info("🛑 [Main] Program is shutting down...")
        # send_telegram_message(CHAT_ID, "⚠️ Alert: Trading bot is shutting down now.") # Uncomment to send alert on shutdown
        cleanup_resources()
        logger.info("👋 [Main] Trading signal bot stopped.")
        os._exit(0) # Use os._exit(0) for a clean exit from all threads

