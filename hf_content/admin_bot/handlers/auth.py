from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes
from config.settings import OWNER_ID, ADMIN_IDS


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in ADMIN_IDS


def require_admin(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_admin(user.id):
            if update.callback_query:
                await update.callback_query.answer("🚫 Access Denied", show_alert=True)
            elif update.message:
                await update.message.reply_text(
                    "🚫 <b>Access Denied</b>\n\n"
                    "This bot is restricted to administrators only.",
                    parse_mode="HTML"
                )
            return
        return await func(update, context)
    return wrapper
