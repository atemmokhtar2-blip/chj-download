import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ContextTypes

from database.users import get_user, create_user, update_user
from middlewares.auth import is_admin
from locales import t

logger = logging.getLogger(__name__)


def get_main_keyboard(lang: str = "ar", user_id: int = None) -> ReplyKeyboardMarkup:
    """Two permanent Arabic shortcuts: Download + Settings."""
    return ReplyKeyboardMarkup(
        [[t("ar", "menu_download"), t("ar", "menu_settings")]],
        resize_keyboard=True,
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)

    if not db_user:
        create_user(
            user_id=user.id,
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            language="ar",
        )
        db_user = get_user(user.id)

    # Force Arabic for both new and existing users, including accounts that
    # previously selected English.
    update_user(user.id, language="ar")

    if db_user and db_user.get("is_banned"):
        await update.message.reply_text(t("ar", "banned"))
        return

    if is_admin(user.id):
        from handlers.admin import admin_command
        await admin_command(update, context)
        return

    await update.message.reply_text(
        t("ar", "welcome_back"),
        parse_mode="HTML",
        reply_markup=get_main_keyboard("ar", user.id),
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keep old language buttons harmless; the bot now uses Arabic only."""
    query = update.callback_query
    update_user(query.from_user.id, language="ar")
    await query.answer("اللغة العربية ثابتة في البوت.")
    try:
        await query.edit_message_text("✅ تم ضبط لغة البوت على العربية.")
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=t("ar", "welcome"),
        parse_mode="HTML",
        reply_markup=get_main_keyboard("ar", query.from_user.id),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    if db_user:
        update_user(user.id, language="ar")
    await update.message.reply_text(t("ar", "help_text"), parse_mode="HTML")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    if db_user:
        update_user(user.id, language="ar")
    await update.message.reply_text(
        t("ar", "settings_text", language="العربية"),
        parse_mode="HTML",
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "settings_lang":
        await query.answer("اللغة العربية ثابتة في البوت.", show_alert=True)
        try:
            await query.edit_message_text("⚙️ لغة البوت ثابتة على العربية.")
        except Exception:
            pass
        return
    await query.answer("هذا الزر لم يعد متاحًا.", show_alert=True)
