"""
Private Telegram channel as durable media vault (CDN pattern).

Most bots only store file_id in SQLite. file_ids can go stale; a private
storage channel keeps a live message the bot can copy_message from —
this is the pattern used by large media libraries, rarely wired into
simple download bots.

Flow:
  1. User download succeeds → bot also copies/sends media into STORAGE_CHANNEL_ID
  2. Cache stores (file_id, vault_chat_id, vault_message_id)
  3. On cache hit → prefer copy_message from vault (zero re-upload)
  4. If copy fails → fall back to file_id send → if that fails → re-download

Requires:
  STORAGE_CHANNEL_ID=-100xxxxxxxxxx
  Bot must be admin in that private channel.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def get_storage_channel_id() -> int | None:
    raw = (
        os.getenv("STORAGE_CHANNEL_ID")
        or os.getenv("MEDIA_VAULT_CHANNEL_ID")
        or ""
    ).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.error("Invalid STORAGE_CHANNEL_ID: %s", raw)
        return None


async def archive_to_vault(bot, message) -> dict[str, Any] | None:
    """
    Copy a just-sent user message into the storage channel.
    Returns {chat_id, message_id, file_id, media_type} or None.
    """
    channel_id = get_storage_channel_id()
    if channel_id is None or message is None:
        return None
    try:
        copied = await bot.copy_message(
            chat_id=channel_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
        file_id = None
        media_type = "document"
        if message.photo:
            file_id = message.photo[-1].file_id
            media_type = "image"
        elif message.video:
            file_id = message.video.file_id
            media_type = "video"
        elif message.audio:
            file_id = message.audio.file_id
            media_type = "audio"
        elif message.document:
            file_id = message.document.file_id
            media_type = "document"
        return {
            "chat_id": channel_id,
            "message_id": copied.message_id,
            "file_id": file_id,
            "media_type": media_type,
        }
    except Exception as e:
        logger.warning("Vault archive failed: %s", e)
        return None


async def deliver_from_vault(bot, chat_id: int, vault_chat_id: int, vault_message_id: int):
    """
    Deliver cached media by copying the vault message into the user chat.
    Returns the new Message or raises.
    """
    return await bot.copy_message(
        chat_id=chat_id,
        from_chat_id=vault_chat_id,
        message_id=vault_message_id,
    )
