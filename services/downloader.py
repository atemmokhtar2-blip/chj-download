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
from middlewares.cancellation import DownloadCancelledError
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


class EmptyFileError(Exception):
    """Raised when a media download produced zero usable bytes.

    yt-dlp's ``FragmentFD`` surfaces this as "The downloaded file is empty"
    when every HLS/DASH fragment was rejected or served empty (expired CDN
    token, geo/agent block, region restriction). No format selector can repair
    the SAME target URL — the downloader uses this type to skip the dead
    target cleanly instead of re-trying every selector on it.
    """


class DownloadImpossibleError(RuntimeError):
    """Raised once EVERY download strategy for a URL has been exhausted.

    Carries an ``empty_file`` flag so callers can distinguish "the host CDN
    rejected every segment (expired token, geo-block, rate-limit)" from "no
    downloadable stream exists". This lets the outer wrappers avoid re-running
    an already-exhausted generic fallback and lets the audio path skip a
    wasteful video->audio retry that would hit the SAME dead CDN.
    """

    def __init__(self, message: str, *, empty_file: bool = False):
        super().__init__(message)
        self.empty_file = empty_file


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
            # User pressed 🛑 — raise inside the yt-dlp thread so the download
            # aborts immediately instead of finishing the whole fallback matrix.
            cancel_check = getattr(progress, "cancel_check", None)
            if cancel_check is not None:
                cancel_check()
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
    from utils.url_safety import is_public_url

    safe, _guard_reason = is_public_url(url)
    if not safe:
        logger.warning("Blocked direct CDN download (SSRF guard) for %s", str(url)[:120])
        return None

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
                cancel_check = getattr(progress, "cancel_check", None)
                with open(out_path, "wb") as output:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        if cancel_check is not None:
                            cancel_check()
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
        except DownloadCancelledError:
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


