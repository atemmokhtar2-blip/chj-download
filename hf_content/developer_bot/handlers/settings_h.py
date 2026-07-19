import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import log_action
from config.settings import OWNER_ID, MAIN_DB_PATH, BACKUPS_DIR, LOGS_DIR

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def settings_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 مسارات النظام",       callback_data="dv_cfg_paths")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية",   callback_data="dv_menu")],
    ])


async def show_settings(query, context):
    await query.edit_message_text(
        "⚙️ <b>الإعدادات</b>\n\nاختر القسم:",
        parse_mode="HTML",
        reply_markup=settings_kb(),
    )


async def show_paths(query, context):
    uid = query.from_user.id
    log_action(uid, "view_paths", "", "ok")

    text = (
        "📂 <b>مسارات النظام</b>\n\n"
        f"🗂 المجلد الجذر:\n<code>{_root}</code>\n\n"
        f"💾 قاعدة البيانات:\n<code>{MAIN_DB_PATH}</code>\n\n"
        f"📦 النسخ الاحتياطية:\n<code>{BACKUPS_DIR}</code>\n\n"
        f"📜 السجلات:\n<code>{LOGS_DIR}</code>\n\n"
        f"👤 معرّف المالك: <code>{OWNER_ID}</code>"
    )
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 الإعدادات", callback_data="dv_settings")]
        ]),
    )
