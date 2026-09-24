import subprocess
import sys
import yt_dlp


def get_ytdlp_version() -> str:
    return getattr(yt_dlp.version, "__version__", "unknown")


def update_ytdlp() -> dict:
    """Upgrade yt-dlp in-place. Intended for admin-triggered maintenance."""
    before = get_ytdlp_version()
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    after = get_ytdlp_version()
    # In a running process the imported module may still report the old version until restart.
    return {
        "ok": proc.returncode == 0,
        "before": before,
        "after": after,
        "stdout": (proc.stdout or "")[-2000:],
        "stderr": (proc.stderr or "")[-2000:],
    }