def download_hls_mp4(play_url: str, prefix: str = "xdl-hls", headers: dict | None = None, timeout: int = 420) -> str | None:
    """Download an HLS (.m3u8) stream into a faststart MP4 via ffmpeg.

    Generic path for platforms whose direct player asset is an HLS playlist.
    Tries a stream-copy remux first (fast and lossless), then falls back to a
    full H.264/AAC re-encode when the playlist carries codecs that copy cannot
    package. The central audio guard (_prepare_video_for_delivery) still
    verifies the result before it can ever be delivered.
    """
    if not play_url or not str(play_url).startswith("http"):
        return None
    from utils.url_safety import is_public_url

    safe, _reason = is_public_url(play_url)
    if not safe:
        logger.warning("Blocked HLS download (SSRF guard) for %s", str(play_url)[:120])
        return None

    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return None

    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    fd, out_path = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".mp4", dir=TEMP_DIR)
    os.close(fd)
    _safe_remove(out_path)

    header_args: list[str] = []
    if headers:
        header_args = ["-headers", "\r\n".join(f"{k}: {v}" for k, v in headers.items() if k and v)]

    passes = (
        [
            "-c:v", "copy",
            "-c:a", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
        ],
        [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
        ],
    )

    for pass_args in passes:
        _safe_remove(out_path)
        try:
            proc = subprocess.run(
                [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    *header_args,
                    "-i", play_url,
                    *pass_args,
                    out_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) >= 2048:
                return out_path
            logger.debug("HLS download pass failed for %s: %s", str(play_url)[:120], proc.stderr[-300:])
        except Exception as exc:
            logger.debug("HLS download pass crashed for %s: %s", str(play_url)[:120], exc)

    _safe_remove(out_path)
    return None


def merge_hls_video_audio(
    video_url: str,
    audio_url: str,
    prefix: str = "xdl-merge",
    headers: dict | None = None,
    timeout: int = 420,
) -> str | None:
    """Merge a video-only HLS rendition with a separate audio HLS rendition.

    Pinterest (and a few other split-DASH/HLS platforms) exposes video and
    audio as SEPARATE ``.m3u8`` playlists. A single-input ``-c copy`` remux of
    the video playlist can therefore produce a silent file. This runs ffmpeg
    with BOTH inputs (video + audio) and falls back to a full H.264/AAC
    re-encode when stream-copy cannot package the codecs.
    """
    if not video_url or not audio_url:
        return None
    from utils.url_safety import is_public_url

    for u in (video_url, audio_url):
        safe, _reason = is_public_url(u)
        if not safe:
            logger.warning("Blocked HLS merge (SSRF guard) for %s", str(u)[:120])
            return None

    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        logger.warning("HLS merge skipped: no ffmpeg available")
        return None

    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    fd, out_path = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".mp4", dir=TEMP_DIR)
    os.close(fd)
    _safe_remove(out_path)

    header_args: list[str] = []
    if headers:
        header_args = ["-headers", "\r\n".join(
            f"{k}: {v}" for k, v in headers.items() if k and v)]

    def _inputs() -> list[str]:
        args: list[str] = []
        if header_args:
            args.extend(header_args)
        args.extend(["-i", video_url])
        if header_args:
            args.extend(header_args)
        args.extend(["-i", audio_url])
        return args

    passes = (
        [
            "-map", "0:v:0?",
            "-map", "1:a:0?",
            "-c:v", "copy",
            "-c:a", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
        ],
        [
            "-map", "0:v:0?",
            "-map", "1:a:0?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
        ],
    )

    for pass_args in passes:
        _safe_remove(out_path)
        try:
            proc = subprocess.run(
                [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    *_inputs(),
                    *pass_args,
                    out_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) >= 2048:
                return out_path
            logger.debug(
                "HLS merge pass failed for %s + %s: %s",
                str(video_url)[:120], str(audio_url)[:120], (proc.stderr or "")[-300:],
            )
        except Exception as exc:
            logger.debug(
                "HLS merge pass crashed for %s + %s: %s",
                str(video_url)[:100], str(audio_url)[:100], exc,
            )

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
    """Reject silent or unverifiable video files so callers fall back to yt-dlp merge.

    Fail-closed: if we cannot prove the file has an audio stream we drop it.
    Delivering a silent video is always worse than retrying through a path that
    can merge audio (yt-dlp progressive or bestvideo+bestaudio).
    """
    if not path:
        return None
    has_audio = _has_audio_stream(path)
    if has_audio is False:
        logger.warning("Rejected %s download without audio stream: %s", source, path)
        _safe_remove(path)
        return None
    if has_audio is None:
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


def extract_audio_mp3(path: str, bitrate: str = "192") -> str | None:
    """Extract the audio track of a local video to MP3 via ffmpeg.

    Used by platforms (e.g. Instagram) whose direct yt-dlp audio resolution is
    weak: instead grab the verified video through the robust video path, then
    transcode its audio track locally. Returns None on any failure.
    """
    ffmpeg = _ffmpeg_path()
    if not ffmpeg or not path or not os.path.exists(path):
        return None
    rate = _valid_audio_bitrate(bitrate)
    fd, out_path = tempfile.mkstemp(prefix="xdl-audio-", suffix=".mp3", dir=TEMP_DIR)
    os.close(fd)
    _safe_remove(out_path)
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-y",
                "-i", path,
                "-vn",
                "-acodec", "libmp3lame",
                "-b:a", f"{rate}k",
                "-f", "mp3",
                out_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            check=False,
        )
        if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) < 2048:
            logger.debug("ffmpeg audio extract failed for %s: %s", path, proc.stderr[-300:])
            _safe_remove(out_path)
            return None
        return out_path
    except Exception as exc:
        logger.debug("ffmpeg audio extract failed for %s: %s", path, exc)
        _safe_remove(out_path)
        return None


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
    try:
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
                    if os.path.getsize(candidate) == 0:
                        _safe_remove(candidate)
                        raise EmptyFileError(
                            "yt-dlp produced a zero-byte file (CDN rejected fragments)"
                        )
                    return candidate if not check_size else _check_size(candidate)
        return None
    except EmptyFileError:
        raise
    except Exception as exc:
        if _is_empty_file_error(exc):
            raise EmptyFileError(str(exc)) from exc
        raise


