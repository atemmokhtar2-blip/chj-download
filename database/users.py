import hashlib
import secrets
from datetime import datetime
from .db import db_cursor
from config.settings import POINTS_DAILY


def generate_referral_code(user_id: int) -> str:
    raw = f"{user_id}-{secrets.token_hex(4)}"
    return hashlib.md5(raw.encode()).hexdigest()[:8].upper()


def get_user(user_id: int) -> dict | None:
    with db_cursor() as c:
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        return dict(row) if row else None


def create_user(user_id: int, username: str, first_name: str, last_name: str,
                language: str = "en", referred_by: int = None) -> dict:
    code = generate_referral_code(user_id)
    with db_cursor() as c:
        c.execute("""
            INSERT OR IGNORE INTO users
            (user_id, username, first_name, last_name, language, referral_code, referred_by, points)
            VALUES (?, ?, ?, ?, ?, ?, ?, 10)
        """, (user_id, username, first_name, last_name, language, code, referred_by))
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


def get_user_by_referral(code: str) -> dict | None:
    with db_cursor() as c:
        c.execute("SELECT * FROM users WHERE referral_code = ?", (code,))
        row = c.fetchone()
        return dict(row) if row else None


def add_points(user_id: int, points: int):
    with db_cursor() as c:
        c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (points, user_id))


def deduct_points(user_id: int, points: int) -> bool:
    with db_cursor() as c:
        c.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if not row or row["points"] < points:
            return False
        c.execute("UPDATE users SET points = points - ? WHERE user_id = ?", (points, user_id))
        return True


def increment_downloads(user_id: int):
    with db_cursor() as c:
        c.execute("UPDATE users SET downloads = downloads + 1 WHERE user_id = ?", (user_id,))


def increment_referrals(user_id: int):
    with db_cursor() as c:
        c.execute("UPDATE users SET referrals = referrals + 1 WHERE user_id = ?", (user_id,))


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


def get_top_referrers(period: str = "all", limit: int = 10) -> list[dict]:
    # Uses the referrals table for accurate period filtering by completed_at.
    with db_cursor() as c:
        if period == "weekly":
            interval = "-7 days"
        elif period == "monthly":
            interval = "-30 days"
        else:
            interval = None

        if interval:
            c.execute("""
                SELECT u.user_id, u.first_name, u.username,
                       COUNT(r.id) AS referrals
                FROM referrals r
                INNER JOIN users u ON u.user_id = r.referrer_id
                WHERE r.status = 'completed'
                  AND r.completed_at >= datetime('now', ?)
                GROUP BY r.referrer_id
                ORDER BY referrals DESC
                LIMIT ?
            """, (interval, limit))
        else:
            c.execute("""
                SELECT user_id, first_name, username, referrals
                FROM users
                WHERE referrals > 0
                ORDER BY referrals DESC
                LIMIT ?
            """, (limit,))
        return [dict(row) for row in c.fetchall()]


def get_total_points_issued() -> int:
    with db_cursor() as c:
        c.execute("SELECT COALESCE(SUM(points), 0) as total FROM users")
        return c.fetchone()["total"]


def ban_user(user_id: int):
    with db_cursor() as c:
        c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))


def unban_user(user_id: int):
    with db_cursor() as c:
        c.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))


def set_vip(user_id: int, days: int):
    with db_cursor() as c:
        c.execute("""
            UPDATE users SET vip_until = datetime('now', '+' || ? || ' days')
            WHERE user_id = ?
        """, (days, user_id))


def claim_daily(user_id: int) -> tuple[bool, int, int]:
    """Returns (success, hours_remaining, minutes_remaining)"""
    with db_cursor() as c:
        c.execute("SELECT last_daily FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row and row["last_daily"]:
            last = datetime.fromisoformat(row["last_daily"])
            diff = datetime.utcnow() - last
            if diff.total_seconds() < 86400:
                remaining = 86400 - diff.total_seconds()
                hours = int(remaining // 3600)
                minutes = int((remaining % 3600) // 60)
                return False, hours, minutes
        c.execute(
            "UPDATE users SET last_daily = datetime('now'), points = points + ? WHERE user_id = ?",
            (POINTS_DAILY, user_id)
        )
        c.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
        total = c.fetchone()["points"]
        return True, 0, total


def get_users_page(offset: int = 0, limit: int = 20) -> list[dict]:
    with db_cursor() as c:
        c.execute("""
            SELECT user_id, username, first_name, downloads, referrals, points, is_banned
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


def adjust_points_admin(user_id: int, amount: int, admin_id: int, note: str = "") -> int:
    """Add (positive) or remove (negative) points. Returns new total or -1 on failure."""
    with db_cursor() as c:
        c.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            return -1
        current = row["points"]
        if amount < 0 and current < abs(amount):
            return -2
        c.execute(
            "UPDATE users SET points = points + ? WHERE user_id = ?",
            (amount, user_id)
        )
        c.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
        new_total = c.fetchone()["points"]
        label = f"admin_add:{admin_id}:{note}" if amount > 0 else f"admin_remove:{admin_id}:{note}"
        c.execute(
            "INSERT INTO rewards_log (user_id, reward_cost, reward_name) VALUES (?, ?, ?)",
            (user_id, abs(amount), label)
        )
        return new_total
