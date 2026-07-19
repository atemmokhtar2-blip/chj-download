import sqlite3
from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import os
from ..auth import require_owner
from ..db_utils import main_db
from ..config import CONTROL_PANEL_DIR

router = APIRouter(prefix="/users")
templates = Jinja2Templates(directory=os.path.join(CONTROL_PANEL_DIR, "templates"))


def _get_users(search: str = "", offset: int = 0, limit: int = 25):
    with main_db() as conn:
        if search:
            if search.lstrip("-").isdigit():
                rows = conn.execute(
                    "SELECT * FROM users WHERE user_id=? LIMIT ? OFFSET ?",
                    (int(search), limit, offset)).fetchall()
            else:
                term = f"%{search.lstrip('@').lower()}%"
                rows = conn.execute(
                    "SELECT * FROM users WHERE lower(username) LIKE ? OR lower(first_name) LIKE ? LIMIT ? OFFSET ?",
                    (term, term, limit, offset)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY join_date DESC LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()
        return [dict(r) for r in rows]


def _count_users(search: str = "") -> int:
    with main_db() as conn:
        if search:
            if search.lstrip("-").isdigit():
                r = conn.execute("SELECT COUNT(*) as c FROM users WHERE user_id=?", (int(search),)).fetchone()
            else:
                term = f"%{search.lstrip('@').lower()}%"
                r = conn.execute(
                    "SELECT COUNT(*) as c FROM users WHERE lower(username) LIKE ? OR lower(first_name) LIKE ?",
                    (term, term)).fetchone()
        else:
            r = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
        return r["c"] if r else 0


@router.get("", response_class=HTMLResponse)
async def users_page(request: Request, search: str = "", page: int = 1,
                     session: dict = Depends(require_owner)):
    limit = 25
    offset = (page - 1) * limit
    users = _get_users(search, offset, limit)
    total = _count_users(search)
    pages = max(1, (total + limit - 1) // limit)
    return templates.TemplateResponse(request, "users.html", {
        "users": users, "search": search,
        "page": page, "pages": pages, "total": total,
        "active_page": "users"
    })


@router.get("/api/list")
async def api_users(search: str = "", page: int = 1, session: dict = Depends(require_owner)):
    limit = 25
    offset = (page - 1) * limit
    users = _get_users(search, offset, limit)
    total = _count_users(search)
    return {"users": users, "total": total, "pages": max(1, (total + limit - 1) // limit)}


@router.get("/api/{user_id}")
async def api_user_detail(user_id: int, session: dict = Depends(require_owner)):
    with main_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return JSONResponse({"error": "User not found"}, status_code=404)
        user = dict(row)
        dl = conn.execute("SELECT COUNT(*) as c FROM downloads WHERE user_id=?", (user_id,)).fetchone()
        user["download_count"] = dl["c"] if dl else 0
        history = conn.execute(
            "SELECT platform, media_type, created_at FROM downloads WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
            (user_id,)).fetchall()
        user["history"] = [dict(h) for h in history]
    return user


@router.post("/api/ban")
async def api_ban(user_id: int = Form(...), session: dict = Depends(require_owner)):
    with main_db() as conn:
        conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
    return {"ok": True, "action": "banned", "user_id": user_id}


@router.post("/api/unban")
async def api_unban(user_id: int = Form(...), session: dict = Depends(require_owner)):
    with main_db() as conn:
        conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
    return {"ok": True, "action": "unbanned", "user_id": user_id}


@router.post("/api/points")
async def api_points(user_id: int = Form(...), amount: int = Form(...),
                     session: dict = Depends(require_owner)):
    with main_db() as conn:
        row = conn.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return JSONResponse({"error": "User not found"}, status_code=404)
        new_pts = max(0, (row["points"] or 0) + amount)
        conn.execute("UPDATE users SET points=? WHERE user_id=?", (new_pts, user_id))
    return {"ok": True, "new_points": new_pts}


@router.post("/api/premium")
async def api_premium(user_id: int = Form(...), days: int = Form(30),
                      session: dict = Depends(require_owner)):
    with main_db() as conn:
        conn.execute(
            "UPDATE users SET vip_until=datetime('now','+'||?||' days') WHERE user_id=?",
            (days, user_id))
    return {"ok": True, "days": days}


@router.post("/api/remove_premium")
async def api_remove_premium(user_id: int = Form(...), session: dict = Depends(require_owner)):
    with main_db() as conn:
        conn.execute("UPDATE users SET vip_until=NULL WHERE user_id=?", (user_id,))
    return {"ok": True}
