from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from config.settings import (
    MAX_FILE_SIZE_BYTES, TEMP_DIR, DOWNLOAD_TIMEOUT, DOWNLOAD_PROXY,
    YTDLP_COOKIES_FILE, YTDLP_COOKIES_FROM_BROWSER, YTDLP_CLIENT,
    AUDIO_BITRATE, AUDIO_BITRATE_HIGH,
)
from services.content_intelligence import build_content_intelligence

ALBUM_MAX_ITEMS = 10
QUALITY_MAX_ROWS = 6
_STANDARD_HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240, 144)
logger = logging.getLogger(__name__)


def _quality_height(label) -> int:
    """Extract a numeric height from a quality label ('1080p60' -> 1080, 'Best' -> 0)."""
    match = re.search(r"(\d{3,4})", str(label or ""))
    return int(match.group(1)) if match else 0


def _standard_height(value) -> int | None:
    """Map any probed height to the nearest Telegram-safe standard resolution."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v < 144:
        return None
    if v >= 2000:
        return 2160
    if v >= 1400:
        return 1440
    if v >= 1000:
        return 1080
    if v >= 700:
        return 720
    if v >= 450:
        return 480
    if v >= 360:
        return 360
    if v >= 240:
        return 240
    return 144


def normalize_qualities(qualities) -> list:
    """Keep only real, standard video resolutions and drop fake quality rows.

    Scrapers for single-file platforms (TikTok/Instagram/Facebook/X/Reddit/
    Pinterest) attach labels like ``Best`` or ``HD`` that would render as
    "📹 Best" buttons even though no resolution probe was performed. Those are
    removed here. Non-standard heights (e.g. 768p, 1916p) are bucketed to the
    nearest standard resolution and deduped, so the inline keyboard only ever
    advertises qualities that really exist on the media.
    """
    buckets: dict[str, dict] = {}
    for candidate in qualities or []:
        if not isinstance(candidate, dict):
            continue
        raw = _quality_height(candidate.get("label"))
        if not raw and candidate.get("height") is not None:
            raw = _quality_height(f"{candidate.get('height')}p")
        std = _standard_height(raw) if raw else None
        if not std:
            continue

        label = f"{std}p"
        format_id = str(candidate.get("format_id") or "best").strip() or "best"
        entry = buckets.get(label)
        if entry is None:
            entry = {"label": label, "format_id": format_id}
            if candidate.get("has_audio") is not None:
                entry["has_audio"] = bool(candidate.get("has_audio"))
            buckets[label] = entry
            continue
        if (not entry.get("format_id") or entry.get("format_id") == "best") and format_id != "best":
            entry["format_id"] = format_id
        if entry.get("has_audio") is None and candidate.get("has_audio") is not None:
            entry["has_audio"] = bool(candidate.get("has_audio"))

    ordered = sorted(buckets.values(), key=lambda q: _quality_height(q["label"]))
    if len(ordered) > QUALITY_MAX_ROWS:
        ordered = ordered[-QUALITY_MAX_ROWS:]
    return ordered




class FileTooLargeError(Exception):
    def __init__(self, size_bytes: int | None = None, limit_bytes: int | None = None):
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__("Media file exceeds the configured size limit.")


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
    if "likee" in host: return "Likee"
    if "kwai" in host or "kw.ai" in host: return "Kwai"
    if "twitch" in host: return "Twitch"
    if "bilibili" in host or "b23.tv" in host: return "Bilibili"
    if "vk.com" in host or "vkvideo" in host or "vk.cc" in host: return "VK"
    if "tumblr" in host or "tmblr.co" in host: return "Tumblr"
    if "imgur" in host: return "Imgur"
    if "streamable" in host: return "Streamable"
    if "coub" in host: return "Coub"
    if "ok.ru" in host or "odnoklassniki" in host: return "Odnoklassniki"
    if "spotify" in host: return "Spotify"
    if "rumble" in host: return "Rumble"
    if "odysee" in host: return "Odysee"
    if "nicovideo" in host or host == "nico.ms": return "Niconico"
    if "douyin" in host: return "Douyin"
    if "xiaohongshu" in host or "xhslink" in host: return "Xiaohongshu"
    return host or "Generic"


YOUTUBE_PLAYER_CLIENTS = {
    "android", "android_music", "ios", "ios_music", "web", "mweb",
    "tv", "tv_embedded", "web_creator", "web_embedded", "web_music",
    "web_safari", "mediaconnect",
}


def _safe_youtube_clients() -> list[str]:
    """Filter env config to valid yt-dlp YouTube player_client values only."""
    candidates = [
        YTDLP_CLIENT, "android", "ios", "web", "mweb", "tv_embedded",
        "web_creator", "web_safari", "web_music", "mediaconnect",
    ]
    out: list[str] = []
    for raw in candidates:
        item = str(raw or "").strip()
        if item in YOUTUBE_PLAYER_CLIENTS and item not in out:
            out.append(item)
    return out or ["android", "ios", "web"]


def _youtube_client_profiles() -> list[tuple[str, list[str]]]:
    """World-class YouTube fallback matrix: mobile, web, TV, music, embedded.

    yt-dlp frequently succeeds or fails based on the player surface, not the URL.
    Trying profile groups gives the extractor more player JSON options per pass,
    while still keeping attempts bounded. The music/embedded surfaces are
    essential for audio downloads and videos embedded off-site, where plain web
    or android clients are often throttled or blocked.
    """
    preferred = _safe_youtube_clients()
    profiles: list[tuple[str, list[str]]] = [
        ("env-preferred", preferred[:4]),
        ("android-ios", ["android", "ios"]),
        ("web-mweb", ["web", "mweb"]),
        ("tv-embedded", ["tv_embedded", "web"]),
        ("creator-web", ["web_creator", "web"]),
        ("safari-web", ["web_safari", "web"]),
        ("music", ["web_music", "android_music", "ios_music", "web"]),
        ("embedded", ["web_embedded", "tv_embedded", "web"]),
        ("mediaconnect", ["mediaconnect", "web"]),
        ("full-matrix", ["android", "ios", "web", "mweb", "tv_embedded", "web_creator", "web_music"]),
    ]
    seen: set[tuple[str, ...]] = set()
    out: list[tuple[str, list[str]]] = []
    for name, clients in profiles:
        filtered = [c for c in clients if c in YOUTUBE_PLAYER_CLIENTS]
        key = tuple(filtered)
        if filtered and key not in seen:
            seen.add(key)
            out.append((name, filtered))
    return out


def _base_opts(progress=None, loop: asyncio.AbstractEventLoop | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "outtmpl": os.path.join(TEMP_DIR, "%(title).80s-%(id)s.%(ext)s"),
        "retries": 5,
        "extractor_retries": 5,
        "file_access_retries": 5,
        "fragment_retries": 8,
        "socket_timeout": DOWNLOAD_TIMEOUT,
        "continuedl": True,
        "overwrites": True,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 10 * 1024 * 1024,
        "windowsfilenames": True,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        },
        "extractor_args": {
            "youtube": {"player_client": _safe_youtube_clients()},
            "tiktok": {"api_hostname": [
                "api16-normal-c-useast1a.tiktokv.com",
                "api22-normal-c-useast1a.tiktokv.com",
                "api19-normal-c-useast1a.tiktokv.com",
                "api16-va.tiktokv.com",
            ]},
        },
    }
    if DOWNLOAD_PROXY:
        opts["proxy"] = DOWNLOAD_PROXY
    if YTDLP_COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (YTDLP_COOKIES_FROM_BROWSER,)
    elif YTDLP_COOKIES_FILE and os.path.exists(YTDLP_COOKIES_FILE):
        opts["cookiefile"] = YTDLP_COOKIES_FILE
    yt_po_token = (os.getenv("YOUTUBE_PO_TOKEN") or os.getenv("YT_PO_TOKEN") or "").strip()
    yt_visitor_data = (os.getenv("YOUTUBE_VISITOR_DATA") or os.getenv("YT_VISITOR_DATA") or "").strip()
    if yt_po_token:
        opts.setdefault("extractor_args", {}).setdefault("youtube", {})["po_token"] = [yt_po_token]
    if yt_visitor_data:
        opts.setdefault("extractor_args", {}).setdefault("youtube", {})["visitor_data"] = [yt_visitor_data]
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

    # Use the system ffmpeg when present; otherwise point yt-dlp at the
    # imageio-ffmpeg bundled binary so video merging and MP3 extraction keep
    # working on hosts that don't install a system ffmpeg.
    ffmpeg_bin = _ffmpeg_path()
    if ffmpeg_bin:
        opts["ffmpeg_location"] = ffmpeg_bin
    return opts


def _run_sync(fn, *args):
    return asyncio.to_thread(fn, *args)


def _extract_info_sync(url: str) -> dict | None:
    """Extract metadata with layered fallbacks so odd URLs don't break analysis.

    Attempts, in order:
      1. full metadata extraction (normal case)
      2. in_playlist flat extraction (playlists / multi-item pages)
      3. plain flat extraction (aggressive fallback)

    yt-dlp already retries network I/O internally; this adds a second layer for
    extractor-level quirks that only surface with different extraction modes.
    """
    variants = (
        {"extract_flat": False},
        {"extract_flat": "in_playlist"},
        {"extract_flat": True},
    )
    last_error: Exception | None = None
    for variant in variants:
        try:
            with yt_dlp.YoutubeDL({**_base_opts(), "skip_download": True, **variant}) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    return info
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Extract pass failed fallback mode=%s url=%s error=%s",
                variant.get("extract_flat"), url[:120], exc,
            )
            continue
    if last_error:
        logger.warning("All extract passes failed for %s: %s", url[:120], last_error)
    return None


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


def _safe_remove(path: str | None) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.debug("Temp cleanup failed for %s: %s", path, exc)


def _check_size(path: str) -> str:
    if os.path.getsize(path) > MAX_FILE_SIZE_BYTES:
        _safe_remove(path)
        raise FileTooLargeError()
    return path


def _download_direct_sync(
    url: str,
    headers: dict | None = None,
    timeout: int = 60,
    progress=None,
    loop: asyncio.AbstractEventLoop | None = None,
    prefix: str = "xdl-direct",
    retries: int = 2,
) -> str | None:
    """Stream a direct CDN video with bounded size, transient retries, and cleanup."""
    import requests

    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    fd, out_path = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".mp4", dir=TEMP_DIR)
    os.close(fd)
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
        ),
        "Accept": "video/*,application/octet-stream,*/*;q=0.8",
    }
    request_headers.update(headers or {})
    attempts = max(1, int(retries) + 1)

    for attempt in range(attempts):
        _safe_remove(out_path)
        try:
            with requests.get(
                url, headers=request_headers, stream=True, timeout=timeout
            ) as response:
                status = response.status_code
                if status not in (200, 206):
                    if status in (408, 425, 429) or status >= 500:
                        if attempt + 1 < attempts:
                            time.sleep(0.5 * (2 ** attempt))
                            continue
                    _safe_remove(out_path)
                    return None

                content_type = (response.headers.get("Content-Type") or "").lower()
                if content_type and not (
                    content_type.startswith("video/")
                    or content_type in {
                        "application/octet-stream",
                        "application/mp4",
                        "binary/octet-stream",
                    }
                ):
                    _safe_remove(out_path)
                    return None

                total = 0
                try:
                    total = max(0, int(response.headers.get("Content-Length") or 0))
                except (TypeError, ValueError):
                    pass
                if total > MAX_FILE_SIZE_BYTES:
                    raise FileTooLargeError(
                        size_bytes=total, limit_bytes=MAX_FILE_SIZE_BYTES
                    )

                downloaded = 0
                with open(out_path, "wb") as output:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        if downloaded + len(chunk) > MAX_FILE_SIZE_BYTES:
                            raise FileTooLargeError(
                                size_bytes=downloaded + len(chunk),
                                limit_bytes=MAX_FILE_SIZE_BYTES,
                            )
                        if downloaded == 0:
                            sample = chunk.lstrip().lower()
                            if sample.startswith((b"<!doctype html", b"<html", b"{", b"[")):
                                _safe_remove(out_path)
                                return None
                        output.write(chunk)
                        downloaded += len(chunk)
                        if progress and loop:
                            try:
                                asyncio.run_coroutine_threadsafe(progress({
                                    "pct": int(downloaded * 100 / total) if total else 0,
                                    "downloaded": downloaded,
                                    "total": total,
                                    "speed": 0,
                                    "eta": 0,
                                }), loop)
                            except RuntimeError as exc:
                                logger.debug("Direct-download progress skipped: %s", exc)

                if downloaded > 2048:
                    return out_path
                _safe_remove(out_path)
                return None
        except FileTooLargeError:
            _safe_remove(out_path)
            raise
        except requests.RequestException as exc:
            _safe_remove(out_path)
            if attempt + 1 < attempts:
                time.sleep(0.5 * (2 ** attempt))
                continue
            logger.warning("Direct CDN download failed after retries: %s", exc)
            return None
        except OSError:
            _safe_remove(out_path)
            raise

    _safe_remove(out_path)
    return None


def _ffmpeg_path() -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        logger.debug("imageio-ffmpeg unavailable: %s", exc)
        return None


def _ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def _has_audio_stream(path: str) -> bool | None:
    """Return whether a local media file contains an audio stream.

    Uses ffprobe when installed; otherwise falls back to ffmpeg stderr parsing.
    The project now depends on imageio-ffmpeg, so even hosts without system
    ffmpeg can still perform a real audio check.
    """
    if not path or not os.path.exists(path):
        return None
    ffprobe = _ffprobe_path()
    if ffprobe:
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v", "error",
                    "-select_streams", "a",
                    "-show_entries", "stream=index",
                    "-of", "json",
                    path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
                check=False,
            )
            if proc.returncode == 0:
                payload = json.loads(proc.stdout or "{}")
                return bool(payload.get("streams"))
            logger.debug("ffprobe audio check failed for %s: %s", path, proc.stderr[:200])
        except Exception as exc:
            logger.debug("ffprobe audio check skipped for %s: %s", path, exc)

    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", path, "-f", "null", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=25,
            check=False,
        )
        output = f"{proc.stdout}\n{proc.stderr}".lower()
        if "audio:" in output or "stream #" in output and ": audio" in output:
            return True
        if "video:" in output or "stream #" in output:
            return False
        return None
    except Exception as exc:
        logger.debug("ffmpeg audio check skipped for %s: %s", path, exc)
        return None


def _require_audio_or_none(path: str | None, *, source: str = "direct") -> str | None:
    """Reject verified silent/video-only files so callers can fall back to yt-dlp merge."""
    if not path:
        return None
    has_audio = _has_audio_stream(path)
    if has_audio is False:
        logger.warning("Rejected %s download without audio stream: %s", source, path)
        _safe_remove(path)
        return None
    if has_audio is None and _ffmpeg_path():
        logger.warning("Rejected %s download because audio stream could not be verified: %s", source, path)
        _safe_remove(path)
        return None
    return path


def _remux_mp4_faststart(path: str) -> str:
    """Copy-remux MP4 for Telegram streaming compatibility without re-encoding.

    Keeps the operation cheap (`-c copy`) while moving metadata to the front of
    the file. If ffmpeg is unavailable or remux fails, the original verified file
    is kept.
    """
    ffmpeg = _ffmpeg_path()
    if not ffmpeg or not path or not os.path.exists(path):
        return path
    ext = os.path.splitext(path)[1].lower()
    if ext not in {".mp4", ".m4v", ".mov"}:
        return path
    fd, out_path = tempfile.mkstemp(prefix="xdl-faststart-", suffix=".mp4", dir=TEMP_DIR)
    os.close(fd)
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i", path,
                "-map", "0:v:0?",
                "-map", "0:a:0?",
                "-c", "copy",
                "-movflags", "+faststart",
                out_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=90,
            check=False,
        )
        if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) < 2048:
            logger.debug("ffmpeg faststart remux skipped for %s: %s", path, proc.stderr[-300:])
            _safe_remove(out_path)
            return path
        # Never replace a verified audio file with a silent remux.
        if _has_audio_stream(out_path) is False:
            logger.warning("Faststart remux produced no audio; keeping original: %s", path)
            _safe_remove(out_path)
            return path
        _check_size(out_path)
        _safe_remove(path)
        return out_path
    except Exception as exc:
        logger.debug("ffmpeg faststart remux failed for %s: %s", path, exc)
        _safe_remove(out_path)
        return path


def _prepare_video_for_delivery(path: str | None, *, source: str = "video", allow_large: bool = False) -> str | None:
    """Final guard before Telegram upload/cache: verify audio and optimize MP4.

    When ``allow_large`` is set we skip the faststart remux (which enforces the
    size limit) and keep the oversized file intact so the caller can split it
    into Telegram-friendly parts afterwards.
    """
    path = _require_audio_or_none(path, source=source)
    if not path:
        return None
    if allow_large:
        return path
    return _remux_mp4_faststart(path)


def _download_sync(url: str, opts: dict) -> str | None:
    return _download_sync_impl(url, opts, check_size=True)


def _download_sync_no_limit(url: str, opts: dict) -> str | None:
    """Same as _download_sync but keeps files that exceed the size limit.

    Large videos are later split into Telegram-friendly parts instead of being
    deleted and reported as failures.
    """
    return _download_sync_impl(url, opts, check_size=False)


def _download_sync_impl(url: str, opts: dict, check_size: bool) -> str | None:
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
                return candidate if not check_size else _check_size(candidate)
    return None


def _video_format_candidates(format_id: str = "best") -> list[str]:
    """Return yt-dlp format fallbacks that strongly prefer video+audio merged output."""
    requested = str(format_id or "best").strip()
    merged_best = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[acodec!=none][vcodec!=none]/best"
    merged_720 = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720][acodec!=none][vcodec!=none]/best[height<=720]"
    safe_small = "best[height<=720][acodec!=none][vcodec!=none]/best[acodec!=none][vcodec!=none]/worst[acodec!=none][vcodec!=none]/worst"
    if requested in {"best", "best_quality"}:
        return [merged_best, merged_720, safe_small]
    return [
        f"{requested}+bestaudio[ext=m4a]/{requested}+bestaudio/bestvideo[format_id={requested}]+bestaudio",
        merged_720,
        merged_best,
        safe_small,
    ]


def _youtube_client_candidates() -> list[str]:
    return _safe_youtube_clients()


async def _generic_download_video(url: str, format_id: str = "best", quality_label: str = "best", progress=None, play_url: str | None = None, allow_large: bool = False) -> str | None:
    loop = asyncio.get_running_loop() if progress else None
    host = urlparse(url).netloc.lower()
    formats = _video_format_candidates(format_id)
    targets: list[tuple[str, str]] = []
    if play_url and play_url != url:
        # Direct CDN URLs are often video-only. Try them first for speed, but if
        # audio verification fails, fall back to the canonical page URL so yt-dlp
        # can locate and merge bestaudio.
        targets.append((play_url, "direct-play-url"))
    targets.append((url, "page-url"))
    last_error: Exception | None = None

    for target, target_kind in targets:
        if "youtu" in host and target_kind == "page-url":
            client_profiles = _youtube_client_profiles()
        else:
            client_profiles = [("default", [YTDLP_CLIENT] if YTDLP_CLIENT else [])]
        for profile_name, clients in client_profiles:
            for fmt in formats:
                opts = {**_base_opts(progress, loop), "format": fmt, "merge_output_format": "mp4", "noplaylist": True}
                if clients:
                    opts.setdefault("extractor_args", {}).setdefault("youtube", {})["player_client"] = clients
                try:
                    sync_fn = _download_sync_no_limit if allow_large else _download_sync
                    path = await _run_sync(sync_fn, target, opts)
                    path = _prepare_video_for_delivery(path, source=f"yt-dlp:{target_kind}", allow_large=allow_large)
                    if path:
                        if target_kind != "page-url" or fmt != formats[0] or profile_name != client_profiles[0][0]:
                            logger.info(
                                "Download fallback succeeded: target=%s profile=%s clients=%s format=%s url=%s",
                                target_kind, profile_name, clients, fmt, url[:100],
                            )
                        return path
                    if target_kind == "direct-play-url":
                        logger.info("Direct play_url produced no verified audio; trying page URL for merge: %s", url[:100])
                        break
                except FileTooLargeError:
                    raise
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Download attempt failed target=%s profile=%s clients=%s format=%s url=%s error=%s",
                        target_kind, profile_name, clients, fmt, url[:100], exc,
                    )
                    continue

    if last_error:
        raise last_error
    raise RuntimeError(
        "No downloadable video stream returned after all formats and fallbacks were exhausted"
    )


def _valid_audio_bitrate(value: str | None) -> str:
    raw = str(value or "").strip()
    return raw if raw.isdigit() and 32 <= int(raw) <= 320 else "192"


def _audio_format_candidates() -> list[str]:
    """Layered audio format fallbacks — m4a-first, then bitrate-tight, then any audio."""
    return [
        "bestaudio[ext=m4a]/bestaudio[acodec!=none]/bestaudio/best[acodec!=none]/best",
        "bestaudio[abr<=256]/bestaudio[abr<=192]/bestaudio/best[acodec!=none]/best",
        "best[acodec!=none]/bestaudio/best",
    ]


async def _generic_download_audio(url: str, progress=None, bitrate: str | None = None) -> str | None:
    """Bullet-proof audio/music download with client-profile + format fallbacks.

    YouTube is the only engine that is player-client sensitive; for every other
    platform a single default profile suffices and we simply walk the format
    candidates. Every attempt runs inside yt-dlp's own network retries, so this
    adds a second layer instead of replacing a failed one-off call.
    """
    loop = asyncio.get_running_loop() if progress else None
    preferred_quality = _valid_audio_bitrate(bitrate or AUDIO_BITRATE)
    host = urlparse(url).netloc.lower()
    formats = _audio_format_candidates()

    if "youtu" in host:
        client_profiles = _youtube_client_profiles()
    else:
        client_profiles = [("default", [YTDLP_CLIENT] if YTDLP_CLIENT else [])]

    last_error: Exception | None = None
    for profile_name, clients in client_profiles:
        for fmt in formats:
            opts = {
                **_base_opts(progress, loop),
                "format": fmt,
                "noplaylist": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": preferred_quality,
                    }
                ],
            }
            if clients and "youtu" in host:
                opts.setdefault("extractor_args", {}).setdefault("youtube", {})["player_client"] = clients
            try:
                path = await _run_sync(_download_sync, url, opts)
                if path:
                    if profile_name != client_profiles[0][0] or fmt != formats[0]:
                        logger.info(
                            "Audio fallback succeeded: profile=%s clients=%s format=%s url=%s",
                            profile_name, clients, fmt, url[:100],
                        )
                    return path
            except FileTooLargeError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Audio attempt failed profile=%s clients=%s format=%s url=%s error=%s",
                    profile_name, clients, fmt, url[:100], exc,
                )
                continue

    if last_error:
        raise last_error
    raise RuntimeError("No downloadable audio stream returned after all formats and fallbacks were exhausted")


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
    from services.engines.platforms import UniversalEngine

    return first_match(url, registered_engines(), UniversalEngine())


def _finalize_analysis(info: dict | None) -> dict | None:
    """Sanitize qualities for every platform before the result reaches the UI.

    Only standard, real resolutions (144p–2160p) survive; scraper-provided
    labels like ``Best`` / ``HD`` are dropped so the keyboard never advertises
    a quality that was not actually probed on the media. The intelligence card
    is kept in sync with the sanitized list.
    """
    if not info:
        return None
    info["qualities"] = normalize_qualities(info.get("qualities") or [])

    intel = info.get("intelligence")
    if isinstance(intel, dict):
        qs = info["qualities"]
        best = qs[-1]["label"] if qs else "best"
        intel["best_quality"] = best
        details = []
        for detail in intel.get("details") or []:
            if isinstance(detail, str) and detail.startswith("Best quality:"):
                details.append(f"Best quality: {best}")
            else:
                details.append(detail)
        intel["details"] = details
    return info


async def analyze_url(url: str) -> dict | None:
    engine = _select_engine(url)
    try:
        info = await engine.analyze(url)
    except Exception as exc:
        logger.warning("Engine analyze failed (%s) for %s: %s", getattr(engine, "name", "unknown"), url[:120], exc)
        info = await _generic_analyze_url(url)
    return _finalize_analysis(info)


async def download_video(url: str, format_id: str = "best", quality_label: str = "best", progress=None, play_url: str | None = None, allow_large: bool = False) -> str | None:
    if allow_large:
        # Keep large files intact so the caller can split them into
        # Telegram-friendly parts instead of deleting them mid-download.
        return await _generic_download_video(url, format_id, quality_label, progress, play_url, allow_large=True)
    engine = _select_engine(url)
    try:
        path = await engine.download_video(url, format_id, quality_label, progress, play_url)
        path = _prepare_video_for_delivery(path, source=f"engine:{getattr(engine, 'name', 'unknown')}")
        if path:
            return path
        logger.warning("Engine returned no verified-audio video (%s); forcing generic merge fallback for %s", getattr(engine, "name", "unknown"), url[:120])
        return await _generic_download_video(url, format_id, quality_label, progress, None)
    except FileTooLargeError:
        raise
    except Exception as exc:
        logger.warning("Engine video download failed (%s) for %s: %s", getattr(engine, "name", "unknown"), url[:120], exc)
        return await _generic_download_video(url, format_id, quality_label, progress, None)


async def download_audio(url: str, progress=None, bitrate: str | None = None) -> str | None:
    requested = _valid_audio_bitrate(bitrate or AUDIO_BITRATE)
    # Explicit high-bitrate requests (e.g. 320) use the reliable yt-dlp/ffmpeg
    # path directly, skipping platform micro-optimisations that default to 192.
    if requested == AUDIO_BITRATE_HIGH or requested != AUDIO_BITRATE:
        return await _generic_download_audio(url, progress, requested)

    engine = _select_engine(url)
    try:
        return await engine.download_audio(url, progress)
    except FileTooLargeError:
        raise
    except Exception as exc:
        logger.warning("Engine audio download failed (%s) for %s: %s", getattr(engine, "name", "unknown"), url[:120], exc)
        return await _generic_download_audio(url, progress, requested)


# Compatibility aliases used by platform engines.
_enforce_max_file_size = _check_size


async def download_image(url: str, image_url: str | None = None) -> str | None:
    import requests
    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    target = image_url or url
    fd, path = tempfile.mkstemp(prefix="xdl-img-", suffix=".jpg", dir=TEMP_DIR)
    os.close(fd)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    r = await asyncio.to_thread(requests.get, target, timeout=45, headers=headers)
    r.raise_for_status()
    content_type = (r.headers.get("Content-Type") or "").lower()
    if content_type and "text/html" in content_type:
        _safe_remove(path)
        raise RuntimeError("image endpoint returned HTML")
    data = r.content
    head = data[:256].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html", b"<script")):
        _safe_remove(path)
        raise RuntimeError("image endpoint returned a web page")
    with open(path, "wb") as fh:
        fh.write(data)
    if os.path.getsize(path) < 64:
        _safe_remove(path)
        raise RuntimeError("image too small")
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
