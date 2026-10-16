"""
Lightweight direct resolvers for high-value fallback platforms.

These platforms previously relied entirely on the catch-all yt-dlp engine
(YtDlpFallbackEngine). Each resolver here adds a real extraction path:

  1. Vimeo      -> player.vimeo.com/{id}/config JSON (progressive mp4 WITH audio)
  2. Streamable -> api.streamable.com + HTML og:video fallback
  3. Imgur      -> i.imgur.com direct mp4 (video/GIF posts) + oEmbed metadata
  4. Coub       -> coub.com/api/v2/coubs/{id} JSON (html5 mp4 file_versions)
  5. Threads    -> /embed player page (og:video + JSON-LD + script CDN mining)
  6. Embed      -> browser-TLS impersonation + og/JSON-LD/<video>/<source>/
                   <script> mining + oEmbed auto-discovery (used by Snapchat,
                   Telegram, Likee, Kwai, Twitch, Bilibili, VK, Tumblr,
                   Odysee, Niconico, Douyin, Xiaohongshu and others)

Every resolved URL still passes the central audio guard
(downloader._prepare_video_for_delivery) before delivery, so a silent direct
file is always rejected and the engine falls back to the yt-dlp merge path.
"""
from __future__ import annotations

import json
import logging
import os
import re
from html import unescape as _html_unescape
from http import cookiejar as _cookiejar
from typing import Any
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
)
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
)


def _load_cookies() -> requests.cookies.RequestsCookieJar:
    """Load the same Netscape cookies.txt that yt-dlp uses.

    This lets the lightweight resolvers unlock login-walled media (Instagram,
    Facebook, YouTube, X, …) exactly like the heavy downloader path, instead of
    being permanently anonymous.
    """
    jar = requests.cookies.RequestsCookieJar()
    path = None
    try:
        from config.settings import YTDLP_COOKIES_FILE
        path = YTDLP_COOKIES_FILE
    except Exception:
        pass
    path = path or "cookies.txt"
    if not path or not os.path.exists(path):
        return jar
    try:
        cj = _cookiejar.MozillaCookieJar()
        cj.load(path, ignore_discard=True, ignore_expires=True)
        jar.update(cj)
    except Exception as exc:
        logger.debug("Cookie loading failed for %s: %s", path, exc)
    return jar


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"})
    try:
        s.cookies.update(_load_cookies())
    except Exception:
        pass
    return s


def _cookie_dict() -> dict:
    try:
        return {c.name: c.value for c in _load_cookies()}
    except Exception:
        return {}


def _impersonate_get(
    url: str,
    headers: dict | None = None,
    timeout: int = 20,
    allow_redirects: bool = True,
):
    """Browser-grade GET with curl_cffi TLS impersonation.

    Many modern embeds (Threads, Snapchat, Likee, Kwai, Xiaohongshu) block or
    serve empty pages to plain-requests TLS fingerprints. curl_cffi impersonates
    a real Chrome handshake (JA3/HTTP2), which is the same world-class technique
    the production Twitter resolver already uses. Falls back to a cookie-carrying
    requests session when curl_cffi is unavailable.
    """
    base = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if headers:
        base.update(headers)
    try:
        from curl_cffi import requests as creq

        return creq.get(
            url,
            headers=base,
            cookies=_cookie_dict() or None,
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
    except Exception:
        pass
    s = _session()
    return s.get(url, headers=base, timeout=timeout, allow_redirects=allow_redirects)


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


_MEDIA_URL_RE = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+?\.(?:mp4|m3u8|webm|m4v|mov)"
    r"(?:[?#][A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?",
    re.I,
)


def _unescape_url(value: str | None) -> str:
    if not value:
        return ""
    try:
        value = _html_unescape(str(value))
    except Exception:
        pass
    return (
        value.replace("\\u0026", "&")
        .replace("\\/", "/")
        .strip()
    )


