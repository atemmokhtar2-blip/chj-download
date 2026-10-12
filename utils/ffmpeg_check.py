import shutil
import subprocess


def _bundled_ffmpeg() -> str | None:
    """Return the imageio-ffmpeg bundled binary when no system ffmpeg exists."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _detect() -> str | None:
    path = shutil.which("ffmpeg") or _bundled_ffmpeg()
    if not path:
        return None
    try:
        r = subprocess.run([path, "-version"], capture_output=True, timeout=10)
        return path if r.returncode == 0 else None
    except Exception:
        return None


FFMPEG_PATH: str | None = _detect()
FFMPEG_AVAILABLE: bool = FFMPEG_PATH is not None
