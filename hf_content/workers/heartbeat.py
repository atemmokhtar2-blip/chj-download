"""
Heartbeat worker — writes /tmp/bot_health.json every 5 minutes.

This is real work: DB queries + atomic file write + structured log entry.
It is NOT a keep-alive loop — the health file is read by the API server
and UptimeRobot pings /api/health to confirm the bot is alive.
"""
import asyncio
import json
import os
import time
from datetime import datetime

BOT_START_TIME = time.time()
HEALTH_FILE = "/tmp/bot_health.json"
HEARTBEAT_INTERVAL = 300  # 5 minutes


async def run_heartbeat():
    """Async heartbeat task — started in post_init alongside the bot."""
    from utils.logger import system_logger, error_logger

    # Write immediately on startup so health file exists before first ping
    _write_health_file()
    system_logger.info("[HEALTH] Heartbeat worker started. Health file: " + HEALTH_FILE)

    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            _write_health_file()
            data = get_health_data()
            system_logger.info(
                f"[HEARTBEAT] uptime={data['uptime_human']} | "
                f"users={data['users_total']} | "
                f"downloads_total={data['downloads_total']} | "
                f"downloads_today={data['downloads_today']} | "
                f"cache={data['cache_entries']}"
            )
        except Exception as exc:
            error_logger.error(f"[HEARTBEAT] Write failed: {exc}", exc_info=True)


def _write_health_file():
    """Collect stats and atomically write the health JSON file."""
    from database.users import get_total_users
    from database.downloads import get_total_downloads, get_downloads_today
    from database.cache import get_cache_count, get_cache_hits

    now = time.time()
    data = {
        "status": "ok",
        "timestamp": now,
        "timestamp_iso": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + " UTC",
        "uptime_seconds": int(now - BOT_START_TIME),
        "uptime_human": _format_uptime(),
        "users_total": _safe(get_total_users),
        "downloads_total": _safe(get_total_downloads),
        "downloads_today": _safe(get_downloads_today),
        "cache_entries": _safe(get_cache_count),
        "cache_hits": _safe(get_cache_hits),
    }

    # Atomic write: write to .tmp then rename — prevents partial reads
    tmp = HEALTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, HEALTH_FILE)


def get_health_data() -> dict:
    """Read health data from file — used by /status command and API route."""
    try:
        with open(HEALTH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "status": "starting",
            "uptime_seconds": int(time.time() - BOT_START_TIME),
            "uptime_human": _format_uptime(),
            "users_total": 0,
            "downloads_total": 0,
            "downloads_today": 0,
            "cache_entries": 0,
            "cache_hits": 0,
            "timestamp_iso": "not written yet",
        }
    except Exception:
        return {"status": "error", "uptime_seconds": 0, "uptime_human": "unknown"}


def _format_uptime() -> str:
    seconds = int(time.time() - BOT_START_TIME)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def _safe(fn):
    try:
        return fn()
    except Exception:
        return 0
