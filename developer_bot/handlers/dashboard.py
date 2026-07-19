from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from handlers.auth import require_owner
from database.db import init_db

MAIN_MENU_TEXT = (
    "🛠 <b>مركز المطور</b>\n\n"
    "مرحباً بك في لوحة تحكم المطوّر المتقدمة.\n"
    "اختر القسم الذي تريد الوصول إليه:"
)


def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 مساعد التحديث الذكي",  callback_data="dv_ai")],
        [InlineKeyboardButton("📁 مدير الملفات",          callback_data="dv_files")],
        [InlineKeyboardButton("📦 إدارة التحديثات",       callback_data="dv_updates")],
        [InlineKeyboardButton("💾 النسخ الاحتياطية",      callback_data="dv_backups")],
        [InlineKeyboardButton("🔄 إدارة التشغيل",         callback_data="dv_services")],
        [InlineKeyboardButton("📊 مراقبة النظام",          callback_data="dv_monitor")],
        [InlineKeyboardButton("🚨 سجل الأخطاء",           callback_data="dv_errors")],
        [InlineKeyboardButton("🩺 فحص المشروع",           callback_data="dv_health")],
        [InlineKeyboardButton("🚑 وضع الطوارئ",           callback_data="dv_emergency")],
        [InlineKeyboardButton("🔐 الأمان",                callback_data="dv_security")],
        [InlineKeyboardButton("⚙️ الإعدادات",             callback_data="dv_settings")],
    ])


@require_owner
async def start_cmd(update: Update, context):
    init_db()
    await update.message.reply_text(
        MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=main_menu_kb()
    )


@require_owner
async def panel_cmd(update: Update, context):
    await update.message.reply_text(
        MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=main_menu_kb()
    )


async def show_main_menu(query, context):
    await query.edit_message_text(
        MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=main_menu_kb()
    )
