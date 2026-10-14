import sqlite3
import logging
import os
import shutil
from contextlib import contextmanager
from config.settings import DATABASE_PATH

logger = logging.getLogger(__name__)


def _has_users(path: str) -> bool:
    """True when the SQLite file at ``path`` has at least one row in ``users``."""
    if not path or not os.path.exists(path):
        return False
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute("SELECT COUNT(*) FROM users").fetchone()
            return bool(row and row[0] and int(row[0]) > 0)
        finally:
            con.close()
    except Exception:
        return False


def _migrate_legacy_database() -> None:
    """One-time copy of the repo-relative DB to the persistent location.

    Older deployments stored ``database/bot.db`` next to the code, which is
    wiped on every rebuild. When the process now points at a persistent path
    (``/data/bot.db`` or ``DATABASE_PATH``) and the persistent file is empty or
    missing while the legacy file still has users, copy the legacy file over so
    no accumulated users are lost during the switch.
    """
    legacy = os.path.join(os.getcwd(), "database", "bot.db")
    try:
        same = os.path.abspath(legacy) == os.path.abspath(DATABASE_PATH)
    except Exception:
        same = True
    if same:
        return
    if not _has_users(legacy):
        return
    if _has_users(DATABASE_PATH):
        return
    try:
        os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
        shutil.copy2(legacy, DATABASE_PATH)
        logger.info("Migrated legacy database %s -> %s", legacy, DATABASE_PATH)
    except Exception:
        logger.exception("Failed to migrate legacy database; starting fresh at %s", DATABASE_PATH)

def get_connection():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    _migrate_legacy_database()
    with db_cursor() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                language    TEXT DEFAULT 'en',
                is_banned   INTEGER DEFAULT 0,
                is_admin    INTEGER DEFAULT 0,
                role        TEXT DEFAULT 'user',
                downloads   INTEGER DEFAULT 0,
                join_date   TEXT DEFAULT (datetime('now')),
                last_seen   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS downloads (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                url         TEXT NOT NULL,
                title       TEXT,
                platform    TEXT,
                quality     TEXT,
                media_type  TEXT,
                file_size   INTEGER,
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS file_cache (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash    TEXT NOT NULL,
                quality     TEXT NOT NULL,
                media_type  TEXT NOT NULL,
                file_id     TEXT NOT NULL,
                title       TEXT,
                platform    TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                hits        INTEGER DEFAULT 0,
                UNIQUE(url_hash, quality, media_type)
            );

            CREATE TABLE IF NOT EXISTS broadcast_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id    INTEGER,
                message     TEXT,
                total       INTEGER,
                success     INTEGER,
                failed      INTEGER,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id    INTEGER,
                action      TEXT,
                target_id   INTEGER,
                detail      TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id                INTEGER PRIMARY KEY,
                video_quality          TEXT    DEFAULT 'auto',
                audio_quality          TEXT    DEFAULT 'normal',
                max_file_size_mb       INTEGER DEFAULT 0,
                delivery_mode          TEXT    DEFAULT 'video',
                auto_cleanup           INTEGER DEFAULT 1,
                history_retention_days INTEGER DEFAULT 30,
                notify_complete        INTEGER DEFAULT 1,
                notify_failure         INTEGER DEFAULT 1,
                notify_progress        INTEGER DEFAULT 1,
                notify_queue           INTEGER DEFAULT 1,
                quiet_mode             INTEGER DEFAULT 0,
                updated_at             TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_downloads_user    ON downloads(user_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_date    ON downloads(created_at);
            CREATE INDEX IF NOT EXISTS idx_cache_hash        ON file_cache(url_hash);
            CREATE INDEX IF NOT EXISTS idx_admin_audit_date  ON admin_audit_log(created_at);
        """)
    logger.info("Database initialized.")
    ensure_cache_schema()

def ensure_cache_schema():
    """Additive migrations for fingerprint + vault columns (safe on existing DBs)."""
    with db_cursor() as c:
        cols = {row[1] for row in c.execute("PRAGMA table_info(file_cache)").fetchall()}
        if "fingerprint" not in cols:
            c.execute("ALTER TABLE file_cache ADD COLUMN fingerprint TEXT DEFAULT ''")
        if "vault_chat_id" not in cols:
            c.execute("ALTER TABLE file_cache ADD COLUMN vault_chat_id INTEGER")
        if "vault_message_id" not in cols:
            c.execute("ALTER TABLE file_cache ADD COLUMN vault_message_id INTEGER")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_fingerprint ON file_cache(fingerprint)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_lookup ON file_cache(url_hash, quality, media_type)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_created_hits ON file_cache(created_at, hits)"
        )
        c.execute("""
            CREATE TABLE IF NOT EXISTS media_fingerprint_index (
                fingerprint TEXT NOT NULL,
                quality     TEXT NOT NULL,
                media_type  TEXT NOT NULL,
                url_hash    TEXT NOT NULL,
                file_id     TEXT NOT NULL,
                vault_chat_id INTEGER,
                vault_message_id INTEGER,
                updated_at  TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (fingerprint, quality, media_type)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_fp_url_hash ON media_fingerprint_index(url_hash)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_fp_updated ON media_fingerprint_index(updated_at)"
        )

