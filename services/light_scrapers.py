"""
Lightweight direct resolvers for high-value fallback platforms.

These platforms previously relied entirely on the catch-all yt-dlp engine
(YtDlpFallbackEngine). Each resolver here adds a real extraction path:

  1. Vimeo      -> player.vimeo.com/{id}/config JSON (progressive mp4 WITH audio)
  2. Streamable -> api.streamable.com + HTML og:video fallback
  3. Imgur      -> i.imgur.com direct mp4 (video/GIF posts) + oEmbed metadata
  4. Coub       -> coub.com/api/v2/coubs/{id} JSON (html5 mp4 file_versions)

Every resolved URL still passes the central audio guard
(downloader._prepare_video_for_delivery) before delivery, so a silent direct
file is always rejected and the engine falls back to the yt-dlp merge path.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _fmt_duration(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "Unknown"
    if s <= 0:
        return "Unknown"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _meta_content(html: str, prop: str) -> str | None:
    """Extract an og:* meta value regardless of attribute order."""
    if not html:
        return None
    m = re.search(
        rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)',
        html,
    )
    if m:
        return m.group(1)
    m = re.search(
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
        html,
    )
    return m.group(1) if m else None


def _video_payload(
    *,
    url: str,
    media_id: str,
    title: str | None,
    uploader: str | None,
    duration,
    platform: str,
    thumbnail: str | None,
    qualities: list[dict] | None,
    candidates: list[str],
    engine_profile: str,
) -> dict[str, Any] | None:
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = (candidate or "").strip()
        if candidate.startswith("http") and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    if not ordered:
        return None

    return {
        "url": url,
        "title": (title or "").strip() or f"{platform} media",
        "uploader": (uploader or platform).strip() or platform,
        "duration": _fmt_duration(duration),
        "platform": platform,
        "media_type": "video",
        "thumbnail": thumbnail,
        "qualities": qualities or [{"label": "best", "format_id": "best"}],
        "media_id": media_id,
        "downloadable": True,
        "play_url": ordered[0],
        "video_candidates": ordered,
        "engine_profile": engine_profile,
    }


# --------------------------------------------------------------------------- #
# Vimeo
# --------------------------------------------------------------------------- #
def _vimeo_id(url: str) -> str | None:
    m = re.search(r"player\.vimeo\.com/video/(\d{5,12})", url)
    if m:
        return m.group(1)
    m = re.search(r"vimeo\.com/(?:channels/[\w.\-]+/)?(?:videos/)?(\d{5,12})", url)
    if m:
        return m.group(1)
    # bare numeric path (vimeo.com/123456789)
    m = re.search(r"vimeo\.com/(\d{5,12})", url)
    return m.group(1) if m else None


def resolve_vimeo(url: str) -> dict[str, Any] | None:
    """Resolve a Vimeo video from the player config JSON.

    Progressive files carry their own audio track, so they are preferred over
    adaptive/DASH streams which are video-only.
    """
    video_id = _vimeo_id(url)
    if not video_id:
        return None
    s = _session()
    s.headers.update({"Referer": "https://player.vimeo.com/", "Accept": "application/json"})
    try:
        r = s.get(f"https://player.vimeo.com/video/{video_id}/config", timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as exc:
        logger.debug("Vimeo config fetch failed for %s: %s", url, exc)
        return None

    try:
        files = (data.get("request") or {}).get("files") or {}
        progressive = files.get("progressive") or []

        rows: list[tuple[int, str]] = []
        for f in progressive:
            candidate = str(f.get("url") or "")
            if not candidate.startswith("http"):
                continue
            try:
                height = int(f.get("height") or 0)
            except (TypeError, ValueError):
                height = 0
            rows.append((height, candidate))

        # Highest resolution first; progressive streams always include audio.
        rows.sort(key=lambda row: row[0], reverse=True)
        candidates: list[str] = []
        labels: list[str] = []
        for height, candidate in rows:
            if candidate not in candidates:
                candidates.append(candidate)
                if height and f"{height}p" not in labels and len(labels) < 6:
                    labels.append(f"{height}p")

        if not candidates:
            # No progressive variant -> let yt-dlp handle the adaptive merge.
            return None

        video = data.get("video") or {}
        title = video.get("title")
        try:
            duration = int(float(video.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        thumbs = video.get("thumbs") or {}
        thumbnail = thumbs.get("1280") or thumbs.get("960") or thumbs.get("640") or thumbs.get("base")
        uploader = ((video.get("owner") or {}).get("name")) or None

        qualities = [{"label": label, "format_id": "best"} for label in (labels or ["best"])]
        return _video_payload(
            url=url,
            media_id=video_id,
            title=title,
            uploader=uploader,
            duration=duration,
            platform="Vimeo",
            thumbnail=thumbnail,
            qualities=qualities,
            candidates=candidates,
            engine_profile="vimeo-player-config+progressive-audio",
        )
    except Exception as exc:
        logger.debug("Vimeo config parse failed for %s: %s", url, exc)
        return None


# --------------------------------------------------------------------------- #
# Streamable
# --------------------------------------------------------------------------- #
def _streamable_code(url: str) -> str | None:
    m = re.search(r"streamable\.com/(?:e/)?([a-z0-9]{4,12})", url)
    return m.group(1) if m else None


def resolve_streamable(url: str) -> dict[str, Any] | None:
    """Resolve a Streamable video via the public JSON API, then HTML og:video."""
    code = _streamable_code(url)
    if not code:
        return None

    s = _session()
    mp4: str | None = None
    title: str | None = None
    thumbnail: str | None = None

    # 1) Public JSON API (no auth) is the cleanest source.
    try:
        r = s.get(f"https://api.streamable.com/videos/{code}", timeout=15)
        if r.status_code == 200:
            payload = r.json()
            mp4 = ((payload.get("files") or {}).get("mp4") or {}).get("url")
            title = payload.get("title")
            thumbnail = payload.get("thumbnail_url")
    except Exception as exc:
        logger.debug("Streamable API fetch failed for %s: %s", url, exc)

    # 2) HTML fallback: og:video/og:video:url meta + mp4 CDN links.
    if not mp4:
        try:
            r = s.get(f"https://streamable.com/{code}", timeout=15)
            html = r.text or ""
            title = title or _meta_content(html, "og:title")
            thumbnail = thumbnail or _meta_content(html, "og:image")

            def _rank(u: str) -> int:
                u = u.lower()
                score = 0
                if "mobile" in u:
                    score -= 50
                if "cdn-cf-east" in u:
                    score += 20
                if "mp4" in u:
                    score += 30
                return score

            found = re.findall(r"https?://[^\"'\s]+\.mp4[^\"'\s]*", html)
            if found:
                mp4 = sorted(set(found), key=_rank, reverse=True)[0]
        except Exception as exc:
            logger.debug("Streamable HTML fetch failed for %s: %s", url, exc)

    return _video_payload(
        url=url,
        media_id=code,
        title=title,
        uploader=None,
        duration=None,
        platform="Streamable",
        thumbnail=thumbnail,
        qualities=None,
        candidates=[mp4] if mp4 else [],
        engine_profile="streamable-api+og-video",
    )


# --------------------------------------------------------------------------- #
# Imgur
# --------------------------------------------------------------------------- #
def _imgur_id(url: str) -> str | None:
    m = re.search(r"imgur\.com/(?:gallery/|a/)?([A-Za-z0-9]{5,12})", url)
    return m.group(1) if m else None


def resolve_imgur(url: str) -> dict[str, Any] | None:
    """Resolve only video/GIF Imgur posts to their direct .mp4.

    Static images intentionally return None so the generic yt-dlp extractor
    keeps classifying image vs. video correctly.
    """
    img_id = _imgur_id(url)
    if not img_id:
        return None

    s = _session()
    mp4 = f"https://i.imgur.com/{img_id}.mp4"
    is_video = False

    path = url.split("?", 1)[0].lower()
    if path.endswith(".gifv") or path.endswith(".mp4"):
        is_video = True
    else:
        # A successful HEAD to the .mp4 variant means the post has a video
        # rendition; static images 404 this endpoint.
        try:
            r = s.head(mp4, timeout=15, allow_redirects=True)
            ctype = (r.headers.get("Content-Type") or "").lower()
            if r.status_code == 200 and ("video/" in ctype or not ctype):
                is_video = True
        except Exception as exc:
            logger.debug("Imgur video HEAD probe failed for %s: %s", url, exc)

    if not is_video:
        return None

    title = None
    thumbnail = None
    try:
        r = s.get(
            f"https://api.imgur.com/oembed.json?url=https%3A%2F%2Fimgur.com%2F{img_id}",
            timeout=15,
        )
        if r.status_code == 200:
            payload = r.json()
            title = payload.get("title")
            thumbnail = payload.get("thumbnail_url")
    except Exception as exc:
        logger.debug("Imgur oEmbed fetch failed for %s: %s", url, exc)

    return _video_payload(
        url=url,
        media_id=img_id,
        title=title,
        uploader=None,
        duration=None,
        platform="Imgur",
        thumbnail=thumbnail,
        qualities=None,
        candidates=[mp4],
        engine_profile="imgur-direct-mp4+oembed",
    )


# --------------------------------------------------------------------------- #
# Coub
# --------------------------------------------------------------------------- #
def _coub_id(url: str) -> str | None:
    m = re.search(r"coub\.com/(?:view|embed)/([A-Za-z0-9]+)", url)
    return m.group(1) if m else None


def resolve_coub(url: str) -> dict[str, Any] | None:
    """Resolve a Coub loop from the public API v2 html5 file_versions."""
    coub_id = _coub_id(url)
    if not coub_id:
        return None

    s = _session()
    s.headers.update({"Accept": "application/json", "Referer": "https://coub.com/"})
    try:
        r = s.get(f"https://coub.com/api/v2/coubs/{coub_id}", timeout=20)
        if r.status_code != 200:
            return None
        payload = r.json()
    except Exception as exc:
        logger.debug("Coub API fetch failed for %s: %s", url, exc)
        return None

    try:
        html5 = (payload.get("file_versions") or {}).get("html5") or {}
        video_obj = html5.get("video") or {}

        candidates: list[str] = []
        for key in ("high", "med", "low"):
            item = video_obj.get(key) or {}
            candidate = item.get("url") if isinstance(item, dict) else None
            if isinstance(candidate, str) and candidate.startswith("http"):
                candidates.append(candidate)
        direct = video_obj.get("url") if isinstance(video_obj, dict) else None
        if isinstance(direct, str) and direct.startswith("http"):
            candidates.append(direct)

        title = payload.get("title")
        try:
            duration = int(float(payload.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        thumbnail = payload.get("picture") or payload.get("original_picture") or payload.get("timeline_picture")

        return _video_payload(
            url=url,
            media_id=coub_id,
            title=title,
            uploader=None,
            duration=duration,
            platform="Coub",
            thumbnail=thumbnail,
            qualities=None,
            candidates=candidates,
            engine_profile="coub-api-v2-html5",
        )
    except Exception as exc:
        logger.debug("Coub API parse failed for %s: %s", url, exc)
        return None
