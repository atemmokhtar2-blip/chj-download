"""Shared database utility functions for the control panel."""
import sqlite3
from contextlib import contextmanager
from .config import MAIN_DB, SUPPORT_DB, DEV_DB


@contextmanager
def main_db():
    conn = sqlite3.connect(MAIN_DB, check_same_thread=False, timeout=10)
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


@contextmanager
def support_db():
    conn = sqlite3.connect(SUPPORT_DB, check_same_thread=False, timeout=10)
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


@contextmanager
def dev_db():
    conn = sqlite3.connect(DEV_DB, check_same_thread=False, timeout=10)
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


def fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    elif b < 1024**2:
        return f"{b/1024:.1f} KB"
    elif b < 1024**3:
        return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def get_dashboard_stats() -> dict:
    stats = {
        "total_users": 0, "vip_users": 0, "banned_users": 0,
        "total_downloads": 0, "today_downloads": 0, "week_downloads": 0,
        "total_referrals": 0, "open_tickets": 0, "active_today": 0,
        "total_points_issued": 0,
    }
    try:
        with main_db() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
            stats["total_users"] = row["c"] if row else 0

            row = conn.execute("SELECT COUNT(*) as c FROM users WHERE vip_until > datetime('now')").fetchone()
            stats["vip_users"] = row["c"] if row else 0

            row = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_banned=1").fetchone()
            stats["banned_users"] = row["c"] if row else 0

            row = conn.execute("SELECT COUNT(*) as c FROM downloads").fetchone()
            stats["total_downloads"] = row["c"] if row else 0

            row = conn.execute("SELECT COUNT(*) as c FROM downloads WHERE date(created_at)=date('now')").fetchone()
            stats["today_downloads"] = row["c"] if row else 0

            row = conn.execute("SELECT COUNT(*) as c FROM downloads WHERE created_at >= datetime('now','-7 days')").fetchone()
            stats["week_downloads"] = row["c"] if row else 0

            row = conn.execute("SELECT COUNT(*) as c FROM referrals").fetchone()
            stats["total_referrals"] = row["c"] if row else 0

            row = conn.execute("SELECT COUNT(*) as c FROM users WHERE date(last_seen)=date('now')").fetchone()
            stats["active_today"] = row["c"] if row else 0

            row = conn.execute("SELECT COALESCE(SUM(points),0) as s FROM users").fetchone()
            stats["total_points_issued"] = row["s"] if row else 0
    except Exception:
        pass

    try:
        with support_db() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM support_tickets WHERE status='open'").fetchone()
            stats["open_tickets"] = row["c"] if row else 0
    except Exception:
        pass

    return stats


def get_recent_activity(limit: int = 10) -> list:
    try:
        with main_db() as conn:
            rows = conn.execute("""
                SELECT u.first_name, u.username, d.platform, d.media_type, d.created_at
                FROM downloads d
                JOIN users u ON u.user_id = d.user_id
                ORDER BY d.created_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_downloads_chart(days: int = 7) -> list:
    try:
        with main_db() as conn:
            rows = conn.execute("""
                SELECT date(created_at) as day, COUNT(*) as cnt
                FROM downloads
                WHERE created_at >= datetime('now', ? || ' days')
                GROUP BY date(created_at)
                ORDER BY day
            """, (f"-{days}",)).fetchall()
            return [{"day": r["day"], "cnt": r["cnt"]} for r in rows]
    except Exception:
        return []
