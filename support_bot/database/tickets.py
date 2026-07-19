from .db import db_cursor


def create_ticket(user_id: int, username: str, first_name: str, message: str) -> int:
    with db_cursor() as c:
        c.execute("""
            INSERT INTO support_tickets (user_id, username, first_name)
            VALUES (?, ?, ?)
        """, (user_id, username or "", first_name or ""))
        ticket_id = c.lastrowid
        c.execute("""
            INSERT INTO ticket_messages (ticket_id, sender_id, sender_role, message)
            VALUES (?, ?, 'user', ?)
        """, (ticket_id, user_id, message))
        return ticket_id


def add_message(ticket_id: int, sender_id: int, role: str, message: str) -> int:
    with db_cursor() as c:
        c.execute("""
            INSERT INTO ticket_messages (ticket_id, sender_id, sender_role, message)
            VALUES (?, ?, ?, ?)
        """, (ticket_id, sender_id, role, message))
        return c.lastrowid


def get_ticket(ticket_id: int) -> dict | None:
    with db_cursor() as c:
        c.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,))
        row = c.fetchone()
        return dict(row) if row else None


def get_ticket_messages(ticket_id: int) -> list[dict]:
    with db_cursor() as c:
        c.execute("""
            SELECT * FROM ticket_messages
            WHERE ticket_id = ?
            ORDER BY created_at ASC
        """, (ticket_id,))
        return [dict(r) for r in c.fetchall()]


def get_user_open_ticket(user_id: int) -> dict | None:
    with db_cursor() as c:
        c.execute("""
            SELECT * FROM support_tickets
            WHERE user_id = ? AND status = 'open'
            ORDER BY created_at DESC
            LIMIT 1
        """, (user_id,))
        row = c.fetchone()
        return dict(row) if row else None


def get_user_tickets(user_id: int, limit: int = 10) -> list[dict]:
    with db_cursor() as c:
        c.execute("""
            SELECT * FROM support_tickets
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (user_id, limit))
        return [dict(r) for r in c.fetchall()]


def get_open_tickets(limit: int = 20, offset: int = 0) -> list[dict]:
    with db_cursor() as c:
        c.execute("""
            SELECT * FROM support_tickets
            WHERE status = 'open'
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        return [dict(r) for r in c.fetchall()]


def get_closed_tickets(limit: int = 20, offset: int = 0) -> list[dict]:
    with db_cursor() as c:
        c.execute("""
            SELECT * FROM support_tickets
            WHERE status = 'closed'
            ORDER BY closed_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        return [dict(r) for r in c.fetchall()]


def count_tickets(status: str = "open") -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) AS cnt FROM support_tickets WHERE status = ?", (status,))
        return c.fetchone()["cnt"]


def close_ticket(ticket_id: int, closed_by: int) -> bool:
    with db_cursor() as c:
        c.execute("""
            UPDATE support_tickets
            SET status = 'closed', closed_at = datetime('now'), closed_by = ?
            WHERE id = ? AND status = 'open'
        """, (closed_by, ticket_id))
        return c.rowcount > 0
