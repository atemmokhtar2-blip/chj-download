from .db import db_cursor

def log_download(user_id: int, url: str, title: str, platform: str,
                 quality: str, media_type: str, file_size: int = 0) -> int:
    with db_cursor() as c:
        c.execute("""
            INSERT INTO downloads (user_id, url, title, platform, quality, media_type, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, url, title, platform, quality, media_type, file_size))
        row_id = c.lastrowid

    # Referral rule (a): a user's FIRST successful download qualifies their
    # pending referral. Hooked here because every successful delivery path
    # funnels through log_download — one choke point, no missed paths.
    try:
        from services import referral
        referral.on_successful_download(user_id)
    except Exception:
        pass
    return row_id

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

def get_downloads_24h() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) AS cnt FROM downloads WHERE created_at >= datetime('now', '-1 day')")
        return c.fetchone()["cnt"]

def get_total_data_bytes() -> int:
    with db_cursor() as c:
        c.execute("SELECT COALESCE(SUM(file_size), 0) AS total FROM downloads")
        return int(c.fetchone()["total"] or 0)

def get_top_user_today(limit: int = 3) -> list[dict]:
    """Most active downloader(s) today, by real download count."""
    with db_cursor() as c:
        c.execute("""
            SELECT u.user_id, u.first_name, u.username, d.downloads
            FROM (
                SELECT user_id, COUNT(*) AS downloads
                FROM downloads
                WHERE date(created_at) = date('now')
                GROUP BY user_id
            ) d
            JOIN users u ON u.user_id = d.user_id
            ORDER BY d.downloads DESC, u.user_id ASC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in c.fetchall()]
