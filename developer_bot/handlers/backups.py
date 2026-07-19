import os
import shutil
import zipfile
import io
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from database.db import log_action, save_backup, get_backups, delete_backup_record
from utils.logger import action_logger, error_logger
from config.settings import BACKUPS_DIR, PROTECTED_PATHS

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_project = os.path.join(_root)


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    elif b < 1024 ** 2:
        return f"{b / 1024:.1f} KB"
    elif b < 1024 ** 3:
        return f"{b / 1024 ** 2:.1f} MB"
    return f"{b / 1024 ** 3:.2f} GB"


def backups_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ إنشاء نسخة احتياطية",  callback_data="dv_bkp_create")],
        [InlineKeyboardButton("📂 عرض النسخ",            callback_data="dv_bkp_list")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية",     callback_data="dv_menu")],
    ])


async def show_backups_menu(query, context):
    await query.edit_message_text(
        "💾 <b>النسخ الاحتياطية</b>\n\nاختر الإجراء:",
        parse_mode="HTML",
        reply_markup=backups_kb(),
    )


async def create_backup(query, context):
    uid = query.from_user.id
    await query.edit_message_text("⏳ <b>جارٍ إنشاء النسخة الاحتياطية…</b>", parse_mode="HTML")

    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"backup_{ts}"
        backup_path = os.path.join(BACKUPS_DIR, f"{backup_name}.zip")

        exclude_dirs = {".git", "__pycache__", "temp", "backups", ".venv"}
        buf = io.BytesIO()
        size = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirnames, files in os.walk(_root):
                dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
                for filename in files:
                    full = os.path.join(dirpath, filename)
                    arcname = os.path.relpath(full, _root)
                    try:
                        zf.write(full, arcname)
                        size += os.path.getsize(full)
                    except (OSError, PermissionError):
                        pass
        buf.seek(0)
        with open(backup_path, "wb") as f:
            f.write(buf.read())

        zip_size = os.path.getsize(backup_path)
        save_backup(backup_name, backup_path, zip_size, "manual")
        log_action(uid, "create_backup", backup_name, "ok")
        action_logger.info("Backup created: %s (%s)", backup_name, _fmt_size(zip_size))

        buf.seek(0)
        with open(backup_path, "rb") as f:
            data = f.read()

        await query.message.reply_document(
            document=InputFile(io.BytesIO(data), filename=f"{backup_name}.zip"),
            caption=(
                f"✅ <b>تم إنشاء النسخة الاحتياطية</b>\n\n"
                f"📦 الاسم: <code>{backup_name}</code>\n"
                f"💾 الحجم: <b>{_fmt_size(zip_size)}</b>"
            ),
            parse_mode="HTML",
        )
        await query.edit_message_text(
            f"✅ <b>تمت النسخة الاحتياطية بنجاح</b>\n\n"
            f"📦 <code>{backup_name}.zip</code>\n"
            f"💾 {_fmt_size(zip_size)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 النسخ الاحتياطية", callback_data="dv_backups")]
            ]),
        )
    except Exception as e:
        error_logger.error("Backup error: %s", e)
        log_action(uid, "create_backup", "", f"error: {e}")
        await query.edit_message_text(
            f"❌ <b>فشل إنشاء النسخة الاحتياطية</b>\n\n<code>{e}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 النسخ الاحتياطية", callback_data="dv_backups")]
            ]),
        )


async def show_backups_list(query, context):
    backups = get_backups()
    if not backups:
        text = "💾 <b>النسخ الاحتياطية</b>\n\nلا توجد نسخ احتياطية محفوظة."
        rows = [[InlineKeyboardButton("🔙 رجوع", callback_data="dv_backups")]]
    else:
        lines = []
        rows = []
        for b in backups[:10]:
            lines.append(
                f"📦 <code>{b['name']}</code>\n"
                f"   💾 {_fmt_size(b['size_bytes'])}  |  📅 {b['created_at'][:16]}"
            )
            rows.append([InlineKeyboardButton(
                f"🗑 حذف {b['name'][:20]}",
                callback_data=f"dv_bkp_del_confirm_{b['id']}",
            )])
        text = "💾 <b>النسخ الاحتياطية المحفوظة</b>\n\n" + "\n\n".join(lines)
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="dv_backups")])

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def confirm_delete_backup(query, context, backup_id: int):
    backups = get_backups()
    b = next((x for x in backups if x["id"] == backup_id), None)
    if not b:
        await query.answer("النسخة غير موجودة", show_alert=True)
        return
    await query.edit_message_text(
        f"⚠️ <b>تأكيد الحذف</b>\n\nهل تريد حذف النسخة:\n<code>{b['name']}</code>?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ تأكيد الحذف", callback_data=f"dv_bkp_del_do_{backup_id}"),
                InlineKeyboardButton("❌ إلغاء",        callback_data="dv_bkp_list"),
            ]
        ]),
    )


async def do_delete_backup(query, context, backup_id: int):
    uid = query.from_user.id
    backups = get_backups()
    b = next((x for x in backups if x["id"] == backup_id), None)
    if not b:
        await query.answer("النسخة غير موجودة", show_alert=True)
        return

    # Safety: never delete protected paths
    path = b["path"]
    for prot in PROTECTED_PATHS:
        if path.startswith(prot):
            await query.answer("🚫 لا يمكن حذف هذا الملف (محمي)", show_alert=True)
            return

    try:
        if os.path.exists(path):
            os.remove(path)
        delete_backup_record(backup_id)
        log_action(uid, "delete_backup", b["name"], "ok")
    except Exception as e:
        error_logger.error("Delete backup error: %s", e)
        await query.answer(f"فشل الحذف: {e}", show_alert=True)
        return

    await query.edit_message_text(
        f"🗑 <b>تم حذف النسخة الاحتياطية</b>\n\n<code>{b['name']}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 النسخ الاحتياطية", callback_data="dv_bkp_list")]
        ]),
    )
