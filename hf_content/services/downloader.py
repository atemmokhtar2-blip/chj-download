import asyncio
import os
import re
import tempfile
import aiohttp
from typing import Callable, Optional
import yt_dlp
from config.settings import TEMP_DIR, MAX_FILE_SIZE_BYTES, DOWNLOAD_TIMEOUT
from utils.helpers import sanitize_filename, format_duration, format_size, get_platform
from utils.logger import download_logger, error_logger
from utils.ffmpeg_check import FFMPEG_AVAILABLE, FFMPEG_PATH

# Platforms that primarily serve images/carousels
IMAGE_PLATFORMS = {"Pinterest", "Instagram"}
# Platforms that serve audio
AUDIO_PLATFORMS = {"SoundCloud", "Spotify"}


def _extract_info_sync(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "extract_flat": False,
    }
    # For Pinterest and Instagram, allow images
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def _detect_media_type(info: dict, platform: str) -> str:
    """Detect content type: video, audio, image, album."""
    # Check if it's a playlist/album
    entries = info.get("entries")
    if entries:
        return "album"

    formats = info.get("formats", [])

    # Check for audio-only platforms
    if platform in AUDIO_PLATFORMS:
        return "audio"

    # Check formats for video
    has_video = any(
        f.get("vcodec", "none") not in ("none", None) and f.get("height")
        for f in formats
    )
    has_audio_only = any(
        f.get("vcodec", "none") in ("none", None) and
        f.get("acodec", "none") not in ("none", None)
        for f in formats
    )

    # If no formats at all, try to detect from other fields
    if not formats:
        # Pinterest/Instagram images come with a direct URL
        ext = (info.get("ext") or "").lower()
        if ext in ("jpg", "jpeg", "png", "webp", "gif"):
            return "image"
        url_field = info.get("url", "")
        if any(x in url_field.lower() for x in [".jpg", ".jpeg", ".png", ".webp"]):
            return "image"
        # SoundCloud/Spotify audio
        if platform in AUDIO_PLATFORMS:
            return "audio"
        return "video"

    if has_video:
        return "video"
    if has_audio_only:
        return "audio"

    # Fallback: Pinterest image
    if platform == "Pinterest":
        return "image"

    return "video"


async def analyze_url(url: str) -> dict | None:
    try:
        loop = asyncio.get_event_loop()
        info = await asyncio.wait_for(
            loop.run_in_executor(None, _extract_info_sync, url),
            timeout=30
        )
        if not info:
            return None

        platform = get_platform(url)
        media_type = _detect_media_type(info, platform)
        formats = info.get("formats", [])

        quality_map = {}
        audio_formats = []

        for f in formats:
            height = f.get("height")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            fmt_id = f.get("format_id", "")
            filesize = f.get("filesize") or f.get("filesize_approx") or 0

            # Collect audio-only formats
            if vcodec in ("none", None) and acodec not in ("none", None):
                audio_formats.append({
                    "format_id": fmt_id,
                    "filesize": filesize,
                    "abr": f.get("abr", 0),
                })
                continue

            if vcodec in ("none", None) or not height:
                continue

            label = f"{height}p"
            if label not in quality_map:
                quality_map[label] = {
                    "label": label,
                    "height": height,
                    "format_id": fmt_id,
                    "filesize": filesize,
                    "has_audio": acodec not in ("none", None),
                }
            else:
                existing = quality_map[label]
                if filesize > existing["filesize"]:
                    quality_map[label] = {
                        "label": label,
                        "height": height,
                        "format_id": fmt_id,
                        "filesize": filesize,
                        "has_audio": acodec not in ("none", None),
                    }

        qualities = sorted(quality_map.values(), key=lambda x: x["height"])
        thumbnail = info.get("thumbnail", "")
        duration_secs = info.get("duration", 0)

        # Handle image type — extract direct image URL
        image_url = None
        if media_type == "image":
            image_url = _extract_image_url(info)

        # Handle album entries
        album_items = []
        if media_type == "album":
            entries = info.get("entries", [])
            for entry in entries[:10]:  # limit to 10 items
                if entry:
                    item_type = _detect_media_type(entry, platform)
                    album_items.append({
                        "title": entry.get("title", ""),
                        "url": entry.get("webpage_url") or entry.get("url", ""),
                        "thumbnail": entry.get("thumbnail", ""),
                        "type": item_type,
                    })

        return {
            "title": info.get("title", "Unknown"),
            "uploader": info.get("uploader") or info.get("channel") or "Unknown",
            "duration": format_duration(int(duration_secs)) if duration_secs else "Unknown",
            "duration_secs": duration_secs,
            "thumbnail": thumbnail,
            "platform": platform,
            "media_type": media_type,
            "qualities": qualities,
            "audio_formats": audio_formats,
            "image_url": image_url,
            "album_items": album_items,
            "url": url,
            "webpage_url": info.get("webpage_url", url),
            "ext": info.get("ext", ""),
        }
    except asyncio.TimeoutError:
        error_logger.error(f"Timeout analyzing: {url}")
        return None
    except Exception as e:
        error_logger.error(f"Error analyzing {url}: {e}")
        return None


