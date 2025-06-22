import os
import logging
import pickle
import io
from flask import Flask, Response, make_response, render_template_string
import psycopg2
from psycopg2.extras import RealDictCursor
from decouple import config

# ==============================================================================
# ------------------------------ الإعدادات الأساسية ------------------------------
# ==============================================================================

# إعداد نظام التسجيل (Logging) لعرض المعلومات والأخطاء
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger('ModelDownloader')

# إنشاء تطبيق Flask
app = Flask(__name__)

# تحميل رابط قاعدة البيانات من ملف .env
try:
    DB_URL = config('DATABASE_URL')
    logger.info("✅ تم تحميل رابط قاعدة البيانات بنجاح.")
except Exception as e:
    logger.critical(f"❌ لم يتم العثور على متغير DATABASE_URL في ملف .env. تأكد من وجود الملف والمتغير. الخطأ: {e}")
    exit(1)

# ==============================================================================
# ----------------------------- دوال مساعدة للاتصال -----------------------------
# ==============================================================================

def get_db_connection():
    """
    تقوم بإنشاء وإرجاع اتصال جديد بقاعدة البيانات.
    ترجع None في حال فشل الاتصال.
    """
    try:
        conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        logger.error(f"❌ [DB] فشل إنشاء اتصال جديد بقاعدة البيانات: {e}")
        return None

# ==============================================================================
# --------------------------------- واجهة الويب --------------------------------
# ==============================================================================

# HTML Template for the main page
# قالب HTML للصفحة الرئيسية لعرض قائمة النماذج
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تحميل النماذج</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; line-height: 1.6; background-color: #f4f4f9; color: #333; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: 20px auto; background: #fff; padding: 30px; border-radius: 10px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        h1 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }
        ul { list-style-type: none; padding: 0; }
        li { background-color: #ecf0f1; margin-bottom: 10px; border-radius: 5px; transition: all 0.3s ease; }
        li:hover { background-color: #bdc3c7; transform: translateX(5px); }
        a { display: block; padding: 15px 20px; color: #2980b9; text-decoration: none; font-weight: 500; font-size: 1.1em; }
        a:hover { color: #1c587f; }
        .error { color: #c0392b; background-color: #f2dede; border: 1px solid #ebccd1; padding: 15px; border-radius: 5px; }
        .empty { color: #7f8c8d; text-align: center; font-size: 1.2em; padding: 40px 0; }
    </style>
</head>
<body>
    <div class="container">
        <h1>النماذج المتاحة للتحميل</h1>
        {% if error %}
            <p class="error"><b>خطأ:</b> {{ error }}</p>
        {% elif models %}
            <ul>
                {% for model in models %}
                <li>
                    <a href="/download/{{ model.model_name }}" download>{{ model.model_name }}</a>
                </li>
                {% endfor %}
            </ul>
        {% else %}
            <p class="empty">لا توجد نماذج في قاعدة البيانات.</p>
        {% endif %}
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    """
    الصفحة الرئيسية التي تعرض قائمة بجميع النماذج المتاحة.
    """
    conn = get_db_connection()
    if not conn:
        return render_template_string(HTML_TEMPLATE, error="فشل الاتصال بقاعدة البيانات.")

    models_list = []
    try:
        with conn.cursor() as cur:
            # جلب أحدث نسخة من كل نموذج بناءً على الاسم
            cur.execute("""
                SELECT DISTINCT ON (model_name) model_name
                FROM ml_models
                ORDER BY model_name, trained_at DESC;
            """)
            models_list = cur.fetchall()
            logger.info(f"✅ تم العثور على {len(models_list)} نموذج فريد في قاعدة البيانات.")
    except Exception as e:
        logger.error(f"❌ خطأ أثناء جلب قائمة النماذج: {e}")
        return render_template_string(HTML_TEMPLATE, error=str(e))
    finally:
        if conn:
            conn.close()

    return render_template_string(HTML_TEMPLATE, models=models_list)

@app.route('/download/<model_name>')
def download_model(model_name):
    """
    تقوم بتحميل بيانات النموذج المحدد من قاعدة البيانات وإرسالها كملف.
    """
    conn = get_db_connection()
    if not conn:
        return "خطأ: فشل الاتصال بقاعدة البيانات.", 500

    try:
        with conn.cursor() as cur:
            # جلب أحدث بيانات للنموذج المحدد
            cur.execute(
                "SELECT model_data FROM ml_models WHERE model_name = %s ORDER BY trained_at DESC LIMIT 1;",
                (model_name,)
            )
            result = cur.fetchone()

            if not result or 'model_data' not in result:
                logger.warning(f"⚠️ لم يتم العثور على النموذج '{model_name}' في قاعدة البيانات.")
                return f"Model '{model_name}' not found.", 404

            model_data = result['model_data']
            logger.info(f"✅ تم العثور على بيانات النموذج '{model_name}'. بدء التحميل...")

            # إنشاء اسم للملف
            file_name = f"{model_name}.pkl"

            # إرسال البيانات كملف للتحميل
            return send_file(
                io.BytesIO(model_data),
                mimetype='application/octet-stream',
                as_attachment=True,
                download_name=file_name
            )

    except Exception as e:
        logger.error(f"❌ خطأ أثناء تحميل النموذج '{model_name}': {e}")
        return "حدث خطأ أثناء معالجة طلبك.", 500
    finally:
        if conn:
            conn.close()

# دالة send_file مخصصة لتجنب الاعتماد الكامل على flask.send_file إذا كانت هناك مشاكل
def send_file(data, mimetype, as_attachment, download_name):
    response = make_response(data.read())
    response.headers['Content-Type'] = mimetype
    if as_attachment:
        response.headers['Content-Disposition'] = f'attachment; filename="{download_name}"'
    return response

# ==============================================================================
# --------------------------------- نقطة البداية --------------------------------
# ==============================================================================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🌍 لبدء التحميل، افتح الرابط التالي في متصفحك: http://127.0.0.1:{port}")
    app.run(host='0.0.0.0', port=port, debug=True)
