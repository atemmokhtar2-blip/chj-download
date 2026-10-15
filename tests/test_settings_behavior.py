"""Deep behavior tests for user settings.

Two goals:
  1. Isolation — preferences/history/account data must never leak between users.
  2. Durability & real effect — saved values must be committed to PostgreSQL
     and must actually change the download engine's decisions.
"""

from __future__ import annotations

import pytest

# The ``isolated_db`` fixture (tests/conftest.py) gives every test its own
# freshly-created PostgreSQL database and yields its DSN.


# ---- lifetime persistence --------------------------------------------------

def test_clone_menu_hides_referral_and_plans_but_links_main_bot():
    """Inside a PRIVATE CLONE the settings menu must NOT show the referral
    center ("اكسب نسختك من البوت") nor the plans card ("الخطط والحدود") —
    those belong to the MAIN bot. Instead the clone shows ONE direct URL
    door: "اصنع بوتك الخاص" -> t.me/<main bot>. The MAIN bot's menu keeps
    both entries exactly as before (nothing removed there)."""
    from handlers.settings_menu import build_home, _main_bot_username

    s = {"video_quality": "auto", "delivery_mode": "video",
         "max_file_size_mb": 100, "notify_complete": 1}

    # MAIN bot menu — unchanged: referral + plans both present, no URL door.
    _t_main, rows_main = build_home(s)
    flat_main = [cb for row in rows_main for _label, cb in row]
    assert "settings:referral" in flat_main
    assert "settings:plan" in flat_main
    assert not any(str(cb).startswith("https://") for cb in flat_main)

    # CLONE menu — both entries gone, one URL door to the main bot instead.
    _t_clone, rows_clone = build_home(s, is_clone=True)
    flat_clone = [cb for row in rows_clone for _label, cb in row]
    assert "settings:referral" not in flat_clone
    assert "settings:plan" not in flat_clone
    doors = [cb for cb in flat_clone if str(cb).startswith("https://t.me/")]
    assert doors == [f"https://t.me/{_main_bot_username()}"]
    door_labels = [label for row in rows_clone for label, cb in row
                   if str(cb).startswith("https://")]
    assert any("اصنع بوتك الخاص" in label for label in door_labels)

    # Every other section survives identically in both menus.
    for cb in ("settings:quality", "settings:delivery", "settings:queue",
               "settings:history", "settings:privacy", "settings:notify",
               "settings:reset", "settings:close"):
        assert cb in flat_main and cb in flat_clone


def test_settings_are_committed_to_disk_not_kept_in_memory(isolated_db):
    from database.user_settings import save_settings

    save_settings(2001, video_quality="1080p", audio_quality="high",
                  max_file_size_mb=175, delivery_mode="file",
                  history_retention_days=365, notify_complete=0,
                  quiet_mode=1)

    # Open PostgreSQL directly (fresh connection, no module state) and
    # verify every field was really persisted with the exact value we saved.
    import psycopg2

    con = psycopg2.connect(isolated_db)
    try:
        with con.cursor() as cur:
            cur.execute(
                "SELECT video_quality, audio_quality, max_file_size_mb, "
                "delivery_mode, history_retention_days, notify_complete, quiet_mode "
                "FROM user_settings WHERE user_id = 2001"
            )
            row = cur.fetchone()
    finally:
        con.close()

    assert tuple(row) == ("1080p", "high", 175, "file", 365, 0, 1)


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

    import psycopg2

    con = psycopg2.connect(isolated_db)
    try:
        with con.cursor() as cur:
            cur.execute("DELETE FROM user_settings WHERE user_id = %s", (uid,))
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


def test_audio_button_maps_to_bitrate_specific_cache_label():
    from handlers.download import _resolve_download_choice

    info = {"media_type": "video", "qualities": []}
    # audio_quality low/normal/high must produce DISTINCT cache/history labels,
    # otherwise a stale cached MP3 (e.g. 320) is served after the user asks for
    # a different bitrate — i.e. the button is saved but has no real effect.
    _, _, _, label_low, br_low, _ = _resolve_download_choice(
        "dl_audio", info, {"audio_quality": "low", "delivery_mode": "video"}
    )
    _, _, _, label_mid, br_mid, _ = _resolve_download_choice(
        "dl_audio", info, {"audio_quality": "normal"}
    )
    _, _, _, label_high, br_high, _ = _resolve_download_choice(
        "dl_audio", info, {"audio_quality": "high"}
    )
    assert (label_low, br_low) == ("audio_128", "128")
    assert (label_mid, br_mid) == ("audio_192", "192")
    assert (label_high, br_high) == ("audio_320", "320")
    assert len({label_low, label_mid, label_high}) == 3


def test_audio_320_button_is_always_320_regardless_of_setting():
    from handlers.download import _resolve_download_choice

    info = {"media_type": "video", "qualities": []}
    _, _, _, label, br, _ = _resolve_download_choice(
        "dl_audio_320", info, {"audio_quality": "low"}
    )
    assert (label, br) == ("audio_320", "320")


def test_smart_audio_honors_audio_quality_setting():
    from handlers.download import _resolve_download_choice

    info = {"media_type": "video", "qualities": []}
    _, _, _, label_hi, br_hi, _ = _resolve_download_choice(
        "dl_smart", info, {"delivery_mode": "audio", "audio_quality": "high"}
    )
    _, _, _, label_lo, br_lo, _ = _resolve_download_choice(
        "dl_smart", info, {"delivery_mode": "audio", "audio_quality": "low"}
    )
    assert (label_hi, br_hi) == ("audio_320", "320")
    assert (label_lo, br_lo) == ("audio_128", "128")


def test_explicit_video_button_is_not_overridden_by_default_quality():
    from handlers.download import _resolve_download_choice

    info = {"media_type": "video", "qualities": [{"label": "1080p"}, {"label": "720p"}]}
    _, _, _, smart_label, _, _ = _resolve_download_choice(
        "dl_smart", info, {"video_quality": "720p", "delivery_mode": "video"}
    )
    _, _, _, explicit_label, _, _ = _resolve_download_choice(
        "dl_video_1080p", info, {"video_quality": "360p", "delivery_mode": "video"}
    )
    assert smart_label == "720p"
    assert explicit_label == "1080p"


def test_delivery_mode_file_and_audio_guard_mapping():
    from handlers.download import _resolve_download_choice

    info = {"media_type": "video", "qualities": [{"label": "720p"}]}
    _, _, _, _, _, delivery_smart = _resolve_download_choice(
        "dl_smart", info, {"delivery_mode": "file"}
    )
    _, _, _, _, _, delivery_explicit_file = _resolve_download_choice(
        "dl_video_best", info, {"delivery_mode": "file"}
    )
    # An explicit video button must never silently become an MP3 even when the
    # saved default delivery mode is audio.
    _, _, _, _, _, delivery_explicit_audio_guard = _resolve_download_choice(
        "dl_video_best", info, {"delivery_mode": "audio"}
    )
    assert delivery_smart == "file"
    assert delivery_explicit_file == "file"
    assert delivery_explicit_audio_guard == "video"


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
