import os
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from ..auth import require_owner
from ..config import CONTROL_PANEL_DIR, LOGS_DIR

router = APIRouter(prefix="/logs")
templates = Jinja2Templates(directory=os.path.join(CONTROL_PANEL_DIR, "templates"))

LOG_FILES = {
    "system": "system.log",
    "errors": "errors.log",
    "downloads": "downloads.log",
    "admin": "admin.log",
    "admin_errors": "admin_errors.log",
    "support": "support_system.log",
    "dev": "dev_system.log",
    "security": "security.log",
}


def _read_log(filename: str, lines: int = 200, search: str = "") -> list:
    path = os.path.join(LOGS_DIR, filename)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:]
        if search:
            tail = [l for l in tail if search.lower() in l.lower()]
        return [l.rstrip() for l in tail]
    except Exception:
        return []


def _log_file_info() -> list:
    infos = []
    for key, fname in LOG_FILES.items():
        path = os.path.join(LOGS_DIR, fname)
        exists = os.path.exists(path)
        size = os.path.getsize(path) if exists else 0
        infos.append({"key": key, "file": fname, "exists": exists, "size": size})
    return infos


@router.get("", response_class=HTMLResponse)
async def logs_page(request: Request, log: str = "system", search: str = "",
                    session: dict = Depends(require_owner)):
    file_infos = _log_file_info()
    lines = _read_log(LOG_FILES.get(log, "system.log"), 300, search)
    return templates.TemplateResponse(request, "logs.html", {
        "file_infos": file_infos, "lines": lines,
        "current_log": log, "search": search, "active_page": "logs"
    })


@router.get("/api/read")
async def api_read(log: str = "system", lines: int = 200, search: str = "",
                   session: dict = Depends(require_owner)):
    filename = LOG_FILES.get(log, "system.log")
    return {"lines": _read_log(filename, lines, search), "log": log}


@router.get("/api/files")
async def api_files(session: dict = Depends(require_owner)):
    return _log_file_info()


@router.post("/api/clear")
async def api_clear(request: Request, session: dict = Depends(require_owner)):
    body = await request.json()
    log = body.get("log", "")
    filename = LOG_FILES.get(log)
    if not filename:
        return JSONResponse({"error": "غير موجود"}, status_code=400)
    path = os.path.join(LOGS_DIR, filename)
    try:
        with open(path, "w") as f:
            f.write("")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
