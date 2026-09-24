from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

import yt_dlp

from config.settings import (
    MAX_FILE_SIZE_BYTES,
    YTDLP_COOKIES_FILE,
    YTDLP_COOKIES_FROM_BROWSER,
    DOWNLOAD_PROXY,
    YTDLP_CLIENT,
)
from utils.logger import download_logger

SOUNDCLOUD_HOSTS = {
    "soundcloud.com",
    "www.soundcloud.com",
    "m.soundcloud.com",
    "on.soundcloud.com",
}

_PRIVATE_PATTERNS = (
    "private",
    "not available",
    "removed",
    "deleted",
    "copyright",
    "not found",
    "404",
    "geo",
    "region",
    "country",
    "sign in",
    "login",
)


def _format_duration(seconds: int | float | None) -> str:
    try:
        total = int(seconds or 0)
    except Exception:
        total = 0
    if total <= 0:
        return "Unknown"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _base_opts(extra: dict | None = None) -> dict:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "skip_download": True,
        "extract_flat": False,
        "max_filesize": MAX_FILE_SIZE_BYTES,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "http_headers": {
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://soundcloud.com/",
        },
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "geo_bypass": True,
    }
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


def normalize_soundcloud_url(url: str) -> str:
    """Normalize SoundCloud desktop/mobile/share URLs while keeping playlist slugs intact."""
    parsed = urlparse((url or "").strip())
    if not parsed.scheme:
        parsed = urlparse("https://" + (url or "").strip())
    host = parsed.netloc.lower().replace("www.", "")
    if host == "m.soundcloud.com":
        host = "soundcloud.com"
    if host == "on.soundcloud.com":
        # Keep short share links; yt-dlp resolves them reliably and they may carry tokens.
        return urlunparse(("https", host, parsed.path.rstrip("/"), "", parsed.query, ""))
    clean_path = re.sub(r"/+$", "", parsed.path or "")
    return urlunparse(("https", host or "soundcloud.com", clean_path, "", parsed.query, ""))


def _classify_error(exc: BaseException) -> dict:
    msg = str(exc)
    low = msg.lower()
    reason = "extract_failed"
    if any(token in low for token in ("private", "sign in", "login")):
        reason = "private_or_login_required"
    elif any(token in low for token in ("not available", "geo", "region", "country")):
        reason = "region_or_availability_blocked"
    elif any(token in low for token in ("removed", "deleted", "not found", "404")):
        reason = "removed_or_not_found"
    return {
        "title": "SoundCloud audio unavailable",
        "uploader": "Unknown",
        "duration": "Unknown",
        "duration_secs": 0,
        "thumbnail": "",
        "platform": "SoundCloud",
        "media_type": "audio",
        "qualities": [],
        "audio_formats": [],
        "image_url": None,
        "album_items": [],
        "requires_login": "login" in low or "sign in" in low or "private" in low,
        "error_reason": reason,
        "error_detail": msg[:300],
        "provider_score": 0,
        "provider_candidates": [{"source": "yt-dlp", "ok": False, "error": msg[:180]}],
        "engine_profile": "soundcloud-pro-error-diagnostics",
    }


def _is_playlist(info: dict) -> bool:
    if info.get("_type") in ("playlist", "multi_video"):
        return True
    entries = info.get("entries") or []
    return bool(entries and len([e for e in entries if e]) > 1)


def _format_audio_label(fmt: dict, fallback: str = "audio") -> str:
    abr = fmt.get("abr") or fmt.get("tbr") or 0
    ext = (fmt.get("ext") or "audio").upper()
    protocol = fmt.get("protocol") or ""
    if abr:
        return f"{int(abr)} kbps {ext}"
    if "hls" in protocol:
        return f"HLS {ext}"
    return fallback


def _build_audio_formats(info: dict) -> list[dict]:
    formats = info.get("formats") or []
    audio_formats: list[dict] = []
    seen: set[str] = set()
    for fmt in formats:
        acodec = fmt.get("acodec")
        vcodec = fmt.get("vcodec")
        if acodec in (None, "none") and vcodec not in (None, "none"):
            continue
        fmt_id = str(fmt.get("format_id") or fmt.get("format") or "")
        if not fmt_id:
            continue
        filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        if filesize and filesize > MAX_FILE_SIZE_BYTES:
            continue
        key = f"{fmt_id}:{fmt.get('abr') or fmt.get('tbr') or 0}:{fmt.get('ext') or ''}"
        if key in seen:
            continue
        seen.add(key)
        audio_formats.append({
            "format_id": fmt_id,
            "label": _format_audio_label(fmt, "SoundCloud audio"),
            "ext": fmt.get("ext") or "",
            "abr": fmt.get("abr") or fmt.get("tbr") or 0,
            "filesize": filesize,
            "protocol": fmt.get("protocol") or "",
            "source_preference": fmt.get("source_preference") or 0,
        })
    audio_formats.sort(
        key=lambda item: (
            int(item.get("abr") or 0),
            1 if str(item.get("protocol") or "").startswith("https") else 0,
            int(item.get("filesize") or 0),
        ),
        reverse=True,
    )
    if not audio_formats:
        audio_formats = [{"format_id": "bestaudio/best", "label": "Best audio", "abr": 0, "filesize": 0}]
    return audio_formats[:8]


