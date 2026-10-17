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

from utils import tls

logger = logging.getLogger(__name__)

_UA = tls.chrome_ua()
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


def _session():
    """Browser-grade session shared by every JSON-API resolver.

    Prefer a curl_cffi Session carrying the newest Chrome JA3/JA4 + HTTP/2
    fingerprint (the same layer the HTML/embed paths use) and attach the shared
    cookies.txt jar so login-walled JSON endpoints keep working. The plain
    requests Session is used only when curl_cffi is unavailable — this upgrade
    automatically hardens Vimeo, Streamable, Imgur, Coub, Rumble, Dailymotion,
    Twitch GQL, Ok.ru, Odysee, VK and any future resolver's direct fetch path.
    """
    base_headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"}
    try:
        jar = _load_cookies()
    except Exception:
        jar = requests.cookies.RequestsCookieJar()

    s = tls.session(headers=base_headers)
    if s is None:
        s = requests.Session()
        s.headers.update(base_headers)
        try:
            s.cookies.update(jar)
        except Exception:
            pass
        return s
    # curl_cffi Session: attach the same Netscape jar (its Cookies container
    # accepts a requests CookieJar / dict update transparently).
    try:
        s.cookies.update(jar)
    except Exception:
        try:
            for c in jar:
                s.cookies.set(c.name, c.value, domain=c.domain, path=c.path or "/")
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
        "Priority": "u=0, i",
        "Sec-CH-UA": '"Chromium";v="139", "Google Chrome";v="139", "Not_A Brand";v="24"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
    }
    if headers:
        base.update(headers)
    try:
        resp = tls.get(
            url,
            headers=base,
            cookies=_cookie_dict() or None,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
        if resp is not None:
            return resp
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
        # Many publishers put the direct media file in the VideoObject's `url`
        # field rather than the canonical contentUrl/embedUrl keys — and then
        # nest it several objects deep. Detect the node type first so we can
        # harvest `url` only where it is unambiguously a video.
        node_type = node.get("@type") or node.get("type") or ""
        type_items = node_type if isinstance(node_type, (list, tuple)) else [node_type]
        is_video_object = any(
            str(t).lower() in {"videoobject", "video"} for t in type_items
        )
        for key, value in node.items():
            if isinstance(value, str) and value.startswith("http"):
                if key in ("contentUrl", "embedUrl") or (key == "url" and is_video_object):
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


def _with_embed_param(url: str) -> str:
    """Append ``embed=1`` to a URL (safe global crawl hint).

    Many publishers only render og:video / player JSON on their embed or
    publish variant. This is used as a second page fetch when the canonical
    page yields no media, and is harmless when the site ignores the param.
    """
    if not url:
        return url
    try:
        query = urlparse(url).query.lower()
    except Exception:
        query = ""
    if "embed" in query:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}embed=1"


def _fetch_html_variants(url: str, headers: dict) -> tuple[str, str]:
    """Desktop then mobile-UA browser-impersonated fetch.

    Returns ``(html, resolved_url)``; html is "" when every profile fails.
    """
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
            if html:
                return html, getattr(r, "url", url) or url
        except Exception as exc:
            logger.debug("Embed meta fetch failed (%s) for %s: %s", profile, url, exc)
    return "", url


def _collect_video_candidates(html: str) -> list[str]:
    """Ranked direct media URLs from every HTML layer the resolver supports."""
    if not html:
        return []
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
    return candidates


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
      7. An ``embed=1`` variant of the page when the canonical URL hides media.

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

    html, resolved_url = _fetch_html_variants(url, headers)
    candidates = _collect_video_candidates(html)

    oembed_endpoints = _oembed_endpoints(html) if html else []
    for value in _oembed_media_urls(oembed_endpoints, extra_headers):
        if value not in candidates:
            candidates.append(value)

    # Embed/publish variant (layer 7): many platforms only expose media there.
    embed_variant = _with_embed_param(url)
    if not candidates and embed_variant != url:
        variant_html, _variant_resolved = _fetch_html_variants(embed_variant, headers)
        for value in _collect_video_candidates(variant_html):
            if value not in candidates:
                candidates.append(value)
        for value in _oembed_media_urls(_oembed_endpoints(variant_html), extra_headers):
            if value not in candidates:
                candidates.append(value)

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
# Telegram (public channels via t.me embed og:video)
# --------------------------------------------------------------------------- #
_TG_EMBED_RE = re.compile(r"(?:t\.me|telegram\.me|telegram\.dog)/([^/?#]+)/(\d+)")


