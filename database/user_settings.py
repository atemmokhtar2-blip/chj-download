"""Per-user download preferences + privacy/history helpers.

Every row is keyed by the Telegram ``user_id`` so two users can never read or
modify each other's preferences. All writes go through a strict allowlist:
unknown keys are dropped, and enums/booleans/ints are clamped to valid ranges.
"""

from __future__ import annotations

import os
import time

from .db import db_cursor
from config.settings import TEMP_DIR

# Field name -> default value. These are the ONLY fields the settings table
# stores, and the ONLY keys ``save_settings`` will ever accept.
USER_SETTINGS_DEFAULTS: dict[str, object] = {
    "video_quality": "auto",        # auto | 360p | 480p | 720p | 1080p | highest
    "audio_quality": "normal",      # low | normal | high  -> 128/192/320 kbps
    "max_file_size_mb": 0,          # 0 = no extra limit beyond the global cap
    "delivery_mode": "video",       # video | file | audio (default send method)
    "auto_cleanup": 1,              # clean stale temp files periodically
    "history_retention_days": 30,   # 0 = keep history forever
    "notify_complete": 1,           # completion summary messages
    "notify_failure": 1,            # detailed failure reasons
    "notify_progress": 1,           # live progress updates
    "notify_queue": 1,              # queue-wait notices
    "quiet_mode": 0,                # reduce non-essential messages
}

USER_SETTINGS_FIELDS = frozenset(USER_SETTINGS_DEFAULTS)

VIDEO_QUALITY_OPTIONS = ("auto", "360p", "480p", "720p", "1080p", "highest")
AUDIO_QUALITY_OPTIONS = ("low", "normal", "high")
DELIVERY_MODES = ("video", "file", "audio")
SIZE_PRESETS_MB = (50, 100, 250, 500)
HISTORY_RETENTION_OPTIONS = (7, 30, 90, 365)
AUDIO_QUALITY_BITRATE = {"low": 128, "normal": 192, "high": 320}
MAX_CUSTOM_SIZE_MB = 2000

_BOOL_FIELDS = {
    "auto_cleanup", "notify_complete", "notify_failure",
    "notify_progress", "notify_queue", "quiet_mode",
}


def _validate(key: str, value: object) -> object:
    """Clamp a single field to its allowed domain. Returns a safe value."""
    if key == "video_quality":
        v = str(value or "").strip().lower()
        if v in ("best", "max"):
            v = "highest"
        return v if v in VIDEO_QUALITY_OPTIONS else USER_SETTINGS_DEFAULTS["video_quality"]

    if key == "audio_quality":
        v = str(value or "").strip().lower()
        return v if v in AUDIO_QUALITY_OPTIONS else USER_SETTINGS_DEFAULTS["audio_quality"]

    if key == "delivery_mode":
        v = str(value or "").strip().lower()
        return v if v in DELIVERY_MODES else USER_SETTINGS_DEFAULTS["delivery_mode"]

    if key == "max_file_size_mb":
        try:
            n = int(value)
        except (TypeError, ValueError):
            return 0
        return min(max(n, 0), MAX_CUSTOM_SIZE_MB)

    if key == "history_retention_days":
        try:
            n = int(value)
        except (TypeError, ValueError):
            return USER_SETTINGS_DEFAULTS["history_retention_days"]
        return min(max(n, 0), 3650)

    if key in _BOOL_FIELDS:
        return 1 if bool(value) else 0

    return value


def get_user_settings(user_id: int) -> dict:
    """Return the user's settings merged over defaults (never writes a row)."""
    settings = dict(USER_SETTINGS_DEFAULTS)
    with db_cursor() as c:
        row = c.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row:
        for key in USER_SETTINGS_FIELDS:
            if key in row.keys():
                settings[key] = row[key]
    return settings


