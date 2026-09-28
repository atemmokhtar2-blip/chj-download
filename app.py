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


def render_health_center():
    try:
        from services.runtime_diagnostics import render_diagnostics_html
        return render_diagnostics_html().replace("\n", "  \n")
    except Exception as exc:
        return f"❌ Health diagnostics unavailable: `{exc}`"


def render_engine_arsenal():
    try:
        from services.engine_maintenance import get_ytdlp_version
        from services.engines import list_engines
        version = get_ytdlp_version()
        engines = list_engines()
    except Exception as exc:
        return f"⚠️ Engine diagnostics unavailable: `{exc}`"

    engine_lines = "\n".join(f"- ✅ **{engine['name']}** — ready" for engine in engines)
    return f"""
## 🧠 Engine Arsenal

- **yt-dlp version:** `{version}`
- **Active engines:** `{len(engines)}`
- **Audio-only pipeline:** MP3 extraction via FFmpeg post-processing.
- **Video pipeline:** best quality + selectable qualities when available.
- **Reliability:** retry, fragment retry, cache delivery, concurrency guard, rate limits.

### Engines
{engine_lines}

### Recommended production switches
- Add `TELEGRAM_BOT_TOKEN` as an environment variable.
- Add `STORAGE_CHANNEL_ID` for durable Telegram Media Vault cache.
- Add `REDIS_URL` for distributed rate limits when scaling.
- Add cookies/proxy only for platforms that require them.
"""


def render_content_intelligence():
    return """
## 🧠 Smart Content Intelligence

ميزة جديدة تجعل البوت لا يكتفي بإظهار زر تحميل فقط، بل يحلل الرابط قبل التحميل ويعرض للمستخدم بطاقة ذكية:

- **Content Score /99** لتقييم جاهزية الرابط وجودته.
- **أفضل جودة متاحة** بناءً على metadata من yt-dlp.
- **تقدير الحجم المتوقع** عندما توفره المنصة.
- **Size Risk** لتجنب فشل Telegram بسبب الملفات الكبيرة.
- **Badges ذكية** مثل Viral / Short-form / HD / Album-ready / Audio optimized.

### لماذا هذه ميزة عالمية؟
بدل تجربة تنزيل عمياء، المستخدم يعرف قبل الضغط هل الأفضل تحميل فيديو، جودة أقل، أو MP3. هذا يقلل الفشل، يسرّع الاختيار، ويعطي إحساس Premium لا تقدمه معظم بوتات التحميل.

### المرحلة القادمة
- توصية تلقائية بزر واحد: “Smart Download”.
- تحليل احتمالية Watermark حسب المنصة.
- تقدير زمن التحميل بناءً على حجم الملف وحالة السيرفر.
- تنبيه أدمن عندما تزيد نسبة فشل منصة معينة.
"""


def render_development_plan():
    return """
## 🚀 خطة تحويل X Downloader لمنتج عالمي

### تم تنفيذه الآن
- واجهة Control Center جديدة بتصميم احترافي ومناسب للإطلاق.
- زر **MP3 / صوت فقط** يظهر مع الفيديوهات مباشرة من أزرار Telegram.
- لوحة Live metrics لعدد المستخدمين، التحميلات، الكاش، وأحدث العمليات.
- تنبيه أمان واضح يمنع تسريب التوكنات داخل الواجهة أو السجلات.
- **Smart Content Intelligence**: تقييم الرابط، أفضل جودة، تقدير الحجم، ومخاطرة Telegram قبل التحميل.

### Bot UX — الأولوية القادمة
1. اختيار واضح: فيديو أفضل جودة / جودة محددة / MP3 فقط / صورة / ألبوم.
2. رسائل تقدم مفهومة: تحليل الرابط → اختيار الصيغة → تحميل → معالجة → رفع.
3. زر إلغاء للتحميلات الطويلة، ورسائل فشل بشرح قابل للتنفيذ.
4. قوالب عربية وإنجليزية قصيرة وقوية لكل المنصات.

### Scale & Reliability
1. Redis queue للتحميلات الثقيلة وحدود استخدام دقيقة لكل مستخدم.
2. Media Vault دائم لتسليم الملفات من الكاش بدل إعادة التنزيل.
3. تحديث yt-dlp تلقائي/مراقب مع fallback engines لكل منصة.
4. مراقبة أخطاء فورية للأدمن عبر Telegram.

### Monetization & Growth
1. خطط مجانية/مدفوعة ونقاط يومية وإحالات.
2. صفحة هبوط عامة تعرض المنصات المدعومة والمزايا.
3. لوحة Admin لإدارة الحظر، البث، الإعدادات، والاشتراكات.
4. تجهيز Docker + CI/CD + قاعدة بيانات PostgreSQL عند التوسع.
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
:root {
    --xd-bg: #07110d;
    --xd-panel: rgba(12, 28, 21, 0.78);
    --xd-panel-strong: rgba(18, 44, 34, 0.94);
    --xd-border: rgba(144, 238, 188, 0.18);
    --xd-accent: #24d18f;
    --xd-accent-2: #67e8f9;
    --xd-text: #eefcf5;
    --xd-muted: #a8c7ba;
}
.gradio-container {
    max-width: 1240px !important;
    margin: auto !important;
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif !important;
    background:
        radial-gradient(circle at 15% 5%, rgba(36, 209, 143, 0.18), transparent 34%),
        radial-gradient(circle at 85% 0%, rgba(103, 232, 249, 0.16), transparent 30%),
        var(--xd-bg) !important;
    color: var(--xd-text) !important;
}
.dashboard-hero, .xd-card {
    border: 1px solid var(--xd-border);
    border-radius: 28px;
    padding: 28px;
    background: linear-gradient(145deg, var(--xd-panel-strong), rgba(7, 17, 13, 0.72));
    box-shadow: 0 24px 70px rgba(0, 0, 0, 0.28);
}
.dashboard-hero h1 { letter-spacing: -0.04em; font-size: 42px !important; }
.dashboard-hero p, .xd-card p, .xd-card li { color: var(--xd-muted); font-size: 16px; line-height: 1.75; }
.xd-badges { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
.xd-badge { border: 1px solid var(--xd-border); color: var(--xd-text); background: rgba(36, 209, 143, 0.10); padding: 8px 12px; border-radius: 999px; font-size: 13px; }
.xd-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin: 18px 0 8px; }
.xd-mini { border: 1px solid var(--xd-border); border-radius: 20px; padding: 16px; background: rgba(255,255,255,0.04); }
.xd-mini strong { display: block; font-size: 22px; color: var(--xd-accent); }
button.primary { background: var(--xd-accent) !important; color: #03100b !important; border-radius: 14px !important; font-weight: 800 !important; }
@media (max-width: 780px) { .xd-grid { grid-template-columns: 1fr; } .dashboard-hero h1 { font-size: 30px !important; } }
"""

