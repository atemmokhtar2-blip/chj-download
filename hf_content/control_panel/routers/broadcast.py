import asyncio
import httpx
from fastapi import APIRouter, Request, Depends, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import os
from ..auth import require_owner
from ..db_utils import main_db
from ..config import CONTROL_PANEL_DIR, BOT_TOKEN

router = APIRouter(prefix="/broadcast")
templates = Jinja2Templates(directory=os.path.join(CONTROL_PANEL_DIR, "templates"))

_broadcast_status = {"running": False, "total": 0, "success": 0, "failed": 0, "done": False}


def _get_all_user_ids():
    with main_db() as conn:
        rows = conn.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
        return [r["user_id"] for r in rows]


def _save_broadcast_log(admin_id, msg, total, success, failed):
    try:
        with main_db() as conn:
            conn.execute(
                "INSERT INTO broadcast_log (admin_id, message, total, success, failed) VALUES (?,?,?,?,?)",
                (admin_id, msg[:500], total, success, failed))
    except Exception:
        pass


async def _do_broadcast(user_ids: list, text: str, parse_mode: str, admin_id: int):
    global _broadcast_status
    _broadcast_status = {"running": True, "total": len(user_ids), "success": 0, "failed": 0, "done": False}
    async with httpx.AsyncClient(timeout=10) as client:
        for uid in user_ids:
            try:
                resp = await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": uid, "text": text, "parse_mode": parse_mode})
                if resp.status_code == 200 and resp.json().get("ok"):
                    _broadcast_status["success"] += 1
                else:
                    _broadcast_status["failed"] += 1
            except Exception:
                _broadcast_status["failed"] += 1
            await asyncio.sleep(0.05)
    _save_broadcast_log(admin_id, text, len(user_ids),
                        _broadcast_status["success"], _broadcast_status["failed"])
    _broadcast_status["running"] = False
    _broadcast_status["done"] = True


@router.get("", response_class=HTMLResponse)
async def broadcast_page(request: Request, session: dict = Depends(require_owner)):
    logs = []
    user_count = 0
    try:
        with main_db() as conn:
            rows = conn.execute(
                "SELECT * FROM broadcast_log ORDER BY created_at DESC LIMIT 10").fetchall()
            logs = [dict(r) for r in rows]
            row = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_banned=0").fetchone()
            user_count = row["c"] if row else 0
    except Exception:
        pass
    return templates.TemplateResponse(request, "broadcast.html", {
        "logs": logs, "user_count": user_count,
        "status": _broadcast_status, "active_page": "broadcast"
    })


@router.post("/api/send")
async def api_send(request: Request, background_tasks: BackgroundTasks,
                   text: str = Form(...), parse_mode: str = Form("HTML"),
                   session: dict = Depends(require_owner)):
    if _broadcast_status.get("running"):
        return JSONResponse({"error": "بث جارٍ بالفعل"}, status_code=400)
    user_ids = _get_all_user_ids()
    if not user_ids:
        return JSONResponse({"error": "لا يوجد مستخدمون"}, status_code=400)
    background_tasks.add_task(_do_broadcast, user_ids, text, parse_mode, session["uid"])
    return {"ok": True, "total": len(user_ids)}


@router.get("/api/status")
async def api_status(session: dict = Depends(require_owner)):
    return _broadcast_status
