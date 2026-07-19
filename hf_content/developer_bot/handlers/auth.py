import functools
from telegram import Update
from config.settings import OWNER_ID
from utils.logger import action_logger


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def require_owner(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context, *args, **kwargs):
        user = update.effective_user
        if not user or not is_owner(user.id):
            if update.message:
                await update.message.reply_text("🚫 وصول مرفوض — هذا البوت للمطوّر فقط.")
            elif update.callback_query:
                await update.callback_query.answer("🚫 وصول مرفوض", show_alert=True)
            action_logger.warning("DENIED access attempt from user_id=%s", user.id if user else "unknown")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper
