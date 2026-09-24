import gradio as gr
import threading
import os
import sys
import time
import logging
import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("HF_APP")
APP_STARTED_AT = time.time()
BOT_HEALTH = {
    "status": "starting",
    "last_error": "",
    "started_at": "",
}

# ZeroGPU mandatory import
try:
    import spaces
    logger.info("Successfully imported spaces")
except ImportError:
    class spaces:
        @staticmethod
        def GPU(func):
            return func
    logger.info("Spaces import failed, using fallback")

# Add current dir to path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

from config.settings import (  # noqa: E402
    BOT_TOKEN,
    ADMIN_IDS,
    DATABASE_PATH,
    MAX_FILE_SIZE_MB,
    RATE_LIMIT_SECONDS,
    RATE_LIMIT_BURST,
    HOURLY_DOWNLOAD_LIMIT,
    DAILY_DOWNLOAD_LIMIT,
    MAX_CONCURRENT_DOWNLOADS,
    SUPPORTED_DOMAINS,
    TEMP_DIR,
    REDIS_URL,
    STORAGE_CHANNEL_ID,
)

@spaces.GPU
def dummy_gpu_task():
    return "GPU Initialized"

def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def safe_scalar(query: str, default=0):
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            cur = conn.execute(query)
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else default
    except Exception as exc:
        logger.warning("Dashboard query failed: %s", exc)
        return default


def safe_rows(query: str, limit: int = 10):
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query).fetchmany(limit)]
    except Exception as exc:
        logger.warning("Dashboard rows failed: %s", exc)
        return []


