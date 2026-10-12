"""Safe wrappers around Telegram message-edit calls.

Telegram raises ``BadRequest: message is not modified`` whenever an edit
produces exactly the same text + reply markup as the current message. This is
a *benign* signal, not a real error, but it bubbles up as an unhandled
exception and makes the bot reply "unexpected error" to a perfectly valid
action (e.g. tapping the same filter button twice in the admin panel).

Import :func:`safe_edit` anywhere the bot edits messages and wrap the bound
``edit_*`` method instead of calling it directly.
"""

from __future__ import annotations

import logging
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

# These Telegram errors describe benign, transient edit states — not real
# failures. Swallow them so a tap on a stale/unchanged button, or an edit to a
# message the user already deleted, never surfaces "unexpected error".
BENIGN_EDIT_ERRORS = (
    "message is not modified",
    "message to edit not found",
    "message can't be edited",
    "there is no caption in the message to edit",
)


def is_benign_edit_error(error: Exception) -> bool:
    msg = str(error).lower()
    return any(token in msg for token in BENIGN_EDIT_ERRORS)


async def safe_edit(edit_callable, *args, **kwargs):
    """Invoke a bound Telegram edit method, ignoring benign edit errors.

    Usage::

        await safe_edit(query.edit_message_text, text=..., parse_mode="HTML")
        await safe_edit(message.edit_text, "text", parse_mode="HTML")
        await safe_edit(message.edit_caption, "caption", parse_mode="HTML")
    """
    try:
        return await edit_callable(*args, **kwargs)
    except BadRequest as exc:
        if is_benign_edit_error(exc):
            logger.debug("Ignored benign Telegram edit error: %s", exc)
            return None
        raise
