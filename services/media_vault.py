from __future__ import annotations

from telegram.error import TelegramError

from config.settings import STORAGE_CHANNEL_ID


def get_storage_channel_id() -> str:
    return STORAGE_CHANNEL_ID


async def archive_to_vault(bot, sent_message):
    """Copy a sent Telegram file to a storage channel when configured."""
    if not STORAGE_CHANNEL_ID:
        return None
    try:
        copied = await bot.copy_message(
            chat_id=STORAGE_CHANNEL_ID,
            from_chat_id=sent_message.chat_id,
            message_id=sent_message.message_id,
        )
        return {"chat_id": STORAGE_CHANNEL_ID, "message_id": copied.message_id}
    except TelegramError:
        return None


async def deliver_from_vault(bot, target_chat_id: int, vault_chat_id: int, vault_message_id: int):
    return await bot.copy_message(
        chat_id=target_chat_id,
        from_chat_id=vault_chat_id,
        message_id=vault_message_id,
    )
