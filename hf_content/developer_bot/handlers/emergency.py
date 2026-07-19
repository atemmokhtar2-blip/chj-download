"""
🚑 وضع الطوارئ — Emergency Recovery
"""
import os
import sys
import signal
import subprocess
import zipfile
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
import io
from database.db import log_action, get_backups
from utils.logger import action_logger, error_logger

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOTS = {
    "main":    os.path.join(_root, "bot.py"),
    "support": os.path.join(_root, "support_bot", "bot.py"),
    "admin":   os.path.join(_root, "admin_bot",   "bot.py"),
}
BOT_LABELS = {
    "main": "البوت الأساسي",
    "support": "بوت الدعم",
    "admin": "بوت الأدمن",
}


def emergency_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("♻️ استعادة آخر نسخة احتياطية",   callback_data="dv_em_restore_last")],
        [InlineKeyboardButton("🔄 إعادة تشغيل جميع البوتات",    callback_data="dv_em_restart_all")],
        [InlineKeyboardButton("🩺 فحص المشروع الطارئ",          callback_data="dv_health")],
        [InlineKeyboardButton("📜 آخر سجلات الأخطاء",           callback_data="dv_err_view_main_err")],
        [InlineKeyboardButton("💾 نسخة احتياطية طارئة",         callback_data="dv_em_backup_now")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية",            callback_data="dv_menu")],
    ])


async def show_emergency_menu(query, context):
    await query.edit_message_text(
        "🚑 <b>وضع الطوارئ</b>\n\n"
        "⚠️ هذا القسم للتعامل مع الأعطال الحرجة.\n"
        "استخدمه بحذر شديد — جميع الإجراءات تُسجَّل.\n\n"
        "اختر الإجراء الطارئ:",
        parse_mode="HTML",
        reply_markup=emergency_kb(),
    )


async def restore_last_backup(query, context):
    uid = query.from_user.id
    backups = get_backups()
    if not backups:
        await query.edit_message_text(
            "❌ <b>لا توجد نسخ احتياطية متاحة للاستعادة.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 إنشاء نسخة احتياطية", callback_data="dv_bkp_create")],
                [InlineKeyboardButton("🔙 وضع الطوارئ", callback_data="dv_emergency")],
            ]),
        )
        return

    latest = backups[0]
    await query.edit_message_text(
        f"⚠️ <b>تأكيد الاستعادة الطارئة</b>\n\n"
        f"📦 النسخة: <code>{latest['name']}</code>\n"
        f"📅 التاريخ: {latest['created_at'][:16]}\n\n"
        "سيتم استبدال ملفات المشروع الحالية بهذه النسخة.\n"
        "<b>هل أنت متأكد تمامًا؟</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ تأكيد الاستعادة", callback_data=f"dv_em_restore_do_{latest['id']}")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="dv_emergency")],
        ]),
    )


async def do_restore_backup(query, context, backup_id: int):
    uid = query.from_user.id
    backups = get_backups()
    b = next((x for x in backups if x["id"] == backup_id), None)
    if not b or not os.path.exists(b["path"]):
        await query.edit_message_text(
            "❌ النسخة الاحتياطية غير موجودة على القرص.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 وضع الطوارئ", callback_data="dv_emergency")]
            ]),
        )
        return

    await query.edit_message_text("⏳ <b>جارٍ استعادة النسخة الاحتياطية…</b>", parse_mode="HTML")

    try:
        protected = {".env", "database", "developer.db", "bot.db"}
        restored = 0
        skipped = 0
        with zipfile.ZipFile(b["path"], "r") as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                parts = name.split("/")
                if any(p in protected for p in parts):
                    skipped += 1
                    continue
                dest = os.path.join(_root, name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(name) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                restored += 1

        log_action(uid, "emergency_restore", b["name"], f"restored={restored},skipped={skipped}")
        action_logger.info("Emergency restore: %s — %d files restored", b["name"], restored)

        await query.edit_message_text(
            f"✅ <b>تمت الاستعادة الطارئة بنجاح</b>\n\n"
            f"📁 ملفات مُستعادة: <b>{restored}</b>\n"
            f"🔐 ملفات محمية (تم تخطيها): <b>{skipped}</b>\n\n"
            "يُنصح بإعادة تشغيل جميع البوتات الآن.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 إعادة تشغيل الكل", callback_data="dv_em_restart_all")],
                [InlineKeyboardButton("🔙 وضع الطوارئ", callback_data="dv_emergency")],
            ]),
        )
    except Exception as e:
        error_logger.error("Emergency restore error: %s", e, exc_info=True)
        log_action(uid, "emergency_restore", b["name"], f"error: {e}")
        await query.edit_message_text(
            f"❌ <b>فشلت الاستعادة</b>\n\n<code>{e}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 وضع الطوارئ", callback_data="dv_emergency")]
            ]),
        )


def _find_pid(script_path: str) -> list:
    try:
        result = subprocess.check_output(["pgrep", "-f", script_path], text=True).strip()
        return [int(p) for p in result.split() if p.isdigit()]
    except subprocess.CalledProcessError:
        return []


async def emergency_restart_all(query, context):
    uid = query.from_user.id
    await query.edit_message_text("🔄 <b>جارٍ إعادة تشغيل جميع البوتات…</b>", parse_mode="HTML")

    results = []
    for key, script in BOTS.items():
        pids = _find_pid(script)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        subprocess.Popen(
            [sys.executable, script],
            cwd=_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        results.append(f"✅ {BOT_LABELS[key]}")

    log_action(uid, "emergency_restart_all", "", "ok")
    await query.edit_message_text(
        "🔄 <b>تمت إعادة تشغيل جميع البوتات</b>\n\n" + "\n".join(results),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🩺 فحص المشروع", callback_data="dv_health")],
            [InlineKeyboardButton("🔙 وضع الطوارئ", callback_data="dv_emergency")],
        ]),
    )


async def emergency_backup(query, context):
    """Quick emergency backup with minimal output."""
    uid = query.from_user.id
    await query.edit_message_text("⏳ <b>جارٍ إنشاء نسخة احتياطية طارئة…</b>", parse_mode="HTML")

    try:
        from database.db import save_backup
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"emergency_{ts}"
        path = os.path.join(_root, "backups", f"{name}.zip")
        exclude = {".git", "__pycache__", "temp", "backups", ".venv"}

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for dp, dirs, files in os.walk(_root):
                dirs[:] = [d for d in dirs if d not in exclude]
                for fname in files:
                    full = os.path.join(dp, fname)
                    try:
                        zf.write(full, os.path.relpath(full, _root))
                    except (OSError, PermissionError):
                        pass

        size = os.path.getsize(path)
        save_backup(name, path, size, "emergency")
        log_action(uid, "emergency_backup", name, "ok")

        def fmt(b):
            return f"{b/1024/1024:.1f} MB" if b > 1024*1024 else f"{b/1024:.1f} KB"

        await query.edit_message_text(
            f"✅ <b>تمت النسخة الاحتياطية الطارئة</b>\n\n"
            f"📦 <code>{name}.zip</code>\n"
            f"💾 {fmt(size)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 وضع الطوارئ", callback_data="dv_emergency")]
            ]),
        )
    except Exception as e:
        error_logger.error("Emergency backup error: %s", e)
        await query.edit_message_text(
            f"❌ فشلت النسخة الطارئة: <code>{e}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="dv_emergency")]
            ]),
        )