def resolve_telegram(url: str) -> dict[str, Any] | None:
    """Resolve a public t.me video through Telegram's embed preview page.

    Telegram publishes a rich og:video / twitter:player:stream preview when the
    message URL is loaded with ``?embed=1&mode=tme`` — the same world-class
    technique every Telegram downloader uses. yt-dlp has no reliable t.me
    extractor, so without this resolver the engine had no direct video path at
    all.
    """
    m = _TG_EMBED_RE.search(url or "")
    if not m:
        return None
    channel, message_id = m.group(1), m.group(2)
    base = f"https://t.me/{channel}/{message_id}"
    variants = [f"{base}?embed=1&mode=tme", f"{base}?embed=1", base]

    headers = {
        "Referer": "https://t.me/",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }

    html = ""
    resolved_url = url
    for variant in variants:
        html, resolved_url = _fetch_html_variants(variant, headers)
        if html:
            break

    if not html:
        return None

    candidates = _collect_video_candidates(html)
    for value in _oembed_media_urls(_oembed_endpoints(html), headers):
        if value not in candidates:
            candidates.append(value)

    if not candidates:
        return None

    candidates = sorted(candidates, key=_rank_candidate, reverse=True)[:12]
    title = _meta_any(html, "og:title")
    thumbnail = _meta_any(html, "og:image")

    return _video_payload(
        url=resolved_url,
        media_id=message_id,
        title=title,
        uploader=channel,
        duration=None,
        platform="Telegram",
        thumbnail=thumbnail,
        qualities=None,
        candidates=candidates,
        engine_profile="telegram-embed-og-video",
    )


# --------------------------------------------------------------------------- #
# Tumblr (public oEmbed JSON API + page fallback)
# --------------------------------------------------------------------------- #
def _tumblr_oembed_url(target: str) -> str:
    return f"https://www.tumblr.com/oembed/1.0?url={quote(target, safe='')}"


def resolve_tumblr(url: str) -> dict[str, Any] | None:
    """Resolve a Tumblr video via the public oEmbed endpoint, then page HTML.

    Tumblr exposes a public oEmbed JSON API whose ``html`` block frequently
    contains the actual <video><source mp4>. This is a stable first-class API
    (not scraping), so it survives the theme/JS churn that breaks HTML mining.
    """
    headers = {"Referer": "https://www.tumblr.com/", "Accept": "application/json,text/html,*/*;"}

    candidates: list[str] = []
    title: str | None = None
    thumbnail: str | None = None
    uploader: str | None = None

    def add(value: str | None) -> None:
        value = _unescape_url(value)
        if value.startswith("http") and _VIDEO_FILE_RE.search(value) and value not in candidates:
            candidates.append(value)

    try:
        r = _impersonate_get(_tumblr_oembed_url(url), headers=headers, timeout=15)
        if int(getattr(r, "status_code", 0) or 0) == 200:
            raw = (getattr(r, "text", "") or "").strip()
            if raw.startswith("{"):
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    title = payload.get("title")
                    uploader = payload.get("author_name")
                    thumbnail = payload.get("thumbnail_url")
                    for value in _collect_media_urls_from_html(payload.get("html") or ""):
                        add(value)
    except Exception as exc:
        logger.debug("Tumblr oEmbed fetch failed for %s: %s", url, exc)

    if not candidates:
        html, _ = _fetch_html_variants(url, headers)
        title = title or _meta_any(html, "og:title")
        thumbnail = thumbnail or _meta_any(html, "og:image")
        for value in _collect_video_candidates(html):
            add(value)

    if not candidates:
        return None

    candidates = sorted(candidates, key=_rank_candidate, reverse=True)[:12]
    return _video_payload(
        url=url,
        media_id=None,
        title=title,
        uploader=uploader,
        duration=None,
        platform="Tumblr",
        thumbnail=thumbnail,
        qualities=None,
        candidates=candidates,
        engine_profile="tumblr-oembed+html",
    )