def _is_empty_file_error(exc: BaseException | None) -> bool:
    """True when yt-dlp's fragmented downloader wrote zero bytes.

    yt-dlp's FragmentFD reports "The downloaded file is empty" when every
    HLS/DASH fragment either failed silently or was served as an empty body
    (expired CDN token, geo/agent block, region restriction). No format
    selector can fix that for the SAME target URL — the right move is to
    advance to the next target/candidate instead of burning retries.
    """
    if isinstance(exc, EmptyFileError):
        return True
    text = str(exc or "").lower()
    return "file is empty" in text or "empty file" in text


def is_cdn_rejection(exc: BaseException | None) -> bool:
    """Public counterpart of :func:`_is_empty_file_error` for the UI layer.

    Returns True when an error means the host CDN rejected the media fragments —
    either through the typed ``DownloadImpossibleError(empty_file=True)`` marker
    or through the raw yt-dlp string ("The downloaded file is empty"). The
    Telegram handler uses this so a cryptic technical reason never leaks to a
    user, no matter which layer surfaced the failure.
    """
    if isinstance(exc, DownloadImpossibleError) and bool(getattr(exc, "empty_file", False)):
        return True
    return _is_empty_file_error(exc)


def _video_format_candidates(format_id: str = "best") -> list[str]:
    """Return yt-dlp format fallbacks that strongly prefer video+audio merged output.

    The FINAL fallback in every chain must never raise "Requested format is
    not available": split-only platforms (e.g. Pinterest HLS, where video and
    audio live in SEPARATE renditions with no combined format) would hit that
    error on ``best`` / ``worst`` / ``best[acodec!=none][vcodec!=none]`` and
    the real root cause (usually a missing ffmpeg merge) would be masked.
    The universal escape hatch is ``bestvideo*+bestaudio*/best*`` — any
    rendition matches, and the central audio guard rejects silent files
    afterwards instead of failing with a misleading yt-dlp format error.
    """
    requested = str(format_id or "best").strip()
    # Quality-first selectors: prefer H.264 (avc) video inside MP4 + m4a audio.
    # H.264/AAC is what Telegram hardware-accelerates, so the "best" pick is not
    # just the highest resolution but the highest resolution that plays smoothly
    # and merges cleanly. VP9/AV1 rows are still reachable through the later
    # fallbacks when a platform offers nothing else.
    merged_best = (
        "bestvideo[ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]"
        "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo+bestaudio"
        "/best[acodec!=none][vcodec!=none]"
        "/bestvideo*+bestaudio*/best*"
    )
    merged_720 = (
        "bestvideo[height<=720][ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]"
        "/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height<=720]+bestaudio"
        "/best[height<=720][acodec!=none][vcodec!=none]"
        "/best[height<=720]"
        "/bestvideo*+bestaudio*/best*"
    )
    safe_small = (
        "best[height<=720][acodec!=none][vcodec!=none]"
        "/best[acodec!=none][vcodec!=none]"
        "/bestvideo*+bestaudio*/best*"
    )
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
    cancel_check = getattr(progress, "cancel_check", None)
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
        # A single dead target (the CDN rejected every fragment) must not burn
        # the remaining format/profile candidates on the SAME URL — mark it dead
        # and advance to the next target. This also covers YouTube's multi-profile
        # matrix, where a bare `break` would only leave the format loop and then
        # re-run every selector on the next profile.
        target_dead = False
        for profile_name, clients in client_profiles:
            if target_dead:
                break
            for fmt in formats:
                if target_dead:
                    break
                if cancel_check is not None:
                    cancel_check()
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
                        target_dead = True
                        break
                except FileTooLargeError:
                    raise
                except DownloadCancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Download attempt failed target=%s profile=%s clients=%s format=%s url=%s error=%s",
                        target_kind, profile_name, clients, fmt, url[:100], exc,
                    )
                    if _is_empty_file_error(exc):
                        logger.info(
                            "Empty-file/CDN rejection on target=%s; skipping to next target for %s",
                            target_kind, url[:100],
                        )
                        target_dead = True
                        break
                    continue

    if last_error:
        if _is_empty_file_error(last_error):
            raise DownloadImpossibleError(
                "The media stream returned zero data — the host CDN rejected or "
                "expired the segment URLs. Try again in a moment, pick a lower "
                "quality, or send a different link.",
                empty_file=True,
            )
        raise last_error
    raise DownloadImpossibleError(
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
    cancel_check = getattr(progress, "cancel_check", None)
    preferred_quality = _valid_audio_bitrate(bitrate or AUDIO_BITRATE)
    host = urlparse(url).netloc.lower()
    formats = _audio_format_candidates()

    if "youtu" in host:
        client_profiles = _youtube_client_profiles()
    else:
        client_profiles = [("default", [YTDLP_CLIENT] if YTDLP_CLIENT else [])]

    last_error: Exception | None = None
    # Like the video path: when the host CDN rejects every fragment (empty
    # file), no audio format selector can repair the SAME URL — stop walking
    # profiles/selectors and surface a clear message instead.
    stream_rejected = False
    for profile_name, clients in client_profiles:
        if stream_rejected:
            break
        for fmt in formats:
            if stream_rejected:
                break
            if cancel_check is not None:
                cancel_check()
            opts = {
                **_base_opts(progress, loop),
                "format": fmt,
                "noplaylist": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": preferred_quality,
                    },
                    # Embed title/artist metadata so delivered MP3s show proper
                    # tags in music players instead of "Unknown - Unknown".
                    {"key": "FFmpegMetadata"},
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
            except DownloadCancelledError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Audio attempt failed profile=%s clients=%s format=%s url=%s error=%s",
                    profile_name, clients, fmt, url[:100], exc,
                )
                if _is_empty_file_error(exc):
                    stream_rejected = True
                    break
                continue

    if last_error:
        if _is_empty_file_error(last_error):
            raise DownloadImpossibleError(
                "The media stream returned zero data — the host CDN rejected or "
                "expired the segment URLs. Try again in a moment or send a different link.",
                empty_file=True,
            )
        raise last_error
    raise DownloadImpossibleError(
        "No downloadable audio stream returned after all formats and fallbacks were exhausted"
    )


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


