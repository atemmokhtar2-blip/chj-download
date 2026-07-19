"""
Heartbeat worker — writes /tmp/bot_health.json every 5 minutes.
Also includes a self-ping mechanism to keep Hugging Face Spaces alive.
"""
import asyncio
import json
import os
import time
import requests
from datetime import datetime

BOT_START_TIME = time.time()
HEALTH_FILE = "/tmp/bot_health.json"
HEARTBEAT_INTERVAL = 300  # 5 minutes
SELF_PING_INTERVAL = 1800 # 30 minutes (HF Spaces sleep after 48h of inactivity or 1h of no HTTP traffic)

async def run_heartbeat():
    """Async heartbeat task — started in post_init alongside the bot."""
    from utils.logger import system_logger, error_logger

    # Write immediately on startup so health file exists before first ping
    _write_health_file()
    system_logger.info("[HEALTH] Heartbeat worker started. Health file: " + HEALTH_FILE)

    # Start self-ping task in background
    asyncio.create_task(keep_alive_ping())

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

async def keep_alive_ping():
    """Periodically ping the Space's own URL to prevent sleeping."""
    from utils.logger import system_logger
    
    # Try to get the Space URL from environment variables
    # Hugging Face usually provides SPACE_ID
    space_id = os.getenv("SPACE_ID")
    if not space_id:
        system_logger.warning("[KEEP-ALIVE] SPACE_ID not found, self-ping disabled.")
        return

    # Construct the URL (e.g., https://user-repo.hf.space)
    # Format is usually: https://{user}-{repo}.hf.space
    url = f"https://{space_id.replace('/', '-')}.hf.space"
    
    system_logger.info(f"[KEEP-ALIVE] Starting self-ping for: {url}")
    
    while True:
        try:
            # Use a simple GET request
            response = requests.get(url, timeout=10)
            system_logger.info(f"[KEEP-ALIVE] Ping successful: {response.status_code}")
        except Exception as e:
            system_logger.error(f"[KEEP-ALIVE] Ping failed: {e}")
        
        await asyncio.sleep(SELF_PING_INTERVAL)

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