def save_settings(user_id: int, **fields) -> dict:
    """Validate + persist one or more fields, then return the full settings."""
    clean: dict[str, object] = {}
    for key, value in fields.items():
        if key not in USER_SETTINGS_FIELDS:
            continue
        clean[key] = _validate(key, value)
    if not clean:
        return get_user_settings(user_id)

    # Self-heal any entry path: user_settings has a FK to users, so make sure
    # the parent row exists before the upsert (e.g. /settings before /start).
    with db_cursor() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))

    cols = ", ".join(clean.keys())
    placeholders = ", ".join("?" for _ in clean)
    updates = ", ".join(f"{k} = excluded.{k}" for k in clean)
    with db_cursor() as c:
        c.execute(
            f"INSERT INTO user_settings (user_id, {cols}) "
            f"VALUES (?, {placeholders}) "
            f"ON CONFLICT(user_id) DO UPDATE SET {updates}, updated_at = datetime('now')",
            [user_id, *clean.values()],
        )
    return get_user_settings(user_id)


def reset_settings(user_id: int) -> dict:
    """Erase all preferences for a user (history/files are untouched)."""
    with db_cursor() as c:
        c.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
    return get_user_settings(user_id)


def delete_user_data(user_id: int) -> None:
    """GDPR-style 'forget me': settings + history + the account row itself.

    This intentionally does NOT touch ``file_cache`` (it is content-addressed
    by URL hash shared across users, not personal data) or the Telegram media
    already delivered. The user row is removed; a future /start recreates it.
    """
    with db_cursor() as c:
        c.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM downloads WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM users WHERE user_id = ?", (user_id,))


# ---- Download history (scoped strictly to the calling user) ----

def get_history_count(user_id: int) -> int:
    with db_cursor() as c:
        row = c.execute(
            "SELECT COUNT(*) AS cnt FROM downloads WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["cnt"] or 0)


def get_user_history(user_id: int, limit: int = 10, offset: int = 0) -> list[dict]:
    with db_cursor() as c:
        rows = c.execute(
            """
            SELECT id, url, title, platform, quality, media_type, file_size, created_at
            FROM downloads WHERE user_id = ?
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (user_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_history_entry(user_id: int, entry_id: int) -> dict | None:
    with db_cursor() as c:
        row = c.execute(
            "SELECT * FROM downloads WHERE user_id = ? AND id = ?",
            (user_id, entry_id),
        ).fetchone()
        return dict(row) if row else None


def delete_history_entry(user_id: int, entry_id: int) -> bool:
    """Delete ONE history row that belongs to ``user_id``. Returns success."""
    with db_cursor() as c:
        cur = c.execute(
            "DELETE FROM downloads WHERE user_id = ? AND id = ?", (user_id, entry_id)
        )
        return cur.rowcount > 0


def clear_history(user_id: int) -> int:
    """Delete the user's entire download history (server files untouched)."""
    with db_cursor() as c:
        cur = c.execute("DELETE FROM downloads WHERE user_id = ?", (user_id,))
        return cur.rowcount


def prune_history_for_user(user_id: int) -> int:
    """Delete the user's history rows older than their configured retention."""
    settings = get_user_settings(user_id)
    days = int(settings.get("history_retention_days") or 0)
    if days <= 0:
        return 0
    with db_cursor() as c:
        cur = c.execute(
            "DELETE FROM downloads WHERE user_id = ? AND created_at < datetime('now', ?)",
            (user_id, f"-{days} days"),
        )
        return cur.rowcount


def prune_expired_history() -> int:
    """Apply each user's retention policy. Returns total rows removed."""
    with db_cursor() as c:
        rows = c.execute(
            "SELECT user_id, history_retention_days FROM user_settings "
            "WHERE history_retention_days > 0"
        ).fetchall()
    total = 0
    for row in rows:
        days = int(row["history_retention_days"])
        with db_cursor() as c:
            cur = c.execute(
                "DELETE FROM downloads WHERE user_id = ? AND created_at < datetime('now', ?)",
                (row["user_id"], f"-{days} days"),
            )
            total += cur.rowcount
    return total


def cleanup_stale_temp_files(max_age_seconds: int = 3600, temp_dir: str | None = None) -> int:
    """Delete temp files older than ``max_age_seconds``.

    Files currently being written always have a fresh mtime, so age-based
    pruning never touches an in-flight download.
    """
    root = temp_dir or TEMP_DIR
    removed = 0
    try:
        now = time.time()
        for name in os.listdir(root):
            path = os.path.join(root, name)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > max_age_seconds:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed
