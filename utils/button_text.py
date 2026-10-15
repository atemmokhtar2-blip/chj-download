"""Shared helpers for Telegram keyboard labels.

Button labels KEEP their emoji — emoji are part of the UX (color dots,
section icons, state markers). ``normalize_button_text`` exists only for
emoji-insensitive *comparison* of reply-keyboard presses (clients may send
the label with or without variation selectors).
"""
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


def normalize_button_text(value: str) -> str:
    """Comparison-only form of a label: emoji removed, whitespace collapsed.

    Used to match incoming reply-keyboard messages against expected menu
    buttons regardless of emoji/variation-selector differences between
    clients. Never use this to *render* a button — rendering keeps emoji.
    """
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", str(value or ""))).strip()


def strip_button_emoji(value: str) -> str:
    """Deprecated: labels keep their emoji now. Kept for compatibility."""
    return str(value or "").strip()


def inline_button(text: str, *args, **kwargs):
    """Create an inline button preserving the label exactly (emoji included).

    Any legacy ``style=`` kwarg is ignored — Telegram inline buttons have no
    style/color option, and passing it would raise a TypeError.
    """
    kwargs.pop("style", None)
    return _TelegramInlineKeyboardButton(str(text or "").strip(), *args, **kwargs)
