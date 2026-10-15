import sqlite3
import logging
import os
import re
import shutil
from contextlib import contextmanager
from config.settings import DATABASE_PATH

logger = logging.getLogger(__name__)


# ============================================================================
# Dual-backend main database.
#
# PRIMARY: PostgreSQL (the same DSN pg.py resolves: NEON_DATABASE_URL /
# POSTGRES_URL / postgres DATABASE_URL). When it is set, EVERYTHING — users,
# downloads, cache, settings, tickets, audit logs — lives in Postgres and
# survives restarts/redeploys permanently.
#
# FALLBACK: the SQLite file (DATABASE_PATH) for local dev / hosts without
# Postgres. All SQL below (and in every database/*.py module) is written in
# the portable SQLite dialect; on Postgres it is translated on the fly by
# _pg_translate() — datetime modifiers, INSERT OR IGNORE, AUTOINCREMENT DDL,
# date() helpers and `?` placeholders are all mapped to PG equivalents.
# ============================================================================

_PG_TS_FMT = "YYYY-MM-DD HH24:MI:SS"

# Tables whose PK is a serial ``id`` — INSERTs get RETURNING id appended so
# ``cursor.lastrowid`` keeps working on Postgres.
_ID_RETURN_TABLES = {
    "downloads", "file_cache", "broadcast_log", "admin_audit_log",
    "support_tickets", "support_ticket_messages",
}

_RE_DT_CONCAT_DAYS = re.compile(
    r"datetime\(\s*'now'\s*,\s*'-'\s*\|\|\s*\?\s*\|\|\s*' days'\s*\)", re.I)
_RE_DT_PARAM = re.compile(r"datetime\(\s*'now'\s*,\s*\?\s*\)", re.I)
_RE_DT_LITERAL = re.compile(r"datetime\(\s*'now'\s*,\s*'([^']*)'\s*\)", re.I)
_RE_DT_NOW = re.compile(r"datetime\(\s*'now'\s*\)", re.I)
_RE_DATE_NOW = re.compile(r"(?<![A-Za-z0-9_])date\(\s*'now'\s*\)", re.I)
_RE_DATE_COL = re.compile(r"(?<![A-Za-z0-9_])date\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)", re.I)
_RE_INSERT_OR_IGNORE = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", re.I)
_RE_PK_AUTOINC = re.compile(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.I)
_RE_PK_PLAIN = re.compile(r"INTEGER\s+PRIMARY\s+KEY", re.I)
_RE_INSERT_INTO = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)


def _pg_dsn() -> str:
    """The Postgres DSN in force (resolved once by database/pg.py)."""
    from .pg import PG_DSN
    return PG_DSN


def backend_name() -> str:
    """'postgres' when a Postgres DSN is configured, else 'sqlite'."""
    return "postgres" if _pg_dsn() else "sqlite"


def _pg_now() -> str:
    return f"to_char(now() at time zone 'utc', '{_PG_TS_FMT}')"


def _pg_translate(sql: str) -> str:
    """Translate the portable SQLite dialect used across database/*.py to PG."""
    s = sql
    # datetime('now', '-' || ? || ' days')  ->  make_interval(days => ?)
    s = _RE_DT_CONCAT_DAYS.sub(
        f"to_char((now() at time zone 'utc') - make_interval(days => ?), '{_PG_TS_FMT}')", s)
    # datetime('now', ?)  — param is an SQLite modifier string like
    # '-86400 seconds' / '-30 days'; both are valid PG interval literals.
    s = _RE_DT_PARAM.sub(
        f"to_char((now() at time zone 'utc') + (?)::interval, '{_PG_TS_FMT}')", s)
    # datetime('now', '-7 days')  ->  literal interval
    s = _RE_DT_LITERAL.sub(
        lambda m: f"to_char((now() at time zone 'utc') + interval '{m.group(1)}', '{_PG_TS_FMT}')", s)
    # datetime('now')
    s = _RE_DT_NOW.sub(_pg_now(), s)
    # date('now') / date(col)
    s = _RE_DATE_NOW.sub("CURRENT_DATE", s)
    s = _RE_DATE_COL.sub(r"(\1)::date", s)
    # INSERT OR IGNORE -> INSERT ... ON CONFLICT DO NOTHING
    if _RE_INSERT_OR_IGNORE.search(s):
        s = _RE_INSERT_OR_IGNORE.sub("INSERT INTO", s)
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # DDL types: SQLite AUTOINCREMENT -> PG SERIAL; widen INTEGER -> BIGINT
    # (Telegram ids exceed int4).
    s = _RE_PK_AUTOINC.sub("SERIAL PRIMARY KEY", s)
    s = _RE_PK_PLAIN.sub("BIGINT PRIMARY KEY", s)
    s = re.sub(r"\bINTEGER\b", "BIGINT", s)
    # Placeholders last.
    s = s.replace("?", "%s")
    return s


