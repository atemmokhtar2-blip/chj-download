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

import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlparse

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


# --------------------------------------------------------------------------- #
# Generic embed resolver (og:video / JSON-LD / twitter:player:stream)
# --------------------------------------------------------------------------- #
_VIDEO_FILE_RE = re.compile(r"\.(mp4|m3u8|webm|mov|m4v)([?#]|$)", re.I)


def _meta_any(html: str, key: str) -> str | None:
    """Extract a meta tag value regardless of property/name or attribute order."""
    if not html:
        return None
    key_re = re.escape(key)
    m = re.search(
        rf'<meta[^>]+(?:property|name)=["\']{key_re}["\'][^>]+content=["\']([^"\']+)',
        html,
        re.I,
    )
    if m:
        return m.group(1)
    m = re.search(
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{key_re}["\']',
        html,
        re.I,
    )
    return m.group(1) if m else None


def _walk_jsonld(node, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("contentUrl", "embedUrl") and isinstance(value, str) and value.startswith("http"):
                out.append(value)
            else:
                _walk_jsonld(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_jsonld(item, out)


def _jsonld_video_urls(html: str) -> list[str]:
    urls: list[str] = []
    for block in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    ):
        raw = (block.group(1) or "").strip()
        if not raw:
            continue
        try:
            _walk_jsonld(json.loads(raw), urls)
        except Exception:
            continue
    return urls


def _rank_candidate(u: str) -> int:
    low = u.lower()
    score = 0
    if ".mp4" in low:
        score += 500
    elif ".m4v" in low:
        score += 480
    elif ".mov" in low:
        score += 460
    elif ".webm" in low:
        score += 420
    elif ".m3u8" in low:
        score += 300
    m = re.search(r"(?:^|[-_/.])(\d{3,4})p?[-_/.?]", low)
    if m:
        try:
            score += min(int(m.group(1)), 2160)
        except ValueError:
            pass
    return score


def resolve_embed_media(url: str, extra_headers: dict | None = None) -> dict[str, Any] | None:
    """Generic direct-media resolver for pages exposing player metadata.

    Works across platforms whose pages publish og:video / twitter:player:stream
    meta tags or JSON-LD contentUrl. The central audio guard still runs later,
    so a silent direct file is rejected and engines fall back to yt-dlp.
    """
    s = _session()
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)
    try:
        r = s.get(url, timeout=20, allow_redirects=True)
        if r.status_code != 200:
            return None
        html = r.text or ""
        resolved_url = r.url or url
    except Exception as exc:
        logger.debug("Embed meta fetch failed for %s: %s", url, exc)
        return None

    candidates: list[str] = []

    def add(value: str | None) -> None:
        value = (value or "").strip()
        value = value.replace("&amp;", "&").replace("\\u0026", "&").replace("\\/", "/")
        if value.startswith("http") and _VIDEO_FILE_RE.search(value) and value not in candidates:
            candidates.append(value)

    for key in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        add(_meta_any(html, key))

    for value in _jsonld_video_urls(html):
        add(value)

    if not candidates:
        return None

    # Dedupe, mp4 first / m3u8 last; keep the highest-resolution hints on top.
    candidates = sorted(candidates, key=_rank_candidate, reverse=True)[:12]

    title = _meta_any(html, "og:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else None
    thumbnail = _meta_any(html, "og:image") or _meta_any(html, "twitter:image")
    uploader = _meta_any(html, "og:site_name")

    return _video_payload(
        url=resolved_url,
        media_id=None,
        title=title,
        uploader=uploader,
        duration=None,
        platform="Generic",
        thumbnail=thumbnail,
        qualities=None,
        candidates=candidates,
        engine_profile="embed-og-video-jsonld",
    )


# --------------------------------------------------------------------------- #
# Rumble
# --------------------------------------------------------------------------- #
def _rumble_id(url: str) -> str | None:
    m = re.search(r"rumble\.com/(?:embed/)?([A-Za-z0-9]+)", url)
    return m.group(1) if m else None


def _collect_mpeg_urls(
    node,
    out: list[tuple[int, str]] | None = None,
    q_hint: int = 0,
) -> list[tuple[int, str]]:
    """Walk an embed JSON payload and collect (quality, mp4_url) rows.

    The quality key is often a numeric sibling *above* the url (e.g. ``{720:
    {"url": "…"}}``), so the numeric key is propagated down while walking.
    """
    if out is None:
        out = []
    if isinstance(node, dict):
        for key, value in node.items():
            try:
                key_q = int(key)
                is_numeric = True
            except (TypeError, ValueError):
                key_q = q_hint
                is_numeric = False
            if (
                isinstance(value, str)
                and value.startswith("http")
                and ".mp4" in value.lower()
                and ".m3u8" not in value.lower()
            ):
                out.append((key_q, value))
            elif isinstance(value, (dict, list)):
                _collect_mpeg_urls(value, out, key_q if is_numeric else q_hint)
    elif isinstance(node, list):
        for item in node:
            _collect_mpeg_urls(item, out, q_hint)
    return out


def resolve_rumble(url: str) -> dict[str, Any] | None:
    """Resolve a Rumble video via the public embedJS JSON endpoint."""
    video_id = _rumble_id(url)
    if not video_id:
        return None

    s = _session()
    s.headers.update({"Accept": "application/json", "Referer": f"https://rumble.com/embed/{video_id}/"})
    try:
        r = s.get(f"https://rumble.com/embedJS/u3/?request=video&ver=2&v={video_id}", timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as exc:
        logger.debug("Rumble embedJS fetch failed for %s: %s", url, exc)
        return None

    rows = _collect_mpeg_urls(data)
    if not rows:
        return None
    rows.sort(key=lambda row: row[0], reverse=True)
    candidates = [u for _, u in rows]

    labels: list[str] = []
    for q, _u in rows:
        if q and f"{q}p" not in labels and len(labels) < 6:
            labels.append(f"{q}p")

    title = None
    thumbnail = None
    try:
        rr = s.get(f"https://rumble.com/api/Media/oEmbed.json?url={quote(url, safe='')}", timeout=15)
        if rr.status_code == 200:
            payload = rr.json()
            title = payload.get("title")
            thumbnail = payload.get("thumbnail_url")
    except Exception as exc:
        logger.debug("Rumble oEmbed fetch failed for %s: %s", url, exc)

    return _video_payload(
        url=url,
        media_id=video_id,
        title=title,
        uploader=None,
        duration=None,
        platform="Rumble",
        thumbnail=thumbnail,
        qualities=[{"label": q, "format_id": "best"} for q in (labels or ["best"])],
        candidates=candidates,
        engine_profile="rumble-embedjs",
    )


# --------------------------------------------------------------------------- #
# Dailymotion
# --------------------------------------------------------------------------- #
def _dailymotion_id(url: str) -> str | None:
    m = re.search(r"dailymotion\.com/video/([a-z0-9]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"dai\.ly/([a-z0-9]+)", url)
    return m.group(1) if m else None


def resolve_dailymotion(url: str) -> dict[str, Any] | None:
    """Resolve a Dailymotion video via the player metadata JSON.

    Progressive MP4 qualities carry audio and are preferred; HLS is appended as
    a secondary candidate and the audio guard still filters anything silent.
    """
    video_id = _dailymotion_id(url)
    if not video_id:
        return None

    s = _session()
    s.headers.update({"Accept": "application/json", "Referer": "https://www.dailymotion.com/"})
    try:
        r = s.get(f"https://www.dailymotion.com/player/metadata/video/{video_id}", timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as exc:
        logger.debug("Dailymotion metadata fetch failed for %s: %s", url, exc)
        return None

    mp4: list[tuple[int, str]] = []
    hls: list[tuple[int, str]] = []
    for qkey, entries in (data.get("qualities") or {}).items():
        try:
            q = int(qkey)
        except (TypeError, ValueError):
            q = 0
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            candidate = entry.get("url")
            if not isinstance(candidate, str) or not candidate.startswith("http"):
                continue
            low = candidate.lower()
            if ".m3u8" in low or ".mpd" in low:
                hls.append((q, candidate))
            elif ".mp4" in low or "video/" in (entry.get("type") or "").lower():
                mp4.append((q, candidate))

    rows = sorted(mp4, key=lambda row: row[0], reverse=True) + sorted(hls, key=lambda row: row[0], reverse=True)
    if not rows:
        return None

    labels: list[str] = []
    for q, _u in mp4:
        if q and f"{q}p" not in labels and len(labels) < 7:
            labels.append(f"{q}p")

    title = None
    thumbnail = None
    try:
        rr = s.get(f"https://www.dailymotion.com/services/oembed?url={quote(url, safe='')}", timeout=15)
        if rr.status_code == 200:
            payload = rr.json()
            title = payload.get("title")
            thumbnail = payload.get("thumbnail_url")
    except Exception as exc:
        logger.debug("Dailymotion oEmbed fetch failed for %s: %s", url, exc)

    return _video_payload(
        url=url,
        media_id=video_id,
        title=title,
        uploader=None,
        duration=None,
        platform="Dailymotion",
        thumbnail=thumbnail,
        qualities=[{"label": q, "format_id": "best"} for q in (labels or ["best"])],
        candidates=[u for _, u in rows],
        engine_profile="dailymotion-player-metadata",
    )
