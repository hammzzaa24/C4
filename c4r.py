import os
import time
import logging
import psycopg2
import numpy as np
import pandas as pd
import datetime as dt
from decouple import config
from binance.client import Client
from psycopg2.extras import RealDictCursor, execute_values
from scipy.signal import find_peaks
from sklearn.cluster import DBSCAN
from typing import List, Dict, Optional, Tuple
import threading
import http.server
import socketserver
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------- إعداد نظام التسجيل (Logging) ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('sr_scanner_scalping_edition.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('SR_Scanner_Scalping')

# ---------------------- تحميل متغيرات البيئة ----------------------
try:
    API_KEY: str = config('BINANCE_API_KEY')
    API_SECRET: str = config('BINANCE_API_SECRET')
    DB_URL: str = config('DATABASE_URL')
except Exception as e:
    logger.critical(f"❌ فشل حاسم في تحميل متغيرات البيئة الأساسية: {e}")
    exit(1)

# ---------------------- إعداد الثوابت (نسخة السكالبينج) ----------------------
ANALYSIS_INTERVAL_MINUTES = 15
MAX_WORKERS = 10
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 5

DATA_FETCH_DAYS_1H = 30
DATA_FETCH_DAYS_15M = 7
DATA_FETCH_DAYS_5M = 3

ATR_PROMINENCE_MULTIPLIER_1H = 0.8
ATR_PROMINENCE_MULTIPLIER_15M = 0.6
ATR_PROMINENCE_MULTIPLIER_5M = 0.5
ATR_PERIOD = 14
ATR_SHORT_PERIOD = 7
ATR_LONG_PERIOD = 28

WIDTH_1H = 8
WIDTH_15M = 5
WIDTH_5M = 3

VOLUME_CONFIRMATION_ENABLED = True
VOLUME_AVG_PERIOD = 20
VOLUME_SPIKE_FACTOR = 1.6

CLUSTER_EPS_PERCENT = 0.0015
CONFLUENCE_ZONE_PERCENT = 0.002
VOLUME_PROFILE_BINS = 100

# ---------------------- قسم خادم الويب ----------------------
class WebServerHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        html_content = """
        <!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8"><title>حالة الماسح</title>
        <style>body{font-family: 'Segoe UI', sans-serif; background-color: #1a1a1a; color: #f0f0f0; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0;} .container{text-align: center; padding: 40px; background-color: #2b2b2b; border-radius: 10px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); border: 1px solid #00aaff;} h1{color: #00aaff;} .status{font-weight: bold; color: #28a745;}</style>
        </head><body><div class="container"><h1>⚡️ ماسح الدعم والمقاومة - إصدار السكالبينج ⚡️</h1><h2>(مخصص للإطارات الزمنية الصغيرة)</h2><p>الخدمة <span class="status">تعمل</span>.</p><p>يتم التحديث كل 15 دقيقة.</p></div></body></html>
        """
        self.wfile.write(html_content.encode('utf-8'))

def run_web_server():
    PORT = int(os.environ.get("PORT", 8080))
    with socketserver.TCPServer(("", PORT), WebServerHandler) as httpd:
        logger.info(f"🌐 خادم الويب يعمل على المنفذ {PORT}")
        httpd.serve_forever()

# ---------------------- دوال Binance والبيانات ----------------------
def get_binance_client() -> Optional[Client]:
    try:
        client = Client(API_KEY, API_SECRET)
        client.ping()
        logger.info("✅ [Binance] تم الاتصال بواجهة برمجة تطبيقات Binance بنجاح.")
        return client
    except Exception as e:
        logger.critical(f"❌ [Binance] فشل الاتصال بواجهة برمجة التطبيقات: {e}")
        return None

def fetch_historical_data_with_retry(client: Client, symbol: str, interval: str, days: int) -> Optional[pd.DataFrame]:
    for attempt in range(API_RETRY_ATTEMPTS):
        try:
            start_str = (pd.to_datetime('today') - pd.Timedelta(days=days)).strftime('%Y-%m-%d')
            klines = client.get_historical_klines(symbol, interval, start_str)
            if not klines:
                logger.warning(f"⚠️ [{symbol}] لم يتم العثور على بيانات على فريم {interval}.")
                return None
            df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume', 'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore'])
            numeric_cols = ['open', 'high', 'low', 'close', 'volume']
            for col in numeric_cols: df[col] = pd.to_numeric(df[col], errors='coerce')
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize('UTC')
            df.set_index('timestamp', inplace=True)
            return df[numeric_cols].dropna()
        except Exception as e:
            logger.error(f"❌ [{symbol}] خطأ في جلب البيانات (محاولة {attempt + 1}/{API_RETRY_ATTEMPTS}): {e}")
            if attempt < API_RETRY_ATTEMPTS - 1: time.sleep(API_RETRY_DELAY)
    logger.critical(f"❌ [{symbol}] فشل جلب البيانات بعد {API_RETRY_ATTEMPTS} محاولات.")
    return None

def get_validated_symbols(client: Client, filename: str = 'crypto_list.txt') -> List[str]:
    # ... (no changes in this function)
    logger.info(f"ℹ️ [التحقق] قراءة الرموز من '{filename}' والتحقق منها...")
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(script_dir, filename)
        with open(file_path, 'r', encoding='utf-8') as f:
            raw_symbols = {line.strip().upper() for line in f if line.strip() and not line.startswith('#')}
        formatted = {f"{s}USDT" if not s.endswith('USDT') else s for s in raw_symbols}
        exchange_info = client.get_exchange_info()
        active = {s['symbol'] for s in exchange_info['symbols'] if s.get('quoteAsset') == 'USDT' and s.get('status') == 'TRADING'}
        validated = sorted(list(formatted.intersection(active)))
        logger.info(f"✅ [التحقق] سيتم تحليل {len(validated)} عملة معتمدة.")
        return validated
    except Exception as e:
        logger.error(f"❌ [التحقق] حدث خطأ أثناء التحقق من الرموز: {e}", exc_info=True)
        return []

# ---------------------- دوال قاعدة البيانات ----------------------
def init_db() -> Optional[psycopg2.extensions.connection]:
    logger.info("[قاعدة البيانات] بدء تهيئة الاتصال...")
    conn = None
    try:
        conn = psycopg2.connect(DB_URL, connect_timeout=10, cursor_factory=RealDictCursor)
        with conn.cursor() as cur:
            # تحديث الجدول ليشمل عمود 'score'
            cur.execute("""
                CREATE TABLE IF NOT EXISTS support_resistance_levels (
                    id SERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    level_price DOUBLE PRECISION NOT NULL,
                    level_type TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    strength NUMERIC NOT NULL,
                    score NUMERIC DEFAULT 0,
                    last_tested_at TIMESTAMP WITH TIME ZONE,
                    details TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    CONSTRAINT unique_level UNIQUE (symbol, level_price, timeframe, level_type)
                );
            """)
            # التأكد من وجود عمود 'score' وإضافته إذا لم يكن موجودًا
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='support_resistance_levels' AND column_name='score'")
            if not cur.fetchone():
                logger.info("[DB] عمود 'score' غير موجود، سيتم إضافته...")
                cur.execute("ALTER TABLE support_resistance_levels ADD COLUMN score NUMERIC DEFAULT 0;")
                logger.info("✅ [DB] تم إضافة عمود 'score' بنجاح.")

            conn.commit()
        logger.info("✅ [قاعدة البيانات] تم تهيئة وتحديث جدول 'support_resistance_levels' بنجاح.")
        return conn
    except Exception as e:
        logger.critical(f"❌ [قاعدة البيانات] فشل الاتصال أو تهيئة الجدول: {e}")
        if conn: conn.rollback()
        return None

def save_levels_to_db_batch(conn: psycopg2.extensions.connection, all_final_levels: List[Dict]):
    if not all_final_levels:
        logger.info("ℹ️ [DB] لا توجد مستويات نهائية ليتم حفظها.")
        return
    logger.info(f"⏳ [DB] جاري حفظ {len(all_final_levels)} مستوى من جميع العملات في قاعدة البيانات...")
    try:
        with conn.cursor() as cur:
            symbols_processed = list(set(level['symbol'] for level in all_final_levels))
            cur.execute("DELETE FROM support_resistance_levels WHERE symbol = ANY(%s);", (symbols_processed,))
            logger.info(f"[DB] تم حذف البيانات القديمة لـ {len(symbols_processed)} عملة.")
            
            # تحديث استعلام الإدخال ليشمل 'score'
            insert_query = """
                INSERT INTO support_resistance_levels 
                (symbol, level_price, level_type, timeframe, strength, score, last_tested_at, details) 
                VALUES %s;
            """
            # تحديث البيانات المدخلة لتشمل 'score'
            values_to_insert = [
                (level.get('symbol'), level.get('level_price'), level.get('level_type'), 
                 level.get('timeframe'), level.get('strength'), level.get('score', 0), 
                 level.get('last_tested_at'), level.get('details')) 
                for level in all_final_levels
            ]
            execute_values(cur, insert_query, values_to_insert)
        conn.commit()
        logger.info(f"✅ [DB] تم حفظ جميع المستويات بنجاح باستخدام الحفظ المجمع.")
    except Exception as e:
        logger.error(f"❌ [DB] حدث خطأ أثناء الحفظ المجمع في قاعدة البيانات: {e}", exc_info=True)
        conn.rollback()


# ---------------------- دوال التحليل وتحديد المستويات ----------------------

# =========================================================================
# =============== START: المرحلة الثانية - دالة تقييم قوة المستوى ===============
# =========================================================================
def calculate_level_score(level: Dict) -> int:
    """
    تحسب "درجة" للمستوى بناءً على عدة معايير لتحديد قوته وأهميته.
    
    Args:
        level (Dict): قاموس يحتوي على تفاصيل المستوى.

    Returns:
        int: الدرجة النهائية للمستوى.
    """
    score = 0
    
    # 1. نقاط القوة الأساسية (عدد الارتكازات)
    # كل ارتكاز يضيف 10 نقاط.
    score += float(level.get('strength', 1)) * 10

    # 2. نقاط حداثة المستوى (Recency)
    # كلما كان المستوى حديثًا، كان أكثر أهمية.
    last_tested = level.get('last_tested_at')
    if last_tested:
        # تأكد من أن last_tested هو كائن datetime مدرك للمنطقة الزمنية
        if isinstance(last_tested, dt.datetime) and last_tested.tzinfo is None:
             last_tested = last_tested.replace(tzinfo=dt.timezone.utc)
        
        days_since_tested = (dt.datetime.now(dt.timezone.utc) - last_tested).days
        
        if days_since_tested < 2:
            score += 30  # نقاط إضافية عالية للمستويات التي تم اختبارها مؤخرًا جدًا
        elif days_since_tested < 7:
            score += 15  # نقاط إضافية للمستويات التي تم اختبارها خلال الأسبوع الماضي
        elif days_since_tested < 30:
            score += 5   # نقاط قليلة للمستويات التي تم اختبارها خلال الشهر الماضي

    # 3. نقاط التوافق (Confluence)
    # المستويات التي تجمع بين عدة تحليلات هي الأقوى.
    if level.get('level_type') == 'confluence':
        num_timeframes = len(level.get('timeframe', '').split(','))
        num_details = len(level.get('details', '').split(','))
        # كل إطار زمني أو نوع مستوى مدمج يضيف 20 نقطة
        score += (num_timeframes + num_details) * 20
        # إضافة نقاط إضافية إذا كان الـ POC جزءًا من منطقة التوافق
        if 'poc' in level.get('details', ''):
            score += 25 
            
    # 4. نقاط لنقاط التحكم في الحجم (POC)
    if level.get('level_type') == 'poc':
        score += 15 # إعطاء نقاط أساسية للـ POC

    return int(score)
# =======================================================================
# ================= END: المرحلة الثانية - دالة تقييم قوة المستوى ================
# =======================================================================


def calculate_atr(df: pd.DataFrame, period: int) -> float:
    if df.empty or len(df) < period:
        return 0
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    return atr.iloc[-1] if not atr.empty else 0

def find_price_action_levels(df: pd.DataFrame, atr_value: float, prominence_multiplier: float, width: int, cluster_eps_percent: float) -> List[Dict]:
    lows = df['low'].to_numpy()
    highs = df['high'].to_numpy()
    
    dynamic_prominence = atr_value * prominence_multiplier
    if dynamic_prominence == 0:
        logger.warning("[Peaks] قيمة ATR تساوي صفر، سيتم استخدام قيمة بروز افتراضية صغيرة.")
        dynamic_prominence = highs.mean() * 0.01 
    logger.debug(f"[Peaks] استخدام بروز ديناميكي بقيمة: {dynamic_prominence:.4f} ومضاعف: {prominence_multiplier:.2f}")

    low_peaks_indices, _ = find_peaks(-lows, prominence=dynamic_prominence, width=width)
    high_peaks_indices, _ = find_peaks(highs, prominence=dynamic_prominence, width=width)

    if VOLUME_CONFIRMATION_ENABLED and not df.empty:
        df['volume_avg'] = df['volume'].rolling(window=VOLUME_AVG_PERIOD, min_periods=1).mean()
        
        confirmed_low_indices = [idx for idx in low_peaks_indices if df['volume'].iloc[idx] >= df['volume_avg'].iloc[idx] * VOLUME_SPIKE_FACTOR]
        confirmed_high_indices = [idx for idx in high_peaks_indices if df['volume'].iloc[idx] >= df['volume_avg'].iloc[idx] * VOLUME_SPIKE_FACTOR]
        
        logger.debug(f"[Volume Filter] Lows before: {len(low_peaks_indices)}, after: {len(confirmed_low_indices)}. "
                     f"Highs before: {len(high_peaks_indices)}, after: {len(confirmed_high_indices)}")
        
        low_peaks_indices = np.array(confirmed_low_indices)
        high_peaks_indices = np.array(confirmed_high_indices)

    def cluster_and_strengthen(prices: np.ndarray, indices: np.ndarray, level_type: str) -> List[Dict]:
        if len(indices) < 2: return []
        points = prices[indices].reshape(-1, 1)
        eps_value = points.mean() * cluster_eps_percent
        if eps_value == 0: return []
        db = DBSCAN(eps=eps_value, min_samples=2).fit(points)
        clustered_levels = []
        for label in set(db.labels_):
            if label != -1:
                mask = (db.labels_ == label)
                cluster_indices = indices[mask]
                clustered_levels.append({
                    "level_price": float(prices[cluster_indices].mean()),
                    "level_type": level_type,
                    "strength": int(len(cluster_indices)),
                    "last_tested_at": df.index[cluster_indices[-1]].to_pydatetime()
                })
        return clustered_levels

    support_levels = cluster_and_strengthen(lows, low_peaks_indices, 'support')
    resistance_levels = cluster_and_strengthen(highs, high_peaks_indices, 'resistance')
    return support_levels + resistance_levels

def analyze_volume_profile(df: pd.DataFrame, bins: int) -> List[Dict]:
    # ... (no changes in this function)
    price_min, price_max = df['low'].min(), df['high'].max()
    if price_min >= price_max: return []
    price_bins = np.linspace(price_min, price_max, bins + 1)
    bin_centers = (price_bins[:-1] + price_bins[1:]) / 2
    volume_by_bin = np.zeros(bins)
    for _, row in df.iterrows():
        low_idx = np.searchsorted(price_bins, row['low'], side='right') - 1
        high_idx = np.searchsorted(price_bins, row['high'], side='left')
        low_idx, high_idx = max(0, low_idx), min(bins, high_idx)
        if high_idx > low_idx:
            volume_per_bin = row['volume'] / (high_idx - low_idx)
            for i in range(low_idx, high_idx): volume_by_bin[i] += volume_per_bin
    if np.sum(volume_by_bin) == 0: return []
    poc_index = np.argmax(volume_by_bin)
    return [{"level_price": float(bin_centers[poc_index]), "level_type": 'poc', "strength": float(volume_by_bin[poc_index]), "last_tested_at": None}]


def find_confluence_zones(levels: List[Dict], confluence_percent: float) -> Tuple[List[Dict], List[Dict]]:
    # ... (no changes in this function)
    if not levels: return [], []
    levels.sort(key=lambda x: x['level_price'])
    tf_weights = {'1h': 3, '15m': 2, '5m': 1} 
    type_weights = {'poc': 2.5, 'support': 1.5, 'resistance': 1.5}
    confluence_zones, used_indices = [], set()
    for i in range(len(levels)):
        if i in used_indices: continue
        current_zone_levels, current_zone_indices = [levels[i]], {i}
        for j in range(i + 1, len(levels)):
            if j in used_indices: continue
            price_i, price_j = levels[i]['level_price'], levels[j]['level_price']
            if price_i > 0 and (abs(price_j - price_i) / price_i) <= confluence_percent:
                current_zone_levels.append(levels[j])
                current_zone_indices.add(j)
        if len(current_zone_levels) > 1:
            used_indices.update(current_zone_indices)
            total_strength_for_avg = sum(l['strength'] for l in current_zone_levels)
            if total_strength_for_avg == 0: continue
            avg_price = sum(l['level_price'] * l['strength'] for l in current_zone_levels) / total_strength_for_avg
            total_strength = sum(l['strength'] * tf_weights.get(l.get('timeframe'), 1) * type_weights.get(l.get('level_type'], 1) for l in current_zone_levels)
            timeframes = sorted(list(set(l['timeframe'] for l in current_zone_levels)))
            details = sorted(list(set(l['level_type'] for l in current_zone_levels)))
            last_tested = max((l['last_tested_at'] for l in current_zone_levels if l['last_tested_at']), default=None)
            confluence_zones.append({
                "level_price": avg_price, "level_type": 'confluence', "strength": float(total_strength), 
                "timeframe": ",".join(timeframes), "details": ",".join(details), "last_tested_at": last_tested
            })
    remaining_levels = [level for i, level in enumerate(levels) if i not in used_indices]
    return confluence_zones, remaining_levels

# ---------------------- حلقة العمل الرئيسية للتحليل ----------------------

def analyze_single_symbol(symbol: str, client: Client) -> List[Dict]:
    logger.info(f"--- بدء تحليل (سكالبينج) للعملة: {symbol} ---")
    raw_levels = []
    
    timeframes_config = {
        '1h':  {'days': DATA_FETCH_DAYS_1H,  'prominence_multiplier': ATR_PROMINENCE_MULTIPLIER_1H,  'width': WIDTH_1H},
        '15m': {'days': DATA_FETCH_DAYS_15M, 'prominence_multiplier': ATR_PROMINENCE_MULTIPLIER_15M, 'width': WIDTH_15M},
        '5m':  {'days': DATA_FETCH_DAYS_5M,  'prominence_multiplier': ATR_PROMINENCE_MULTIPLIER_5M,  'width': WIDTH_5M}
    }

    for tf, config in timeframes_config.items():
        df = fetch_historical_data_with_retry(client, symbol, tf, config['days'])
        if df is not None and not df.empty:
            
            atr_standard = calculate_atr(df, period=ATR_PERIOD)
            atr_short = calculate_atr(df, period=ATR_SHORT_PERIOD)
            atr_long = calculate_atr(df, period=ATR_LONG_PERIOD)
            
            dynamic_prominence_multiplier = config['prominence_multiplier']
            if atr_long > 0 and atr_short > atr_long * 1.25:
                dynamic_prominence_multiplier *= 1.2
            elif atr_long > 0 and atr_short < atr_long * 0.8:
                dynamic_prominence_multiplier *= 0.8

            pa_levels = find_price_action_levels(
                df, atr_standard, dynamic_prominence_multiplier, config['width'], CLUSTER_EPS_PERCENT
            )
            
            vol_levels = analyze_volume_profile(df, bins=VOLUME_PROFILE_BINS)
            
            for level in pa_levels + vol_levels:
                level['timeframe'] = tf
            raw_levels.extend(pa_levels + vol_levels)
        else:
            logger.warning(f"⚠️ [{symbol}-{tf}] تعذر جلب البيانات، سيتم التخطي.")
        
    if not raw_levels:
        logger.info(f"ℹ️ [{symbol}] لم يتم العثور على أي مستويات أولية.")
        return []

    confluence_zones, remaining_singles = find_confluence_zones(raw_levels, CONFLUENCE_ZONE_PERCENT)
    final_levels = confluence_zones + remaining_singles
    
    # =========================================================================
    # =============== START: المرحلة الثانية - حساب درجة كل مستوى ===============
    # =========================================================================
    for level in final_levels:
        level['symbol'] = symbol
        level['score'] = calculate_level_score(level)
    # =======================================================================
    # ================= END: المرحلة الثانية - حساب درجة كل مستوى ================
    # =======================================================================
        
    logger.info(f"--- ✅ انتهى تحليل {symbol}، تم العثور على {len(final_levels)} مستوى نهائي. ---")
    return final_levels

def run_full_analysis():
    # ... (no changes in this function)
    logger.info("🚀 بدء تشغيل محلل السكالبينج...")
    
    client = get_binance_client()
    if not client: return
    conn = init_db()
    if not conn: return
    symbols_to_scan = get_validated_symbols(client, 'crypto_list.txt')
    if not symbols_to_scan:
        logger.warning("⚠️ لا توجد عملات لتحليلها. إيقاف الدورة الحالية.")
        conn.close()
        return

    logger.info(f"🌀 سيتم تحليل {len(symbols_to_scan)} عملة باستخدام {MAX_WORKERS} خيطاً متوازياً.")
    all_final_levels = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {executor.submit(analyze_single_symbol, symbol, client): symbol for symbol in symbols_to_scan}
        for i, future in enumerate(as_completed(future_to_symbol)):
            symbol = future_to_symbol[future]
            try:
                symbol_levels = future.result()
                if symbol_levels: all_final_levels.extend(symbol_levels)
                logger.info(f"🔄 ({i+1}/{len(symbols_to_scan)}) تمت معالجة نتائج {symbol}.")
            except Exception as e:
                logger.error(f"❌ حدث خطأ فادح أثناء تحليل {symbol}: {e}", exc_info=True)

    if all_final_levels:
        # ترتيب المستويات حسب الدرجة قبل الحفظ (اختياري لكن مفيد)
        all_final_levels.sort(key=lambda x: x.get('score', 0), reverse=True)
        save_levels_to_db_batch(conn, all_final_levels)
    else:
        logger.info("ℹ️ لم يتم العثور على أي مستويات في أي عملة خلال هذه الدورة.")

    conn.close()
    logger.info("🎉🎉🎉 اكتملت دورة تحليل السكالبينج! 🎉🎉🎉")

def analysis_scheduler():
    while True:
        try:
            run_full_analysis()
        except Exception as e:
            logger.error(f"❌ حدث خطأ فادح في دورة التحليل الرئيسية: {e}", exc_info=True)
        
        sleep_duration_seconds = ANALYSIS_INTERVAL_MINUTES * 60
        logger.info(f"👍 اكتملت دورة التحليل. سيتم الانتظار لمدة {ANALYSIS_INTERVAL_MINUTES} دقيقة.")
        time.sleep(sleep_duration_seconds)

# ---------------------- نقطة انطلاق البرنامج ----------------------
if __name__ == "__main__":
    web_server_thread = threading.Thread(target=run_web_server, daemon=True)
    web_server_thread.start()
    analysis_thread = threading.Thread(target=analysis_scheduler, daemon=True)
    analysis_thread.start()
    try:
        while True: time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("🛑 تم طلب إيقاف البرنامج. وداعاً!")
