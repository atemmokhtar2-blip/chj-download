"""Support ticket storage + workflow.

Tickets live in the MAIN PostgreSQL database (``database.db`` schema) so they work
for every user — main-bot users and clone users alike — and survive alongside
the existing users/downloads data.
"""

from __future__ import annotations

import secrets
import string

from .db import db_cursor

CATEGORY = ("token", "clone", "other")
STATUS = ("open", "answered", "closed")


def _mk_ticket_no() -> str:
    return "TKT-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))


def _normalize_ticket_id(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def open_ticket(user_id, category: str = "other", subject: str = "", body: str = "") -> dict:
    category = category if category in CATEGORY else "other"
    ticket_no = _mk_ticket_no()
    with db_cursor() as c:
        c.execute(
            "INSERT INTO support_tickets (ticket_no, user_id, category, subject, status) "
            "VALUES (?, ?, ?, ?, 'open')",
            (ticket_no, int(user_id), category, (subject or "")[:120]),
        )
        t_id = c.lastrowid
        c.execute(
            "INSERT INTO support_ticket_messages (ticket_id, sender, body) VALUES (?, 'user', ?)",
            (t_id, (body or "")[:1900]),
        )
    return get_ticket(t_id) or {"id": t_id, "ticket_no": ticket_no}


def get_ticket(ticket_id) -> dict | None:
    tid = _normalize_ticket_id(ticket_id)
    if not tid:
        return None
    with db_cursor() as c:
        row = c.execute("SELECT * FROM support_tickets WHERE id = ?", (tid,)).fetchone()
    return dict(row) if row else None


def get_ticket_for_user(ticket_id, user_id) -> dict | None:
    t = get_ticket(ticket_id)
    if t and int(t["user_id"]) == int(user_id):
        return t
    return None


def list_user_tickets(user_id, limit: int = 10) -> list[dict]:
    with db_cursor() as c:
        rows = c.execute(
            "SELECT * FROM support_tickets WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (int(user_id), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_tickets(status: str | None = None, limit: int = 20) -> list[dict]:
    with db_cursor() as c:
        if status in STATUS:
            rows = c.execute(
                "SELECT * FROM support_tickets WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM support_tickets ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def count_tickets(status: str) -> int:
    with db_cursor() as c:
        row = c.execute(
            "SELECT COUNT(*) AS c FROM support_tickets WHERE status = ?", (status,)
        ).fetchone()
    return int(row["c"] or 0) if row else 0


def get_messages(ticket_id, limit: int = 50) -> list[dict]:
    tid = _normalize_ticket_id(ticket_id)
    if not tid:
        return []
    with db_cursor() as c:
        rows = c.execute(
            "SELECT sender, body, created_at FROM support_ticket_messages "
            "WHERE ticket_id = ? ORDER BY id ASC LIMIT ?",
            (tid, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def add_admin_reply(ticket_id, body: str) -> None:
    tid = _normalize_ticket_id(ticket_id)
    if not tid:
        return
    with db_cursor() as c:
        c.execute(
            "INSERT INTO support_ticket_messages (ticket_id, sender, body) VALUES (?, 'admin', ?)",
            (tid, (body or "")[:1900]),
        )
        c.execute(
            "UPDATE support_tickets SET status = 'answered', updated_at = datetime('now') WHERE id = ?",
            (tid,),
        )


def close_ticket(ticket_id) -> None:
    tid = _normalize_ticket_id(ticket_id)
    if tid:
        with db_cursor() as c:
            c.execute(
                "UPDATE support_tickets SET status = 'closed', updated_at = datetime('now') WHERE id = ?",
                (tid,),
            )


def reopen_ticket(ticket_id) -> None:
    tid = _normalize_ticket_id(ticket_id)
    if tid:
        with db_cursor() as c:
            c.execute(
                "UPDATE support_tickets SET status = 'open', updated_at = datetime('now') WHERE id = ?",
                (tid,),
            )
