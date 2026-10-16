"""Shared helpers for Telegram keyboard labels and native button styling.

Labels retain their emoji. Reply-keyboard presses are normalized only for
comparison because Telegram clients may return labels with different emoji
variation selectors.
"""
from __future__ import annotations

import re
from telegram import (
    InlineKeyboardButton as _TelegramInlineKeyboardButton,
    KeyboardButton as _TelegramKeyboardButton,
)

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
    """Comparison-only form of a label: emoji removed, whitespace collapsed."""
    return re.sub(r"\\s{2,}", " ", _EMOJI_RE.sub("", str(value or ""))).strip()


def strip_button_emoji(value: str) -> str:
    """Deprecated compatibility helper; rendered labels keep their emoji."""
    return str(value or "").strip()


def _button_style(text: str, callback_data=None, explicit=None) -> str | None:
    """Choose Telegram's native supported style: primary (blue), success, danger."""
    if explicit in {"primary", "success", "danger"}:
        return explicit

    label = normalize_button_text(text).casefold()
    action = str(callback_data or "").casefold()

    danger_terms = (
        "حذف", "الغاء", "إلغاء", "إغلاق", "اعادة ضبط", "إعادة ضبط",
        "مسح", "حظر", "ايقاف", "إيقاف", "reset", "delete", "cancel",
        "close", "ban", "stop", "remove", "unban",
    )
    success_terms = (
        "تأكيد", "تفعيل", "حفظ", "نشر", "تم", "استرداد", "confirm",
        "activate", "save", "publish", "redeem", "approve",
    )
    primary_terms = (
        "تحميل", "تنزيل", "متابعة", "التالي", "ابدأ", "بدء",
        "download", "continue", "next", "start", "open", "quality",
    )

    if any(term in label or term in action for term in danger_terms):
        return "danger"
    if any(term in label or term in action for term in success_terms):
        return "success"
    if any(term in label or term in action for term in primary_terms):
        return "primary"
    return None


def inline_button(text: str, *args, **kwargs):
    """Create an inline button with a restrained native Telegram color style."""
    label = str(text or "").strip()
    explicit_style = kwargs.pop("style", None)
    callback_data = kwargs.get("callback_data")
    style = _button_style(label, callback_data, explicit_style)
    if style:
        kwargs["style"] = style
    return _TelegramInlineKeyboardButton(label, *args, **kwargs)


def reply_button(text: str, style: str | None = None):
    """Create a reply-keyboard button using Telegram's native style field."""
    label = str(text or "").strip()
    chosen_style = _button_style(label, explicit=style)
    kwargs = {"style": chosen_style} if chosen_style else {}
    return _TelegramKeyboardButton(label, **kwargs)
