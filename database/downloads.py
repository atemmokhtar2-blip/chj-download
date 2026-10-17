from .db import db_cursor

def log_download(user_id: int, url: str, title: str, platform: str,
                 quality: str, media_type: str, file_size: int = 0,
                 instance_id: int = 0) -> int:
    with db_cursor() as c:
        c.execute("""
            INSERT INTO downloads (user_id, url, title, platform, quality, media_type,
                                   file_size, instance_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, url, title, platform, quality, media_type, file_size,
              int(instance_id or 0)))
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


def get_platform_analytics(days: int = 7) -> list[dict]:
    """Per-platform download count + share % + data volume over the last N days."""
    days = max(1, min(int(days or 7), 365))
    with db_cursor() as c:
        c.execute("""
            SELECT COALESCE(platform, 'unknown') AS platform,
                   COUNT(*) AS cnt,
                   COALESCE(SUM(file_size), 0) AS bytes
            FROM downloads
            WHERE created_at >= datetime('now', ?)
            GROUP BY COALESCE(platform, 'unknown')
            ORDER BY cnt DESC
        """, (f"-{days} days",))
        rows = [dict(r) for r in c.fetchall()]
    total = sum(r["cnt"] for r in rows) or 1
    for r in rows:
        r["share"] = round(int(r["cnt"]) * 100.0 / total, 1)
    return rows


def get_suspicious_users(limit: int = 15) -> list[dict]:
    """Users whose last-24h pattern looks abusive: many downloads but few
    distinct URLs (same link hammered) — the classic abuse/bot signal."""
    with db_cursor() as c:
        c.execute("""
            SELECT d.user_id,
                   MAX(u.username) AS username,
                   MAX(u.first_name) AS first_name,
                   COUNT(*) AS total,
                   COUNT(DISTINCT d.url) AS unique_urls,
                   MAX(d.created_at) AS last_dl
            FROM downloads d
            JOIN users u ON u.user_id = d.user_id
            WHERE d.created_at >= datetime('now', '-1 day')
              AND u.is_banned = 0
            GROUP BY d.user_id
            HAVING COUNT(*) >= 6
               AND (COUNT(*) * 1.0 / (COUNT(DISTINCT d.url) * 1.0 + 1e-6)) >= 2.0
            ORDER BY total DESC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in c.fetchall()]


def get_top_users_by_size(limit: int = 10) -> list[dict]:
    """Heaviest bandwidth consumers (real bytes downloaded, all time)."""
    with db_cursor() as c:
        c.execute("""
            SELECT u.user_id,
                   MAX(u.username) AS username,
                   MAX(u.first_name) AS first_name,
                   COUNT(*) AS downloads,
                   COALESCE(SUM(d.file_size), 0) AS bytes
            FROM downloads d
            JOIN users u ON u.user_id = d.user_id
            GROUP BY u.user_id
            ORDER BY bytes DESC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in c.fetchall()]
