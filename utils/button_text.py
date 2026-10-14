"""Shared helpers for clean Telegram keyboard labels."""
from __future__ import annotations

import re
from telegram import InlineKeyboardButton as _TelegramInlineKeyboardButton

# Emoji/pictograph ranges, variation selectors, joiners, flags, and keycaps.
_EMOJI_RE = re.compile(
    "["
    "\\U0001F1E6-\\U0001F1FF"
    "\\U0001F300-\\U0001FAFF"
    "\\U00002600-\\U000027BF"
    "\\U00002300-\\U000023FF"
    "\\uFE0E-\\uFE0F"
    "\\u200D"
    "\\u20E3"
    "\\U0001F3FB-\\U0001F3FF"
    "]+"
)

def strip_button_emoji(value: str) -> str:
    """Remove decorative emoji from button labels while preserving text."""
    return re.sub(r"\\s{2,}", " ", _EMOJI_RE.sub("", str(value or ""))).strip()

def inline_button(text: str, *args, **kwargs):
    """Create an inline button with emoji-free text and preserve all options."""
    return _TelegramInlineKeyboardButton(strip_button_emoji(text), *args, **kwargs)
