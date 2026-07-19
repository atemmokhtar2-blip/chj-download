from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes

from database.users import get_user
from services.subscription import check_subscription
from locales import t


def require_subscription(func):
    """
    Decorator that blocks any handler unless the user is subscribed to REQUIRED_CHANNEL.
    Checks maintenance mode first — only admins bypass.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return

        from utils.maintenance import is_maintenance
        from middlewares.auth import is_admin
        if is_maintenance() and not is_admin(user.id):
            lang = "en"
            db_u = get_user(user.id)
            if db_u:
                lang = db_u.get("language", "en")
            await update.message.reply_text(t(lang, "maintenance_text"), parse_mode="HTML")
            return

        db_user = get_user(user.id)
        lang = db_user.get("language", "en") if db_user else "en"

        if not db_user:
            await update.message.reply_text(t(lang, "no_db_user"))
            return

        if db_user.get("is_banned"):
            await update.message.reply_text(t(lang, "banned"))
            return

        subscribed = await check_subscription(context.bot, user.id)
        if not subscribed:
            from handlers.start import send_subscription_prompt
            await send_subscription_prompt(update, lang)
            return

        return await func(update, context)

    return wrapper