def _format_height_candidates(formats) -> list:
    """Extract real video quality candidates from raw yt-dlp format rows.

    Only rows that actually carry a video stream (``vcodec`` not ``none``) and a
    numeric height are considered. Audio-only rows and placeholder rows are
    excluded so the keyboard can never advertise a quality that does not exist.
    """
    out: list[dict] = []
    for f in formats or []:
        if not isinstance(f, dict):
            continue
        if f.get("vcodec") in (None, "none"):
            continue
        try:
            height = int(f.get("height") or 0)
        except (TypeError, ValueError):
            height = 0
        if height <= 0:
            continue
        out.append({
            "label": f"{height}p",
            "format_id": str(f.get("format_id") or "best"),
            "has_audio": f.get("acodec") not in (None, "none"),
        })
    return out


def _heights_from_urls(*urls) -> list[int]:
    """Parse concrete video heights embedded in CDN filenames (e.g. Reddit's DASH_720)."""
    heights: list[int] = []
    for value in urls or []:
        url = str(value or "")
        if not url.startswith("http"):
            continue
        m = re.search(r"DASH_(\d{3,4})", url)
        if m:
            heights.append(int(m.group(1)))
            continue
        m = re.search(r"(\d{3,4})x(\d{3,4})", url)
        if m:
            heights.append(min(int(m.group(1)), int(m.group(2))))
    return heights


def _collect_quality_candidates(info: dict) -> list:
    """Gather every real, probed quality hint from an analyzed media result.

    Sources, in order of confidence:
      1. Existing ``qualities`` entries (YouTube already builds safe selectors,
         and generic yt-dlp analysis already probed every DASH/HLS rendition).
      2. Raw ``formats`` rows that carry a true video stream + height.
      3. Single-file direct height hints (``direct_height`` on Facebook/others).
      4. Concrete heights embedded in CDN URLs (``DASH_720``, ``1920x1080``).

    Fake labels (``Best`` / ``HD``) are left for ``normalize_qualities`` to drop;
    this function's job is only to widen the real signal, never to invent one.
    """
    candidates: list[dict] = [
        q for q in (info.get("qualities") or []) if isinstance(q, dict)
    ]
    candidates.extend(_format_height_candidates(info.get("formats")))

    try:
        direct_height = int(info.get("direct_height") or 0)
    except (TypeError, ValueError):
        direct_height = 0
    if direct_height > 0:
        candidates.append({"label": f"{direct_height}p", "format_id": "best"})

    for key in ("play_url", "hd_url", "sd_url"):
        for height in _heights_from_urls(info.get(key)):
            candidates.append({"height": height, "format_id": "best"})

    return candidates


