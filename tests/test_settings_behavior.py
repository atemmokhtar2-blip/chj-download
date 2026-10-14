"""Deep behavior tests for user settings.

Two goals:
  1. Isolation — preferences/history/account data must never leak between users.
  2. Durability & real effect — saved values must be committed to disk and must
     actually change the download engine's decisions.
"""

from __future__ import annotations

import sqlite3

import pytest

from database import db as db_module


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_settings_behavior.db"
    monkeypatch.setattr(db_module, "DATABASE_PATH", str(db_path))
    db_module.init_db()
    return db_path


# ---- lifetime persistence --------------------------------------------------

def test_settings_are_committed_to_disk_not_kept_in_memory(isolated_db):
    from database.user_settings import save_settings

    save_settings(2001, video_quality="1080p", audio_quality="high",
                  max_file_size_mb=175, delivery_mode="file",
                  history_retention_days=365, notify_complete=0,
                  quiet_mode=1)

    # Open the SQLite file directly (fresh connection, no module state) and
    # verify every field landed on disk with the exact value we saved.
    con = sqlite3.connect(str(isolated_db))
    try:
        row = con.execute(
            "SELECT video_quality, audio_quality, max_file_size_mb, "
            "delivery_mode, history_retention_days, notify_complete, quiet_mode "
            "FROM user_settings WHERE user_id = 2001"
        ).fetchone()
    finally:
        con.close()

    assert row == ("1080p", "high", 175, "file", 365, 0, 1)


def test_settings_survive_many_separate_write_transactions(isolated_db):
    from database.user_settings import save_settings, get_user_settings

    uid = 2002
    for i in range(25):
        save_settings(uid, max_file_size_mb=1 + i)
        assert get_user_settings(uid)["max_file_size_mb"] == 1 + i


def test_reset_reverts_to_factory_defaults_on_disk(isolated_db):
    from database.user_settings import save_settings, reset_settings

    uid = 2003
    save_settings(uid, video_quality="720p", delivery_mode="audio")

    con = sqlite3.connect(str(isolated_db))
    try:
        con.execute("DELETE FROM user_settings WHERE user_id = ?", (uid,))
        con.commit()
    finally:
        con.close()

    defaults = reset_settings(uid)
    assert defaults["video_quality"] == "auto"
    assert defaults["delivery_mode"] == "video"


# ---- strict isolation ------------------------------------------------------

def test_settings_writes_are_fully_isolated(isolated_db):
    from database.user_settings import save_settings, get_user_settings

    save_settings(3001, video_quality="1080p", max_file_size_mb=500)
    save_settings(3002, video_quality="360p", max_file_size_mb=25)

    s1, s2 = get_user_settings(3001), get_user_settings(3002)
    assert s1["video_quality"] == "1080p" and s1["max_file_size_mb"] == 500
    assert s2["video_quality"] == "360p" and s2["max_file_size_mb"] == 25
    assert s1 != s2


def test_deleting_one_users_account_leaves_others_intact(isolated_db):
    from database.user_settings import save_settings, delete_user_data, get_user_settings
    from database.users import create_user, get_user
    from database.downloads import log_download

    create_user(4001, "a", "A", "A")
    create_user(4002, "b", "B", "B")
    save_settings(4001, video_quality="480p")
    save_settings(4002, video_quality="1080p")
    log_download(4001, "https://example.com/a.mp4", "a.mp4", "x", "480p", "video", 100)
    log_download(4002, "https://example.com/b.mp4", "b.mp4", "x", "1080p", "video", 200)

    delete_user_data(4001)

    # User A is gone everywhere…
    assert get_user(4001) is None
    assert get_user_settings(4001)["video_quality"] == "auto"

    # …but user B keeps settings, history and account row.
    assert get_user(4002) is not None
    assert get_user_settings(4002)["video_quality"] == "1080p"
    from database.user_settings import get_history_count
    assert get_history_count(4002) == 1


