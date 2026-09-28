from __future__ import annotations

import subprocess
import sys


def get_ytdlp_version() -> str:
    try:
        import yt_dlp
        return getattr(yt_dlp.version, "__version__", "unknown")
    except Exception as exc:
        return f"unavailable: {exc}"


def update_ytdlp() -> dict:
    before = get_ytdlp_version()
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=180)
    after = get_ytdlp_version()
    return {
        "ok": proc.returncode == 0,
        "before": before,
        "after": after,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
