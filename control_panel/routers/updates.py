import os
import zipfile
import shutil
import subprocess
import sys
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from ..auth import require_owner
from ..db_utils import dev_db
from ..config import CONTROL_PANEL_DIR, PROJECT_ROOT, TEMP_DIR, BACKUPS_DIR, PROTECTED_NAMES

router = APIRouter(prefix="/updates")
templates = Jinja2Templates(directory=os.path.join(CONTROL_PANEL_DIR, "templates"))

_update_status = {"running": False, "step": "", "log": [], "done": False, "success": False}
SKIP_DIRS = {".git", "__pycache__", "temp", "backups", ".venv", "node_modules"}


def _analyze_zip(zip_path: str) -> dict:
    result = {"files": [], "protected": [], "new": [], "modified": [], "total": 0, "deps_changed": False}
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        result["total"] = len(names)
        for name in names:
            if name.endswith("/"):
                continue
            basename = os.path.basename(name)
            dest = os.path.join(PROJECT_ROOT, name)
            if basename in PROTECTED_NAMES:
                result["protected"].append(name)
                continue
            if "requirements" in name.lower():
                result["deps_changed"] = True
            result["files"].append(name)
            if os.path.exists(dest):
                result["modified"].append(name)
            else:
                result["new"].append(name)
    return result


def _create_backup(label: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{label}_{ts}"
    path = os.path.join(BACKUPS_DIR, f"{name}.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, files in os.walk(PROJECT_ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in files:
                fp = os.path.join(dirpath, fn)
                try:
                    zf.write(fp, os.path.relpath(fp, PROJECT_ROOT))
                except (OSError, PermissionError):
                    pass
    size = os.path.getsize(path)
    try:
        with dev_db() as conn:
            conn.execute(
                "INSERT INTO backups (name, path, size_bytes, note) VALUES (?,?,?,?)",
                (name, path, size, label))
    except Exception:
        pass
    return name


async def _apply_zip_bg(zip_path: str, analysis: dict, version: str):
    global _update_status
    _update_status = {"running": True, "step": "backup", "log": [], "done": False, "success": False}

    def log(msg):
        _update_status["log"].append(msg)

    try:
        log("⏳ جارٍ إنشاء نسخة احتياطية…")
        backup_name = _create_backup("pre_update")
        log(f"✅ نسخة احتياطية: {backup_name}")

        _update_status["step"] = "apply"
        log("📦 جارٍ تطبيق الملفات…")
        applied = 0
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in analysis["files"]:
                dest = os.path.join(PROJECT_ROOT, name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(name) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                applied += 1
        log(f"✅ طُبِّق {applied} ملف")

        if analysis.get("deps_changed"):
            _update_status["step"] = "deps"
            log("📦 جارٍ تثبيت التبعيات…")
            req = os.path.join(PROJECT_ROOT, "extracted_project", "requirements.txt")
            if os.path.exists(req):
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", req, "-q"],
                    capture_output=True, text=True, timeout=120)
                log("✅ تم تثبيت التبعيات" if r.returncode == 0 else f"⚠️ {r.stderr[:200]}")

        try:
            with dev_db() as conn:
                conn.execute(
                    "INSERT INTO updates (version, filename, status) VALUES (?,?,?)",
                    (version, os.path.basename(zip_path), "applied"))
        except Exception:
            pass

        _update_status["step"] = "done"
        log("🎉 اكتمل التحديث بنجاح!")
        _update_status["success"] = True
    except Exception as e:
        log(f"❌ فشل: {e}")
        _update_status["success"] = False
    finally:
        _update_status["running"] = False
        _update_status["done"] = True
        try:
            os.remove(zip_path)
        except Exception:
            pass


def _get_history() -> list:
    try:
        with dev_db() as conn:
            rows = conn.execute("SELECT * FROM updates ORDER BY id DESC LIMIT 20").fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def _get_backups() -> list:
    try:
        with dev_db() as conn:
            rows = conn.execute("SELECT * FROM backups ORDER BY id DESC LIMIT 20").fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


@router.get("", response_class=HTMLResponse)
async def updates_page(request: Request, session: dict = Depends(require_owner)):
    history = _get_history()
    backups = _get_backups()
    return templates.TemplateResponse(request, "updates.html", {
        "history": history, "backups": backups,
        "status": _update_status, "active_page": "updates"
    })


@router.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...), session: dict = Depends(require_owner)):
    if not file.filename.endswith(".zip"):
        return JSONResponse({"error": "يجب أن يكون الملف ZIP"}, status_code=400)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(TEMP_DIR, f"upload_{ts}.zip")
    content = await file.read()
    with open(zip_path, "wb") as f:
        f.write(content)
    try:
        analysis = _analyze_zip(zip_path)
        analysis["zip_path"] = zip_path
        return analysis
    except Exception as e:
        os.remove(zip_path)
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/api/apply")
async def api_apply(request: Request, background_tasks: BackgroundTasks,
                    session: dict = Depends(require_owner)):
    if _update_status.get("running"):
        return JSONResponse({"error": "تحديث جارٍ بالفعل"}, status_code=400)
    body = await request.json()
    zip_path = body.get("zip_path", "")
    version = body.get("version", datetime.now().strftime("%Y%m%d"))
    if not zip_path or not os.path.exists(zip_path):
        return JSONResponse({"error": "لم يتم رفع ملف"}, status_code=400)
    analysis = _analyze_zip(zip_path)
    background_tasks.add_task(_apply_zip_bg, zip_path, analysis, version)
    return {"ok": True, "total": len(analysis["files"])}


@router.get("/api/status")
async def api_status(session: dict = Depends(require_owner)):
    return _update_status


@router.post("/api/backup")
async def api_backup(session: dict = Depends(require_owner)):
    try:
        name = _create_backup("manual")
        return {"ok": True, "name": name}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/restore")
async def api_restore(request: Request, background_tasks: BackgroundTasks,
                      session: dict = Depends(require_owner)):
    body = await request.json()
    backup_id = body.get("id")
    try:
        with dev_db() as conn:
            row = conn.execute("SELECT * FROM backups WHERE id=?", (backup_id,)).fetchone()
            if not row:
                return JSONResponse({"error": "النسخة غير موجودة"}, status_code=404)
            bpath = row["path"]
        if not os.path.exists(bpath):
            return JSONResponse({"error": "ملف النسخة غير موجود"}, status_code=404)
        analysis = {"files": [], "deps_changed": False}
        with zipfile.ZipFile(bpath, "r") as zf:
            for name in zf.namelist():
                if not name.endswith("/") and os.path.basename(name) not in PROTECTED_NAMES:
                    analysis["files"].append(name)
        background_tasks.add_task(_apply_zip_bg, bpath, analysis, f"restore_{backup_id}")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