# --------------------------------------------------------------------------- #
# Bilibili (window.__playinfo__ progressive durl extraction)
# --------------------------------------------------------------------------- #
_BILI_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def resolve_bilibili(url: str) -> dict[str, Any] | None:
    """Resolve a Bilibili video from ``window.__playinfo__`` on the page.

    Bilibili embeds its authoritative player state as a JSON assignment:
    ``window.__playinfo__ = { data: { durl: [{url, backup_url, ...}] } }``.
    The ``durl`` entries are progressive MP4 files that carry BOTH audio and
    video and can be downloaded directly with just a Referer header — the same
    extraction path used by every open-source Bilibili downloader. DASH-only
    ``.m4s`` segments are intentionally ignored here (silent) and left to the
    yt-dlp merge path.
    """
    m = _BILI_BVID_RE.search(url or "")
    if not m:
        return None
    bvid = m.group(0)
    headers = {
        "Referer": "https://www.bilibili.com/",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    html, resolved_url = _fetch_html_variants(f"https://www.bilibili.com/video/{bvid}", headers)
    if not html:
        return None

    candidates: list[str] = []

    def add(value: str | None) -> None:
        value = _unescape_url(value)
        if not value:
            return
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith("http") and _VIDEO_FILE_RE.search(value) and value not in candidates:
            candidates.append(value)

    # 1) Authoritative player state: progressive durl mp4s (with audio).
    pm = re.search(r"window\.__playinfo__\s*=\s*(.*?)</script>", html, re.S)
    if pm:
        try:
            play = json.loads(pm.group(1).strip())
            data = play.get("data") or {}
            for durl in data.get("durl") or []:
                if isinstance(durl, dict):
                    add(durl.get("url"))
                    for backup in durl.get("backup_url") or []:
                        add(backup)
            # backup DASH audio/video (video-only .m4s is rejected later, so
            # only progressive durl candidates survive — exactly what we want).
            dash = data.get("dash") or {}
            for stream in (dash.get("video") or []) + (dash.get("audio") or []):
                if isinstance(stream, dict):
                    add(stream.get("baseUrl") or stream.get("base_url"))
        except Exception as exc:
            logger.debug("Bilibili playinfo parse failed: %s", exc)

    # 2) Generic mining (catches durl URLs even if playinfo shape drifts).
    for value in _script_media_urls(html):
        add(value)
    for value in _video_tag_urls(html):
        add(value)
    for key in ("og:video", "og:video:url", "twitter:player:stream"):
        add(_meta_any(html, key))

    if not candidates:
        return None

    candidates = sorted(candidates, key=_rank_candidate, reverse=True)[:12]
    title = _meta_any(html, "og:title")
    thumbnail = _meta_any(html, "og:image")

    return _video_payload(
        url=resolved_url,
        media_id=bvid,
        title=title,
        uploader=None,
        duration=None,
        platform="Bilibili",
        thumbnail=thumbnail,
        qualities=None,
        candidates=candidates,
        engine_profile="bilibili-playinfo-durl",
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


# --------------------------------------------------------------------------- #
# Twitch (clips via the public GQL API + embed fallback)
# --------------------------------------------------------------------------- #
# Public client id used by Twitch's own web app; widely used by open-source
# clip downloaders. It is not a secret — the Twitch site ships it in every
# page bundle — but it unlocks the authoritative GQL endpoint instead of
# scraping the heavy SPA.
_TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"


def _twitch_clip_slug(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"(?:clips\.twitch\.tv/|twitch\.tv/[^/?#]+/clip/)([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def resolve_twitch(url: str) -> dict[str, Any] | None:
    """Resolve Twitch clips through the authoritative GQL API.

    Twitch clip pages are heavy SPAs that defeat simple HTML scraping. The GQL
    endpoint returns ``clip.videoQualities[].sourceURL`` — progressive MP4
    renditions that already carry audio — which is the same world-class
    technique every popular Twitch clip downloader uses. Non-clip Twitch URLs
    (VODs, channels) intentionally fall straight back to the generic embed
    resolver + yt-dlp.
    """
    slug = _twitch_clip_slug(url)
    if not slug:
        return resolve_embed_media(url, extra_headers={"Referer": "https://clips.twitch.tv/"})

    s = _session()
    headers = {
        "Client-ID": _TWITCH_CLIENT_ID,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _UA,
    }
    payload = {
        "query": (
            "query ClipsDownload($slug: ID!) {"
            "  clip(slug: $slug) {"
            "    title"
            "    broadcaster { displayName login }"
            "    videoQualities { frameRate quality sourceURL }"
            "  }"
            "}"
        ),
        "variables": {"slug": slug},
    }
    try:
        r = s.post("https://gql.twitch.tv/gql", json=payload, headers=headers, timeout=20)
        if int(getattr(r, "status_code", 0) or 0) != 200:
            return resolve_embed_media(url, extra_headers={"Referer": "https://clips.twitch.tv/"})
        data = r.json()
    except Exception as exc:
        logger.debug("Twitch GQL fetch failed for %s: %s", url, exc)
        return resolve_embed_media(url, extra_headers={"Referer": "https://clips.twitch.tv/"})

    clip = (data.get("data") or {}).get("clip") or {}
    qualities = clip.get("videoQualities") or []

    rows: list[tuple[int, str]] = []
    for item in qualities or []:
        if not isinstance(item, dict):
            continue
        src = item.get("sourceURL") or item.get("source_url")
        if not isinstance(src, str) or not src.startswith("http"):
            continue
        try:
            height = int(str(item.get("quality") or "0").strip().strip("p") or 0)
        except (TypeError, ValueError):
            height = 0
        rows.append((height, src))

    rows.sort(key=lambda row: row[0], reverse=True)
    candidates: list[str] = []
    labels: list[str] = []
    for height, candidate in rows:
        if candidate not in candidates:
            candidates.append(candidate)
            if height and f"{height}p" not in labels and len(labels) < 6:
                labels.append(f"{height}p")

    if not candidates:
        return resolve_embed_media(url, extra_headers={"Referer": "https://clips.twitch.tv/"})

    title = clip.get("title")
    uploader = (clip.get("broadcaster") or {}).get("displayName") if isinstance(clip.get("broadcaster"), dict) else None
    thumbnail = f"https://clips-media-assets2.twitch.tv/{slug}-preview-480x272.jpg"

    return _video_payload(
        url=url,
        media_id=slug,
        title=title,
        uploader=uploader,
        duration=None,
        platform="Twitch",
        thumbnail=thumbnail,
        qualities=[{"label": q, "format_id": "best"} for q in (labels or ["best"])],
        candidates=candidates,
        engine_profile="twitch-gql-clips+embed-fallback",
    )


# --------------------------------------------------------------------------- #
# Odnoklassniki (ok.ru videoembed JSON)
# --------------------------------------------------------------------------- #
def _ok_video_id(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"ok\.ru/(?:video|live)/(\d{6,})", url)
    return m.group(1) if m else None


def resolve_odnoklassniki(url: str) -> dict[str, Any] | None:
    """Resolve ok.ru videos via the public videoembed JSON endpoint.

    ``ok.ru/videoembed/{id}`` returns an authoritative JSON payload with every
    mp4 rendition and its height — no auth, no token dance, far more reliable
    than HTML scraping the full player page.
    """
    video_id = _ok_video_id(url)
    if not video_id:
        return None

    s = _session()
    s.headers.update({"Accept": "application/json", "Referer": "https://ok.ru/"})
    try:
        r = s.get(f"https://ok.ru/videoembed/{video_id}", timeout=20)
        if int(getattr(r, "status_code", 0) or 0) != 200:
            return None
        data = r.json()
    except Exception as exc:
        logger.debug("Odnoklassniki videoembed fetch failed for %s: %s", url, exc)
        return None

    rows: list[tuple[int, str]] = []
    for entry in (data.get("videos") or []) if isinstance(data, dict) else []:
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("url")
        if not isinstance(candidate, str) or not candidate.startswith("http"):
            continue
        try:
            height = int(str(entry.get("quality") or entry.get("height") or "0").strip().strip("p") or 0)
        except (TypeError, ValueError):
            height = 0
        rows.append((height, candidate))

    rows.sort(key=lambda row: row[0], reverse=True)
    candidates: list[str] = []
    labels: list[str] = []
    for height, candidate in rows:
        if candidate not in candidates:
            candidates.append(candidate)
            if height and f"{height}p" not in labels and len(labels) < 6:
                labels.append(f"{height}p")

    if not candidates:
        return None

    title = data.get("movie") or data.get("title")
    thumbnail = data.get("thumbnailUrl") or data.get("thumbnail")

    return _video_payload(
        url=url,
        media_id=video_id,
        title=title,
        uploader=None,
        duration=None,
        platform="Odnoklassniki",
        thumbnail=thumbnail,
        qualities=[{"label": q, "format_id": "best"} for q in (labels or ["best"])],
        candidates=candidates,
        engine_profile="odnoklassniki-videoembed",
    )


# --------------------------------------------------------------------------- #
# Odysee (LBRY) — public JSON-RPC streaming URL
# --------------------------------------------------------------------------- #
def resolve_odysee(url: str) -> dict[str, Any] | None:
    """Resolve an Odysee/LBRY video via the public LBRY JSON-RPC API.

    Odysee content is blockchain-hosted (LBRY). Its page player is a heavy SPA,
    but the official JSON-RPC endpoint returns an authoritative
    ``streaming_url`` — a progressive mp4 that already carries audio — with no
    auth or API key. This is the same world-class technique yt-dlp's Odysee
    extractor uses, reusing the bot's own cookie-aware HTTP stack so it falls
    back to the embed/yt-dlp path when the claim is unavailable.
    """
    parsed = urlparse(url or "")
    segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if ":" not in segment:
        return None
    name, claim_id = segment.rsplit(":", 1)
    if not name or not claim_id:
        return None

    # Localised metadata first (cheap og tags from the canonical page).
    title = thumbnail = None
    try:
        html, _ = _fetch_html_variants(url, {"Referer": "https://odysee.com/"})
        if html:
            title = _meta_any(html, "og:title")
            thumbnail = _meta_any(html, "og:image")
    except Exception:
        pass

    streaming_url = None
    try:
        s = _session()
        r = s.post(
            "https://api.na-backend.odysee.com/api/v1/proxy?m=get",
            json={
                "jsonrpc": "2.0",
                "method": "get",
                "params": {"uri": f"lbry://{name}#{claim_id}", "save_file": False, "timeout": 15},
                "id": 1,
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=25,
        )
        if int(getattr(r, "status_code", 0) or 0) == 200:
            body = r.json()
            streaming_url = ((body or {}).get("result") or {}).get("streaming_url")
    except Exception as exc:
        logger.debug("Odysee LBRY get failed for %s: %s", url, exc)

    if not streaming_url or not str(streaming_url).startswith("http"):
        return None

    return _video_payload(
        url=url,
        media_id=claim_id,
        title=title,
        uploader=None,
        duration=None,
        platform="Odysee",
        thumbnail=thumbnail,
        qualities=None,
        candidates=[str(streaming_url)],
        engine_profile="lbry-jsonrpc-streaming-url",
    )


# --------------------------------------------------------------------------- #
# VK — video_ext.php JSON (official player endpoint)
# --------------------------------------------------------------------------- #
_VK_VIDEO_RE = re.compile(r"vk(?:video)?\.(?:com|ru)/(?:video|clip)(-?\d+)_(\d+)", re.I)


def resolve_vk(url: str) -> dict[str, Any] | None:
    """Resolve VK videos/clips via the official ``video_ext.php`` JSON endpoint.

    VK's HTML page player is a heavy JS app, but the site still ships the exact
    ``video_ext.php?oid=...&id=...&hash=...`` endpoint inside the page (the same
    technique yt-dlp's VK extractor uses). That endpoint returns an
    authoritative JSON ``payload`` of progressive MP4 renditions — no app id,
    no auth — so the resolver gets real CDN URLs with audio instead of relying
    on HTML scraping. Clips fall back to script-mined CDN URLs from the page.
    """
    m = _VK_VIDEO_RE.search(url or "")
    if not m:
        return None
    oid, vid = m.group(1), m.group(2)
    kind = "clip" if "/clip" in url else "video"
    page_url = f"https://vk.com/{kind}{oid}_{vid}"

    html, resolved = _fetch_html_variants(
        page_url,
        {"Referer": "https://vk.com/", "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
    )
    if not html:
        return None

    candidates: list[str] = []

    def add(value: str | None) -> None:
        value = _unescape_url(value)
        if not value:
            return
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith("http") and _VIDEO_FILE_RE.search(value) and value not in candidates:
            candidates.append(value)

    # 1) Authoritative video_ext.php payload (classic VK videos).
    if kind == "video":
        pm = re.search(r"video_ext\.php\?[^\"'&;\s]+", html)
        if pm:
            ext = pm.group(0)
            if not ext.startswith("http"):
                ext = "https://vk.com/" + ext
            try:
                s = _session()
                r = s.get(ext, headers={"Referer": page_url, "Accept": "application/json"}, timeout=20)
                if int(getattr(r, "status_code", 0) or 0) == 200:
                    payload = (r.json() or {}).get("payload") or {}
                    for entry in payload.values():
                        if isinstance(entry, dict):
                            for value in entry.values():
                                add(value)
            except Exception as exc:
                logger.debug("VK video_ext.php fetch failed for %s: %s", url, exc)

    # 2) Script-mined CDN URLs from the canonical page (works for clips and as
    #    a fallback when video_ext.php is absent).
    for value in _collect_video_candidates(html):
        add(value)

    if not candidates:
        return None

    candidates = sorted(candidates, key=_rank_candidate, reverse=True)[:12]
    title = _meta_any(html, "og:title")
    thumbnail = _meta_any(html, "og:image")

    return _video_payload(
        url=url,
        media_id=f"{oid}_{vid}",
        title=title,
        uploader=None,
        duration=None,
        platform="VK",
        thumbnail=thumbnail,
        qualities=None,
        candidates=candidates,
        engine_profile=f"vk-video-ext-json+script-cdn-{kind}",
    )


# --------------------------------------------------------------------------- #
# Snapchat — Spotlight canonical embed og:video
# --------------------------------------------------------------------------- #
_SNAP_SPOTLIGHT_RE = re.compile(r"snapchat\.com/spotlight/([A-Za-z0-9]+)", re.I)


def resolve_snapchat(url: str) -> dict[str, Any] | None:
    """Resolve a Snapchat Spotlight video from its canonical embed page.

    Spotlight short links bounce through ``snapchat.com/spotlight/<id>``; that
    canonical page publishes og:video with a CDN MP4 (already carrying audio)
    to browser-TLS fetches. A dedicated resolver adds the right Referer and
    canonical URL instead of trusting whatever short/redirect URL the user
    pasted, then falls back to the generic layered extractor.
    """
    m = _SNAP_SPOTLIGHT_RE.search(url or "")
    if not m:
        return None
    spotlight_id = m.group(1)
    canonical = f"https://www.snapchat.com/spotlight/{spotlight_id}"

    html, resolved = _fetch_html_variants(
        canonical,
        {"Referer": "https://www.snapchat.com/"},
    )
    if not html:
        return None

    candidates = [c for c in _collect_video_candidates(html) if c.startswith("http")]
    if not candidates:
        return None

    candidates = sorted(candidates, key=_rank_candidate, reverse=True)[:12]
    title = _meta_any(html, "og:title")
    thumbnail = _meta_any(html, "og:image")

    return _video_payload(
        url=url,
        media_id=spotlight_id,
        title=title,
        uploader=None,
        duration=None,
        platform="Snapchat",
        thumbnail=thumbnail,
        qualities=None,
        candidates=candidates,
        engine_profile="snapchat-spotlight-embed-og-video",
    )


# --------------------------------------------------------------------------- #
# Bluesky — official public xrpc API (no auth, no JS scraping)
# --------------------------------------------------------------------------- #
_BLUE_POST_RE = re.compile(r"bsky\.app/profile/([^/?#]+)/post/([A-Za-z0-9]+)", re.I)


def resolve_bluesky(url: str) -> dict[str, Any] | None:
    """Resolve a Bluesky video post via the official public xrpc API.

    Bluesky's read API is fully unauthenticated. Resolve the profile handle to
    its DID, build the post AT-URI, fetch the post record, then walk the video
    embed for its stream playlist (mp4/m3u8) — the same world-class technique
    the web client uses internally, with zero login or heavy JS parsing. Image
    posts return None so the generic engine keeps classifying them.
    """
    m = _BLUE_POST_RE.search(url or "")
    if not m:
        return None
    handle, rkey = m.group(1), m.group(2)

    s = _session()
    s.headers.update({"Accept": "application/json"})
    base_api = "https://public.api.bsky.app"

    def _get_json(target: str):
        try:
            r = s.get(target, timeout=20)
            if int(getattr(r, "status_code", 0) or 0) != 200:
                return None
            return r.json()
        except Exception:
            return None

    did = handle if handle.startswith("did:") else None
    if not did:
        try:
            info = _get_json(
                f"{base_api}/xrpc/com.atproto.identity.resolveHandle?handle={quote(handle, safe='')}"
            )
            did = (info or {}).get("did")
        except Exception as exc:
            logger.debug("Bluesky handle resolve failed for %s: %s", handle, exc)
    if not did:
        return None

    at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"

    # Prefer the hydrated post view (getPostThread): native video embeds expose
    # their real HLS playlist URL there, while the raw getPosts record only
    # carries blob refs (no downloadable URL).
    post: dict = {}
    record: dict = {}
    try:
        thread = _get_json(
            f"{base_api}/xrpc/app.bsky.feed.getPostThread?uri={quote(at_uri, safe='')}&depth=0"
        )
        post = (((thread or {}).get("thread") or {}).get("post")) or {}
        if isinstance(post, dict):
            record = post.get("record") if isinstance(post.get("record"), dict) else {}
    except Exception as exc:
        logger.debug("Bluesky getPostThread failed for %s: %s", url, exc)

    if not isinstance(post, dict) or not post:
        posts = _get_json(f"{base_api}/xrpc/app.bsky.feed.getPosts?uris={quote(at_uri, safe='')}")
        post_list = (posts or {}).get("posts") or []
        post = post_list[0] if (isinstance(post_list, list) and post_list and isinstance(post_list[0], dict)) else {}
        record = post.get("record") if isinstance(post.get("record"), dict) else {}

    if not record and not post:
        return None

    title = None
    raw_text = record.get("text") or ""
    if raw_text:
        title = re.sub(r"\s+", " ", raw_text).strip()[:120]
    thumbnail = None
    if isinstance(post.get("embed"), dict):
        embed = post["embed"]
        thumbnail = embed.get("thumbnail")
        if not thumbnail:
            images = embed.get("images") or []
            if images and isinstance(images[0], dict):
                thumbnail = images[0].get("thumb")

    candidates: list[str] = []

    def _walk(node) -> None:
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)
        elif isinstance(node, str) and node.startswith("http"):
            low = node.lower()
            if (".m3u8" in low or ".mp4" in low or ".webm" in low) and node not in candidates:
                candidates.append(node)

    # Walk the hydrated view's embed — the native video playlist lives here.
    _walk(post.get("embed"))

    if not candidates:
        return None

    candidates = sorted(candidates, key=_rank_candidate, reverse=True)[:12]
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    uploader = author.get("displayName") or author.get("handle")

    return _video_payload(
        url=url,
        media_id=rkey,
        title=title,
        uploader=uploader,
        duration=None,
        platform="Bluesky",
        thumbnail=thumbnail,
        qualities=None,
        candidates=candidates,
        engine_profile="bluesky-public-xrpc-video-embed",
    )


# --------------------------------------------------------------------------- #
# Kick — official public JSON API (VOD + clips)
# --------------------------------------------------------------------------- #
_KICK_VIDEO_RE = re.compile(r"kick\.com/(?:[^/?#]+/)?(?:videos|video)/([\w-]+)", re.I)
_KICK_CLIP_QUERY_RE = re.compile(r"kick\.com/[^?#]*\?clip=([\w-]+)", re.I)
_KICK_CLIP_PATH_RE = re.compile(r"kick\.com/[^/?#]+/clips/([\w-]+)", re.I)


def resolve_kick(url: str) -> dict[str, Any] | None:
    """Resolve Kick VODs/clips through the official unauthenticated JSON API.

    Kick (Twitch's main rival) exposes public endpoints with no key/signature:

      https://kick.com/api/v2/video/{slug}   -> playback_url / source (HLS mp4)
      https://kick.com/api/v2/clips/{id}     -> clip_url / video_url (progressive)

    The call rides the browser-grade (curl_cffi) session, so the API cannot
    403 the plain-Python TLS fingerprint. Localised og tags supply title /
    thumbnail before the authoritative JSON answers.
    """
    slug = clip_id = None
    m = _KICK_VIDEO_RE.search(url or "")
    if m:
        slug = m.group(1)
    else:
        m = _KICK_CLIP_QUERY_RE.search(url or "") or _KICK_CLIP_PATH_RE.search(url or "")
        if m:
            clip_id = m.group(1)
    if not slug and not clip_id:
        return None

    title = thumbnail = uploader = None
    media_id = slug or clip_id
    try:
        html, _ = _fetch_html_variants(url, {"Referer": "https://kick.com/"})
        if html:
            title = _meta_any(html, "og:title")
            thumbnail = _meta_any(html, "og:image")
    except Exception:
        pass

    candidates: list[str] = []
    try:
        s = _session()
        endpoint = (
            f"https://kick.com/api/v2/video/{slug}"
            if slug else f"https://kick.com/api/v2/clips/{clip_id}"
        )
        r = s.get(endpoint, headers={"Accept": "application/json"}, timeout=20)
        if int(getattr(r, "status_code", 0) or 0) != 200:
            return None
        ctype = str(getattr(r, "headers", {}).get("Content-Type") or "").lower()
        if "text/html" in ctype:
            return None
        body = r.json()

        if slug:
            play = body.get("playback_url") or body.get("source") or body.get("url")
            if play and str(play).startswith("http"):
                candidates.append(str(play))
            media_id = body.get("slug") or slug
            if not title:
                title = body.get("session_title") or body.get("title")
            thumb_node = body.get("thumbnail")
            if not thumbnail and isinstance(thumb_node, dict) and isinstance(thumb_node.get("src"), str):
                thumbnail = thumb_node["src"]
            channel = body.get("channel") if isinstance(body.get("channel"), dict) else {}
            user = channel.get("user") if isinstance(channel.get("user"), dict) else {}
            uploader = user.get("username")
        else:
            clip = body.get("clip") if isinstance(body.get("clip"), dict) else body
            if isinstance(clip, dict):
                media_id = clip.get("id") or clip_id
                if not title:
                    title = clip.get("title")
                for key in ("clip_url", "video_url", "mp4_url", "source", "url"):
                    v = clip.get(key)
                    if v and str(v).startswith("http"):
                        candidates.append(str(v))
    except Exception as exc:
        logger.debug("Kick API resolve failed for %s: %s", url, exc)

    if not candidates:
        return None

    return _video_payload(
        url=url,
        media_id=media_id,
        title=title,
        uploader=uploader,
        duration=None,
        platform="Kick",
        thumbnail=thumbnail,
        qualities=None,
        candidates=candidates[:8],
        engine_profile="kick-api-json-playback-url",
    )


# --------------------------------------------------------------------------- #
# Rutube — official play/options JSON API
# --------------------------------------------------------------------------- #
_RUTUBE_VIDEO_RE = re.compile(r"rutube\.ru/(?:video|shorts|embed)/(?:private/)?([0-9a-fA-F]{8,64})", re.I)


def resolve_rutube(url: str) -> dict[str, Any] | None:
    """Resolve Rutube videos via the official play/options JSON API.

    Rutube's public endpoint returns an authoritative HLS master with no auth
    or key — the exact technique its own player and yt-dlp's extractor use:

      https://rutube.ru/api/play/options/{id}/?format=json&no_404=true
      -> video_balancer.m3u8 (mp4 HLS, carries audio) + title + thumbnail_url

    Fetched through the browser-grade session to defeat the Python TLS
    fingerprint. Returns None for clips/playlists so the embed/yt-dlp path
    continues to own those shapes.
    """
    m = _RUTUBE_VIDEO_RE.search(url or "")
    if not m:
        return None
    video_id = m.group(1)

    try:
        s = _session()
        r = s.get(
            f"https://rutube.ru/api/play/options/{video_id}/?format=json&no_404=true",
            headers={"Accept": "application/json"},
            timeout=20,
        )
        if int(getattr(r, "status_code", 0) or 0) != 200:
            return None
        body = r.json()
    except Exception as exc:
        logger.debug("Rutube play/options failed for %s: %s", url, exc)
        return None

    balancer = (body or {}).get("video_balancer") if isinstance((body or {}).get("video_balancer"), dict) else {}
    m3u8 = balancer.get("m3u8") or balancer.get("default") or (body or {}).get("url")
    if not m3u8 or not str(m3u8).startswith("http"):
        return None

    author = (body or {}).get("author")
    uploader = author.get("name") if isinstance(author, dict) else None
    thumb = (body or {}).get("thumbnail_url") or balancer.get("thumbnail_url")
    try:
        duration = int((body or {}).get("duration") or 0) or None
    except (TypeError, ValueError):
        duration = None

    return _video_payload(
        url=url,
        media_id=video_id,
        title=(body or {}).get("title"),
        uploader=uploader,
        duration=duration,
        platform="Rutube",
        thumbnail=thumb if isinstance(thumb, str) and thumb.startswith("http") else None,
        qualities=None,
        candidates=[str(m3u8)],
        engine_profile="rutube-play-options-m3u8",
    )


# --------------------------------------------------------------------------- #
# Weibo — render_data CDN mining (f.video.weibocdn.com)
# --------------------------------------------------------------------------- #
_WEIBO_TV_RE = re.compile(
    r"(?:weibo\.com/tv/show/(\d+)(?::(\d+))?|weibo\.tv/v/(\d+)|m\.weibo\.cn/detail/(\d+))",
    re.I,
)
_WEIBO_CDN_RE = re.compile(
    r'https?://(?:f\.video\.weibocdn\.com|video\.weibo\.com)/[^"\s<>()]+?\.(?:mp4|m3u8|webm|mov)(?:\?[^"\s<>()]*)?',
    re.I,
)


def resolve_weibo(url: str) -> dict[str, Any] | None:
    """Resolve a Weibo video by mining the SPA's render_data JSON.

    Weibo (one of the largest CN social platforms) serves video exclusively
    from ``f.video.weibocdn.com`` after the page hydrates its ``$render_data``
    blob. That blob escapes slashes (``https:\\/\\/f.video...``), which plain
    meta parsers cannot see. This resolver fetches through the browser-grade
    TLS layer, normalises the JSON escaping, mines the CDN directly, then falls
    back to the generic og/JSON-LD/`<video>` collector before yt-dlp.
    """
    m = _WEIBO_TV_RE.search(url or "")
    if not m:
        return None
    media_id = next((g for g in m.groups() if g), None)

    headers = {"Referer": "https://weibo.com/"}
    html, resolved_url = _fetch_html_variants(url, headers)

    candidates: list[str] = []
    if html:
        raw = html.replace("\\u0026", "&").replace("\\/", "/")
        for value in _WEIBO_CDN_RE.findall(raw):
            if value not in candidates:
                candidates.append(value)
        for value in _collect_video_candidates(html):
            if value not in candidates:
                candidates.append(value)

    # Some posts only render media on the embed/publish variant.
    if not candidates:
        embed_url = _with_embed_param(url)
        if embed_url and embed_url != url:
            variant_html, _ = _fetch_html_variants(embed_url, headers)
            for value in _collect_video_candidates(variant_html):
                if value not in candidates:
                    candidates.append(value)

    if not candidates:
        return None

    return _video_payload(
        url=resolved_url,
        media_id=media_id or None,
        title=_meta_any(html, "og:title") if html else None,
        uploader=None,
        duration=None,
        platform="Weibo",
        thumbnail=_meta_any(html, "og:image") if html else None,
        qualities=None,
        candidates=sorted(candidates, key=_rank_candidate, reverse=True)[:12],
        engine_profile="weibo-render-data-cdn-mining",
    )


# --------------------------------------------------------------------------- #
# Chzzk — Naver game streaming/VOD (KR Twitch successor)
# --------------------------------------------------------------------------- #
_CHZZK_URL_RE = re.compile(r"chzzk\.naver\.com/(video|live)/([A-Za-z0-9]+)", re.I)


def _chzzk_meta(content: dict) -> tuple[str | None, str | None, str | None, int | None]:
    """Digest the public metadata API payload into (title, uploader, thumb, duration)."""
    channel = content.get("channel") if isinstance(content.get("channel"), dict) else {}
    title = content.get("videoTitle") or content.get("liveTitle")
    uploader = channel.get("channelName") or channel.get("channelId")
    thumb = (
        content.get("thumbnailImageUrl")
        or content.get("defaultThumbnailImageUrl")
        or content.get("liveImageUrl")
    )
    duration = content.get("duration") or content.get("videoContentDuration")
    try:
        duration = int(duration) if duration else None
    except (TypeError, ValueError):
        duration = None
    return title, uploader, thumb, duration


def resolve_chzzk(url: str) -> dict[str, Any] | None:
    """Resolve Chzzk (Naver game streaming/VOD) via its public JSON API.

    VOD:  ``api.chzzk.naver.com/service/v1/videos/{videoNo}``
    Live: ``api.chzzk.naver.com/service/v2/channels/{channelId}/live-detail``

    The API supplies title / channel / thumbnail before any player token is
    minted. The playable media is then mined from the SPA render data (VOD) or
    the ``livePlaybackJson`` descriptor (live) using the same media-URL matcher
    the generic resolver uses. If only metadata resolves and no direct media URL
    is found, we return None so yt-dlp can finish through the universal fallback.
    """
    m = _CHZZK_URL_RE.search(url or "")
    if not m:
        return None
    kind = m.group(1).lower()
    key = m.group(2)

    headers = {"Referer": "https://chzzk.naver.com/", "Accept": "application/json"}

    if kind == "live":
        api = f"https://api.chzzk.naver.com/service/v2/channels/{key}/live-detail"
    else:
        api = f"https://api.chzzk.naver.com/service/v1/videos/{key}"

    content: dict = {}
    try:
        s = _session()
        r = s.get(api, headers=headers, timeout=20)
        if int(getattr(r, "status_code", 0) or 0) == 200:
            body = r.json() if hasattr(r, "json") else {}
            if isinstance(body, dict) and isinstance(body.get("content"), dict):
                content = body["content"]
    except Exception as exc:
        logger.debug("Chzzk metadata API failed for %s: %s", url, exc)

    title, uploader, thumb, duration = _chzzk_meta(content)

    html, resolved_url = _fetch_html_variants(url, headers)

    candidates: list[str] = []

    def add(value: str) -> None:
        value = _unescape_url(value)
        if value.startswith("http") and value not in candidates:
            candidates.append(value)

    if html:
        raw = html.replace("\\u0026", "&").replace("\\/", "/")
        for mm in _MEDIA_URL_RE.finditer(raw):
            add(mm.group(0))
        for value in _collect_video_candidates(html):
            add(value)

    playback = content.get("livePlaybackJson")
    if isinstance(playback, str) and playback.strip():
        text = playback.replace("\\u0026", "&").replace("\\/", "/")
        for mm in _MEDIA_URL_RE.finditer(text):
            add(mm.group(0))

    if not candidates:
        return None

    return _video_payload(
        url=resolved_url,
        media_id=key,
        title=title,
        uploader=uploader,
        duration=duration,
        platform="Chzzk",
        thumbnail=thumb if isinstance(thumb, str) and thumb.startswith("http") else None,
        qualities=None,
        candidates=sorted(candidates, key=_rank_candidate, reverse=True)[:12],
        engine_profile="chzzk-public-api+render-mining",
    )
