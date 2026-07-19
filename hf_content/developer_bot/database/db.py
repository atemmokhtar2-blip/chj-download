import sqlite3
import os
from contextlib import contextmanager
from config.settings import DEV_DB_PATH


@contextmanager
def dev_db():
    conn = sqlite3.connect(DEV_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with dev_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS action_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id    INTEGER NOT NULL,
                action      TEXT NOT NULL,
                detail      TEXT,
                result      TEXT,
                ts          DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS backups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                path        TEXT NOT NULL,
                size_bytes  INTEGER DEFAULT 0,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                note        TEXT
            );

            CREATE TABLE IF NOT EXISTS updates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                version     TEXT NOT NULL,
                filename    TEXT NOT NULL,
                applied_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                status      TEXT DEFAULT 'pending',
                note        TEXT
            );
        """)


def log_action(owner_id: int, action: str, detail: str = "", result: str = "ok"):
    with dev_db() as conn:
        conn.execute(
            "INSERT INTO action_log (owner_id, action, detail, result) VALUES (?, ?, ?, ?)",
            (owner_id, action, detail, result),
        )


def get_recent_actions(limit: int = 20):
    with dev_db() as conn:
        rows = conn.execute(
            "SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def save_backup(name: str, path: str, size: int, note: str = ""):
    with dev_db() as conn:
        conn.execute(
            "INSERT INTO backups (name, path, size_bytes, note) VALUES (?, ?, ?, ?)",
            (name, path, size, note),
        )


def get_backups():
    with dev_db() as conn:
        rows = conn.execute(
            "SELECT * FROM backups ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_backup_record(backup_id: int):
    with dev_db() as conn:
        conn.execute("DELETE FROM backups WHERE id = ?", (backup_id,))


def save_update_record(version: str, filename: str, status: str = "applied", note: str = ""):
    with dev_db() as conn:
        conn.execute(
            "INSERT INTO updates (version, filename, status, note) VALUES (?, ?, ?, ?)",
            (version, filename, status, note),
        )


def get_updates():
    with dev_db() as conn:
        rows = conn.execute(
            "SELECT * FROM updates ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]
