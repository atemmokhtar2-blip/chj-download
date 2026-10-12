from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from config.settings import TEMP_DIR

logger = logging.getLogger(__name__)


def _ffmpeg_path() -> str | None:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _probe_duration(path: str) -> float | None:
    """Return media duration in seconds via ffprobe, with an ffmpeg fallback."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            proc = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0:
                first_line = (proc.stdout or "").strip().splitlines()
                if first_line:
                    duration = float(first_line[0])
                    if duration > 0:
                        return duration
        except Exception as exc:
            logger.debug("ffprobe duration failed: %s", exc)

    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", path, "-f", "null", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
        if match:
            h, m, s = match.groups()
            return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception as exc:
        logger.debug("ffmpeg duration fallback failed: %s", exc)
    return None


def _run_segment(ffmpeg: str, pre_args: list[str], out_pattern: str, t_seconds: int) -> list[str]:
    """Run a single ffmpeg segment pass and return existing part files."""
    out_path = Path(out_pattern)
    parent = out_path.parent
    prefix = out_path.name.rsplit("_%03d", 1)[0]

    for old in parent.glob(f"{prefix}_*.mp4"):
        try:
            os.remove(old)
        except OSError:
            pass

    cmd = [
        ffmpeg, "-y",
        *pre_args,
        "-f", "segment",
        "-segment_time", str(t_seconds),
        "-reset_timestamps", "1",
        out_pattern,
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=1800,
        check=False,
    )
    if proc.returncode != 0:
        logger.debug("ffmpeg segment failed: %s", (proc.stderr or "")[-500:])

    return sorted(str(p) for p in parent.glob(f"{prefix}_*.mp4"))


def split_video_parts(path: str, max_bytes: int) -> list[str]:
    """Split a video into parts that each fit below ``max_bytes``.

    Returns a list of part paths on success, ``[path]`` when the original file
    already fits, or ``[]`` when splitting is impossible (no ffmpeg, no
    duration, or all attempts produced oversized parts).
    """
    path = str(path or "")
    if not path or not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size <= max_bytes:
        return [path]

    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        logger.warning("Cannot split large video (%s bytes): ffmpeg unavailable", size)
        return []

    duration = _probe_duration(path)
    if not duration or duration <= 0:
        logger.warning("Cannot split large video: duration unknown")
        return []

    # Estimated bytes-per-second, then seconds per part with a ~15% margin.
    bitrate = size / duration
    target_t = max(2, int((max_bytes * 0.85) / bitrate)) if bitrate > 0 else 600

    stem = (os.path.splitext(os.path.basename(path))[0] or "part")[:40]
    out_pattern = os.path.join(TEMP_DIR, f"{stem}_part_%03d.mp4")

    def all_within(parts: list[str]) -> bool:
        return bool(parts) and all(os.path.getsize(p) <= max_bytes for p in parts)

    # Pass 1: fast stream-copy split (cuts on keyframes only).
    parts = _run_segment(
        ffmpeg,
        ["-i", path, "-map", "0", "-c", "copy"],
        out_pattern,
        target_t,
    )
    if all_within(parts):
        return parts

    # Pass 2+: re-encode with increasingly smaller chunks for size control.
    t = max(2, target_t // 2)
    for _ in range(4):
        parts = _run_segment(
            ffmpeg,
            [
                "-i", path,
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "26",
                "-c:a", "aac",
                "-b:a", "128k",
            ],
            out_pattern,
            t,
        )
        if all_within(parts):
            return parts
        t = max(2, t // 2)

    # Absolute fallback: return only whole parts that each fit.
    fitting = [p for p in parts if os.path.getsize(p) <= max_bytes]
    return fitting if len(fitting) >= 2 else []
