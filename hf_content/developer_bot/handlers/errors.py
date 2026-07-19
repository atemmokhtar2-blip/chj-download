import os
import io
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from database.db import log_action
from utils.logger import error_logger

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOGS_DIR = os.path.join(_root, "logs")

LOG_FILES = {
    "main_err":   "error.log",
    "admin_err":  "admin_errors.log",
    "dev_err":    "dev_errors.log",
    "dev_sys":    "dev_system.log",
    "dev_act":    "dev_actions.log",
    "admin_sys":  "admin_system.log",
    "admin_act":  "admin_actions.log",
}

LABEL_MAP = {
    "main_err":  "أخطاء البوت الرئيسي",
    "admin_err": "أخطاء بوت الأدمن",
    "dev_err":   "أخطاء بوت المطوّر",
    "dev_sys":   "سجل النظام — المطوّر",
    "dev_act":   "سجل الإجراءات — المطوّر",
    "admin_sys": "سجل النظام — الأدمن",
    "admin_act": "سجل الإجراءات — الأدمن",
}


def _read_tail(path: str, lines: int = 30) -> str:
    if not os.path.exists(path):
        return "الملف غير موجود."
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:]) or "الملف فارغ."
    except Exception as e:
        return f"خطأ في القراءة: {e}"


def errors_menu_kb():
    rows = []
    rows.append([InlineKeyboardButton("📜 آخر الأخطاء (الرئيسي)", callback_data="dv_err_view_main_err")])
    rows.append([InlineKeyboardButton("⚠️ أخطاء بوت الأدمن",      callback_data="dv_err_view_admin_err")])
    rows.append([InlineKeyboardButton("🔍 سجل المطوّر",             callback_data="dv_err_view_dev_sys")])
    rows.append([InlineKeyboardButton("📋 سجل الإجراءات",           callback_data="dv_err_view_dev_act")])
    rows.append([InlineKeyboardButton("📤 تصدير جميع السجلات",      callback_data="dv_err_export")])
    rows.append([InlineKeyboardButton("🧹 تنظيف السجلات",           callback_data="dv_err_clear_confirm")])
    rows.append([InlineKeyboardButton("🔙 القائمة الرئيسية",        callback_data="dv_menu")])
    return InlineKeyboardMarkup(rows)


async def show_errors_menu(query, context):
    await query.edit_message_text(
        "🚨 <b>مركز الأخطاء والسجلات</b>\n\nاختر السجل الذي تريد عرضه:",
        parse_mode="HTML",
        reply_markup=errors_menu_kb(),
    )


async def show_log_view(query, context, log_key: str):
    uid = query.from_user.id
    filename = LOG_FILES.get(log_key)
    if not filename:
        await query.answer("سجل غير معروف", show_alert=True)
        return

    path = os.path.join(LOGS_DIR, filename)
    content = _read_tail(path, 25)
    label = LABEL_MAP.get(log_key, log_key)
    log_action(uid, "view_log", log_key, "ok")

    text = f"📜 <b>{label}</b>\n\n<pre>{content[:3800]}</pre>"
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📤 تصدير {label}", callback_data=f"dv_err_dl_{log_key}")],
            [InlineKeyboardButton("🔙 سجل الأخطاء", callback_data="dv_errors")],
        ]),
    )


async def export_log_file(query, context, log_key: str):
    uid = query.from_user.id
    filename = LOG_FILES.get(log_key)
    if not filename:
        await query.answer("سجل غير معروف", show_alert=True)
        return

    path = os.path.join(LOGS_DIR, filename)
    if not os.path.exists(path):
        await query.answer("الملف غير موجود.", show_alert=True)
        return

    try:
        with open(path, "rb") as f:
            data = f.read()
        label = LABEL_MAP.get(log_key, log_key)
        await query.message.reply_document(
            document=InputFile(io.BytesIO(data), filename=filename),
            caption=f"📤 {label}",
        )
        log_action(uid, "export_log", log_key, "ok")
    except Exception as e:
        error_logger.error("Export log error: %s", e)
        await query.answer(f"فشل التصدير: {e}", show_alert=True)


async def export_all_logs(query, context):
    uid = query.from_user.id
    import zipfile, io
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, filename in LOG_FILES.items():
            path = os.path.join(LOGS_DIR, filename)
            if os.path.exists(path):
                zf.write(path, filename)
                count += 1
    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    await query.message.reply_document(
        document=InputFile(io.BytesIO(buf.read()), filename=f"logs_{ts}.zip"),
        caption=f"📦 تم تصدير {count} ملف سجل",
    )
    log_action(uid, "export_all_logs", f"files={count}", "ok")


async def confirm_clear_logs(query, context):
    await query.edit_message_text(
        "⚠️ <b>تنظيف السجلات</b>\n\n"
        "هل أنت متأكد من حذف محتوى جميع ملفات السجلات؟\n"
        "لا يمكن التراجع عن هذا الإجراء.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ تأكيد التنظيف", callback_data="dv_err_clear_do"),
                InlineKeyboardButton("❌ إلغاء", callback_data="dv_errors"),
            ]
        ]),
    )


async def do_clear_logs(query, context):
    uid = query.from_user.id
    cleared = 0
    for filename in LOG_FILES.values():
        path = os.path.join(LOGS_DIR, filename)
        if os.path.exists(path):
            try:
                open(path, "w").close()
                cleared += 1
            except Exception:
                pass
    log_action(uid, "clear_logs", f"cleared={cleared}", "ok")
    await query.edit_message_text(
        f"🧹 <b>تم تنظيف {cleared} ملف سجل بنجاح.</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 مركز الأخطاء", callback_data="dv_errors")]
        ]),
    )
