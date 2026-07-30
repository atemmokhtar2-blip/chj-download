from .db import db_cursor

def log_download(user_id: int, url: str, title: str, platform: str,
                 quality: str, media_type: str, file_size: int = 0) -> int:
    with db_cursor() as c:
        c.execute("""
            INSERT INTO downloads (user_id, url, title, platform, quality, media_type, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, url, title, platform, quality, media_type, file_size))
        return c.lastrowid

def get_downloads_today() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) as cnt FROM downloads WHERE date(created_at) = date('now')")
        return c.fetchone()["cnt"]

def get_downloads_by_platform() -> dict:
    with db_cursor() as c:
        c.execute("""
            SELECT platform, COUNT(*) AS cnt
            FROM downloads
            GROUP BY platform
        """)
        return {row["platform"]: row["cnt"] for row in c.fetchall()}

def get_total_downloads() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) as cnt FROM downloads")
        return c.fetchone()["cnt"]

def get_user_download_stats(user_id: int) -> dict:
    with db_cursor() as c:
        c.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN media_type = 'video' THEN 1 ELSE 0 END) AS video,
                SUM(CASE WHEN media_type = 'audio' THEN 1 ELSE 0 END) AS audio
            FROM downloads WHERE user_id = ?
        """, (user_id,))
        row = c.fetchone()
        return {"total": row["total"] or 0, "video": row["video"] or 0, "audio": row["audio"] or 0}

def get_downloads_week() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) as cnt FROM downloads WHERE created_at >= datetime('now', '-7 days')")
        return c.fetchone()["cnt"]

def get_downloads_month() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) AS cnt FROM downloads WHERE created_at >= datetime('now', '-30 days')")
        return c.fetchone()["cnt"]
