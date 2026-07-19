import shutil
import subprocess


def _detect() -> str | None:
    path = shutil.which("ffmpeg")
    if not path:
        return None
    try:
        r = subprocess.run([path, "-version"], capture_output=True, timeout=5)
        return path if r.returncode == 0 else None
    except Exception:
        return None


FFMPEG_PATH: str | None = _detect()
FFMPEG_AVAILABLE: bool = FFMPEG_PATH is not None
