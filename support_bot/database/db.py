import sqlite3
from contextlib import contextmanager
from config.settings import SUPPORT_DB_PATH, MAIN_DB_PATH


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_cursor():
    conn = _open(SUPPORT_DB_PATH)
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def is_main_bot_user(user_id: int) -> bool:
    """Return True only if this user_id exists in the main bot's users table."""
    try:
        conn = _open(MAIN_DB_PATH)
        row = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def init_db():
    with db_cursor() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                username    TEXT    DEFAULT '',
                first_name  TEXT    DEFAULT '',
                status      TEXT    DEFAULT 'open',
                created_at  TEXT    DEFAULT (datetime('now')),
                closed_at   TEXT,
                closed_by   INTEGER
            );

            CREATE TABLE IF NOT EXISTS ticket_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   INTEGER NOT NULL,
                sender_id   INTEGER NOT NULL,
                sender_role TEXT    NOT NULL,
                message     TEXT    NOT NULL,
                created_at  TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (ticket_id) REFERENCES support_tickets(id)
            );

            CREATE INDEX IF NOT EXISTS idx_st_user    ON support_tickets(user_id);
            CREATE INDEX IF NOT EXISTS idx_st_status  ON support_tickets(status);
            CREATE INDEX IF NOT EXISTS idx_tm_ticket  ON ticket_messages(ticket_id);
        """)
