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

NOT_MODIFIED = "message is not modified"


def is_not_modified_error(error: Exception) -> bool:
    return NOT_MODIFIED in str(error).lower()


async def safe_edit(edit_callable, **kwargs):
    """Invoke a bound Telegram edit method, ignoring the harmless not-modified error.

    Usage::

        await safe_edit(query.edit_message_text, text=..., parse_mode="HTML")
        await safe_edit(message.edit_text, text=..., reply_markup=...)
    """
    try:
        return await edit_callable(**kwargs)
    except BadRequest as exc:
        if is_not_modified_error(exc):
            logger.debug("Ignored benign 'message is not modified' edit error.")
            return None
        raise
