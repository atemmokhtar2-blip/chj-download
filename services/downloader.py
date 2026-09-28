from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from config.settings import MAX_FILE_SIZE_BYTES, TEMP_DIR, DOWNLOAD_TIMEOUT, DOWNLOAD_PROXY, YTDLP_COOKIES_FILE, YTDLP_COOKIES_FROM_BROWSER
from services.content_intelligence import build_content_intelligence

ALBUM_MAX_ITEMS = 10
logger = logging.getLogger(__name__)


class FileTooLargeError(Exception):
    pass


def _platform(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    if "youtu" in host: return "YouTube"
    if "tiktok" in host: return "TikTok"
    if "instagram" in host: return "Instagram"
    if "facebook" in host or "fb.watch" in host: return "Facebook"
    if "twitter" in host or host == "x.com": return "X"
    if "soundcloud" in host: return "SoundCloud"
    if "reddit" in host: return "Reddit"
    if "pinterest" in host or "pin.it" in host: return "Pinterest"
    if "vimeo" in host: return "Vimeo"
    if "dailymotion" in host or "dai.ly" in host: return "Dailymotion"
    return host or "Generic"


def _base_opts(progress=None, loop: asyncio.AbstractEventLoop | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "outtmpl": os.path.join(TEMP_DIR, "%(title).80s-%(id)s.%(ext)s"),
        "retries": 5,
        "fragment_retries": 8,
        "socket_timeout": DOWNLOAD_TIMEOUT,
        "continuedl": True,
        "merge_output_format": "mp4",
        "http_headers": {"User-Agent": "Mozilla/5.0 XDownloader/1.0"},
    }
    if DOWNLOAD_PROXY:
        opts["proxy"] = DOWNLOAD_PROXY
    if YTDLP_COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (YTDLP_COOKIES_FROM_BROWSER,)
    elif YTDLP_COOKIES_FILE and os.path.exists(YTDLP_COOKIES_FILE):
        opts["cookiefile"] = YTDLP_COOKIES_FILE
    if progress and loop:
        def hook(d):
            if d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            pct = int(downloaded * 100 / total) if total else 0
            try:
                asyncio.run_coroutine_threadsafe(progress({
                    "pct": pct,
                    "downloaded": downloaded,
                    "total": total,
                    "speed": d.get("speed") or 0,
                    "eta": d.get("eta") or 0,
                }), loop)
            except RuntimeError as exc:
                logger.debug("Progress hook skipped: %s", exc)
        opts["progress_hooks"] = [hook]
    return opts


def _run_sync(fn, *args):
    return asyncio.to_thread(fn, *args)


def _extract_info_sync(url: str) -> dict | None:
    with yt_dlp.YoutubeDL({**_base_opts(), "skip_download": True, "extract_flat": False}) as ydl:
        return ydl.extract_info(url, download=False)


async def analyze_url(url: str) -> dict | None:
    try:
        info = await _run_sync(_extract_info_sync, url)
        if not info:
            return None
        entries = info.get("entries")
        if entries and isinstance(entries, list):
            items = [e for e in entries[:ALBUM_MAX_ITEMS] if e]
            if len(items) > 1:
                result = {
                    "url": url,
                    "title": info.get("title") or "Album",
                    "uploader": info.get("uploader") or info.get("channel") or "Unknown",
                    "duration": "Album",
                    "platform": _platform(url),
                    "media_type": "album",
                    "thumbnail": info.get("thumbnail") or (items[0].get("thumbnail") if items else None),
                    "album_items": [{"url": e.get("webpage_url") or e.get("url"), "title": e.get("title"), "type": "video"} for e in items],
                    "downloadable": True,
                }
                result["intelligence"] = build_content_intelligence(result, info)
                return result
            if items:
                info = items[0]
        formats = info.get("formats") or []
        qualities = []
        seen = set()
        for f in formats:
            height = f.get("height")
            if height and f.get("vcodec") != "none":
                label = f"{height}p"
                if label not in seen:
                    qualities.append({"label": label, "format_id": f.get("format_id")})
                    seen.add(label)
        qualities = sorted(qualities, key=lambda q: int(q["label"].replace("p", "")))
        ext = (info.get("ext") or "").lower()
        media_type = "audio" if info.get("vcodec") == "none" or ext in {"mp3", "m4a", "opus"} else "video"
        if not qualities and media_type == "video":
            qualities = [{"label": "best", "format_id": "best"}]
        result = {
            "url": info.get("webpage_url") or url,
            "title": info.get("title") or "Untitled",
            "uploader": info.get("uploader") or info.get("channel") or "Unknown",
            "duration": _format_duration(info.get("duration")),
            "platform": _platform(url),
            "media_type": media_type,
            "thumbnail": info.get("thumbnail"),
            "qualities": qualities[-5:],
            "media_id": info.get("id"),
            "downloadable": True,
        }
        result["intelligence"] = build_content_intelligence(result, info)
        return result
    except Exception as exc:
        logger.warning("URL analysis failed for %s: %s", url[:120], exc)
        return None


def _format_duration(seconds) -> str:
    if not seconds:
        return "Unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _check_size(path: str) -> str:
    if os.path.getsize(path) > MAX_FILE_SIZE_BYTES:
        try: os.remove(path)
        except OSError: pass
        raise FileTooLargeError()
    return path


def _download_sync(url: str, opts: dict) -> str | None:
    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if opts.get("merge_output_format") == "mp4":
            mp4 = os.path.splitext(filename)[0] + ".mp4"
            if os.path.exists(mp4):
                filename = mp4
        # audio postprocessors can change extension
        base = os.path.splitext(filename)[0]
        for candidate in (base + ".mp3", base + ".m4a", filename):
            if os.path.exists(candidate):
                return _check_size(candidate)
    return None


async def download_video(url: str, format_id: str = "best", quality_label: str = "best", progress=None, play_url: str | None = None) -> str | None:
    fmt = "bestvideo+bestaudio/best" if format_id in {"best", "best_quality"} else f"{format_id}+bestaudio/{format_id}/best"
    loop = asyncio.get_running_loop() if progress else None
    opts = {**_base_opts(progress, loop), "format": fmt, "merge_output_format": "mp4"}
    return await _run_sync(_download_sync, play_url or url, opts)


async def download_audio(url: str, progress=None) -> str | None:
    loop = asyncio.get_running_loop() if progress else None
    opts = {
        **_base_opts(progress, loop),
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    return await _run_sync(_download_sync, url, opts)


async def download_image(url: str, image_url: str | None = None) -> str | None:
    import requests
    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    target = image_url or url
    fd, path = tempfile.mkstemp(prefix="xdl-img-", suffix=".jpg", dir=TEMP_DIR)
    os.close(fd)
    r = await asyncio.to_thread(requests.get, target, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    with open(path, "wb") as fh:
        fh.write(r.content)
    return _check_size(path)


async def download_album(items: list[dict], progress=None) -> list[dict]:
    downloaded = []
    for idx, item in enumerate(items[:ALBUM_MAX_ITEMS], start=1):
        if progress:
            await progress({"album_index": idx, "album_total": min(len(items), ALBUM_MAX_ITEMS)})
        path = await download_video(item.get("url"), "best", "best")
        if path:
            downloaded.append({"path": path, "title": item.get("title") or f"item-{idx}", "type": "video"})
    return downloaded
