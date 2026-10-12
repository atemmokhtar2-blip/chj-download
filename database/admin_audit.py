from __future__ import annotations

from .db import db_cursor


def log_admin_action(admin_id: int, action: str, target_id: int | None = None, detail: str = "") -> None:
    """Record a security-relevant admin action in the audit trail."""
    with db_cursor() as c:
        c.execute(
            """
            INSERT INTO admin_audit_log (admin_id, action, target_id, detail)
            VALUES (?, ?, ?, ?)
            """,
            (admin_id, action or "action", target_id, (detail or "")[:300]),
        )


def get_admin_audit(limit: int = 12) -> list[dict]:
    """Return the most recent admin audit entries, newest first."""
    with db_cursor() as c:
        c.execute(
            "SELECT * FROM admin_audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in c.fetchall()]