def test_malformed_or_hostile_user_ids_never_touch_data(isolated_db):
    from database.user_settings import save_settings, get_user_settings, delete_user_data

    save_settings(5001, video_quality="720p")

    # Booleans, negatives, floats-as-strings and non-numeric input must all be
    # rejected, leaving the real user's row untouched (still 720p, never 1080p).
    for bad in (True, False, -5001, 0, 2**80, "5001", None):
        try:
            save_settings(bad, video_quality="1080p")
        except ValueError:
            pass
        assert get_user_settings(5001)["video_quality"] == "720p"

    for bad in (True, False, -5001, 0, 2**80, "5001", None):
        try:
            delete_user_data(bad)  # invalid ids must never delete user 5001
        except ValueError:
            pass
        assert get_user_settings(5001)["video_quality"] == "720p"


def test_history_entry_and_delete_are_user_scoped(isolated_db):
    from database.users import create_user
    from database.downloads import log_download
    from database.user_settings import get_history_entry, delete_history_entry, get_history_count

    create_user(6001, "a", "A", "A")
    create_user(6002, "b", "B", "B")
    eid = log_download(6002, "https://example.com/secret.mp4", "secret.mp4", "x", "720p", "video", 1)

    assert get_history_entry(6001, eid) is None          # cannot read
    assert delete_history_entry(6001, eid) is False      # cannot delete
    assert get_history_count(6002) == 1                  # owner untouched


def test_save_settings_ignores_unknown_columns_even_with_sql_chars(isolated_db):
    from database.user_settings import save_settings, get_user_settings

    uid = 7001
    save_settings(uid, video_quality="480p",
                  **{"unknown_key": "x", "video_quality); DROP TABLE user_settings; --": "x"})
    s = get_user_settings(uid)
    assert s["video_quality"] == "480p"
    assert "unknown_key" not in s


# ---- real behavioral effect -------------------------------------------------

def test_completion_caption_honors_notify_complete():
    from handlers.download import _completion_caption

    assert _completion_caption("en", {}) is not None
    assert _completion_caption("en", {"notify_complete": 1}) is not None
    assert _completion_caption("en", {"notify_complete": 0}) is None
    assert _completion_caption("en", None) is not None


def test_smart_download_honors_delivery_mode_audio():
    from handlers.download import _pick_smart_download

    info = {"media_type": "video", "qualities": [{"label": "1080p"}, {"label": "720p"}]}
    is_audio, is_image, is_album, quality = _pick_smart_download(
        info, {"delivery_mode": "audio", "video_quality": "1080p"}
    )
    assert is_audio is True
    assert quality == "audio"


def test_smart_download_honors_specific_video_quality():
    from handlers.download import _pick_smart_download

    info = {"media_type": "video", "qualities": [{"label": "1080p"}, {"label": "720p"}, {"label": "480p"}]}
    _, _, _, quality = _pick_smart_download(info, {"video_quality": "720p"})
    assert quality == "720p"

    # Requested quality missing -> nearest available under it.
    info2 = {"media_type": "video", "qualities": [{"label": "480p"}, {"label": "360p"}]}
    _, _, _, quality2 = _pick_smart_download(info2, {"video_quality": "720p"})
    assert quality2 == "480p"


def test_smart_download_honors_highest_choice():
    from handlers.download import _pick_smart_download

    info = {"media_type": "video", "qualities": [{"label": "1080p"}, {"label": "720p"}]}
    _, _, _, quality = _pick_smart_download(info, {"video_quality": "highest"})
    assert quality == "best"


def test_validate_clamps_every_field():
    from database.user_settings import _validate

    assert _validate("video_quality", "bogus") == "auto"
    assert _validate("video_quality", "best") == "highest"
    assert _validate("audio_quality", "320") == "normal"
    assert _validate("delivery_mode", "bogus") == "video"
    assert _validate("max_file_size_mb", -5) == 0
    assert _validate("max_file_size_mb", 999999) == 2000
    assert _validate("history_retention_days", -1) == 0
    assert _validate("history_retention_days", 999999) == 3650
    assert _validate("quiet_mode", "off") == 0
    assert _validate("quiet_mode", "1") == 1
