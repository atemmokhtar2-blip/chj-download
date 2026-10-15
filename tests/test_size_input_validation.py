"""Validation for the custom max-size input (📏 إدخال حجم مخصص).

The input must be a REAL number: a missing value, a name/words, zero, a
negative number, or a value above MAX_CUSTOM_SIZE_MB are all rejected with a
distinct message, and the prompt stays pending so the user can retry.
"""
from __future__ import annotations

import asyncio

import pytest

# The ``isolated_db`` fixture (tests/conftest.py) provides a dedicated
# PostgreSQL database per test.


class _Msg:
    def __init__(self, text: str):
        self.text = text
        self.replies = []

    async def reply_text(self, *args, **kwargs):
        self.replies.append((args, kwargs))


class _Update:
    def __init__(self, text: str, uid: int):
        self.message = _Msg(text)
        self.effective_user = type("_User", (), {"id": uid})()


class _Context:
    def __init__(self):
        self.user_data: dict = {}


def _enter_custom_size(uid, context):
    from handlers.settings_menu import _dispatch

    _dispatch(uid, "settings:quality:size:custom", context)


def _consume(update, context):
    from handlers.settings_menu import try_handle_settings_text

    return asyncio.run(try_handle_settings_text(update, context))


# ---- the parser ------------------------------------------------------------

def test_parse_size_accepts_valid_numbers():
    from handlers.settings_menu import _parse_size_mb

    assert _parse_size_mb("75") == 75.0
    assert _parse_size_mb("75MB") == 75.0
    assert _parse_size_mb("75 mb") == 75.0
    assert _parse_size_mb(" 120 ") == 120.0
    assert _parse_size_mb("١٢٣") == 123.0   # Arabic-Indic digits
    assert _parse_size_mb("۱۲۳") == 123.0   # Persian digits
    assert _parse_size_mb("2.5") == 2.5     # decimals (rounded on save)
    assert _parse_size_mb("0") == 0.0       # parsed, then rejected by handler
    assert _parse_size_mb("-7") == -7.0     # parsed, then rejected by handler


def test_parse_size_rejects_non_numbers():
    from handlers.settings_menu import _parse_size_mb

    assert _parse_size_mb("") is None       # missing
    assert _parse_size_mb("   ") is None    # blank
    assert _parse_size_mb("abc") is None    # letters
    assert _parse_size_mb("محمد") is None   # a name
    assert _parse_size_mb(".") is None
    assert _parse_size_mb("NaN") is None
    assert _parse_size_mb("Infinity") is None


# ---- the full prompt flow ---------------------------------------------------

def test_custom_size_flow_accepts_valid_and_rejects_every_bad_shape(isolated_db):
    from database.user_settings import get_user_settings

    uid = 90001
    ctx = _Context()
    _enter_custom_size(uid, ctx)
    assert ctx.user_data["settings_input"] == "size"

    # Every bad shape is consumed, shows a message, keeps the prompt pending,
    # and NEVER writes a value.
    for bad in ("", "abc", "محمد", "0", "-5", "999999"):
        consumed = _consume(_Update(bad, uid), ctx)
        assert consumed is True, f"input {bad!r} should be consumed"
        assert ctx.user_data["settings_input"] == "size", f"input {bad!r} should stay pending"
        assert get_user_settings(uid)["max_file_size_mb"] == 0, f"input {bad!r} must not be saved"

    # A valid value finally saves.
    consumed = _consume(_Update("64", uid), ctx)
    assert consumed is True
    assert get_user_settings(uid)["max_file_size_mb"] == 64


def test_custom_size_flow_rounds_decimal_to_whole_mb(isolated_db):
    from database.user_settings import get_user_settings

    uid = 90002
    ctx = _Context()
    _enter_custom_size(uid, ctx)
    assert _consume(_Update("50.5", uid), ctx) is True
    assert get_user_settings(uid)["max_file_size_mb"] == 51
