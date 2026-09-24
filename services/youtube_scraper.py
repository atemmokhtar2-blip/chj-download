"""
YouTube pro resolver.

Keeps yt-dlp as the source of truth, but adds YouTube-specific normalization,
format scoring, playlist/shorts/live detection, Telegram-safe quality buttons,
and clear diagnostics for login/age/member-only failures.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from typing import Any

import yt_dlp

from config.settings import (
    MAX_FILE_SIZE_BYTES,
    YTDLP_COOKIES_FILE,
    YTDLP_COOKIES_FROM_BROWSER,
    DOWNLOAD_PROXY,
    YTDLP_CLIENT,
)

logger = logging.getLogger(__name__)

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com", "m.youtube.com")


def _format_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "Unknown"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _safe_impersonate():
    try:
        import curl_cffi
        parts = [int(x) for x in curl_cffi.__version__.split(".")[:3] if x.isdigit()]
        ver = tuple(parts + [0] * (3 - len(parts)))
        if ver >= (0, 16, 0):
            return None
        return yt_dlp.networking.impersonate.ImpersonateTarget("chrome")
    except Exception:
        return None


def _base_opts(extra: dict | None = None) -> dict:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "max_filesize": MAX_FILE_SIZE_BYTES,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "http_headers": {
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        },
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "sleep_interval_requests": 0.2,
        "geo_bypass": True,
        "http_chunk_size": 10485760,
    }
    impersonate = _safe_impersonate()
    if impersonate is not None:
        opts["impersonate"] = impersonate
    cookie_path = YTDLP_COOKIES_FILE or "cookies.txt"
    if cookie_path and os.path.exists(cookie_path):
        opts["cookiefile"] = cookie_path
    elif YTDLP_COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (YTDLP_COOKIES_FROM_BROWSER,)
    if DOWNLOAD_PROXY:
        opts["proxy"] = DOWNLOAD_PROXY
    if YTDLP_CLIENT:
        opts["http_client"] = YTDLP_CLIENT
    if extra:
        opts.update(extra)
    return opts


def normalize_youtube_url(url: str) -> str:
    """Normalize youtu.be/mobile/music/shorts/live links while preserving playlist ids."""
    if not url:
        return url
    url = url.strip()
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.netloc.lower().replace("m.youtube.com", "www.youtube.com").replace("music.youtube.com", "www.youtube.com")
    query = parse_qs(parsed.query)

    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]
        params = {"v": video_id} if video_id else {}
        if query.get("list"):
            params["list"] = query["list"][0]
        return urlunparse(("https", "www.youtube.com", "/watch", "", urlencode(params), ""))

    path = parsed.path or "/"
    m = re.search(r"/(shorts|live|embed|v)/([^/?#]+)", path)
    if m:
        params = {"v": m.group(2)}
        if query.get("list"):
            params["list"] = query["list"][0]
        return urlunparse(("https", "www.youtube.com", "/watch", "", urlencode(params), ""))

    if host.endswith("youtube-nocookie.com"):
        host = "www.youtube.com"
    if host.endswith("youtube.com") and not host.startswith("www."):
        host = "www.youtube.com"
    return urlunparse((parsed.scheme or "https", host, path, "", parsed.query, ""))


def _extract_sync(url: str) -> dict:
    profiles: list[tuple[str, dict[str, Any]]] = [
        ("android", {"extractor_args": {"youtube": {"player_client": ["android", "web"]}}}),
        ("web", {"extractor_args": {"youtube": {"player_client": ["web", "ios"]}}}),
        ("ios", {"extractor_args": {"youtube": {"player_client": ["ios", "android"]}}}),
    ]
    last_err: Exception | None = None
    for profile_name, extra in profiles:
        opts = _base_opts({
            "skip_download": True,
            "extract_flat": False,
            "noplaylist": True,
            "youtube_include_dash_manifest": True,
            "youtube_include_hls_manifest": True,
            **extra,
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if isinstance(info, dict):
                    info["_yt_profile"] = profile_name
                return info
        except Exception as exc:  # pragma: no cover - network/provider dependent
            last_err = exc
            logger.info("YouTube profile %s failed: %s", profile_name, exc)
    raise last_err or RuntimeError("YouTube extract failed")


def _failure_payload(url: str, exc: Exception) -> dict:
    msg = str(exc)
    lower = msg.lower()
    requires_login = any(x in lower for x in (
        "sign in", "login", "cookies", "confirm your age", "age-restricted",
        "members-only", "private video", "this video is private",
    ))
    return {
        "title": "YouTube video",
        "uploader": "Unknown",
        "duration": "Unknown",
        "duration_secs": 0,
        "thumbnail": "",
        "platform": "YouTube",
        "media_type": "video",
        "qualities": [],
        "audio_formats": [],
        "image_url": None,
        "album_items": [],
        "url": url,
        "webpage_url": url,
        "ext": "mp4",
        "media_id": "",
        "requires_login": requires_login,
        "error_reason": "login_or_age_or_private_required" if requires_login else "youtube_extract_failed",
        "provider_candidates": [{"provider": "yt-dlp-youtube", "ok": False, "error": msg[:240]}],
        "engine_profile": "yt-dlp-youtube-profiles",
    }


def _content_kind(url: str, info: dict) -> str:
    path = urlparse(url).path.lower()
    live_status = (info.get("live_status") or "").lower()
    if "/shorts/" in path or (int(info.get("duration") or 0) <= 180 and info.get("aspect_ratio") and info.get("aspect_ratio") < 1):
        return "shorts"
    if info.get("is_live") or live_status in {"is_live", "is_upcoming", "was_live"}:
        return "live"
    return "video"


def _safe_selector(height: int) -> str:
    h = int(height)
    return (
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={h}]+bestaudio/"
        f"best[height<={h}][ext=mp4]/"
        f"best[height<={h}]/"
        "best[ext=mp4]/best"
    )


def _build_qualities(formats: list[dict]) -> tuple[list[dict], list[dict]]:
    quality_map: dict[str, dict] = {}
    audio_formats: list[dict] = []
    for f in formats or []:
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        height = int(f.get("height") or 0)
        width = int(f.get("width") or 0)
        filesize = int(f.get("filesize") or f.get("filesize_approx") or 0)
        fmt_id = str(f.get("format_id") or "")

        if (vcodec in (None, "none")) and acodec not in (None, "none"):
            audio_formats.append({
                "format_id": fmt_id,
                "filesize": filesize,
                "abr": f.get("abr") or 0,
                "ext": f.get("ext") or "m4a",
            })
            continue
        if vcodec in (None, "none") or not (height or width):
            continue
        display_h = min(height, width) if height and width else (height or width)
        if display_h >= 2000:
            bucket = 2160
        elif display_h >= 1400:
            bucket = 1440
        elif display_h >= 1000:
            bucket = 1080
        elif display_h >= 700:
            bucket = 720
        elif display_h >= 450:
            bucket = 480
        elif display_h >= 340:
            bucket = 360
        else:
            bucket = 240
        if filesize and filesize > MAX_FILE_SIZE_BYTES:
            # Keep selector fallback capable of choosing lower combined streams elsewhere.
            pass
        label = f"{bucket}p"
        score = bucket
        if acodec not in (None, "none"):
            score += 40
        if (f.get("ext") or "") == "mp4":
            score += 20
        existing = quality_map.get(label)
        if not existing or score > existing.get("_score", 0):
            quality_map[label] = {
                "label": label,
                "height": bucket,
                "format_id": _safe_selector(bucket),
                "filesize": filesize,
                "has_audio": acodec not in (None, "none"),
                "tier": "4K" if bucket >= 2160 else "1440p" if bucket >= 1440 else "Full HD" if bucket >= 1080 else "HD" if bucket >= 720 else "SD" if bucket >= 480 else "Low",
                "_score": score,
            }
    qualities = sorted(quality_map.values(), key=lambda q: q["height"])
    if len(qualities) > 6:
        qualities = [qualities[0]] + qualities[-5:]
    for q in qualities:
        q.pop("_score", None)
    audio_formats.sort(key=lambda a: (a.get("abr") or 0, a.get("filesize") or 0), reverse=True)
    return qualities, audio_formats[:6]


def scrape_youtube(url: str) -> dict | None:
    normalized = normalize_youtube_url(url)
    try:
        info = _extract_sync(normalized)
    except Exception as exc:
        return _failure_payload(normalized, exc)

    formats = info.get("formats") or []
    qualities, audio_formats = _build_qualities(formats)
    if not qualities and info.get("url"):
        qualities = [{"label": "Best", "height": 0, "format_id": "best[ext=mp4]/best", "has_audio": True, "tier": "Best"}]

    duration_secs = int(info.get("duration") or 0)
    media_id = str(info.get("id") or info.get("display_id") or "")
    kind = _content_kind(url, info)
    provider_score = 200 + (len(qualities) * 8) + (30 if audio_formats else 0) + (20 if kind == "shorts" else 0)

    result = {
        "title": info.get("title") or "YouTube Video",
        "uploader": info.get("uploader") or info.get("channel") or "YouTube",
        "duration": _format_duration(duration_secs) if duration_secs else "Unknown",
        "duration_secs": duration_secs,
        "thumbnail": info.get("thumbnail") or "",
        "platform": "YouTube",
        "media_type": "video",
        "qualities": qualities,
        "audio_formats": audio_formats,
        "image_url": None,
        "album_items": [],
        "url": normalized,
        "original_url": url,
        "webpage_url": info.get("webpage_url") or normalized,
        "ext": "mp4",
        "media_id": media_id,
        "content_kind": kind,
        "is_live": bool(info.get("is_live")),
        "live_status": info.get("live_status"),
        "availability": info.get("availability"),
        "channel_id": info.get("channel_id"),
        "view_count": info.get("view_count"),
        "provider_score": provider_score,
        "provider_candidates": [{
            "provider": "yt-dlp-youtube",
            "ok": True,
            "profile": info.get("_yt_profile"),
            "formats": len(formats),
            "qualities": [q.get("label") for q in qualities],
        }],
        "engine_profile": "yt-dlp-youtube-profiles+safe-quality-selectors+shorts-live-diagnostics",
    }
    return result
