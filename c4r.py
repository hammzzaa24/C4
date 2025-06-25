import os
import time
import logging
import psycopg2
import numpy as np
import pandas as pd
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
        logging.FileHandler('sr_scanner_v6_volume.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('SR_Scanner_V6_Volume')

# ---------------------- تحميل متغيرات البيئة ----------------------
try:
    API_KEY: str = config('BINANCE_API_KEY')
    API_SECRET: str = config('BINANCE_API_SECRET')
    DB_URL: str = config('DATABASE_URL')
except Exception as e:
    logger.critical(f"❌ فشل حاسم في تحميل متغيرات البيئة الأساسية: {e}")
    exit(1)

# ---------------------- إعداد الثوابت ----------------------
ANALYSIS_INTERVAL_HOURS = 4
MAX_WORKERS = 10
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 5

# كمية البيانات التاريخية
DATA_FETCH_DAYS_1D = 600
DATA_FETCH_DAYS_4H = 200
DATA_FETCH_DAYS_15M = 30

# مضاعفات البروز المعتمدة على ATR
ATR_PROMINENCE_MULTIPLIER_1D = 1.5
ATR_PROMINENCE_MULTIPLIER_4H = 1.0
ATR_PROMINENCE_MULTIPLIER_15M = 0.75
ATR_PERIOD = 14

# عرض القمم (عدد الشموع)
WIDTH_1D = 10
WIDTH_4H = 5
WIDTH_15M = 10

# --- جديد: معايير تأكيد حجم التداول ---
# هذا القسم يضيف طبقة فلترة للقمم والقيعان بناءً على حجم التداول
VOLUME_CONFIRMATION_ENABLED = True  # مفتاح لتشغيل/إيقاف الميزة بسهولة
VOLUME_AVG_PERIOD = 50              # الفترة الزمنية لحساب متوسط حجم التداول (SMA)
VOLUME_SPIKE_FACTOR = 1.8           # لتأكيد القمة/القاع، يجب أن يكون حجم التداول أعلى بـ 1.8 مرة من متوسطه

# معايير التجميع والدمج
CLUSTER_EPS_PERCENT = 0.005
CONFLUENCE_ZONE_PERCENT = 0.005
VOLUME_PROFILE_BINS = 100

# ---------------------- قسم خادم الويب ----------------------
# (لا تغيير هنا)
class WebServerHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        html_content = """
        <!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8"><title>حالة الماسح</title>
        <style>body{font-family: 'Segoe UI', sans-serif; background-color: #f4f4f9; color: #333; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0;} .container{text-align: center; padding: 40px; background-color: white; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1);} h1{color: #0056b3;} .status{font-weight: bold; color: #28a745;}</style>
        </head><body><div class="container"><h1>📊 ماسح الدعم والمقاومة V6</h1><h2>(مع بروز ATR الديناميكي وتأكيد حجم التداول)</h2><p>الخدمة <span class="status">تعمل</span>.</p></div></body></html>
        """
        self.wfile.write(html_content.encode('utf-8'))

def run_web_server():
    PORT = int(os.environ.get("PORT", 8080))
    with socketserver.TCPServer(("", PORT), WebServerHandler) as httpd:
        logger.info(f"🌐 خادم الويب يعمل على المنفذ {PORT}")
        httpd.serve_forever()

# ---------------------- دوال Binance والبيانات ----------------------
# (لا تغيير هنا)
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
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df[numeric_cols].dropna()
        except Exception as e:
            logger.error(f"❌ [{symbol}] خطأ في جلب البيانات (محاولة {attempt + 1}/{API_RETRY_ATTEMPTS}): {e}")
            if attempt < API_RETRY_ATTEMPTS - 1: time.sleep(API_RETRY_DELAY)
    logger.critical(f"❌ [{symbol}] فشل جلب البيانات بعد {API_RETRY_ATTEMPTS} محاولات.")
    return None

def get_validated_symbols(client: Client, filename: str = 'crypto_list.txt') -> List[str]:
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
# (لا تغيير هنا)
def init_db() -> Optional[psycopg2.extensions.connection]:
    logger.info("[قاعدة البيانات] بدء تهيئة الاتصال...")
    conn = None
    try:
        conn = psycopg2.connect(DB_URL, connect_timeout=10, cursor_factory=RealDictCursor)
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS support_resistance_levels (
                    id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, level_price DOUBLE PRECISION NOT NULL,
                    level_type TEXT NOT NULL, timeframe TEXT NOT NULL, strength NUMERIC NOT NULL,
                    last_tested_at TIMESTAMP, details TEXT, created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    CONSTRAINT unique_level UNIQUE (symbol, level_price, timeframe, level_type)
                );
            """)
            conn.commit()
            cur.execute("SELECT data_type FROM information_schema.columns WHERE table_name = 'support_resistance_levels' AND column_name = 'strength';")
            result = cur.fetchone()
            if result and result['data_type'] in ('bigint', 'integer'):
                logger.info(f"[DB] 'strength' column is {result['data_type']}. Altering to NUMERIC...")
                cur.execute("ALTER TABLE support_resistance_levels ALTER COLUMN strength TYPE NUMERIC USING strength::numeric;")
                conn.commit()
                logger.info("✅ [DB] Successfully altered 'strength' column to NUMERIC.")
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
            insert_query = "INSERT INTO support_resistance_levels (symbol, level_price, level_type, timeframe, strength, last_tested_at, details) VALUES %s;"
            values_to_insert = [(level.get('symbol'), level.get('level_price'), level.get('level_type'), level.get('timeframe'), level.get('strength'), level.get('last_tested_at'), level.get('details')) for level in all_final_levels]
            execute_values(cur, insert_query, values_to_insert)
        conn.commit()
        logger.info(f"✅ [DB] تم حفظ جميع المستويات بنجاح باستخدام الحفظ المجمع.")
    except Exception as e:
        logger.error(f"❌ [DB] حدث خطأ أثناء الحفظ المجمع في قاعدة البيانات: {e}", exc_info=True)
        conn.rollback()

# ---------------------- دوال التحليل وتحديد المستويات ----------------------

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    return atr.iloc[-1] if not atr.empty else 0

def find_price_action_levels(df: pd.DataFrame, atr_value: float, prominence_multiplier: float, width: int, cluster_eps_percent: float) -> List[Dict]:
    """
    --- معدّل: تحديد القمم والقيعان مع فلترة إضافية باستخدام حجم التداول ---
    """
    lows = df['low'].to_numpy()
    highs = df['high'].to_numpy()
    
    dynamic_prominence = atr_value * prominence_multiplier
    if dynamic_prominence == 0:
        logger.warning("[Peaks] قيمة ATR تساوي صفر، سيتم استخدام قيمة بروز افتراضية صغيرة.")
        dynamic_prominence = highs.mean() * 0.01 
    logger.debug(f"[Peaks] استخدام بروز ديناميكي بقيمة: {dynamic_prominence:.4f}")

    low_peaks_indices, _ = find_peaks(-lows, prominence=dynamic_prominence, width=width)
    high_peaks_indices, _ = find_peaks(highs, prominence=dynamic_prominence, width=width)

    # --- جديد: قسم تأكيد حجم التداول ---
    if VOLUME_CONFIRMATION_ENABLED and not df.empty:
        # حساب متوسط حجم التداول المتحرك البسيط
        df['volume_avg'] = df['volume'].rolling(window=VOLUME_AVG_PERIOD, min_periods=1).mean()
        
        # فلترة القيعان (الدعم) التي يتزامن معها فوليوم عالٍ
        confirmed_low_indices = []
        for idx in low_peaks_indices:
            peak_volume = df['volume'].iloc[idx]
            avg_volume = df['volume_avg'].iloc[idx]
            if not pd.isna(avg_volume) and avg_volume > 0 and peak_volume >= avg_volume * VOLUME_SPIKE_FACTOR:
                confirmed_low_indices.append(idx)
        
        # فلترة القمم (المقاومة) التي يتزامن معها فوليوم عالٍ
        confirmed_high_indices = []
        for idx in high_peaks_indices:
            peak_volume = df['volume'].iloc[idx]
            avg_volume = df['volume_avg'].iloc[idx]
            if not pd.isna(avg_volume) and avg_volume > 0 and peak_volume >= avg_volume * VOLUME_SPIKE_FACTOR:
                confirmed_high_indices.append(idx)
        
        logger.debug(f"[Volume Filter] Lows before: {len(low_peaks_indices)}, after: {len(confirmed_low_indices)}. "
                     f"Highs before: {len(high_peaks_indices)}, after: {len(confirmed_high_indices)}")
        
        # استخدام المؤشرات المؤكدة فقط
        low_peaks_indices = np.array(confirmed_low_indices)
        high_peaks_indices = np.array(confirmed_high_indices)
    # --- نهاية قسم التأكيد ---

    def cluster_and_strengthen(prices: np.ndarray, indices: np.ndarray, level_type: str) -> List[Dict]:
        if len(indices) < 2: return []
        points = prices[indices].reshape(-1, 1)
        eps_value = points.mean() * cluster_eps_percent
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

# (باقي دوال التحليل لا تتغير)
def analyze_volume_profile(df: pd.DataFrame, bins: int) -> List[Dict]:
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
    if not levels: return [], []
    levels.sort(key=lambda x: x['level_price'])
    tf_weights = {'1d': 3, '4h': 2, '15m': 1}
    type_weights = {'poc': 2.5, 'support': 1.5, 'resistance': 1.5}
    confluence_zones, used_indices = [], set()
    for i in range(len(levels)):
        if i in used_indices: continue
        current_zone_levels, current_zone_indices = [levels[i]], {i}
        for j in range(i + 1, len(levels)):
            if j in used_indices: continue
            price_i, price_j = levels[i]['level_price'], levels[j]['level_price']
            if (abs(price_j - price_i) / price_i) <= confluence_percent:
                current_zone_levels.append(levels[j])
                current_zone_indices.add(j)
        if len(current_zone_levels) > 1:
            used_indices.update(current_zone_indices)
            avg_price = sum(l['level_price'] * l['strength'] for l in current_zone_levels) / sum(l['strength'] for l in current_zone_levels)
            total_strength = sum(l['strength'] * tf_weights.get(l.get('timeframe'), 1) * type_weights.get(l['level_type'], 1) for l in current_zone_levels)
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
    logger.info(f"--- بدء تحليل العملة: {symbol} ---")
    raw_levels = []
    
    timeframes_config = {
        '1d':  {'days': DATA_FETCH_DAYS_1D,  'prominence_multiplier': ATR_PROMINENCE_MULTIPLIER_1D,  'width': WIDTH_1D},
        '4h':  {'days': DATA_FETCH_DAYS_4H,  'prominence_multiplier': ATR_PROMINENCE_MULTIPLIER_4H,  'width': WIDTH_4H},
        '15m': {'days': DATA_FETCH_DAYS_15M, 'prominence_multiplier': ATR_PROMINENCE_MULTIPLIER_15M, 'width': WIDTH_15M}
    }

    for tf, config in timeframes_config.items():
        df = fetch_historical_data_with_retry(client, symbol, tf, config['days'])
        if df is not None and not df.empty:
            atr_value = calculate_atr(df, period=ATR_PERIOD)
            logger.debug(f"[{symbol}-{tf}] Calculated ATR: {atr_value:.4f}")
            
            pa_levels = find_price_action_levels(
                df, 
                atr_value, 
                config['prominence_multiplier'], 
                config['width'], 
                CLUSTER_EPS_PERCENT
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
    
    for level in final_levels:
        level['symbol'] = symbol
        
    logger.info(f"--- ✅ انتهى تحليل {symbol}، تم العثور على {len(final_levels)} مستوى نهائي. ---")
    return final_levels


def run_full_analysis():
    logger.info("🚀 بدء تشغيل محلل الدعوم والمقاومات V6 (مع تأكيد حجم التداول)...")
    
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
        save_levels_to_db_batch(conn, all_final_levels)
    else:
        logger.info("ℹ️ لم يتم العثور على أي مستويات في أي عملة خلال هذه الدورة.")

    conn.close()
    logger.info("🎉🎉🎉 اكتملت عملية تحليل جميع المستويات لجميع العملات! 🎉🎉🎉")

def analysis_scheduler():
    while True:
        try:
            run_full_analysis()
        except Exception as e:
            logger.error(f"❌ حدث خطأ فادح في دورة التحليل الرئيسية: {e}", exc_info=True)
        sleep_duration_seconds = ANALYSIS_INTERVAL_HOURS * 60 * 60
        logger.info(f"👍 اكتملت دورة التحليل. سيتم الانتظار لمدة {ANALYSIS_INTERVAL_HOURS} ساعات.")
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