def _finalize_analysis(info: dict | None) -> dict | None:
    """Sanitize and probe qualities for every platform before the UI renders.

    Every analysis result — no matter which engine produced it — passes through
    one quality gate: only standard, real resolutions (144p–2160p) that were
    actually seen in formats, scraper height fields, or CDN URLs survive.
    ``Best``/``HD`` labels are dropped, so the inline keyboard only advertises
    qualities that genuinely exist on the media. The intelligence card is kept
    in sync with the final list.
    """
    if not info:
        return None
    info["qualities"] = normalize_qualities(_collect_quality_candidates(info))

    intel = info.get("intelligence")
    if isinstance(intel, dict):
        qs = info["qualities"]
        best = qs[-1]["label"] if qs else "best"
        intel["best_quality"] = best
        intel["available_qualities"] = [q["label"] for q in qs]
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
    except DownloadCancelledError:
        raise
    except Exception as exc:
        logger.warning("Engine analyze failed (%s) for %s: %s", getattr(engine, "name", "unknown"), url[:120], exc)
        info = await _generic_analyze_url(url)
    if not info:
        # Hardening: transient extractor/network hiccups are common on busy
        # platforms. One delayed retry through the generic yt-dlp path recovers
        # a meaningful share of first-pass failures without user-visible cost.
        await asyncio.sleep(0.8)
        try:
            info = await _generic_analyze_url(url)
            if info:
                logger.info("Analysis retry succeeded for %s", url[:100])
        except Exception as exc:
            logger.warning("Analysis retry failed for %s: %s", url[:120], exc)
            info = None
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
    except DownloadCancelledError:
        raise
    except DownloadImpossibleError:
        # The engine already walked its direct candidates AND exhausted the
        # generic fallback. Re-running it here would double the wait on a dead
        # URL — surface the clear result instead.
        raise
    except Exception as exc:
        logger.warning("Engine video download failed (%s) for %s: %s", getattr(engine, "name", "unknown"), url[:120], exc)
        return await _generic_download_video(url, format_id, quality_label, progress, None)


