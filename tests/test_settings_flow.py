"""Settings menu tests — every button callback, per-user isolation, input flows.

These tests run against an isolated temporary SQLite DB so they never touch
the real ``database/bot.db`` or any Telegram API.
"""

from __future__ import annotations

import pytest

from database import db as db_module


class _Msg:
    def __init__(self, text: str):
        self.text = text

    async def reply_text(self, *args, **kwargs):
        return None


class _User:
    def __init__(self, uid: int):
        self.id = uid


class _Update:
    def __init__(self, text: str, uid: int):
        self.message = _Msg(text)
        self.effective_user = _User(uid)


class _Context:
    def __init__(self):
        self.user_data: dict = {}


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Point database.db at a fresh temp file and initialise the schema."""
    db_path = tmp_path / "test_settings.db"
    monkeypatch.setattr(db_module, "DATABASE_PATH", str(db_path))
    db_module.init_db()
    return db_path


def test_defaults_and_save_validation(isolated_db):
    from database.user_settings import get_user_settings, save_settings

    s = get_user_settings(777)
    assert s["video_quality"] == "auto"
    assert s["audio_quality"] == "normal"
    assert s["delivery_mode"] == "video"
    assert s["max_file_size_mb"] == 0
    assert s["history_retention_days"] == 30
    assert s["quiet_mode"] == 0

    save_settings(777, video_quality="nonsense", max_file_size_mb=999999, unknown_key="x")
    s = get_user_settings(777)
    assert s["video_quality"] == "auto"          # invalid -> default
    assert s["max_file_size_mb"] == 2000         # clamped to MAX_CUSTOM_SIZE_MB


def test_button_quality_flow(isolated_db):
    from database.user_settings import get_user_settings
    from handlers.settings_menu import _dispatch

    uid = 111
    # home has all 8 sections
    text, rows = _dispatch(uid, "settings:home", _Context())
    labels = [lbl for row in rows for lbl, _ in row]
    assert any("🎯 الجودة والحجم" in lbl for lbl in labels)
    assert any("🔒 الخصوصية والمساحة" in lbl for lbl in labels)

    # quality menu
    text, rows = _dispatch(uid, "settings:quality", _Context())
    assert "الوضع الحالي" in text

    # smart auto
    text, rows = _dispatch(uid, "settings:quality:set:auto", _Context())
    assert get_user_settings(uid)["video_quality"] == "auto"

    # video qualities
    for q in ("360p", "480p", "720p", "1080p", "highest"):
        text, rows = _dispatch(uid, f"settings:quality:video:set:{q}", _Context())
        assert get_user_settings(uid)["video_quality"] == q

    # audio qualities
    for q in ("low", "normal", "high"):
        text, rows = _dispatch(uid, f"settings:quality:audio:set:{q}", _Context())
        assert get_user_settings(uid)["audio_quality"] == q

    # size presets + no extra limit
    for mb in (50, 100, 250, 500, 0):
        text, rows = _dispatch(uid, f"settings:quality:size:set:{mb}", _Context())
        assert get_user_settings(uid)["max_file_size_mb"] == mb


def test_custom_size_text_flow(isolated_db):
    import asyncio
    from database.user_settings import get_user_settings
    from handlers.settings_menu import _dispatch, try_handle_settings_text

    uid = 111
    ctx = _Context()
    _dispatch(uid, "settings:quality:size:custom", ctx)
    assert ctx.user_data["settings_input"] == "size"

    consumed = asyncio.run(try_handle_settings_text(_Update("75", uid), ctx))
    assert consumed is True
    assert get_user_settings(uid)["max_file_size_mb"] == 75

    # invalid input keeps the prompt pending
    ctx2 = _Context()
    _dispatch(uid, "settings:quality:size:custom", ctx2)
    asyncio.run(try_handle_settings_text(_Update("abc", uid), ctx2))
    assert ctx2.user_data["settings_input"] == "size"


def test_custom_size_validates_number_types_and_range(isolated_db):
    import asyncio
    from database.user_settings import get_user_settings
    from handlers.settings_menu import _dispatch, try_handle_settings_text

    uid = 1234
    valid_cases = [
        ("75", 75),          # plain integer
        ("75.5", 76),        # decimal -> half-up rounding
        ("50.4", 50),        # decimal -> rounds down
        ("٧٥", 75),          # Arabic-Indic digits
        ("75 MB", 75),       # English unit suffix
        ("75ميجا", 75),      # Arabic unit suffix
        ("1,5", 2),          # comma decimal separator -> 1.5 -> 2
    ]
    for raw_text, expected in valid_cases:
        ctx = _Context()
        _dispatch(uid, "settings:quality:size:custom", ctx)
        consumed = asyncio.run(try_handle_settings_text(_Update(raw_text, uid), ctx))
        assert consumed is True
        assert get_user_settings(uid)["max_file_size_mb"] == expected

    # Anything non-numeric or out of range must keep the prompt pending and
    # never overwrite the last good value.
    last_good = valid_cases[-1][1]
    for raw_text in ("abc", "خمسين", "999999", "0", "-10", "75.5.3", ""):
        ctx = _Context()
        _dispatch(uid, "settings:quality:size:custom", ctx)
        asyncio.run(try_handle_settings_text(_Update(raw_text, uid), ctx))
        assert ctx.user_data.get("settings_input") == "size"
        assert get_user_settings(uid)["max_file_size_mb"] == last_good


def test_delivery_flow(isolated_db):
    from database.user_settings import get_user_settings
    from handlers.settings_menu import _dispatch

    uid = 222
    for mode in ("video", "file", "audio"):
        _dispatch(uid, f"settings:delivery:set:{mode}", _Context())
        assert get_user_settings(uid)["delivery_mode"] == mode
    _dispatch(uid, "settings:delivery:info", _Context())  # renders without crashing


def test_history_empty_and_clear(isolated_db):
    from database.users import create_user
    from database.downloads import log_download
    from database.user_settings import get_history_count, clear_history
    from handlers.settings_menu import _dispatch

    uid = 333
    create_user(uid, "u", "f", "l")
    text, rows = _dispatch(uid, "settings:history", _Context())
    assert "لا توجد تحميلات" in text

    eid = log_download(uid, "https://example.com/v.mp4", "video_01.mp4", "example", "720p", "video", 12345)
    text, rows = _dispatch(uid, f"settings:history:detail:{eid}", _Context())
    assert "video_01.mp4" in text

    text, rows = _dispatch(uid, "settings:history:clear:confirm", _Context())
    text, rows = _dispatch(uid, "settings:history:clear:yes", _Context())
    assert get_history_count(uid) == 0

    # deleting a non-owned entry must not affect another user
    uid_b = 444
    create_user(uid_b, "u", "f", "l")
    eid2 = log_download(uid_b, "https://example.com/other.mp4", "other.mp4", "example", "720p", "video", 1)
    from database.user_settings import delete_history_entry
    assert delete_history_entry(uid, eid2) is False  # not owned by uid
    assert get_history_count(uid_b) == 1
    clear_history(uid_b)


def test_privacy_and_notify_flow(isolated_db):
    from database.user_settings import get_user_settings
    from handlers.settings_menu import _dispatch

    uid = 555
    _dispatch(uid, "settings:privacy:clean", _Context())  # no temp files, safe

    for days in (7, 30, 90, 365, 0):
        _dispatch(uid, f"settings:privacy:retention:set:{days}", _Context())
        assert get_user_settings(uid)["history_retention_days"] == days

    # notification toggles flip
    before = get_user_settings(uid)["quiet_mode"]
    _dispatch(uid, "settings:notify:toggle:quiet_mode", _Context())
    assert get_user_settings(uid)["quiet_mode"] == (0 if before else 1)


def test_reset_and_delete_account(isolated_db):
    from database.user_settings import save_settings, get_user_settings
    from database.users import get_user
    from handlers.settings_menu import _dispatch

    uid = 666
    from database.users import create_user
    create_user(uid, "u", "f", "l")
    save_settings(uid, video_quality="1080p", delivery_mode="audio")
    assert get_user_settings(uid)["video_quality"] == "1080p"

    _dispatch(uid, "settings:reset:yes", _Context())
    assert get_user_settings(uid)["video_quality"] == "auto"

    # account deletion removes settings + user row
    _dispatch(uid, "settings:privacy:delete:confirm", _Context())
    _dispatch(uid, "settings:privacy:delete:yes", _Context())
    assert get_user(uid) is None


def test_preferences_do_not_leak_between_users(isolated_db):
    from database.user_settings import save_settings, get_user_settings

    save_settings(1001, video_quality="1080p", max_file_size_mb=250)
    save_settings(1002, video_quality="360p", max_file_size_mb=50)

    s1 = get_user_settings(1001)
    s2 = get_user_settings(1002)
    assert s1["video_quality"] == "1080p"
    assert s2["video_quality"] == "360p"
    assert s1["max_file_size_mb"] == 250
    assert s2["max_file_size_mb"] == 50


def test_unknown_callback_falls_back_to_home(isolated_db):
    from handlers.settings_menu import _dispatch

    uid = 123
    text, rows = _dispatch(uid, "totally:unknown:cb", _Context())
    labels = [lbl for row in rows for lbl, _ in row]
    assert any("🎯 الجودة والحجم" in lbl for lbl in labels)


def test_main_keyboard_has_download_and_settings_shortcuts(isolated_db):
    from locales import t
    from handlers.start import get_main_keyboard

    kb = get_main_keyboard("ar", 555)
    labels = [btn.text for row in kb.keyboard for btn in row]
    assert "📥 تحميل" in labels
    assert "⚙️ الإعدادات" in labels
    assert t("ar", "menu_settings") == "⚙️ الإعدادات"


def test_settings_button_opens_the_real_settings_menu(isolated_db):
    """Tapping the main-menu Settings button must open the full settings menu,
    not the legacy static /settings text."""
    import asyncio
    from handlers.settings_menu import settings_command

    captured = {}

    class _User:
        id = 4242

    class _Message:
        async def reply_text(self, text, **kwargs):
            captured["text"] = text
            captured["parse_mode"] = kwargs.get("parse_mode")

    class _Update:
        effective_user = _User()
        message = _Message()

    asyncio.run(settings_command(_Update(), None))
    assert "إعدادات حسابك" in captured["text"]
    assert captured["parse_mode"] == "HTML"
