from .db import db_cursor

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
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [user_id]
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
    with db_cursor() as c:
        c.execute("""
            SELECT user_id, username, first_name, downloads, is_banned
            FROM users ORDER BY join_date DESC LIMIT ? OFFSET ?
        """, (limit, offset))
        return [dict(row) for row in c.fetchall()]

def get_active_today() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) AS cnt FROM users WHERE date(last_seen) = date('now')")
        return c.fetchone()["cnt"]

def search_users(query: str) -> list[dict]:
    with db_cursor() as c:
        if query.lstrip("-").isdigit():
            c.execute(
                "SELECT * FROM users WHERE user_id = ?", (int(query),)
            )
        else:
            term = query.lstrip("@").lower()
            c.execute(
                "SELECT * FROM users WHERE lower(username) = ? OR lower(first_name) LIKE ?",
                (term, f"%{term}%")
            )
        return [dict(row) for row in c.fetchall()]