def _extract_image_url(info: dict) -> str | None:
    """Extract direct image URL from info dict (Pinterest, Instagram images)."""
    # Try direct url field
    direct_url = info.get("url", "")
    if direct_url:
        ext = direct_url.split("?")[0].lower()
        if any(ext.endswith(x) for x in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
            return direct_url

    # Try formats
    formats = info.get("formats", [])
    for f in formats:
        furl = f.get("url", "")
        if furl:
            ext = furl.split("?")[0].lower()
            if any(ext.endswith(x) for x in [".jpg", ".jpeg", ".png", ".webp"]):
                return furl

    # Try thumbnail as fallback (Pinterest often puts image in thumbnail)
    thumbnail = info.get("thumbnail", "")
    if thumbnail:
        return thumbnail

    return info.get("url") or None


async def download_image(url: str, image_url: str) -> str | None:
    """Download an image from a direct URL."""
    try:
        safe_name = sanitize_filename(f"image_{hash(url) % 100000}")
        # Determine extension from URL
        clean_url = image_url.split("?")[0]
        ext = ".jpg"
        for e in [".png", ".webp", ".gif", ".jpeg"]:
            if clean_url.lower().endswith(e):
                ext = e
                break
        out_path = os.path.join(TEMP_DIR, f"{safe_name}{ext}")

        async with aiohttp.ClientSession() as session:
            async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    with open(out_path, "wb") as f:
                        f.write(content)
                    return out_path
        return None
    except Exception as e:
        error_logger.error(f"Image download error {url}: {e}")
        return None


def _download_sync(url: str, fmt_id: str, out_path: str,
                   progress_hook: Callable = None) -> str:
    ydl_opts = {
        "format": fmt_id,
        "outtmpl": out_path,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
        "postprocessor_args": ["-c:a", "aac"],
    }
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    candidates = [
        out_path + ".mp4",
        out_path,
        out_path.replace(".%(ext)s", ".mp4"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    base = out_path.replace(".%(ext)s", "")
    for ext in [".mp4", ".mkv", ".webm", ".avi"]:
        if os.path.exists(base + ext):
            return base + ext

    raise FileNotFoundError(f"Downloaded file not found for {url}")


def _download_audio_sync(url: str, out_path: str,
                         progress_hook: Callable = None) -> str:
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_path,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "writethumbnail": False,
        "embedthumbnail": False,
    }
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    mp3_path = out_path.replace(".%(ext)s", ".mp3")
    if os.path.exists(mp3_path):
        return mp3_path

    base = out_path.replace(".%(ext)s", "")
    if os.path.exists(base + ".mp3"):
        return base + ".mp3"

    raise FileNotFoundError("MP3 file not found after conversion")


async def download_video(url: str, format_id: str, quality_label: str,
                         progress_callback: Callable = None) -> str | None:
    safe_name = sanitize_filename(f"video_{hash(url) % 100000}_{quality_label}")
    out_path = os.path.join(TEMP_DIR, f"{safe_name}.%(ext)s")

    last_percent = [0]
    loop = asyncio.get_running_loop()

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            if total and progress_callback:
                pct = int(downloaded / total * 100)
                if pct >= last_percent[0] + 5:
                    last_percent[0] = pct
                    data = {"pct": pct, "downloaded": downloaded,
                            "total": total, "speed": speed, "eta": eta}
                    loop.call_soon_threadsafe(
                        lambda d=data: loop.create_task(progress_callback(d))
                    )

    try:
        fmt = f"{format_id}+bestaudio/best[height<={quality_label.replace('p', '')}]"
        file_path = await asyncio.wait_for(
            loop.run_in_executor(None, _download_sync, url, fmt, out_path, hook),
            timeout=DOWNLOAD_TIMEOUT
        )

        if not os.path.exists(file_path):
            return None

        size = os.path.getsize(file_path)
        if size > MAX_FILE_SIZE_BYTES:
            os.remove(file_path)
            raise FileTooLargeError(size)

        download_logger.info(f"Downloaded video: {url} [{quality_label}] -> {file_path}")
        return file_path

    except FileTooLargeError:
        raise
    except Exception as e:
        error_logger.error(f"Video download error {url}: {e}")
        return None


async def download_audio(url: str, progress_callback: Callable = None) -> str | None:
    safe_name = sanitize_filename(f"audio_{hash(url) % 100000}")
    out_path = os.path.join(TEMP_DIR, f"{safe_name}.%(ext)s")

    last_percent = [0]
    loop = asyncio.get_running_loop()

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            if total and progress_callback:
                pct = int(downloaded / total * 100)
                if pct >= last_percent[0] + 5:
                    last_percent[0] = pct
                    data = {"pct": pct, "downloaded": downloaded,
                            "total": total, "speed": speed, "eta": eta}
                    loop.call_soon_threadsafe(
                        lambda d=data: loop.create_task(progress_callback(d))
                    )

    try:
        file_path = await asyncio.wait_for(
            loop.run_in_executor(None, _download_audio_sync, url, out_path, hook),
            timeout=DOWNLOAD_TIMEOUT
        )

        if not os.path.exists(file_path):
            return None

        size = os.path.getsize(file_path)
        if size > MAX_FILE_SIZE_BYTES:
            os.remove(file_path)
            raise FileTooLargeError(size)

        download_logger.info(f"Downloaded audio: {url} -> {file_path}")
        return file_path

    except FileTooLargeError:
        raise
    except Exception as e:
        error_logger.error(f"Audio download error {url}: {e}")
        return None


class FileTooLargeError(Exception):
    def __init__(self, size: int):
        self.size = size
        super().__init__(f"File too large: {format_size(size)}")
