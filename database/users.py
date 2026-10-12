from __future__ import annotations

from .db import db_cursor

# Allowlist for update_user: prevents SQL injection via arbitrary column names.
_ALLOWED_USER_FIELDS = {
    "username", "first_name", "last_name", "language",
    "is_banned", "is_admin", "role", "downloads", "last_seen",
}


def get_user(user_id: int) -> dict | None:
    with db_cursor() as c:
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        return dict(row) if row else None

def create_user(user_id: int, username: str, first_name: str, last_name: str,
                language: str = "en") -> dict:
    with db_cursor() as c:
        c.execute("""
            INSERT OR IGNORE INTO users
            (user_id, username, first_name, last_name, language)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, username, first_name, last_name, language))
    return get_user(user_id)

def update_user(user_id: int, **kwargs):
    if not kwargs:
        return
    safe = {k: v for k, v in kwargs.items() if k in _ALLOWED_USER_FIELDS}
    if not safe:
        return
    fields = ", ".join(f"{k} = ?" for k in safe)
    values = list(safe.values()) + [user_id]
    with db_cursor() as c:
        c.execute(f"UPDATE users SET {fields} WHERE user_id = ?", values)

def update_last_seen(user_id: int):
    with db_cursor() as c:
        c.execute("UPDATE users SET last_seen = datetime('now') WHERE user_id = ?", (user_id,))

def increment_downloads(user_id: int):
    with db_cursor() as c:
        c.execute("UPDATE users SET downloads = downloads + 1 WHERE user_id = ?", (user_id,))

def get_all_user_ids() -> list[int]:
    with db_cursor() as c:
        c.execute("SELECT user_id FROM users WHERE is_banned = 0")
        return [row["user_id"] for row in c.fetchall()]

def get_total_users() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) as cnt FROM users")
        return c.fetchone()["cnt"]

def get_banned_users_count() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) as cnt FROM users WHERE is_banned = 1")
        return c.fetchone()["cnt"]

def get_users_count() -> int:
    return get_total_users()

def get_new_users_today() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) as cnt FROM users WHERE date(join_date) = date('now')")
        return c.fetchone()["cnt"]

def ban_user(user_id: int):
    with db_cursor() as c:
        c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))

def unban_user(user_id: int):
    with db_cursor() as c:
        c.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))

def get_users_page(offset: int = 0, limit: int = 20) -> list[dict]:
    """User rows with the REAL download count from the downloads table."""
    with db_cursor() as c:
        c.execute("""
            SELECT u.user_id, u.username, u.first_name, u.last_name,
                   u.is_banned, u.language, u.join_date, u.last_seen,
                   COALESCE(d.cnt, 0) AS downloads
            FROM users u
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS cnt
                FROM downloads
                GROUP BY user_id
            ) d ON d.user_id = u.user_id
            ORDER BY u.join_date DESC, u.user_id DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        return [dict(row) for row in c.fetchall()]

def get_top_users(limit: int = 5) -> list[dict]:
    """Top downloaders by REAL count from the downloads table."""
    with db_cursor() as c:
        c.execute("""
            SELECT u.user_id, u.first_name, u.username,
                   COALESCE(d.cnt, 0) AS downloads
            FROM users u
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS cnt
                FROM downloads
                GROUP BY user_id
            ) d ON d.user_id = u.user_id
            ORDER BY downloads DESC, u.user_id ASC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in c.fetchall()]

def get_active_today() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) AS cnt FROM users WHERE date(last_seen) = date('now')")
        return c.fetchone()["cnt"]

def get_active_24h() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) AS cnt FROM users WHERE last_seen >= datetime('now', '-1 day')")
        return c.fetchone()["cnt"]

def search_users(query: str) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    with db_cursor() as c:
        if query.lstrip("-").isdigit():
            try:
                user_id = int(query)
            except ValueError:
                return []
            c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        else:
            term = query.lstrip("@").lower()
            c.execute(
                "SELECT * FROM users WHERE lower(username) = ? OR lower(first_name) LIKE ?",
                (term, f"%{term}%"),
            )
        return [dict(row) for row in c.fetchall()]
