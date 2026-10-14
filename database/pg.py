"""Dual-backend persistence for the referral & clone-instance network.

PRIMARY: Neon PostgreSQL (serverless Postgres) — set ``NEON_DATABASE_URL``
(or ``POSTGRES_URL`` / a postgres ``DATABASE_URL``). This is the production
path: all referral, reward, instance and secret tables live in Neon.

FALLBACK: the bot's existing SQLite file (``DATABASE_PATH``) with the exact
same tables, so local development and hosts without Postgres keep working.

SQL in this module is written with ``?`` placeholders; on Postgres they are
translated to ``%s``. Rows always come back as plain dicts on both backends.
Ids that need to be returned use ``insert_returning_id`` (PG ``RETURNING``,
SQLite ``lastrowid``).

Connections are short-lived (open → execute → commit → close). Referral
traffic is low-frequency, and Neon is designed for exactly this pattern.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager

from config.settings import DATABASE_PATH

logger = logging.getLogger(__name__)

_local = threading.local()


def _resolve_pg_dsn() -> str:
    for name in ("NEON_DATABASE_URL", "POSTGRES_URL", "DATABASE_URL"):
        dsn = os.getenv(name, "").strip()
        if dsn.startswith("postgres"):
            return dsn
    return ""


PG_DSN = _resolve_pg_dsn()
BACKEND = "postgres" if PG_DSN else "sqlite"


def backend_name() -> str:
    return BACKEND


def _translate(sql: str) -> str:
    """Convert portable `?` placeholders to psycopg2's `%s` on Postgres."""
    if BACKEND == "postgres":
        return sql.replace("?", "%s")
    return sql


class _Cursor:
    """Normalizes sqlite3 / psycopg2 cursors into a dict-row interface."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params: tuple = ()) -> "_Cursor":
        self._raw.execute(_translate(sql), tuple(params))
        return self

    def fetchone(self) -> dict | None:
        row = self._raw.fetchone()
        if row is None:
            return None
        return dict(row)

    def fetchall(self) -> list[dict]:
        return [dict(r) for r in self._raw.fetchall()]

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount

    @property
    def lastrowid(self):
        return getattr(self._raw, "lastrowid", None)


@contextmanager
def ref_cursor(commit: bool = True):
    """Yield a dict-row cursor; commit on success, rollback on error."""
    conn = None
    try:
        if BACKEND == "postgres":
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(PG_DSN, connect_timeout=15)
            raw = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            conn = sqlite3.connect(DATABASE_PATH, timeout=30)
            conn.row_factory = sqlite3.Row
            raw = conn.cursor()
        cur = _Cursor(raw)
        yield cur
        if commit:
            conn.commit()
    except Exception:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def execute(sql: str, params: tuple = ()) -> int:
    """Run a statement; returns affected rowcount."""
    with ref_cursor() as c:
        c.execute(sql, params)
        return c.rowcount


def fetchone(sql: str, params: tuple = ()) -> dict | None:
    with ref_cursor(commit=False) as c:
        c.execute(sql, params)
        return c.fetchone()


def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    with ref_cursor(commit=False) as c:
        c.execute(sql, params)
        return c.fetchall()


def insert_returning_id(sql: str, params: tuple = ()) -> int | None:
    """INSERT and return the new row id on both backends.

    ``sql`` must NOT include a RETURNING clause; it is appended for Postgres,
    while SQLite uses ``lastrowid``.
    """
    with ref_cursor() as c:
        if BACKEND == "postgres":
            c.execute(sql + " RETURNING id", params)
            row = c.fetchone()
            return int(row["id"]) if row else None
        c.execute(sql, params)
        return c.lastrowid


# ---- schema -----------------------------------------------------------------

def _pk_col() -> str:
    return "SERIAL PRIMARY KEY" if BACKEND == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"


def _bigpk() -> str:
    return "BIGINT PRIMARY KEY" if BACKEND == "postgres" else "INTEGER PRIMARY KEY"


def _bigint() -> str:
    return "BIGINT" if BACKEND == "postgres" else "INTEGER"


def init_referral_schema() -> None:
    """Create referral/instance tables (idempotent). Safe to call on boot."""
    pk = _pk_col()
    bpk = _bigpk()
    bi = _bigint()
    ddl = [
        # Mirror of Telegram users known to the referral network.
        f"""CREATE TABLE IF NOT EXISTS ref_users (
            telegram_id {bpk},
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            source TEXT DEFAULT 'main',
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active_at TIMESTAMP,
            activity_days TEXT DEFAULT ''
        )""",
        # One permanent referral code per owner.
        f"""CREATE TABLE IF NOT EXISTS referral_codes (
            owner_id {bi} NOT NULL UNIQUE,
            code TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        # referrer -> referred bindings. referred_id is UNIQUE: every account
        # can only ever be attributed to ONE referrer, once.
        f"""CREATE TABLE IF NOT EXISTS referrals (
            id {pk},
            referrer_id {bi} NOT NULL,
            referred_id {bi} NOT NULL UNIQUE,
            code TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            bound_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            qualified_at TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS referral_rewards (
            user_id {bpk},
            qualified_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'progress',
            claimed_at TIMESTAMP,
            instance_id {bi}
        )""",
        f"""CREATE TABLE IF NOT EXISTS bot_instances (
            id {pk},
            owner_id {bi} NOT NULL,
            bot_id {bi} UNIQUE,
            bot_username TEXT DEFAULT '',
            status TEXT DEFAULT 'pending_token',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            activated_at TIMESTAMP,
            last_error TEXT DEFAULT ''
        )""",
        # Encrypted bot tokens. token_hash (sha256) enforces "never linked
        # twice" without ever storing or comparing plaintext.
        f"""CREATE TABLE IF NOT EXISTS bot_secrets (
            instance_id {bi} PRIMARY KEY,
            token_encrypted TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS bot_settings (
            instance_id {bi} NOT NULL,
            key TEXT NOT NULL,
            value TEXT DEFAULT '',
            PRIMARY KEY (instance_id, key)
        )""",
        f"""CREATE TABLE IF NOT EXISTS activation_log (
            id {pk},
            owner_id {bi},
            instance_id {bi},
            action TEXT DEFAULT '',
            details TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals (referrer_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_instances_owner ON bot_instances (owner_id)",
        "CREATE INDEX IF NOT EXISTS idx_instances_status ON bot_instances (status)",
    ]
    with ref_cursor() as c:
        for stmt in ddl:
            c.execute(stmt)
        for stmt in indexes:
            try:
                c.execute(stmt)
            except Exception:
                logger.debug("Index skipped: %s", stmt[:60])
    logger.info("Referral schema ready on backend=%s", BACKEND)
