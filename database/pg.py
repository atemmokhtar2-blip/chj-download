"""PostgreSQL persistence for the referral & clone-instance network.

PostgreSQL is the ONLY backend (the former SQLite fallback has been removed
completely). The DSN is resolved ONCE at import time from
``NEON_DATABASE_URL`` (preferred), ``POSTGRES_URL``, or a postgres
``DATABASE_URL``. Without a DSN every operation fails loudly with an
actionable error instead of silently degrading — one database, no split
brain between environments.

SQL in this module is written with ``?`` placeholders; they are translated
to ``%s`` before execution. Rows always come back as plain dicts. Ids that
need to be returned use ``insert_returning_id`` (PostgreSQL ``RETURNING``).

Connections are short-lived (open → execute → commit → close). Referral
traffic is low-frequency, and Neon/serverless Postgres is designed for
exactly this pattern.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_local = threading.local()

MISSING_DSN_ERROR = (
    "PostgreSQL is the only database backend, but no DSN is configured. "
    "Set NEON_DATABASE_URL (or POSTGRES_URL / DATABASE_URL) to your "
    "PostgreSQL connection string."
)


def _resolve_pg_dsn() -> str:
    for name in ("NEON_DATABASE_URL", "POSTGRES_URL", "DATABASE_URL"):
        dsn = os.getenv(name, "").strip()
        if dsn.startswith("postgres"):
            return dsn
    return ""


PG_DSN = _resolve_pg_dsn()
BACKEND = "postgres"


def backend_name() -> str:
    return BACKEND


def _translate(sql: str) -> str:
    """Convert portable `?` placeholders to psycopg2's `%s`."""
    return sql.replace("?", "%s")


class _Cursor:
    """Wraps a psycopg2 cursor into the dict-row interface callers expect."""

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


@contextmanager
def ref_cursor(commit: bool = True):
    """Yield a dict-row cursor; commit on success, rollback on error."""
    if not PG_DSN:
        raise RuntimeError(MISSING_DSN_ERROR)
    conn = None
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(PG_DSN, connect_timeout=15)
        raw = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
    """INSERT and return the new row id (PostgreSQL ``RETURNING``).

    ``sql`` must NOT include a RETURNING clause; it is appended here.
    """
    with ref_cursor() as c:
        c.execute(sql + " RETURNING id", params)
        row = c.fetchone()
        return int(row["id"]) if row else None


# ---- schema -----------------------------------------------------------------

def _pk_col() -> str:
    return "SERIAL PRIMARY KEY"


def _bigpk() -> str:
    return "BIGINT PRIMARY KEY"


def _bigint() -> str:
    return "BIGINT"


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
            notified_milestone INTEGER DEFAULT 0,
            claimed_at TIMESTAMP,
            instance_id {bi}
        )""",
        f"""CREATE TABLE IF NOT EXISTS bot_instances (
            id {pk},
            owner_id {bi} NOT NULL,
            bot_id {bi} UNIQUE,
            bot_username TEXT DEFAULT '',
            status TEXT DEFAULT 'pending_token',
            plan TEXT DEFAULT 'basic',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            activated_at TIMESTAMP,
            expires_at TIMESTAMP,
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
        # Per-instance clone configuration. NOTE: deliberately named
        # ``clone_settings`` (NOT ``bot_settings``): the main database has
        # its OWN global key/value ``bot_settings`` table (database/db.py) —
        # same name meant whichever DDL ran first broke the other's
        # ON CONFLICT clauses.
        f"""CREATE TABLE IF NOT EXISTS clone_settings (
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
        # Owner-issued redemption codes ("أكواد التفعيل"). One row per code;
        # uses_consumed counts RESERVED+CONFIRMED activations and is guarded
        # atomically (UPDATE ... WHERE uses_consumed < max_uses) so a code can
        # never be over-used, even by simultaneous redemptions.
        f"""CREATE TABLE IF NOT EXISTS redemption_codes (
            id {pk},
            code TEXT NOT NULL UNIQUE,
            plan TEXT DEFAULT 'basic',
            runtime_days INTEGER DEFAULT 30,
            max_uses INTEGER DEFAULT 1,
            uses_consumed INTEGER DEFAULT 0,
            valid_days INTEGER DEFAULT 7,
            status TEXT DEFAULT 'active',
            note TEXT DEFAULT '',
            created_by {bi},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        )""",
        # Append-only audit trail for codes: created / reserved / used /
        # released / revoked / exhausted. Full code text and bot tokens are
        # NEVER written here — only ids, prefixes and safe metadata.
        f"""CREATE TABLE IF NOT EXISTS redemption_log (
            id {pk},
            code_id {bi} NOT NULL,
            action TEXT DEFAULT '',
            user_id {bi},
            instance_id {bi},
            details TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals (referrer_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_instances_owner ON bot_instances (owner_id)",
        "CREATE INDEX IF NOT EXISTS idx_instances_status ON bot_instances (status)",
        "CREATE INDEX IF NOT EXISTS idx_redemption_log_code ON redemption_log (code_id, action)",
    ]
    with ref_cursor() as c:
        for stmt in ddl:
            c.execute(stmt)
        for stmt in indexes:
            try:
                c.execute(stmt)
            except Exception:
                logger.debug("Index skipped: %s", stmt[:60])
    ensure_reward_milestone_column()
    ensure_instance_plan_columns()
    ensure_clone_settings_migration()
    logger.info("Referral schema ready on backend=%s", BACKEND)


def ensure_clone_settings_migration() -> None:
    """Migrate legacy per-instance settings out of the colliding ``bot_settings``.

    Databases created before the rename may hold a ``bot_settings`` table in
    the REFERRAL shape (instance_id, key, value), which hijacks the main
    database's global key/value ``bot_settings`` and breaks
    ``ON CONFLICT(key)`` upserts. This copies any legacy rows into
    ``clone_settings`` and drops the mis-shaped table so the main schema can
    recreate its own. Idempotent.
    """
    try:
        row = fetchone("SELECT to_regclass('public.bot_settings') AS t")
        if row and row.get("t"):
            cols = fetchall(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'bot_settings'"
            )
            names = {c["column_name"] for c in cols}
            if "instance_id" in names:
                execute(
                    "INSERT INTO clone_settings (instance_id, key, value) "
                    "SELECT instance_id, key, value FROM bot_settings "
                    "ON CONFLICT (instance_id, key) DO NOTHING"
                )
                execute("DROP TABLE bot_settings")
                logger.info("Migrated legacy referral bot_settings -> clone_settings")
    except Exception:
        logger.exception("clone_settings migration failed")


def ensure_instance_plan_columns() -> None:
    """Additive migration: clone plan (basic/pro) + validity-window columns.

    New installs get them from the CREATE TABLE DDL above; this upgrades
    databases created before the plan system existed.
    """
    try:
        execute("ALTER TABLE bot_instances ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'basic'")
        execute("ALTER TABLE bot_instances ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP")
    except Exception:
        logger.exception("instance plan columns migration failed")


def ensure_reward_milestone_column() -> None:
    """Additive migration for the ``notified_milestone`` reward column.

    New installs already get it from the CREATE TABLE DDL above; this
    upgrades databases created before milestone notifications existed.
    """
    try:
        execute(
            "ALTER TABLE referral_rewards "
            "ADD COLUMN IF NOT EXISTS notified_milestone INTEGER DEFAULT 0"
        )
    except Exception:
        logger.exception("referral milestone column migration failed")
