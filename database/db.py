import sqlite3
import logging
from contextlib import contextmanager
from config.settings import DATABASE_PATH

logger = logging.getLogger(__name__)


def get_connection():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
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
                points      INTEGER DEFAULT 0,
                downloads   INTEGER DEFAULT 0,
                referrals   INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                join_date   TEXT DEFAULT (datetime('now')),
                last_seen   TEXT DEFAULT (datetime('now')),
                last_daily  TEXT,
                vip_until   TEXT
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

            CREATE TABLE IF NOT EXISTS favorites (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                url         TEXT NOT NULL,
                title       TEXT,
                platform    TEXT,
                thumbnail   TEXT,
                added_at    TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, url),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS achievements (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                achievement_id TEXT NOT NULL,
                earned_at   TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, achievement_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                username    TEXT,
                platform    TEXT,
                url         TEXT,
                message     TEXT,
                status      TEXT DEFAULT 'open',
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS support_tickets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                message     TEXT,
                reply       TEXT,
                status      TEXT DEFAULT 'open',
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
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

            CREATE TABLE IF NOT EXISTS rewards_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                reward_cost INTEGER,
                reward_name TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                download_id INTEGER,
                rating      TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id     INTEGER NOT NULL,
                referred_id     INTEGER NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'pending',
                reward_given    INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                completed_at    TEXT,
                UNIQUE(referred_id),
                FOREIGN KEY (referrer_id) REFERENCES users(user_id),
                FOREIGN KEY (referred_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS referral_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event       TEXT    NOT NULL,
                referrer_id INTEGER,
                referred_id INTEGER NOT NULL,
                detail      TEXT    DEFAULT '',
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_downloads_user    ON downloads(user_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_date    ON downloads(created_at);
            CREATE INDEX IF NOT EXISTS idx_cache_hash        ON file_cache(url_hash);
            CREATE INDEX IF NOT EXISTS idx_users_referral    ON users(referral_code);
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_referred ON referrals(referred_id);
            CREATE INDEX IF NOT EXISTS idx_referral_audit    ON referral_audit_log(referrer_id);
        """)
    # Idempotent migrations — add columns that may not exist in older installs
    _migrate()
    logger.info("Database initialized.")


def _migrate():
    """Run ALTER TABLE migrations safely — ignores errors if column already exists."""
    migrations = [
        "ALTER TABLE reports ADD COLUMN reply TEXT",
        "ALTER TABLE reports ADD COLUMN closed_by INTEGER",
        "ALTER TABLE reports ADD COLUMN closed_at TEXT",
        # Lucky wheel cooldown tracking
        "ALTER TABLE users ADD COLUMN last_wheel TEXT",
    ]
    for sql in migrations:
        try:
            with db_cursor() as c:
                c.execute(sql)
        except Exception:
            pass
