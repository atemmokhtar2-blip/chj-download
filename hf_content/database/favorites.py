from .db import db_cursor


def add_favorite(user_id: int, url: str, title: str, platform: str, thumbnail: str = "") -> bool:
    with db_cursor() as c:
        try:
            c.execute("""
                INSERT OR IGNORE INTO favorites (user_id, url, title, platform, thumbnail)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, url, title, platform, thumbnail))
            return c.rowcount > 0
        except Exception:
            return False


def remove_favorite(user_id: int, url: str) -> bool:
    with db_cursor() as c:
        c.execute("DELETE FROM favorites WHERE user_id = ? AND url = ?", (user_id, url))
        return c.rowcount > 0


def get_favorites(user_id: int) -> list[dict]:
    with db_cursor() as c:
        c.execute("""
            SELECT url, title, platform, thumbnail, added_at
            FROM favorites WHERE user_id = ?
            ORDER BY added_at DESC
        """, (user_id,))
        return [dict(row) for row in c.fetchall()]


def is_favorite(user_id: int, url: str) -> bool:
    with db_cursor() as c:
        c.execute("SELECT 1 FROM favorites WHERE user_id = ? AND url = ?", (user_id, url))
        return c.fetchone() is not None
