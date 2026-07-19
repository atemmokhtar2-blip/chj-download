"""
Crash monitor — wraps the bot process with structured startup/crash logging.

On startup: logs PID, time, and Python version to system log.
On crash: writes a full crash report to logs/crashes.log and the error log,
          then re-raises so the process exits and Replit restarts the workflow.
"""
import os
import sys
import traceback
from datetime import datetime

CRASH_LOG = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "logs", "crashes.log"
)


def record_startup():
    """Log a clean startup event. Call this once at the top of main()."""
    from utils.logger import system_logger
    system_logger.info(
        f"=== BOT STARTUP === "
        f"PID:{os.getpid()} | "
        f"Python:{sys.version.split()[0]} | "
        f"Time:{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )


def record_crash(exc: Exception):
    """Log crash details to error log and crash file. Call from except block."""
    from utils.logger import error_logger, system_logger

    tb = traceback.format_exc()
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    crash_block = (
        f"\n{'='*60}\n"
        f"CRASH REPORT\n"
        f"Time    : {timestamp} UTC\n"
        f"PID     : {os.getpid()}\n"
        f"Python  : {sys.version.split()[0]}\n"
        f"Error   : {type(exc).__name__}: {exc}\n"
        f"{'='*60}\n"
        f"{tb}"
        f"{'='*60}\n"
    )

    error_logger.critical(f"BOT CRASH: {type(exc).__name__}: {exc}\n{tb}")
    system_logger.critical(f"=== BOT CRASHED === {type(exc).__name__}: {exc}")

    try:
        os.makedirs(os.path.dirname(CRASH_LOG), exist_ok=True)
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(crash_block)
    except Exception:
        pass  # Don't let crash logging cause a secondary failure

    # Write a stale health file so the API /health returns 503 immediately
    try:
        import json, time
        health_file = "/tmp/bot_health.json"
        if os.path.exists(health_file):
            with open(health_file, encoding="utf-8") as f:
                data = json.load(f)
            data["status"] = "crashed"
            data["crash_reason"] = f"{type(exc).__name__}: {exc}"
            data["timestamp"] = 0  # Force stale — API will return 503
            with open(health_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
    except Exception:
        pass