async def download_audio(url: str, progress=None, bitrate: str | None = None) -> str | None:
    from services.engines.base import PlatformEngine

    requested = _valid_audio_bitrate(bitrate or AUDIO_BITRATE)
    last_error: Exception | None = None
    generic_exhausted = False

    # Platform audio overrides only run for default-bitrate requests; explicit
    # high-bitrate requests (e.g. 320) go straight to the reliable yt-dlp/ffmpeg
    # path. A real override is detected by checking the engine replaced the base
    # implementation, which avoids a redundant duplicate yt-dlp pass.
    use_engine = requested == AUDIO_BITRATE and requested != AUDIO_BITRATE_HIGH
    engine = _select_engine(url) if use_engine else None
    if engine is not None and type(engine).download_audio is not PlatformEngine.download_audio:
        try:
            path = await engine.download_audio(url, progress)
            if path:
                return path
        except FileTooLargeError:
            raise
        except DownloadCancelledError:
            raise
        except DownloadImpossibleError as exc:
            # The engine override already exhausted the generic audio path
            # (e.g. SoundCloud's override IS `_generic_download_audio`). Never
            # rerun it. An empty CDN rejection means the video->audio fallback
            # below would hit the SAME dead CDN, so surface it immediately.
            generic_exhausted = True
            last_error = exc
            if exc.empty_file:
                raise
            logger.warning("Engine audio download exhausted (%s) for %s: %s", getattr(engine, "name", "unknown"), url[:120], exc)
        except Exception as exc:
            if last_error is None:
                last_error = exc
            logger.warning("Engine audio download failed (%s) for %s: %s", getattr(engine, "name", "unknown"), url[:120], exc)

    if not generic_exhausted:
        try:
            path = await _generic_download_audio(url, progress, requested)
            if path:
                return path
        except FileTooLargeError:
            raise
        except DownloadCancelledError:
            raise
        except DownloadImpossibleError as exc:
            last_error = exc
            if exc.empty_file:
                # A dead CDN is not fixable by the video path — raise now.
                raise
            logger.warning("Generic audio download exhausted for %s: %s", url[:120], exc)
        except Exception as exc:
            if last_error is None:
                last_error = exc
            logger.warning("Generic audio download failed for %s: %s", url[:120], exc)

    # Universal last resort: download the verified video path, then transcode its
    # audio track locally. This closes the gap for platforms where yt-dlp cannot
    # isolate an audio-only stream but the robust video path still succeeds.
    try:
        video_path = await download_video(url, "best", "best", progress, allow_large=True)
        if not video_path:
            raise RuntimeError("video fallback returned no file")
        audio_path = await _run_sync(extract_audio_mp3, video_path, requested)
        _safe_remove(video_path)
        if not audio_path:
            raise RuntimeError("audio extraction produced no file")
        logger.info("Audio extracted from verified video for %s", url[:120])
        return audio_path
    except FileTooLargeError:
        raise
    except DownloadCancelledError:
        raise
    except DownloadImpossibleError as exc:
        if exc.empty_file:
            raise
        if last_error is None:
            last_error = exc
        logger.warning("Universal video->audio fallback exhausted for %s: %s", url[:120], exc)
    except Exception as exc:
        if last_error is None:
            last_error = exc
        logger.warning("Universal video->audio fallback failed for %s: %s", url[:120], exc)

    if last_error:
        raise last_error
    raise DownloadImpossibleError("No downloadable audio stream returned after all fallbacks were exhausted")


# Compatibility aliases used by platform engines.
_enforce_max_file_size = _check_size


async def download_image(url: str, image_url: str | None = None) -> str | None:
    import requests
    from utils.url_safety import is_public_url

    target = image_url or url
    safe, _guard_reason = await asyncio.to_thread(is_public_url, target)
    if not safe:
        logger.warning("Blocked image download (SSRF guard) for %s", str(target)[:120])
        raise RuntimeError("blocked_url")

    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="xdl-img-", suffix=".jpg", dir=TEMP_DIR)
    os.close(fd)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }

    # Stream to disk with a hard size cap instead of buffering the whole image in
    # memory — a hostile/oversized response could otherwise exhaust RAM across
    # concurrent downloads.
    try:
        with requests.get(target, timeout=45, headers=headers, stream=True) as r:
            r.raise_for_status()
            content_type = (r.headers.get("Content-Type") or "").lower()
            if content_type and "text/html" in content_type:
                _safe_remove(path)
                raise RuntimeError("image endpoint returned HTML")

            try:
                total = max(0, int(r.headers.get("Content-Length") or 0))
            except (TypeError, ValueError):
                total = 0
            if total > MAX_FILE_SIZE_BYTES:
                _safe_remove(path)
                raise FileTooLargeError(size_bytes=total, limit_bytes=MAX_FILE_SIZE_BYTES)

            downloaded = 0
            with open(path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    if downloaded + len(chunk) > MAX_FILE_SIZE_BYTES:
                        _safe_remove(path)
                        raise FileTooLargeError(
                            size_bytes=downloaded + len(chunk),
                            limit_bytes=MAX_FILE_SIZE_BYTES,
                        )
                    if downloaded == 0:
                        sample = chunk.lstrip().lower()
                        if sample.startswith((b"<!doctype html", b"<html", b"<script")):
                            _safe_remove(path)
                            raise RuntimeError("image endpoint returned a web page")
                    fh.write(chunk)
                    downloaded += len(chunk)
    except FileTooLargeError:
        raise
    except Exception:
        _safe_remove(path)
        raise

    if downloaded < 64:
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
