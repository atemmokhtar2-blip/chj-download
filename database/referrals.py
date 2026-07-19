from .db import db_cursor


def create_pending_referral(referrer_id: int, referred_id: int) -> bool:
    """Insert a pending referral. Returns True if created, False if already existed."""
    with db_cursor() as c:
        c.execute("""
            INSERT OR IGNORE INTO referrals (referrer_id, referred_id, status)
            VALUES (?, ?, 'pending')
        """, (referrer_id, referred_id))
        return c.rowcount > 0


def get_referral_by_referred(referred_id: int) -> dict | None:
    """Get the referral record for a given referred user."""
    with db_cursor() as c:
        c.execute("SELECT * FROM referrals WHERE referred_id = ?", (referred_id,))
        row = c.fetchone()
        return dict(row) if row else None


def complete_referral(referred_id: int) -> bool:
    """
    Atomically marks a pending referral as completed and sets reward_given.
    Returns True only if a pending row was actually updated (prevents double-credit).
    """
    with db_cursor() as c:
        c.execute("""
            UPDATE referrals
            SET status = 'completed',
                reward_given = 1,
                completed_at = datetime('now')
            WHERE referred_id = ?
              AND status = 'pending'
              AND reward_given = 0
        """, (referred_id,))
        return c.rowcount > 0


def get_referrer_stats(referrer_id: int) -> dict:
    """Return aggregate stats for a referrer."""
    with db_cursor() as c:
        c.execute("""
            SELECT
                COUNT(*)                                              AS total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = 'pending'   THEN 1 ELSE 0 END) AS pending,
                COALESCE(SUM(reward_given), 0)                        AS rewards_given
            FROM referrals
            WHERE referrer_id = ?
        """, (referrer_id,))
        row = c.fetchone()
        return dict(row) if row else {
            "total": 0, "completed": 0, "pending": 0, "rewards_given": 0
        }


def get_referral_history(referrer_id: int, limit: int = 20) -> list[dict]:
    """Return referral history with referred user info."""
    with db_cursor() as c:
        c.execute("""
            SELECT r.id, r.referred_id, r.status, r.reward_given,
                   r.created_at, r.completed_at,
                   u.first_name, u.username
            FROM referrals r
            LEFT JOIN users u ON u.user_id = r.referred_id
            WHERE r.referrer_id = ?
            ORDER BY r.created_at DESC
            LIMIT ?
        """, (referrer_id, limit))
        return [dict(row) for row in c.fetchall()]


def log_audit(event: str, referrer_id: int | None, referred_id: int, detail: str = ""):
    """Write an immutable audit event."""
    with db_cursor() as c:
        c.execute("""
            INSERT INTO referral_audit_log (event, referrer_id, referred_id, detail)
            VALUES (?, ?, ?, ?)
        """, (event, referrer_id, referred_id, detail))


def get_audit_log(referrer_id: int, limit: int = 50) -> list[dict]:
    """Return audit events for a referrer."""
    with db_cursor() as c:
        c.execute("""
            SELECT * FROM referral_audit_log
            WHERE referrer_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (referrer_id, limit))
        return [dict(row) for row in c.fetchall()]


def get_top_referrers_by_period(period: str = "all", limit: int = 10) -> list[dict]:
    """
    Leaderboard using the referrals table — accurate period filtering
    by completed_at timestamp.
    """
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
                SELECT u.user_id, u.first_name, u.username,
                       COUNT(r.id) AS referrals
                FROM referrals r
                INNER JOIN users u ON u.user_id = r.referrer_id
                WHERE r.status = 'completed'
                GROUP BY r.referrer_id
                ORDER BY referrals DESC
                LIMIT ?
            """, (limit,))
        return [dict(row) for row in c.fetchall()]
