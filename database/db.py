import sqlite3
import logging
from contextlib import contextmanager
from config.settings import DATABASE_PATH

logger = logging.getLogger(__name__)

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

            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_downloads_user    ON downloads(user_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_date    ON downloads(created_at);
            CREATE INDEX IF NOT EXISTS idx_cache_hash        ON file_cache(url_hash);
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