with gr.Blocks(css=CUSTOM_CSS, title="X Downloader Control Center") as demo:
    gr.Markdown(
        """
<div class="dashboard-hero">

# X Downloader Control Center
لوحة قيادة عالمية لبوت تحميل الوسائط: فيديو، صور، ألبومات، و **MP3 صوت فقط** من المنصات الكبرى مع كاش ذكي ومراقبة مباشرة.

<div class="xd-badges">
  <span class="xd-badge">YouTube / Shorts</span>
  <span class="xd-badge">TikTok No Watermark</span>
  <span class="xd-badge">Instagram Reels</span>
  <span class="xd-badge">SoundCloud Audio</span>
  <span class="xd-badge">MP3 Audio Only</span>
  <span class="xd-badge">Smart Cache</span>
</div>

<div class="xd-grid">
  <div class="xd-mini"><strong>13+</strong>Supported global platforms</div>
  <div class="xd-mini"><strong>MP3</strong>زر صوت فقط يظهر مع كل فيديو</div>
  <div class="xd-mini"><strong>Live</strong>Metrics, cache, limits and health</div>
</div>

</div>
        """
    )

    with gr.Tab("📊 Live Command Center"):
        refresh_btn = gr.Button("🔄 Refresh Dashboard", variant="primary")
        overview_md = gr.Markdown(elem_classes=["xd-card"])
        error_md = gr.Markdown(elem_classes=["xd-card"])
        with gr.Row():
            top_platforms_df = gr.Dataframe(label="Top Platforms", interactive=False)
            recent_downloads_df = gr.Dataframe(label="Recent Downloads", interactive=False)

    with gr.Tab("⚙️ Runtime & Security"):
        config_md = gr.Markdown(elem_classes=["xd-card"])
        gr.Markdown(
            """
<div class="xd-card">

### Security note
لا تعرض هذه اللوحة قيمة التوكن نفسها. استخدم `TELEGRAM_BOT_TOKEN` كمتغير بيئة، ولا تلصق أي GitHub أو Telegram token في الشات أو الكود.

</div>
            """
        )

    with gr.Tab("🩺 Health Center"):
        health_refresh_btn = gr.Button("🔄 Run Healthcheck", variant="primary")
        health_md = gr.Markdown(render_health_center(), elem_classes=["xd-card"])

    with gr.Tab("🧠 Engine Arsenal"):
        engine_refresh_btn = gr.Button("🔄 Refresh Engine Status", variant="primary")
        engine_md = gr.Markdown(render_engine_arsenal(), elem_classes=["xd-card"])

    with gr.Tab("✨ Content Intelligence"):
        gr.Markdown(render_content_intelligence(), elem_classes=["xd-card"])

    with gr.Tab("🚀 Global Growth Plan"):
        gr.Markdown(render_development_plan(), elem_classes=["xd-card"])

    engine_refresh_btn.click(render_engine_arsenal, outputs=[engine_md])
    health_refresh_btn.click(render_health_center, outputs=[health_md])

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
