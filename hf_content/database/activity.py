from .db import db_cursor


def get_activity_feed(limit: int = 50) -> list[dict]:
    """
    Merge events from existing tables into a unified chronological feed.
    Zero schema changes — all reads are from tables created by init_db().
    """
    with db_cursor() as c:
        c.execute("""
            SELECT event_type, user_id, username, first_name, detail, created_at
            FROM (
                SELECT 'new_user' AS event_type,
                       u.user_id,
                       COALESCE(u.username, '')    AS username,
                       COALESCE(u.first_name, '')  AS first_name,
                       NULL                        AS detail,
                       u.join_date                 AS created_at
                FROM users u

                UNION ALL

                SELECT 'download',
                       d.user_id,
                       COALESCE(u.username, '')   AS username,
                       COALESCE(u.first_name, '') AS first_name,
                       COALESCE(d.platform, '') || ' · ' || COALESCE(d.quality, '') AS detail,
                       d.created_at
                FROM downloads d
                LEFT JOIN users u ON u.user_id = d.user_id

                UNION ALL

                SELECT 'referral',
                       r.referred_id,
                       COALESCE(u.username, '')    AS username,
                       COALESCE(u.first_name, '')  AS first_name,
                       'via ' || COALESCE(u2.first_name, CAST(r.referrer_id AS TEXT)) AS detail,
                       r.completed_at
                FROM referrals r
                LEFT JOIN users u  ON u.user_id  = r.referred_id
                LEFT JOIN users u2 ON u2.user_id = r.referrer_id
                WHERE r.status = 'completed' AND r.completed_at IS NOT NULL

                UNION ALL

                SELECT 'report',
                       rp.user_id,
                       COALESCE(rp.username, '')  AS username,
                       COALESCE(u.first_name, '') AS first_name,
                       COALESCE(rp.platform, '')  AS detail,
                       rp.created_at
                FROM reports rp
                LEFT JOIN users u ON u.user_id = rp.user_id

                UNION ALL

                SELECT 'points',
                       rl.user_id,
                       COALESCE(u.username, '')   AS username,
                       COALESCE(u.first_name, '') AS first_name,
                       COALESCE(rl.reward_name, '') || ' (' || CAST(rl.reward_cost AS TEXT) || ' pts)' AS detail,
                       rl.created_at
                FROM rewards_log rl
                LEFT JOIN users u ON u.user_id = rl.user_id

                UNION ALL

                SELECT 'broadcast',
                       bl.admin_id,
                       COALESCE(u.username, '')   AS username,
                       COALESCE(u.first_name, '') AS first_name,
                       CAST(bl.success AS TEXT) || '/' || CAST(bl.total AS TEXT) || ' delivered' AS detail,
                       bl.created_at
                FROM broadcast_log bl
                LEFT JOIN users u ON u.user_id = bl.admin_id
            )
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in c.fetchall()]