def _video_tag_urls(html: str) -> list[str]:
    """Extract src attributes from HTML5 <video> and <source> elements.

    A growing number of platforms (and oEmbed html snippets) publish their
    direct player file as a plain HTML5 tag instead of og:video meta.
    """
    if not html:
        return []
    out: list[str] = []
    for m in re.finditer(
        r"<video\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", html, re.S | re.I
    ):
        out.append(_unescape_url(m.group(1)))
    for m in re.finditer(
        r"<(?:video|source)\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", html, re.S | re.I
    ):
        out.append(_unescape_url(m.group(1)))
    return out


def _script_media_urls(html: str) -> list[str]:
    """Mine every direct media URL buried inside inline <script> JSON.

    Modern pages rarely expose og:video anymore — the player state (and the CDN
    URLs) live inside JSON blobs that are invisible to simple meta parsers.
    """
    if not html:
        return []
    out: list[str] = []
    for block in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        raw = block.group(1) or ""
        if not raw:
            continue
        try:
            raw = _html_unescape(raw)
        except Exception:
            pass
        # JSON-in-JS commonly escapes literal slashes (https:\/\/cdn...) and
        # ampersands (\u0026). Normalize both before URL matching so the miner
        # sees the real CDN URL instead of a backslash-broken string.
        raw = raw.replace("\\u0026", "&").replace("\\/", "/")
        for m in _MEDIA_URL_RE.finditer(raw):
            out.append(m.group(0))
    return out


def _collect_media_urls_from_html(html: str) -> list[str]:
    """Every direct media URL detectable from an HTML fragment."""
    if not html:
        return []
    out: list[str] = []
    for value in _video_tag_urls(html) + _script_media_urls(html):
        if value not in out:
            out.append(value)
    for m in _MEDIA_URL_RE.finditer(html):
        value = m.group(0)
        if value not in out:
            out.append(value)
    return out


_OEMBED_LINK_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:json|xml)\+oembed["\'][^>]+href=["\']([^"\']+)',
    re.I,
)
_OEMBED_LINK_RE_ALT = re.compile(
    r'<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']application/(?:json|xml)\+oembed["\']',
    re.I,
)


def _oembed_endpoints(html: str) -> list[str]:
    if not html:
        return []
    found: list[str] = []
    for pat in (_OEMBED_LINK_RE, _OEMBED_LINK_RE_ALT):
        for m in pat.finditer(html):
            href = _unescape_url(m.group(1))
            if href.startswith("http") and href not in found:
                found.append(href)
    return found[:4]


