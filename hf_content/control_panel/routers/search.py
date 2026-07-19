import os
import re
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from ..auth import require_owner
from ..config import CONTROL_PANEL_DIR, PROJECT_ROOT

router = APIRouter(prefix="/search")
templates = Jinja2Templates(directory=os.path.join(CONTROL_PANEL_DIR, "templates"))

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", "temp", "backups", ".cache"}
TEXT_EXTS = {".py", ".txt", ".md", ".json", ".yaml", ".yml", ".cfg", ".ini",
             ".sh", ".js", ".ts", ".html", ".css", ".toml", ".env", ".sql"}
MAX_RESULTS = 200


def _search_project(query: str, case_sensitive: bool = False, regex: bool = False) -> list:
    results = []
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        if regex:
            pattern = re.compile(query, flags)
        else:
            pattern = re.compile(re.escape(query), flags)
    except re.error:
        return [{"error": "نمط Regex غير صالح"}]

    for dirpath, dirnames, files in os.walk(PROJECT_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in TEXT_EXTS:
                continue
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, PROJECT_ROOT).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if pattern.search(line):
                            results.append({"file": rel, "line": lineno, "content": line.strip()[:200]})
                            if len(results) >= MAX_RESULTS:
                                return results
            except Exception:
                pass
    return results


@router.get("", response_class=HTMLResponse)
async def search_page(request: Request, q: str = "", session: dict = Depends(require_owner)):
    results = []
    if q:
        results = _search_project(q)
    by_file: dict = {}
    for r in results:
        if "error" in r:
            by_file["__error__"] = [r]
            break
        by_file.setdefault(r["file"], []).append(r)

    return templates.TemplateResponse(request, "search.html", {
        "q": q, "by_file": by_file, "total": len(results), "active_page": "search"
    })


@router.get("/api")
async def api_search(q: str = "", case: bool = False, regex: bool = False,
                     session: dict = Depends(require_owner)):
    if not q:
        return {"results": [], "total": 0}
    results = _search_project(q, case, regex)
    return {"results": results, "total": len(results)}
