from .db import db_cursor


def create_report(user_id: int, username: str, platform: str, url: str, message: str) -> int:
    with db_cursor() as c:
        c.execute("""
            INSERT INTO reports (user_id, username, platform, url, message)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, username, platform, url, message))
        return c.lastrowid


def get_report_by_id(report_id: int) -> dict | None:
    with db_cursor() as c:
        c.execute("SELECT * FROM reports WHERE id = ?", (report_id,))
        row = c.fetchone()
        return dict(row) if row else None


def get_reports(status: str = "open", limit: int = 20, offset: int = 0) -> list[dict]:
    with db_cursor() as c:
        c.execute("""
            SELECT * FROM reports
            WHERE status = ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (status, limit, offset))
        return [dict(row) for row in c.fetchall()]


def count_reports(status: str = "open") -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) as cnt FROM reports WHERE status = ?", (status,))
        return c.fetchone()["cnt"]


def reply_report(report_id: int, admin_id: int, reply_text: str):
    """Store admin reply and close the report."""
    with db_cursor() as c:
        c.execute("""
            UPDATE reports
            SET reply = ?, closed_by = ?, closed_at = datetime('now'), status = 'closed'
            WHERE id = ?
        """, (reply_text, admin_id, report_id))


def close_report(report_id: int, closed_by: int = None):
    with db_cursor() as c:
        c.execute("""
            UPDATE reports
            SET status = 'closed', closed_by = ?, closed_at = datetime('now')
            WHERE id = ?
        """, (closed_by, report_id))


def create_support_ticket(user_id: int, message: str) -> int:
    with db_cursor() as c:
        c.execute("""
            INSERT INTO support_tickets (user_id, message) VALUES (?, ?)
        """, (user_id, message))
        return c.lastrowid


def reply_support(ticket_id: int, reply: str):
    with db_cursor() as c:
        c.execute("""
            UPDATE support_tickets SET reply = ?, status = 'replied' WHERE id = ?
        """, (reply, ticket_id))


def get_open_tickets(limit: int = 20) -> list[dict]:
    with db_cursor() as c:
        c.execute("""
            SELECT * FROM support_tickets WHERE status = 'open'
            ORDER BY created_at DESC LIMIT ?
        """, (limit,))
        return [dict(row) for row in c.fetchall()]


def log_feedback(user_id: int, download_id: int, rating: str):
    with db_cursor() as c:
        c.execute("""
            INSERT INTO feedback (user_id, download_id, rating) VALUES (?, ?, ?)
        """, (user_id, download_id, rating))