def _oembed_media_urls(endpoints: list[str], extra_headers: dict | None = None) -> list[str]:
    """Auto-discovered oEmbed endpoints frequently contain a ready-to-stream URL.

    The page links to the oEmbed endpoint in its <head>; the endpoint returns
    JSON (often with an html block or a direct url/video_url field) that points
    at the exact media file without any login or token dance.
    """
    out: list[str] = []
    headers = {"Accept": "application/json,text/xml,*/*;q=0.9"}
    if extra_headers:
        headers.update(extra_headers)
    for endpoint in endpoints:
        try:
            r = _impersonate_get(endpoint, headers=headers, timeout=15)
            if int(getattr(r, "status_code", 0) or 0) != 200:
                continue
            raw = getattr(r, "text", "") or ""
            if not raw:
                continue
            stripped = raw.lstrip()
            if stripped.startswith(("<", "<?xml")):
                out.extend(_MEDIA_URL_RE.findall(raw))
                continue
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                continue
            html_block = payload.get("html")
            if html_block:
                out.extend(_collect_media_urls_from_html(str(html_block)))
            for key in ("url", "video_url", "thumbnail_url", "provider_url"):
                value = payload.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    out.append(value)
        except Exception as exc:
            logger.debug("oEmbed fetch failed for %s: %s", endpoint, exc)
    return out


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
    """World-class generic direct-media resolver for embed/publish pages.

    Layered extraction instead of the old meta-only scan:

      1. curl_cffi Chrome TLS impersonation fetch (bypasses plain-requests bot
         fingerprints), with a mobile-UA retry when the desktop profile is
         blocked or served an empty page.
      2. og:video / twitter:player:stream meta tags.
      3. JSON-LD VideoObject (contentUrl / embedUrl).
      4. HTML5 <video> / <source> src attributes.
      5. Inline <script> JSON mining for direct CDN mp4/m3u8/webm URLs.
      6. oEmbed auto-discovery via <link> rel=alternate, then fetch the
         endpoint and mine its html block or direct url fields.

    Every candidate still passes the central audio guard later, so a silent
    direct file is always rejected and the engine falls back to the robust
    yt-dlp merge path.
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)

    html = ""
    resolved_url = url
    for profile in ("desktop", "mobile"):
        try:
            r = _impersonate_get(
                url,
                headers={**headers, "User-Agent": _UA if profile == "desktop" else _MOBILE_UA},
                timeout=20,
                allow_redirects=True,
            )
            status = int(getattr(r, "status_code", 0) or 0)
            if status != 200:
                continue
            html = getattr(r, "text", "") or ""
            resolved_url = getattr(r, "url", url) or url
            if html:
                break
        except Exception as exc:
            logger.debug("Embed meta fetch failed (%s) for %s: %s", profile, url, exc)

    if not html:
        return None

    candidates: list[str] = []

    def add(value: str | None) -> None:
        value = _unescape_url(value)
        if value.startswith("http") and _VIDEO_FILE_RE.search(value) and value not in candidates:
            candidates.append(value)

    for key in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        add(_meta_any(html, key))

    for value in _jsonld_video_urls(html):
        add(value)

    for value in _video_tag_urls(html):
        add(value)

    for value in _script_media_urls(html):
        add(value)

    oembed_endpoints = _oembed_endpoints(html)
    if oembed_endpoints:
        for value in _oembed_media_urls(oembed_endpoints, extra_headers):
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
        engine_profile="browser-impersonation+og-jsonld-video-tag-script-oembed",
    )


# --------------------------------------------------------------------------- #
# Threads
# --------------------------------------------------------------------------- #
_THREADS_POST_RE = re.compile(r"threads\.net/@([^/?#]+)/post/([A-Za-z0-9_-]+)", re.I)


def resolve_threads(url: str) -> dict[str, Any] | None:
    """Resolve a Threads post through its public /embed player page.

    Threads' main post page is login-walled and yt-dlp dropped Threads support,
    so without this resolver the engine had no real path. The /embed endpoint is
    intentionally public and still publishes og:video / JSON-LD / inline player
    JSON with the actual CDN mp4 — the same world-class embed technique used by
    every web preview scraper.
    """
    m = _THREADS_POST_RE.search(url or "")
    if not m:
        return None
    user, code = m.group(1), m.group(2)
    embed_url = f"https://www.threads.net/@{user}/post/{code}/embed"

    headers = {
        "Referer": "https://www.threads.net/",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }
    html = ""
    for profile in ("desktop", "mobile"):
        try:
            r = _impersonate_get(
                embed_url,
                headers={**headers, "User-Agent": _UA if profile == "desktop" else _MOBILE_UA},
                timeout=20,
                allow_redirects=True,
            )
            if int(getattr(r, "status_code", 0) or 0) != 200:
                continue
            html = getattr(r, "text", "") or ""
            if html:
                break
        except Exception as exc:
            logger.debug("Threads embed fetch failed (%s) for %s: %s", profile, url, exc)

    if not html:
        return None

    candidates: list[str] = []

    def add(value: str | None) -> None:
        value = _unescape_url(value)
        if value.startswith("http") and _VIDEO_FILE_RE.search(value) and value not in candidates:
            candidates.append(value)

    for key in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        add(_meta_any(html, key))
    for value in _jsonld_video_urls(html):
        add(value)
    for value in _script_media_urls(html):
        add(value)
    for value in _video_tag_urls(html):
        add(value)

    if not candidates:
        return None

    candidates = sorted(candidates, key=_rank_candidate, reverse=True)[:12]
    title = _meta_any(html, "og:title")
    thumbnail = _meta_any(html, "og:image")

    return _video_payload(
        url=url,
        media_id=code,
        title=title,
        uploader=f"@{user}",
        duration=None,
        platform="Threads",
        thumbnail=thumbnail,
        qualities=None,
        candidates=candidates,
        engine_profile="threads-embed-player+browser-impersonation",
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
