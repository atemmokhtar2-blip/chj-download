import os
import time
import json
from datetime import datetime
import psutil
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from ..auth import require_owner
from ..config import CONTROL_PANEL_DIR, PROJECT_ROOT

router = APIRouter(prefix="/system")
templates = Jinja2Templates(directory=os.path.join(CONTROL_PANEL_DIR, "templates"))

BOT_HEALTH_FILE = "/tmp/bot_health.json"

BOT_SCRIPTS = {
    "main":    ("البوت الرئيسي", "bot.py"),
    "admin":   ("بوت الإدمن", "admin_bot/bot.py"),
    "support": ("بوت الدعم", "support_bot/bot.py"),
}


def _get_system_stats() -> dict:
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(PROJECT_ROOT)
    net = psutil.net_io_counters()
    boot_time = psutil.boot_time()
    uptime_sec = int(time.time() - boot_time)
    uptime = f"{uptime_sec // 3600}h {(uptime_sec % 3600) // 60}m"
    return {
        "cpu_percent": cpu,
        "mem_total": mem.total, "mem_used": mem.used, "mem_percent": mem.percent,
        "disk_total": disk.total, "disk_used": disk.used, "disk_percent": disk.percent,
        "net_sent": net.bytes_sent, "net_recv": net.bytes_recv,
        "uptime": uptime, "ts": datetime.utcnow().strftime("%H:%M:%S"),
    }


def _fmt_bytes(b: int) -> str:
    if b < 1024: return f"{b} B"
    elif b < 1024**2: return f"{b/1024:.1f} KB"
    elif b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def _get_bot_status() -> list:
    health = {}
    try:
        if os.path.exists(BOT_HEALTH_FILE):
            with open(BOT_HEALTH_FILE) as f:
                health = json.load(f)
    except Exception:
        pass

    bots = []
    for key, (label, script) in BOT_SCRIPTS.items():
        running = False
        pid = None
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                cmdline = proc.info.get("cmdline") or []
                if script in " ".join(cmdline):
                    running = True
                    pid = proc.info["pid"]
                    break
        except Exception:
            pass
        last_seen = health.get("ts", "") if key == "main" else ""
        bots.append({"key": key, "label": label, "script": script,
                     "running": running, "pid": pid, "last_seen": last_seen})
    return bots


def _get_processes() -> list:
    procs = []
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent", "memory_percent", "status"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "python" in (proc.info.get("name") or "").lower() or "uvicorn" in cmdline:
                    procs.append({
                        "pid": proc.info["pid"], "name": proc.info["name"],
                        "cmdline": cmdline[:80],
                        "cpu": round(proc.info.get("cpu_percent") or 0, 1),
                        "mem": round(proc.info.get("memory_percent") or 0, 1),
                        "status": proc.info.get("status", ""),
                    })
            except Exception:
                pass
    except Exception:
        pass
    return procs[:20]


@router.get("", response_class=HTMLResponse)
async def system_page(request: Request, session: dict = Depends(require_owner)):
    stats = _get_system_stats()
    bots = _get_bot_status()
    procs = _get_processes()
    return templates.TemplateResponse(request, "system.html", {
        "stats": stats, "bots": bots, "procs": procs,
        "active_page": "system"
    })


@router.get("/api/stats")
async def api_stats(session: dict = Depends(require_owner)):
    stats = _get_system_stats()
    stats["mem_total_h"] = _fmt_bytes(stats["mem_total"])
    stats["mem_used_h"] = _fmt_bytes(stats["mem_used"])
    stats["disk_total_h"] = _fmt_bytes(stats["disk_total"])
    stats["disk_used_h"] = _fmt_bytes(stats["disk_used"])
    stats["net_sent_h"] = _fmt_bytes(stats["net_sent"])
    stats["net_recv_h"] = _fmt_bytes(stats["net_recv"])
    return stats


@router.get("/api/bots")
async def api_bots(session: dict = Depends(require_owner)):
    return _get_bot_status()
