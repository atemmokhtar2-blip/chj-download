import os
import zipfile
import shutil
import hashlib
import io
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from database.db import log_action, save_update_record, get_updates, save_backup
from utils.logger import action_logger, error_logger
from config.settings import TEMP_DIR, BACKUPS_DIR, PROTECTED_PATHS

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROTECTED_NAMES = {
    ".env", "bot.db", "developer.db", "support.db",
    "settings.py", "bot.py",
}

STATE_UPLOAD = "dv_update_upload"
STATE_CONFIRM = "dv_update_confirm"


def updates_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 رفع تحديث ZIP",       callback_data="dv_upd_upload")],
        [InlineKeyboardButton("📜 سجل الإصدارات",       callback_data="dv_upd_history")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية",   callback_data="dv_menu")],
    ])


async def show_updates_menu(query, context):
    await query.edit_message_text(
        "📦 <b>إدارة التحديثات</b>\n\n"
        "يمكنك رفع ملف ZIP يحتوي على ملفات التحديث.\n"
        "سيتم فحص الملفات قبل التطبيق وإنشاء نسخة احتياطية تلقائياً.",
        parse_mode="HTML",
        reply_markup=updates_kb(),
    )


async def prompt_upload(query, context):
    context.user_data["dv_state"] = STATE_UPLOAD
    await query.edit_message_text(
        "📤 <b>رفع تحديث ZIP</b>\n\n"
        "أرسل ملف ZIP الذي يحتوي على ملفات التحديث.\n\n"
        "⚠️ الملفات المحمية لن يتم الكتابة عليها:\n"
        "<code>.env، bot.db، settings.py</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data="dv_updates")]
        ]),
    )


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _analyze_zip(zip_path: str) -> dict:
    result = {"files": [], "protected": [], "new": [], "modified": [], "conflicts": [], "total": 0}
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        result["total"] = len(names)
        for name in names:
            if name.endswith("/"):
                continue
            basename = os.path.basename(name)
            dest = os.path.join(_root, name)
            if basename in PROTECTED_NAMES:
                result["protected"].append(name)
                continue
            if os.path.exists(dest):
                result["modified"].append(name)
            else:
                result["new"].append(name)
            result["files"].append(name)
    return result


async def handle_zip_upload(update, context):
    uid = update.effective_user.id
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".zip"):
        await update.message.reply_text("❌ يرجى إرسال ملف ZIP فقط.")
        return

    await update.message.reply_text("🔍 <b>جارٍ تحليل التحديث…</b>", parse_mode="HTML")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(TEMP_DIR, f"update_{ts}.zip")

    try:
        file_obj = await doc.get_file()
        await file_obj.download_to_drive(zip_path)

        analysis = _analyze_zip(zip_path)
        context.user_data["dv_update_zip"] = zip_path
        context.user_data["dv_update_analysis"] = analysis
        context.user_data["dv_state"] = STATE_CONFIRM

        prot_text = ""
        if analysis["protected"]:
            prot_text = "\n\n🔐 <b>ملفات محمية (لن تُعدَّل):</b>\n" + "\n".join(
                f"• <code>{p}</code>" for p in analysis["protected"][:5]
            )

        new_text = ""
        if analysis["new"]:
            new_text = "\n\n🆕 <b>ملفات جديدة:</b>\n" + "\n".join(
                f"• <code>{n}</code>" for n in analysis["new"][:8]
            )

        mod_text = ""
        if analysis["modified"]:
            mod_text = "\n\n✏️ <b>ملفات سيتم تعديلها:</b>\n" + "\n".join(
                f"• <code>{m}</code>" for m in analysis["modified"][:8]
            )

        text = (
            f"📋 <b>نتيجة فحص التحديث</b>\n\n"
            f"📦 إجمالي الملفات: <b>{analysis['total']}</b>\n"
            f"🆕 ملفات جديدة: <b>{len(analysis['new'])}</b>\n"
            f"✏️ ملفات معدَّلة: <b>{len(analysis['modified'])}</b>\n"
            f"🔐 ملفات محمية: <b>{len(analysis['protected'])}</b>"
            f"{prot_text}{new_text}{mod_text}\n\n"
            "هل تريد تطبيق التحديث؟ سيتم إنشاء نسخة احتياطية أولاً."
        )
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🚀 تطبيق التحديث",  callback_data="dv_upd_apply"),
                    InlineKeyboardButton("❌ إلغاء",          callback_data="dv_updates"),
                ]
            ]),
        )
        log_action(uid, "upload_update", doc.file_name, "analyzed")
    except Exception as e:
        error_logger.error("ZIP upload error: %s", e)
        log_action(uid, "upload_update", doc.file_name, f"error: {e}")
        await update.message.reply_text(f"❌ فشل تحليل الملف: <code>{e}</code>", parse_mode="HTML")


