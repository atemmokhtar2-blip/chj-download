from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import yt_dlp

from config.settings import MAX_FILE_SIZE_BYTES
from utils.helpers import format_duration, format_size
from utils.logger import error_logger
from services.downloader import _base_ydl_opts

SPOTIFY_HOSTS = {"spotify.com", "open.spotify.com", "play.spotify.com"}
SPOTIFY_RE = re.compile(r"spotify\.com/(?:intl-[a-z]{2}/)?(?P<kind>track|album|playlist|episode|show|artist)/(?P<id>[A-Za-z0-9]+)")
MAX_SPOTIFY_ITEMS = 10


def normalize_spotify_url(url: str) -> str:
    """Normalize Spotify desktop/mobile URLs while preserving the object id."""
    raw = (url or "").strip()
    if not raw:
        return raw
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = parsed.netloc.lower().replace("www.", "")
    if host == "play.spotify.com":
        host = "open.spotify.com"
    query = urlencode([
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in {"si", "context"}
    ])
    path = re.sub(r"^/intl-[a-z]{2}/", "/", parsed.path)
    return urlunparse(("https", host, path.rstrip("/"), "", query, ""))


def _spotify_identity(url: str) -> tuple[str, str]:
    m = SPOTIFY_RE.search(url)
    if not m:
        return "unknown", ""
    return m.group("kind"), m.group("id")


def _classify_error(exc: Exception | str) -> str:
    msg = str(exc).lower()
    if any(x in msg for x in ("drm", "protected", "encrypted")):
        return "spotify_drm_protected"
    if any(x in msg for x in ("private", "login", "premium", "authentication", "unauthorized")):
        return "private_or_login_required"
    if any(x in msg for x in ("unavailable", "not available", "country", "region", "geo")):
        return "region_or_availability_blocked"
    if any(x in msg for x in ("404", "not found", "removed", "deleted")):
        return "removed_or_not_found"
    return "spotify_metadata_only"


def _audio_estimates(duration_secs: int) -> list[dict]:
    """Offer clear MP3 conversion estimates, not direct Spotify formats."""
    if not duration_secs:
        return []
    rows = []
    for abr in (320, 192, 128):
        size = int((duration_secs * abr * 1000) / 8)
        if size <= MAX_FILE_SIZE_BYTES:
            rows.append({
                "format_id": f"spotify-search-mp3-{abr}",
                "label": f"MP3 {abr}kbps",
                "abr": abr,
                "filesize": size,
                "filesize_text": format_size(size),
                "source": "metadata-estimate",
            })
    return rows


def _album_items(entries: list[dict], parent_url: str) -> list[dict]:
    items = []
    for i, entry in enumerate([e for e in entries if e][:MAX_SPOTIFY_ITEMS]):
        dur = int(entry.get("duration") or 0)
        title = entry.get("title") or entry.get("track") or f"Spotify item {i + 1}"
        artist = entry.get("artist") or entry.get("uploader") or entry.get("artists") or "Spotify"
        items.append({
            "title": title,
            "uploader": artist,
            "url": entry.get("webpage_url") or parent_url,
            "thumbnail": entry.get("thumbnail") or "",
            "type": "audio",
            "id": str(entry.get("id") or i),
            "duration": format_duration(dur) if dur else "Unknown",
            "duration_secs": dur,
            "metadata_only": True,
            "error_reason": "spotify_metadata_only",
        })
    return items


def _build_result(url: str, info: dict, profile: str, raw_error: str | None = None) -> dict:
    kind, media_id = _spotify_identity(url)
    duration_secs = int(info.get("duration") or 0)
    entries = [e for e in (info.get("entries") or []) if e]
    is_collection = kind in {"album", "playlist", "show", "artist"} or bool(entries)
    album_items = _album_items(entries, url) if is_collection else []

    title = info.get("title") or info.get("fulltitle") or (
        "Spotify Playlist" if is_collection else "Spotify Track"
    )
    uploader = info.get("uploader") or info.get("artist") or info.get("channel") or "Spotify"
    thumbnail = info.get("thumbnail") or ""

    return {
        "title": title,
        "uploader": uploader,
        "duration": format_duration(duration_secs) if duration_secs else "Unknown",
        "duration_secs": duration_secs,
        "thumbnail": thumbnail,
        "platform": "Spotify",
        "media_type": "album" if album_items else "audio",
        "qualities": [],
        "audio_formats": _audio_estimates(duration_secs),
        "image_url": None,
        "album_items": album_items,
        "url": url,
        "webpage_url": info.get("webpage_url") or url,
        "original_url": url,
        "ext": "mp3",
        "media_id": media_id,
        "content_kind": kind,
        "metadata_only": True,
        "requires_external_lookup": True,
        "downloadable": False,
        "error_reason": "spotify_metadata_only",
        "user_message_key": "spotify_metadata_only",
        "provider_score": 90 + (len(album_items) * 3) + (20 if duration_secs else 0),
        "provider_candidates": [{"source": profile, "score": 90, "ok": True}],
        "engine_profile": "spotify-metadata-pro+clear-ui-state",
        "raw_error": raw_error,
    }


def scrape_spotify(url: str) -> dict:
    """Resolve Spotify metadata and explicitly mark it as metadata-only.

    Spotify streams are DRM/licensed and yt-dlp normally exposes metadata/previews,
    not a direct downloadable track. Returning a structured result avoids the UI
    appearing frozen after the user presses download.
    """
    normalized = normalize_spotify_url(url)
    kind, media_id = _spotify_identity(normalized)
    profiles = [
        ("spotify-metadata", {"skip_download": True, "extract_flat": False, "noplaylist": False}),
        ("spotify-flat-collection", {"skip_download": True, "extract_flat": "in_playlist", "noplaylist": False}),
    ]
    last_error: Exception | None = None
    for profile, extra in profiles:
        opts = _base_ydl_opts({
            **extra,
            "ignoreerrors": True,
            "playlistend": MAX_SPOTIFY_ITEMS,
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(normalized, download=False)
            if info:
                return _build_result(normalized, info, profile)
        except Exception as exc:
            last_error = exc
            error_logger.error("Spotify profile %s failed for %s: %s", profile, normalized[:80], exc)

    reason = _classify_error(last_error or "metadata unavailable")
    return {
        "title": "Spotify link",
        "uploader": "Spotify",
        "duration": "Unknown",
        "duration_secs": 0,
        "thumbnail": "",
        "platform": "Spotify",
        "media_type": "audio",
        "qualities": [],
        "audio_formats": [],
        "image_url": None,
        "album_items": [],
        "url": normalized,
        "webpage_url": normalized,
        "original_url": url,
        "ext": "mp3",
        "media_id": media_id,
        "content_kind": kind,
        "metadata_only": True,
        "requires_external_lookup": True,
        "downloadable": False,
        "error_reason": reason,
        "user_message_key": reason,
        "provider_score": 30,
        "provider_candidates": [{"source": "spotify-metadata", "score": 30, "ok": False, "error": str(last_error or reason)[:180]}],
        "engine_profile": "spotify-metadata-pro+clear-ui-state",
        "raw_error": str(last_error or "")[:300],
    }