class _PGCompatCursor:
    """psycopg2 cursor wrapped to behave like the sqlite3 cursor the whole
    codebase expects: ``execute`` returns self (chaining), rows are dicts,
    ``lastrowid`` works, and ``executescript`` is available."""

    def __init__(self, raw):
        self._raw = raw
        self._lastrowid = None

    def execute(self, sql: str, params=()) -> "_PGCompatCursor":
        tsql = _pg_translate(sql)
        self._lastrowid = None
        m = _RE_INSERT_INTO.match(tsql)
        if (m and m.group(1).lower() in _ID_RETURN_TABLES
                and "RETURNING" not in tsql.upper()):
            tsql += " RETURNING id"
            self._raw.execute(tsql, tuple(params))
            row = self._raw.fetchone()
            self._lastrowid = int(row["id"]) if row else None
        else:
            self._raw.execute(tsql, tuple(params))
        return self

    def executemany(self, sql: str, seq) -> "_PGCompatCursor":
        self._raw.executemany(_pg_translate(sql), [tuple(p) for p in seq])
        return self

    def executescript(self, script: str) -> "_PGCompatCursor":
        for stmt in script.split(";"):
            if stmt.strip():
                self._raw.execute(_pg_translate(stmt))
        return self

    def fetchone(self):
        row = self._raw.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(r) for r in self._raw.fetchall()]

    def fetchmany(self, size: int):
        return [dict(r) for r in self._raw.fetchmany(size)]

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount

    @property
    def lastrowid(self):
        return self._lastrowid


def table_columns(table: str) -> set:
    """Column names of a table, on BOTH backends (replaces PRAGMA table_info)."""
    if _pg_dsn():
        with db_cursor() as c:
            c.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ?", (table,),
            )
            return {row["column_name"] for row in c.fetchall()}
    with db_cursor() as c:
        return {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}


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
    # Durability: guarantee that committed writes survive process/OS/power
    # failure (WAL's default NORMAL is crash-safe but NOT power-loss-safe).
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

@contextmanager
def db_cursor():
    dsn = _pg_dsn()
    if dsn:
        # ---- Postgres path: same interface, translated SQL ----
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(dsn, connect_timeout=15)
        try:
            raw = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            yield _PGCompatCursor(raw)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return
    # ---- SQLite fallback (local dev / hosts without Postgres) ----
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
    if backend_name() == "postgres":
        logger.info("Main database backend: postgres (persistent)")
    else:
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

            CREATE TABLE IF NOT EXISTS support_tickets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_no   TEXT NOT NULL UNIQUE,
                user_id     INTEGER NOT NULL,
                category    TEXT NOT NULL DEFAULT 'other',
                subject     TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'open',
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS support_ticket_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   INTEGER NOT NULL,
                sender      TEXT NOT NULL DEFAULT 'user',
                body        TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (ticket_id) REFERENCES support_tickets(id)
            );

            CREATE INDEX IF NOT EXISTS idx_support_user ON support_tickets(user_id);
            CREATE INDEX IF NOT EXISTS idx_support_msg  ON support_ticket_messages(ticket_id);
        """)
    logger.info("Database initialized on backend=%s", backend_name())
    ensure_cache_schema()
    ensure_owner_schema()


def ensure_owner_schema():
    """Additive migrations for the owner panel (safe on existing DBs).

    * ``users.ban_reason`` / ``users.banned_at`` — every ban carries a reason
      and a timestamp (shown in the owner's banned list and audit trail).
    * ``extra_admins`` — admins granted at runtime BY THE OWNER (never owners).
    """
    with db_cursor() as c:
        cols = {row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()}
        if "ban_reason" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT DEFAULT ''")
        if "banned_at" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN banned_at TEXT")
        c.execute("""
            CREATE TABLE IF NOT EXISTS extra_admins (
                user_id   INTEGER PRIMARY KEY,
                added_at  TEXT DEFAULT (datetime('now'))
            )
        """)

def ensure_cache_schema():
    """Additive migrations for fingerprint + vault columns (safe on existing DBs)."""
    # Same PRAGMA-on-Postgres hazard as ensure_owner_schema — use the
    # dual-backend table_columns() helper instead.
    cols = table_columns("file_cache")
    with db_cursor() as c:
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

