from __future__ import annotations

from .db import db_cursor


def log_broadcast(admin_id: int, message: str, total: int, success: int, failed: int) -> None:
    """Record one broadcast attempt so the admin panel can show real history."""
    with db_cursor() as c:
        c.execute(
            """
            INSERT INTO broadcast_log (admin_id, message, total, success, failed)
            VALUES (?, ?, ?, ?, ?)
            """,
            (admin_id, (message or "")[:500], total, success, failed),
        )


def get_broadcast_history(limit: int = 5) -> list[dict]:
    with db_cursor() as c:
        c.execute(
            "SELECT * FROM broadcast_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in c.fetchall()]