def directory_size(path: str) -> int:
    root = Path(path)
    if not root.exists():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def collect_dashboard_data():
    dummy_gpu_task()
    total_users = safe_scalar("SELECT COUNT(*) FROM users")
    banned_users = safe_scalar("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    total_downloads = safe_scalar("SELECT COUNT(*) FROM downloads")
    today_downloads = safe_scalar("SELECT COUNT(*) FROM downloads WHERE date(created_at) = date('now')")
    cache_items = safe_scalar("SELECT COUNT(*) FROM file_cache")
    cache_hits = safe_scalar("SELECT COALESCE(SUM(hits), 0) FROM file_cache")
    top_platforms = safe_rows(
        """
        SELECT COALESCE(platform, 'unknown') AS platform, COUNT(*) AS downloads
        FROM downloads
        GROUP BY COALESCE(platform, 'unknown')
        ORDER BY downloads DESC
        """,
        8,
    )
    recent_downloads = safe_rows(
        """
        SELECT created_at, user_id, COALESCE(platform, 'unknown') AS platform,
               COALESCE(title, url) AS item, COALESCE(media_type, '-') AS media_type
        FROM downloads
        ORDER BY id DESC
        """,
        12,
    )
    status_icon = "🟢" if BOT_HEALTH["status"] == "running" else "🟡" if BOT_HEALTH["status"] == "starting" else "🔴"
    temp_size = directory_size(TEMP_DIR)

    kpi = {
        "Bot Status": f"{status_icon} {BOT_HEALTH['status'].title()}",
        "Uptime": format_uptime(time.time() - APP_STARTED_AT),
        "Users": int(total_users),
        "Downloads Today": int(today_downloads),
        "Total Downloads": int(total_downloads),
        "Cache Items": int(cache_items),
        "Cache Hits": int(cache_hits),
        "Banned Users": int(banned_users),
        "Temp Usage": human_size(temp_size),
    }
    config_summary = {
        "Token Loaded": "Yes" if BOT_TOKEN else "No",
        "Admins": ", ".join(map(str, ADMIN_IDS)) or "None",
        "Supported Domains": len(SUPPORTED_DOMAINS),
        "Max File Size": f"{MAX_FILE_SIZE_MB} MB",
        "Rate Limit": f"{RATE_LIMIT_BURST} request(s) / {RATE_LIMIT_SECONDS}s",
        "Hourly Limit": HOURLY_DOWNLOAD_LIMIT,
        "Daily Limit": DAILY_DOWNLOAD_LIMIT,
        "Concurrent Downloads": MAX_CONCURRENT_DOWNLOADS,
        "Redis": "Configured" if REDIS_URL else "Not configured",
        "Media Vault": "Configured" if STORAGE_CHANNEL_ID else "Not configured",
    }
    return kpi, top_platforms, recent_downloads, config_summary


def render_overview():
    kpi, top_platforms, recent_downloads, config_summary = collect_dashboard_data()
    overview = "\n".join(f"### {key}\n**{value}**" for key, value in kpi.items())
    config_md = "\n".join(f"- **{key}:** {value}" for key, value in config_summary.items())
    error_md = "✅ No critical bot thread errors recorded." if not BOT_HEALTH["last_error"] else f"⚠️ `{BOT_HEALTH['last_error']}`"
    return overview, top_platforms, recent_downloads, config_md, error_md


def render_development_plan():
    return """
## 🚀 خطة التطوير التنفيذية المقترحة

### المرحلة الحالية — تم البدء
- لوحة مراقبة احترافية بدل صفحة status بسيطة.
- قراءة live metrics من SQLite.
- عرض حالة التوكن، Redis، Media Vault، والكاش.

### المرحلة التالية — Bot Engine
1. Queue حقيقي للتحميلات الثقيلة مع أولوية Premium.
2. اختيار الجودة والصيغة من Telegram قبل التحميل.
3. Progress messages دقيقة: فحص → تحميل → معالجة → رفع.
4. إعادة استخدام الملفات من Media Vault بدل إعادة التحميل.

### مرحلة المنتج
1. نظام نقاط واشتراكات.
2. Referral rewards.
3. لوحة Admin Web كاملة مع ban/broadcast/settings.
4. تنبيهات أعطال للأدمن داخل Telegram.

### مرحلة الإنتاج
1. نقل الأسرار بالكامل إلى Environment Variables.
2. Redis إلزامي للـ rate limiting في الإنتاج.
3. PostgreSQL بدل SQLite عند التوسع الكبير.
4. Docker + CI/CD + مراقبة uptime.
"""


def start_bot():
    logger.info("--- STARTING BOT THREAD ---")
    BOT_HEALTH["status"] = "starting"
    BOT_HEALTH["started_at"] = datetime.utcnow().isoformat()
    time.sleep(2)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        logger.info("Importing main from bot...")
        from bot import main
        logger.info("Calling main()...")
        BOT_HEALTH["status"] = "running"
        main()
    except Exception as e:
        BOT_HEALTH["status"] = "error"
        BOT_HEALTH["last_error"] = str(e)
        logger.error(f"CRITICAL ERROR IN BOT THREAD: {e}", exc_info=True)
    finally:
        loop.close()

# Start bot thread
threading.Thread(target=start_bot, daemon=True, name="telegram-bot-thread").start()

CUSTOM_CSS = """
.gradio-container {
    max-width: 1180px !important;
    margin: auto !important;
}
.dashboard-hero {
    border: 1px solid rgba(120, 120, 120, 0.22);
    border-radius: 24px;
    padding: 28px;
    background: linear-gradient(135deg, rgba(22, 163, 74, 0.12), rgba(14, 116, 144, 0.10));
}
"""

with gr.Blocks(css=CUSTOM_CSS, title="X Downloader Control Center") as demo:
    gr.Markdown(
        """
<div class="dashboard-hero">

# 🤖 X Downloader Control Center
لوحة مراقبة وتشغيل للبوت: حالة السيرفر، بيانات الاستخدام، الكاش، الإعدادات، وخطة التطوير.

</div>
        """
    )

    with gr.Tab("📊 Live Overview"):
        refresh_btn = gr.Button("🔄 Refresh Dashboard", variant="primary")
        overview_md = gr.Markdown()
        error_md = gr.Markdown()
        with gr.Row():
            top_platforms_df = gr.Dataframe(label="Top Platforms", interactive=False)
            recent_downloads_df = gr.Dataframe(label="Recent Downloads", interactive=False)

    with gr.Tab("⚙️ Runtime Config"):
        config_md = gr.Markdown()
        gr.Markdown(
            """
> ملاحظة أمان: لا تعرض هذه اللوحة قيمة التوكن نفسها. الأفضل وضع `TELEGRAM_BOT_TOKEN` في متغيرات البيئة وعدم الاعتماد على `token.txt` في الإنتاج.
            """
        )

    with gr.Tab("🧭 Development Plan"):
        gr.Markdown(render_development_plan())

    refresh_btn.click(
        render_overview,
        outputs=[overview_md, top_platforms_df, recent_downloads_df, config_md, error_md],
    )
    demo.load(
        render_overview,
        outputs=[overview_md, top_platforms_df, recent_downloads_df, config_md, error_md],
    )

if __name__ == "__main__":
    logger.info("Launching Gradio app...")
    demo.launch(server_name="0.0.0.0", server_port=7860)
