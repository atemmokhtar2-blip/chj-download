import logging
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, BadRequest
from config.settings import REQUIRED_CHANNEL
from utils.logger import security_logger

logger = logging.getLogger(__name__)

SUBSCRIBED_STATUSES = ("member", "administrator", "creator", "restricted")


async def check_subscription(bot: Bot, user_id: int) -> bool:
    """
    Returns True only if the user is a confirmed member of REQUIRED_CHANNEL.
    Returns False on any error (fail-closed) — never grants access on uncertainty.
    """
    if not REQUIRED_CHANNEL or REQUIRED_CHANNEL.strip().upper() in ("", "NONE"):
        return True

    channel = REQUIRED_CHANNEL.strip()
    if not channel.startswith("@"):
        channel = f"@{channel}"

    try:
        member = await bot.get_chat_member(channel, user_id)
        is_member = member.status in SUBSCRIBED_STATUSES
        if not is_member:
            security_logger.info(
                f"Subscription denied: user={user_id} status={member.status} channel={channel}"
            )
        return is_member

    except BadRequest as e:
        # Channel not found or bot not admin — log clearly and fail closed
        security_logger.error(
            f"Subscription check failed (BadRequest) for user={user_id} channel={channel}: {e}. "
            f"Ensure the bot is an admin of {channel}."
        )
        return False

    except TelegramError as e:
        # Network error, flood wait, etc. — fail closed
        security_logger.error(
            f"Subscription check failed (TelegramError) for user={user_id} channel={channel}: {e}"
        )
        return False


async def build_subscription_keyboard(lang: str) -> InlineKeyboardMarkup:
    from locales import t
    channel = REQUIRED_CHANNEL.strip()
    if not channel.startswith("@"):
        channel = f"@{channel}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "join_button"), url=f"https://t.me/{channel.lstrip('@')}")],
        [InlineKeyboardButton(t(lang, "verify_button"), callback_data="verify_sub")],
    ])
