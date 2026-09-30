from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from config.settings import MAX_FILE_SIZE_BYTES, TEMP_DIR, DOWNLOAD_TIMEOUT, DOWNLOAD_PROXY, YTDLP_COOKIES_FILE, YTDLP_COOKIES_FROM_BROWSER, YTDLP_CLIENT
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
    if "reddit" in host or host == "redd.it" or "v.redd.it" in host: return "Reddit"
    if "pinterest" in host or "pin.it" in host: return "Pinterest"
    if "threads.net" in host: return "Threads"
    if "snapchat" in host: return "Snapchat"
    if "vimeo" in host: return "Vimeo"
    if "dailymotion" in host or "dai.ly" in host: return "Dailymotion"
    if host in {"t.me", "telegram.me", "telegram.dog"}: return "Telegram"
    return host or "Generic"


YOUTUBE_PLAYER_CLIENTS = {"android", "web", "ios", "mweb", "tv", "tv_embedded", "web_creator"}


def _safe_youtube_clients() -> list[str]:
    """Filter env config to valid yt-dlp YouTube player_client values only."""
    candidates = [YTDLP_CLIENT, "android", "web", "ios", "mweb"]
    out: list[str] = []
    for raw in candidates:
        item = str(raw or "").strip()
        if item in YOUTUBE_PLAYER_CLIENTS and item not in out:
            out.append(item)
    return out or ["android", "web"]


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
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        },
        "extractor_args": {
            "youtube": {"player_client": _safe_youtube_clients()},
            "tiktok": {"api_hostname": ["api16-normal-c-useast1a.tiktokv.com"]},
        },
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


async def _generic_analyze_url(url: str) -> dict | None:
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
        for candidate in (base + ".mp3", base + ".m4a", base + ".mp4", filename):
            if os.path.exists(candidate):
                return _check_size(candidate)
    return None


def _video_format_candidates(format_id: str = "best") -> list[str]:
    """Return robust yt-dlp format fallbacks for YouTube/TikTok failures."""
    requested = str(format_id or "best").strip()
    best_mp4 = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
    safe_small = "best[height<=720][ext=mp4]/best[height<=720]/worst[ext=mp4]/worst"
    if requested in {"best", "best_quality"}:
        return [best_mp4, "best[ext=mp4]/best", safe_small]
    return [
        f"{requested}+bestaudio[ext=m4a]/{requested}+bestaudio/{requested}",
        f"bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
        "best[ext=mp4]/best",
        safe_small,
    ]


def _youtube_client_candidates() -> list[str]:
    return _safe_youtube_clients()


async def _generic_download_video(url: str, format_id: str = "best", quality_label: str = "best", progress=None, play_url: str | None = None) -> str | None:
    loop = asyncio.get_running_loop() if progress else None
    target = play_url or url
    host = urlparse(url).netloc.lower()
    formats = _video_format_candidates(format_id)
    clients = _youtube_client_candidates() if "youtu" in host and not play_url else [YTDLP_CLIENT]
    last_error: Exception | None = None

    for client in clients:
        for fmt in formats:
            opts = {**_base_opts(progress, loop), "format": fmt, "merge_output_format": "mp4"}
            if client:
                opts.setdefault("extractor_args", {}).setdefault("youtube", {})["player_client"] = [client]
            try:
                path = await _run_sync(_download_sync, target, opts)
                if path:
                    if fmt != formats[0] or client != clients[0]:
                        logger.info("Download fallback succeeded: client=%s format=%s url=%s", client, fmt, url[:100])
                    return path
            except FileTooLargeError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning("Download attempt failed client=%s format=%s url=%s error=%s", client, fmt, url[:100], exc)
                continue

    if last_error:
        raise last_error
    return None


async def _generic_download_audio(url: str, progress=None) -> str | None:
    loop = asyncio.get_running_loop() if progress else None
    opts = {
        **_base_opts(progress, loop),
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    return await _run_sync(_download_sync, url, opts)


def _normalize_tiktok_info(raw: dict, original_url: str) -> dict:
    """Normalize TikTok scraper output into the downloader contract."""
    images = raw.get("images") or []
    album_items = raw.get("album_items") or []
    image_url = raw.get("image_url") or (images[0] if images else None)
    play_url = raw.get("play_url") or raw.get("video_url") or raw.get("download_url")
    media_type = "album" if album_items or len(images) > 1 else ("image" if image_url and not play_url else "video")
    normalized = {
        "url": raw.get("webpage_url") or original_url,
        "title": raw.get("title") or raw.get("desc") or "TikTok media",
        "uploader": raw.get("uploader") or raw.get("author") or "TikTok",
        "duration": _format_duration(raw.get("duration")),
        "platform": "TikTok",
        "media_type": media_type,
        "thumbnail": raw.get("thumbnail") or image_url,
        "qualities": [{"label": "best", "format_id": "best"}] if play_url or media_type == "video" else [],
        "media_id": raw.get("id") or raw.get("aweme_id"),
        "downloadable": bool(play_url or image_url or album_items or images),
        "play_url": play_url,
        "image_url": image_url,
        "album_items": album_items or [{"url": img, "title": raw.get("title") or "TikTok image", "type": "image"} for img in images],
    }
    normalized["intelligence"] = build_content_intelligence(normalized, raw)
    return normalized


def _select_engine(url: str):
    """Choose a platform-specific engine lazily to avoid import cycles."""
    from services.engines import registered_engines
    from services.engines.base import first_match
    from services.engines.platforms import GenericEngine

    return first_match(url, registered_engines(), GenericEngine())


async def analyze_url(url: str) -> dict | None:
    engine = _select_engine(url)
    try:
        return await engine.analyze(url)
    except Exception as exc:
        logger.warning("Engine analyze failed (%s) for %s: %s", getattr(engine, "name", "unknown"), url[:120], exc)
        return await _generic_analyze_url(url)


async def download_video(url: str, format_id: str = "best", quality_label: str = "best", progress=None, play_url: str | None = None) -> str | None:
    engine = _select_engine(url)
    try:
        return await engine.download_video(url, format_id, quality_label, progress, play_url)
    except FileTooLargeError:
        raise
    except Exception as exc:
        logger.warning("Engine video download failed (%s) for %s: %s", getattr(engine, "name", "unknown"), url[:120], exc)
        return await _generic_download_video(url, format_id, quality_label, progress, play_url)


async def download_audio(url: str, progress=None) -> str | None:
    engine = _select_engine(url)
    try:
        return await engine.download_audio(url, progress)
    except FileTooLargeError:
        raise
    except Exception as exc:
        logger.warning("Engine audio download failed (%s) for %s: %s", getattr(engine, "name", "unknown"), url[:120], exc)
        return await _generic_download_audio(url, progress)


# Compatibility aliases used by platform engines.
_enforce_max_file_size = _check_size


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
        if item.get("type") == "image":
            path = await download_image(item.get("url"), item.get("url"))
            item_type = "image"
        else:
            path = await download_video(item.get("url"), "best", "best")
            item_type = "video"
        if path:
            downloaded.append({"path": path, "title": item.get("title") or f"item-{idx}", "type": item_type})
    return downloaded
