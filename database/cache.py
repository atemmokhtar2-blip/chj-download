import hashlib
from .db import db_cursor


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def get_cached(url: str, quality: str, media_type: str) -> str | None:
    h = url_hash(url)
    with db_cursor() as c:
        c.execute("""
            SELECT file_id FROM file_cache
            WHERE url_hash = ? AND quality = ? AND media_type = ?
        """, (h, quality, media_type))
        row = c.fetchone()
        if row:
            c.execute("""
                UPDATE file_cache SET hits = hits + 1
                WHERE url_hash = ? AND quality = ? AND media_type = ?
            """, (h, quality, media_type))
            return row["file_id"]
        return None


def set_cache(url: str, quality: str, media_type: str, file_id: str,
              title: str = "", platform: str = ""):
    h = url_hash(url)
    with db_cursor() as c:
        c.execute("""
            INSERT OR REPLACE INTO file_cache
            (url_hash, quality, media_type, file_id, title, platform)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (h, quality, media_type, file_id, title, platform))


def get_cache_count() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) as cnt FROM file_cache")
        return c.fetchone()["cnt"]


def get_cache_hits() -> int:
    with db_cursor() as c:
        c.execute("SELECT COALESCE(SUM(hits), 0) as total FROM file_cache")
        return c.fetchone()["total"]


def cleanup_old_cache(days: int = 30):
    with db_cursor() as c:
        c.execute("""
            DELETE FROM file_cache
            WHERE created_at < datetime('now', '-' || ? || ' days')
        """, (days,))
