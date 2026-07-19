import sqlite3
from contextlib import contextmanager
from config.settings import MAIN_DB_PATH, SUPPORT_DB_PATH


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def main_db():
    conn = _open(MAIN_DB_PATH)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def support_db():
    conn = _open(SUPPORT_DB_PATH)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Statistics ────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    with main_db() as c:
        total_users     = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_downloads = c.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        dl_today        = c.execute("SELECT COUNT(*) FROM downloads WHERE date(created_at)=date('now')").fetchone()[0]
        new_users_today = c.execute("SELECT COUNT(*) FROM users WHERE date(join_date)=date('now')").fetchone()[0]
        total_referrals = c.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        banned_users    = c.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]

    with support_db() as s:
        total_tickets = s.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0]
        open_tickets  = s.execute("SELECT COUNT(*) FROM support_tickets WHERE status='open'").fetchone()[0]

    return {
        "total_users":      total_users,
        "total_downloads":  total_downloads,
        "dl_today":         dl_today,
        "new_users_today":  new_users_today,
        "total_referrals":  total_referrals,
        "banned_users":     banned_users,
        "total_tickets":    total_tickets,
        "open_tickets":     open_tickets,
    }


# ── Users ─────────────────────────────────────────────────────────────────────

def get_user_by_id(user_id: int) -> dict | None:
    with main_db() as c:
        row = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_users_by_username(username: str) -> list[dict]:
    clean = username.lstrip("@")
    with main_db() as c:
        rows = c.execute(
            "SELECT * FROM users WHERE username LIKE ? LIMIT 10",
            (f"%{clean}%",)
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_download_count(user_id: int) -> int:
    with main_db() as c:
        row = c.execute("SELECT COUNT(*) FROM downloads WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else 0


def ban_user(user_id: int) -> bool:
    with main_db() as c:
        c.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
        return c.execute("SELECT changes()").fetchone()[0] > 0


def unban_user(user_id: int) -> bool:
    with main_db() as c:
        c.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
        return c.execute("SELECT changes()").fetchone()[0] > 0


def add_points(user_id: int, amount: int) -> int:
    with main_db() as c:
        c.execute("UPDATE users SET points=points+? WHERE user_id=?", (amount, user_id))
        row = c.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else 0


def remove_points(user_id: int, amount: int) -> int:
    with main_db() as c:
        c.execute("UPDATE users SET points=MAX(0,points-?) WHERE user_id=?", (amount, user_id))
        row = c.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else 0


def get_all_active_user_ids() -> list[int]:
    with main_db() as c:
        rows = c.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
        return [r[0] for r in rows]


def count_all_users() -> int:
    with main_db() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_all_users_paginated(offset: int, limit: int) -> list[dict]:
    with main_db() as c:
        rows = c.execute(
            "SELECT * FROM users ORDER BY join_date DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Broadcast ────────────────────────────────────────────────────────────────

def log_broadcast(admin_id: int, message: str, total: int, success: int, failed: int):
    with main_db() as c:
        c.execute(
            "INSERT INTO broadcast_log(admin_id,message,total,success,failed,created_at) "
            "VALUES(?,?,?,?,?,datetime('now'))",
            (admin_id, message[:500], total, success, failed)
        )


def get_broadcast_history(limit: int = 10) -> list[dict]:
    with main_db() as c:
        rows = c.execute(
            "SELECT * FROM broadcast_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Support ───────────────────────────────────────────────────────────────────

def get_open_tickets(limit: int = 10, offset: int = 0) -> list[dict]:
    with support_db() as s:
        rows = s.execute(
            "SELECT * FROM support_tickets WHERE status='open' "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]


def get_closed_tickets(limit: int = 10, offset: int = 0) -> list[dict]:
    with support_db() as s:
        rows = s.execute(
            "SELECT * FROM support_tickets WHERE status='closed' "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]


def get_ticket(ticket_id: int) -> dict | None:
    with support_db() as s:
        row = s.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
        return dict(row) if row else None


def get_ticket_messages(ticket_id: int) -> list[dict]:
    with support_db() as s:
        rows = s.execute(
            "SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY created_at ASC",
            (ticket_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def count_tickets(status: str) -> int:
    with support_db() as s:
        return s.execute(
            "SELECT COUNT(*) FROM support_tickets WHERE status=?", (status,)
        ).fetchone()[0]


def search_tickets_by_user(user_id: int, limit: int = 10) -> list[dict]:
    with support_db() as s:
        rows = s.execute(
            "SELECT * FROM support_tickets WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Downloads ─────────────────────────────────────────────────────────────────

def get_recent_downloads(limit: int = 50) -> list[dict]:
    with main_db() as c:
        rows = c.execute(
            "SELECT d.*, u.username FROM downloads d "
            "LEFT JOIN users u ON d.user_id=u.user_id "
            "ORDER BY d.created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_incomplete_downloads(limit: int = 50) -> list[dict]:
    with main_db() as c:
        rows = c.execute(
            "SELECT d.*, u.username FROM downloads d "
            "LEFT JOIN users u ON d.user_id=u.user_id "
            "WHERE d.file_size IS NULL OR d.file_size=0 OR d.title='' OR d.title IS NULL "
            "ORDER BY d.created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_platform_stats() -> list[dict]:
    with main_db() as c:
        rows = c.execute(
            "SELECT platform, COUNT(*) as cnt FROM downloads "
            "GROUP BY platform ORDER BY cnt DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_top_content(limit: int = 10) -> list[dict]:
    with main_db() as c:
        rows = c.execute(
            "SELECT title, platform, COUNT(*) as cnt FROM downloads "
            "WHERE title IS NOT NULL AND title != '' "
            "GROUP BY title ORDER BY cnt DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Settings (key-value store) ────────────────────────────────────────────────

def _ensure_settings_table(conn: sqlite3.Connection):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def get_setting(key: str, default: str = "") -> str:
    with main_db() as c:
        _ensure_settings_table(c)
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with main_db() as c:
        _ensure_settings_table(c)
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )


def get_admin_lang(user_id: int) -> str:
    """Return admin's preferred language: settings table first, then main DB user row, else 'en'."""
    override = get_setting(f"admin_lang_{user_id}", "")
    if override in ("en", "ar"):
        return override
    with main_db() as c:
        row = c.execute("SELECT language FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row and row[0] in ("en", "ar"):
            return row[0]
    return "en"


def set_admin_lang(user_id: int, lang: str) -> None:
    set_setting(f"admin_lang_{user_id}", lang)


# ── Security ──────────────────────────────────────────────────────────────────

def get_banned_users(limit: int = 20) -> list[dict]:
    with main_db() as c:
        rows = c.execute(
            "SELECT * FROM users WHERE is_banned=1 ORDER BY last_seen DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_reports(status: str = "open", limit: int = 20) -> list[dict]:
    with main_db() as c:
        rows = c.execute(
            "SELECT * FROM reports WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_suspicious_users(limit: int = 10) -> list[dict]:
    """Users with unusually high download count in the last 24 hours."""
    with main_db() as c:
        rows = c.execute(
            "SELECT user_id, COUNT(*) as cnt FROM downloads "
            "WHERE datetime(created_at) >= datetime('now','-1 day') "
            "GROUP BY user_id HAVING cnt >= 10 ORDER BY cnt DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_spam_reporters(limit: int = 10) -> list[dict]:
    """Users with multiple reports submitted."""
    with main_db() as c:
        rows = c.execute(
            "SELECT user_id, username, COUNT(*) as cnt FROM reports "
            "GROUP BY user_id HAVING cnt >= 2 ORDER BY cnt DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