def _entry_to_album_item(entry: dict, index: int) -> dict:
    duration_secs = int(entry.get("duration") or 0)
    return {
        "title": entry.get("title") or f"SoundCloud track {index + 1}",
        "uploader": entry.get("uploader") or entry.get("creator") or entry.get("artist") or "Unknown",
        "url": entry.get("webpage_url") or entry.get("url") or "",
        "thumbnail": entry.get("thumbnail") or "",
        "type": "audio",
        "id": str(entry.get("id") or entry.get("display_id") or index),
        "duration": _format_duration(duration_secs),
        "duration_secs": duration_secs,
    }


def _normalize_info(info: dict, original_url: str, normalized_url: str, profile: str) -> dict:
    entries = [entry for entry in (info.get("entries") or []) if entry]
    playlist = _is_playlist(info)
    album_items = [_entry_to_album_item(entry, idx) for idx, entry in enumerate(entries[:10])] if playlist else []

    duration_secs = int(info.get("duration") or 0)
    audio_formats = _build_audio_formats(info if not playlist else (entries[0] if entries else info))
    best_audio = audio_formats[0] if audio_formats else {"format_id": "bestaudio/best"}
    filesize = best_audio.get("filesize") or info.get("filesize") or info.get("filesize_approx") or 0

    webpage_url = info.get("webpage_url") or normalized_url
    kind = "playlist" if playlist else "track"
    provider_score = 180 + (len(audio_formats) * 10) + (20 if filesize else 0) + (25 if playlist else 0)

    return {
        "title": info.get("title") or (album_items[0]["title"] if album_items else "SoundCloud Track"),
        "uploader": info.get("uploader") or info.get("creator") or info.get("artist") or "Unknown",
        "duration": _format_duration(duration_secs),
        "duration_secs": duration_secs,
        "thumbnail": info.get("thumbnail") or (album_items[0].get("thumbnail") if album_items else ""),
        "platform": "SoundCloud",
        "media_type": "album" if playlist else "audio",
        "content_kind": kind,
        "qualities": [],
        "audio_formats": audio_formats,
        "image_url": None,
        "album_items": album_items,
        "url": normalized_url,
        "original_url": original_url,
        "webpage_url": webpage_url,
        "ext": "mp3",
        "media_id": str(info.get("id") or info.get("display_id") or ""),
        "artist": info.get("artist") or info.get("creator") or info.get("uploader") or "Unknown",
        "track": info.get("track") or info.get("title") or "",
        "album": info.get("album") or info.get("playlist") or "",
        "genre": info.get("genre") or "",
        "view_count": info.get("view_count") or info.get("play_count") or 0,
        "like_count": info.get("like_count") or 0,
        "release_year": info.get("release_year") or None,
        "filesize": filesize,
        "requires_login": False,
        "provider_score": provider_score,
        "provider_candidates": [{
            "source": "yt-dlp",
            "profile": profile,
            "ok": True,
            "formats": len(info.get("formats") or []),
            "entries": len(entries),
        }],
        "engine_profile": "yt-dlp-soundcloud-profiles+audio-format-ranking+playlist-diagnostics",
    }


def scrape_soundcloud(url: str) -> dict:
    normalized = normalize_soundcloud_url(url)
    profiles = [
        {"name": "default", "extra": {}},
        {"name": "playlist-aware", "extra": {"noplaylist": False, "playlistend": 10, "yes_playlist": True}},
        {"name": "no-client", "extra": {"http_client": None}},
    ]
    candidates: list[dict] = []
    last_exc: BaseException | None = None

    for profile in profiles:
        try:
            opts = _base_opts(profile["extra"])
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(normalized, download=False)
            if not info:
                candidates.append({"source": "yt-dlp", "profile": profile["name"], "ok": False, "error": "empty_info"})
                continue
            result = _normalize_info(info, url, normalized, profile["name"])
            result["provider_candidates"] = candidates + result.get("provider_candidates", [])
            return result
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            candidates.append({"source": "yt-dlp", "profile": profile["name"], "ok": False, "error": msg[:180]})
            download_logger.info("SoundCloud profile %s failed for %s: %s", profile["name"], normalized[:80], msg[:180])
            # If this is clearly a terminal availability problem, do not hammer every profile.
            if any(token in msg.lower() for token in _PRIVATE_PATTERNS):
                break

    err = _classify_error(last_exc or RuntimeError("SoundCloud extraction failed"))
    err["url"] = normalized
    err["original_url"] = url
    err["webpage_url"] = normalized
    err["provider_candidates"] = candidates or err["provider_candidates"]
    return err