async def apply_update(query, context):
    uid = query.from_user.id
    zip_path = context.user_data.get("dv_update_zip")
    analysis = context.user_data.get("dv_update_analysis")

    if not zip_path or not os.path.exists(zip_path):
        await query.answer("لا يوجد تحديث معلق", show_alert=True)
        return

    await query.edit_message_text("⏳ <b>جارٍ إنشاء نسخة احتياطية ثم تطبيق التحديث…</b>", parse_mode="HTML")

    try:
        # Step 1: Auto backup
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"pre_update_{ts}"
        backup_path = os.path.join(BACKUPS_DIR, f"{backup_name}.zip")
        exclude_dirs = {".git", "__pycache__", "temp", "backups", ".venv"}
        with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirnames, files in os.walk(_root):
                dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
                for filename in files:
                    full = os.path.join(dirpath, filename)
                    try:
                        zf.write(full, os.path.relpath(full, _root))
                    except (OSError, PermissionError):
                        pass
        bkp_size = os.path.getsize(backup_path)
        save_backup(backup_name, backup_path, bkp_size, "pre-update auto")

        # Step 2: Apply files
        applied = 0
        skipped = 0
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in analysis["files"]:
                dest = os.path.join(_root, name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(name) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                applied += 1

        fn = os.path.basename(zip_path)
        save_update_record(ts, fn, "applied")
        log_action(uid, "apply_update", fn, f"applied={applied}")
        action_logger.info("Update applied: %s (%d files)", fn, applied)

        context.user_data.pop("dv_update_zip", None)
        context.user_data.pop("dv_update_analysis", None)
        context.user_data.pop("dv_state", None)

        await query.edit_message_text(
            f"✅ <b>تم تطبيق التحديث بنجاح</b>\n\n"
            f"📁 الملفات المطبَّقة: <b>{applied}</b>\n"
            f"💾 نسخة احتياطية: <code>{backup_name}</code>\n\n"
            "يُنصح بإعادة تشغيل البوتات.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 إعادة تشغيل الكل", callback_data="dv_svc_restart_all")],
                [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="dv_menu")],
            ]),
        )
    except Exception as e:
        error_logger.error("Apply update error: %s", e)
        log_action(uid, "apply_update", "", f"error: {e}")
        await query.edit_message_text(
            f"❌ <b>فشل تطبيق التحديث</b>\n\n<code>{e}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="dv_updates")]
            ]),
        )


async def show_update_history(query, context):
    updates = get_updates()
    if not updates:
        text = "📜 <b>سجل الإصدارات</b>\n\nلا توجد تحديثات مسجَّلة."
    else:
        lines = []
        for u in updates[:15]:
            lines.append(
                f"• <code>{u['filename']}</code>\n"
                f"  📅 {u['applied_at'][:16]}  |  ✅ {u['status']}"
            )
        text = "📜 <b>سجل الإصدارات</b>\n\n" + "\n\n".join(lines)

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 إدارة التحديثات", callback_data="dv_updates")]
        ]),
    )
