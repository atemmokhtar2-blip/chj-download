import sqlite3
import os
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from ..auth import require_owner
from ..db_utils import main_db, support_db, dev_db, fmt_size
from ..config import CONTROL_PANEL_DIR, MAIN_DB, SUPPORT_DB, DEV_DB

router = APIRouter(prefix="/database")
templates = Jinja2Templates(directory=os.path.join(CONTROL_PANEL_DIR, "templates"))


def _db_info(path: str, name: str) -> dict:
    info = {"name": name, "path": path, "size": 0, "tables": [], "ok": False, "error": ""}
    if not os.path.exists(path):
        info["error"] = "غير موجود"
        return info
    try:
        info["size"] = os.path.getsize(path)
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        for t in tables:
            tname = t["name"]
            count = conn.execute(f"SELECT COUNT(*) as c FROM [{tname}]").fetchone()["c"]
            info["tables"].append({"name": tname, "count": count})
        conn.close()
        info["ok"] = True
    except Exception as e:
        info["error"] = str(e)
    return info


def _find_duplicates() -> dict:
    result = {"dup_users": [], "dup_referrals": [], "orphan_downloads": 0}
    try:
        with main_db() as conn:
            rows = conn.execute("""
                SELECT username, COUNT(*) as cnt FROM users
                WHERE username IS NOT NULL AND username != ''
                GROUP BY lower(username) HAVING cnt > 1
            """).fetchall()
            result["dup_users"] = [{"username": r["username"], "count": r["cnt"]} for r in rows]
            rows = conn.execute("""
                SELECT referred_id, COUNT(*) as cnt FROM referrals
                GROUP BY referred_id HAVING cnt > 1
            """).fetchall()
            result["dup_referrals"] = [{"referred_id": r["referred_id"], "count": r["cnt"]} for r in rows]
            row = conn.execute("""
                SELECT COUNT(*) as c FROM downloads d
                WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = d.user_id)
            """).fetchone()
            result["orphan_downloads"] = row["c"] if row else 0
    except Exception:
        pass
    return result


def _repair(action: str) -> dict:
    results = []
    try:
        with main_db() as conn:
            if action == "orphans":
                c = conn.execute("DELETE FROM downloads WHERE user_id NOT IN (SELECT user_id FROM users)")
                results.append(f"حُذف {c.rowcount} تحميل يتيم")
            elif action == "vacuum":
                conn.execute("VACUUM")
                results.append("تم تحسين قاعدة البيانات الرئيسية")
            elif action == "integrity":
                rows = conn.execute("PRAGMA integrity_check").fetchall()
                for r in rows:
                    results.append(str(r[0]))
    except Exception as e:
        results.append(f"خطأ: {e}")
    return {"results": results}


@router.get("", response_class=HTMLResponse)
async def db_page(request: Request, session: dict = Depends(require_owner)):
    dbs = [
        _db_info(MAIN_DB, "bot.db (رئيسي)"),
        _db_info(SUPPORT_DB, "support.db (دعم)"),
        _db_info(DEV_DB, "developer.db (مطور)"),
    ]
    dups = _find_duplicates()
    return templates.TemplateResponse(request, "db_manager.html", {
        "dbs": dbs, "dups": dups,
        "fmt_size": fmt_size, "active_page": "database"
    })


@router.get("/api/info")
async def api_info(session: dict = Depends(require_owner)):
    return {
        "main": _db_info(MAIN_DB, "bot.db"),
        "support": _db_info(SUPPORT_DB, "support.db"),
        "dev": _db_info(DEV_DB, "developer.db"),
        "duplicates": _find_duplicates(),
    }


@router.post("/api/repair")
async def api_repair(request: Request, session: dict = Depends(require_owner)):
    body = await request.json()
    action = body.get("action", "")
    return _repair(action)
