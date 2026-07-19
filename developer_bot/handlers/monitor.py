import os
import sqlite3
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import log_action
from utils.logger import error_logger

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAIN_DB   = os.path.join(_root, "database", "bot.db")
DEV_DB    = os.path.join(_root, "database", "developer.db")
LOGS_DIR  = os.path.join(_root, "logs")
TEMP_DIR  = os.path.join(_root, "temp")
BACKUPS_DIR = os.path.join(_root, "backups")


def _db_size(path: str) -> int:
    return os.path.getsize(path) if os.path.exists(path) else 0


def _dir_size(path: str) -> int:
    total = 0
    if os.path.isdir(path):
        for dirpath, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    return total


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    elif b < 1024 ** 2:
        return f"{b/1024:.1f} KB"
    elif b < 1024 ** 3:
        return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def _get_stats() -> dict:
    stats = {
        "users": 0, "premium": 0, "downloads": 0,
        "db_size": 0, "storage": 0,
    }
    try:
        conn = sqlite3.connect(MAIN_DB)
        conn.row_factory = sqlite3.Row
        stats["users"]     = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        stats["premium"]   = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_premium=1 OR vip_until > datetime('now')"
        ).fetchone()[0]
        stats["downloads"] = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        conn.close()
    except Exception as e:
        error_logger.error("Monitor stats error: %s", e)

    stats["db_size"]  = _db_size(MAIN_DB) + _db_size(DEV_DB)
    stats["storage"]  = _dir_size(TEMP_DIR) + _dir_size(BACKUPS_DIR)
    return stats


async def show_monitor(query, context):
    uid = query.from_user.id
    s = _get_stats()
    log_action(uid, "monitor_view", "", "ok")

    text = (
        "📊 <b>مراقبة النظام</b>\n\n"
        f"👥 إجمالي المستخدمين:   <b>{s['users']:,}</b>\n"
        f"⭐ المستخدمون المميزون: <b>{s['premium']:,}</b>\n"
        f"📥 إجمالي التنزيلات:   <b>{s['downloads']:,}</b>\n\n"
        f"💾 حجم قاعدة البيانات: <b>{_fmt_size(s['db_size'])}</b>\n"
        f"📦 مساحة التخزين المؤقت: <b>{_fmt_size(s['storage'])}</b>\n\n"
        "🟢 الخدمات النشطة: الخادم الرئيسي، بوت الدعم، بوت الأدمن"
    )
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 تحديث", callback_data="dv_monitor")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="dv_menu")],
        ]),
    )
