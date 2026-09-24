"""
Facebook extractor — Pro resolver for videos, reels and fb.watch links.

Strategies:
  1. Normalize/expand fb.watch and mobile URLs.
  2. yt-dlp metadata extraction for authenticated/complex pages.
  3. Facebook HTML/OG parse for browser-rendered public videos.
  4. Mobile/mbasic page parse as a lightweight fallback.

The resolver returns direct `play_url` when possible and annotates provider
scores so admins can diagnose source quality quickly.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

_PROVIDER_TIMEOUT = 24
_CDN_HOST_HINTS = ("fbcdn", "fbsbx", "akamaihd", "facebook", "cdninstagram", "scontent")


_FB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Referer": "https://www.facebook.com/",
}


def _facebook_cookie() -> str | None:
    raw = (os.getenv("FACEBOOK_COOKIES") or os.getenv("FB_COOKIES") or "").strip()
    if raw:
        return raw
    c_user = (os.getenv("FACEBOOK_C_USER") or "").strip()
    xs = (os.getenv("FACEBOOK_XS") or "").strip()
    if c_user and xs:
        return f"c_user={c_user}; xs={xs};"
    return None


def _http_get(url: str, headers: dict | None = None, timeout: int = 20):
    hdrs = dict(_FB_HEADERS)
    if headers:
        hdrs.update(headers)
    cookie = _facebook_cookie()
    if cookie:
        hdrs["Cookie"] = cookie
    try:
        from curl_cffi import requests as creq
        return creq.get(url, headers=hdrs, impersonate="chrome", timeout=timeout, allow_redirects=True)
    except Exception:
        pass
    import requests
    return requests.get(url, headers=hdrs, timeout=timeout, allow_redirects=True)


def normalize_facebook_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    url = url.replace("://m.facebook.com", "://www.facebook.com")
    url = url.replace("://mobile.facebook.com", "://www.facebook.com")
    url = url.replace("://mbasic.facebook.com", "://www.facebook.com")
    if "fb.watch" in url or "fb.com" in url:
        try:
            r = _http_get(url, timeout=15)
            final = getattr(r, "url", "") or url
            if final:
                url = final
        except Exception as exc:
            logger.debug("FB redirect expansion skipped for %s: %s", url, exc)
    return url


def _unescape_url(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = value.replace("\\/", "/").replace("\\u0025", "%").replace("\\u0026", "&")
    try:
        value = value.encode("utf-8").decode("unicode_escape")
    except Exception:
        pass
    for _ in range(2):
        if "%" in value:
            value = unquote(value)
    return value


def _is_login_or_unavailable(html_text: str) -> bool:
    text = (html_text or "").lower()
    return any(x in text for x in (
        "you must log in", "login", "content isn't available", "content is not available",
        "this video isn't available", "this content isn't available right now",
        "checkpoint", "temporarily blocked", "sorry, this content isn't available",
    ))


def _probe_media_url(url: str | None) -> tuple[bool, int, str]:
    if not url or not str(url).startswith("http"):
        return False, 0, ""
    try:
        host = urlparse(url).netloc.lower()
        if not any(h in host for h in _CDN_HOST_HINTS):
            logger.debug("FB probe non-standard host: %s", host)
        r = _http_get(url, headers={"Range": "bytes=0-2048", "Accept": "video/*,*/*"}, timeout=10)
        status = int(getattr(r, "status_code", 0) or 0)
        headers = getattr(r, "headers", {}) or {}
        ctype = headers.get("Content-Type", "").lower()
        clen = headers.get("Content-Length", "0")
        try:
            size = int(clen or 0)
        except Exception:
            size = 0
        if status not in (200, 206):
            return False, size, ctype
        if any(x in ctype for x in ("text/html", "application/json")):
            return False, size, ctype
        if ctype and not any(x in ctype for x in ("video", "octet-stream", "binary")):
            return False, size, ctype
        return True, size, ctype
    except Exception as exc:
        logger.debug("FB CDN probe failed: %s", exc)
        return False, 0, ""


def _score_result(result: dict | None) -> int:
    if not result:
        return 0
    score = 0
    source = result.get("source") or "unknown"
    source_bonus = {"yt_dlp": 42, "html": 36, "mobile_html": 30, "og": 24}
    score += source_bonus.get(source, 12)
    if result.get("play_url"):
        score += 26
    if result.get("hd_url"):
        score += 16
    if result.get("thumbnail"):
        score += 5
    if result.get("title") and result.get("title") != "Facebook Video":
        score += 5
    ok, size, ctype = _probe_media_url(result.get("play_url")) if result.get("play_url") else (False, 0, "")
    result["cdn_probe"] = {"ok": ok, "size": size, "content_type": ctype}
    if ok:
        score += 35
    if size > 1_000_000:
        score += 4
    if "fbcdn" in (result.get("play_url") or "") or "fbsbx" in (result.get("play_url") or ""):
        score += 4
    result["provider_score"] = score
    return score


def _base_result(url: str, play_url: str, source: str, title: str = "Facebook Video", thumbnail: str = "", hd_url: str = "") -> dict:
    return {
        "title": (title or "Facebook Video").split("\n")[0][:180],
        "uploader": "Facebook",
        "duration": "Unknown",
        "duration_secs": 0,
        "thumbnail": thumbnail or "",
        "platform": "Facebook",
        "media_type": "video",
        "qualities": [
            {"label": "HD", "format_id": "hd", "has_audio": True} if hd_url else None,
            {"label": "Best", "format_id": "best", "has_audio": True},
        ],
        "audio_formats": [],
        "image_url": None,
        "album_items": [],
        "url": url,
        "webpage_url": url,
        "ext": "mp4",
        "media_id": str(abs(hash(url)) % 10_000_000),
        "play_url": hd_url or play_url,
        "sd_url": play_url,
        "hd_url": hd_url or None,
        "source": source,
    } | {"qualities": [q for q in [
        {"label": "HD", "format_id": "hd", "has_audio": True} if hd_url else None,
        {"label": "Best", "format_id": "best", "has_audio": True},
    ] if q]}


def _from_ytdlp(url: str) -> dict | None:
    try:
        import yt_dlp
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 20,
            "extract_flat": False,
            "noplaylist": True,
            "http_headers": _FB_HEADERS,
        }
        if os.path.exists("cookies.txt"):
            opts["cookiefile"] = "cookies.txt"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        formats = info.get("formats") or []
        best = None
        for fmt in sorted(formats, key=lambda f: (int(f.get("height") or 0), int(f.get("tbr") or 0)), reverse=True):
            fu = fmt.get("url")
            if fu and (fmt.get("vcodec") not in (None, "none")):
                best = fmt
                break
        play = (best or {}).get("url") or info.get("url")
        if not play:
            return None
        result = _base_result(
            url=info.get("webpage_url") or url,
            play_url=play,
            source="yt_dlp",
            title=info.get("title") or "Facebook Video",
            thumbnail=info.get("thumbnail") or "",
            hd_url=play if int((best or {}).get("height") or 0) >= 720 else "",
        )
        result["duration_secs"] = int(info.get("duration") or 0)
        result["duration"] = f"{result['duration_secs']}s" if result["duration_secs"] else "Unknown"
        result["media_id"] = str(info.get("id") or result["media_id"])
        return result
    except Exception as exc:
        logger.info("Facebook yt-dlp provider failed: %s", exc)
        return None


def _extract_video_candidates(html_text: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    patterns = [
        ("hd", r'"browser_native_hd_url"\s*:\s*"([^"]+)"'),
        ("sd", r'"browser_native_sd_url"\s*:\s*"([^"]+)"'),
        ("hd", r'"playable_url_quality_hd"\s*:\s*"([^"]+)"'),
        ("sd", r'"playable_url"\s*:\s*"([^"]+)"'),
        ("sd", r'property="og:video" content="([^"]+)"'),
        ("sd", r'property="og:video:url" content="([^"]+)"'),
        ("sd", r'content="(https://[^"]+\.mp4[^"]*)"'),
    ]
    seen: set[str] = set()
    for quality, pat in patterns:
        for m in re.finditer(pat, html_text):
            u = _unescape_url(m.group(1))
            if u and u.startswith("http") and u not in seen:
                seen.add(u)
                candidates.append((quality, u))
    return candidates


def _extract_title_thumb(html_text: str) -> tuple[str, str]:
    title = "Facebook Video"
    thumb = ""
    for pat in (
        r'property="og:title" content="([^"]+)"',
        r'<title[^>]*>(.*?)</title>',
        r'"title"\s*:\s*"([^"]+)"',
    ):
        m = re.search(pat, html_text, re.S)
        if m:
            title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() or title
            break
    for pat in (
        r'property="og:image" content="([^"]+)"',
        r'"preferred_thumbnail".*?"uri"\s*:\s*"([^"]+)"',
        r'"thumbnailImage".*?"uri"\s*:\s*"([^"]+)"',
    ):
        m = re.search(pat, html_text, re.S)
        if m:
            thumb = _unescape_url(m.group(1))
            break
    return title, thumb


def _from_html(url: str, *, mobile: bool = False) -> dict | None:
    fetch_url = url
    if mobile:
        fetch_url = url.replace("www.facebook.com", "mbasic.facebook.com").replace("facebook.com", "mbasic.facebook.com")
    try:
        r = _http_get(fetch_url, timeout=20)
        text = getattr(r, "text", "") or ""
        if len(text) < 400:
            return None
        candidates = _extract_video_candidates(text)
        if not candidates:
            return {"requires_login": True, "source": "mobile_html" if mobile else "html"} if _is_login_or_unavailable(text) else None
        title, thumb = _extract_title_thumb(text)
        hd = next((u for q, u in candidates if q == "hd"), "")
        sd = next((u for q, u in candidates if q == "sd"), "") or candidates[0][1]
        return _base_result(
            url=getattr(r, "url", "") or url,
            play_url=sd,
            source="mobile_html" if mobile else "html",
            title=title,
            thumbnail=thumb,
            hd_url=hd,
        )
    except Exception as exc:
        logger.info("Facebook HTML provider failed mobile=%s: %s", mobile, exc)
        return None


def scrape_facebook(url: str) -> dict | None:
    url = normalize_facebook_url(url)
    providers = [
        ("yt_dlp", lambda: _from_ytdlp(url)),
        ("html", lambda: _from_html(url, mobile=False)),
        ("mobile_html", lambda: _from_html(url, mobile=True)),
    ]
    results: list[dict] = []
    login_hint = False
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        future_map = {pool.submit(fn): name for name, fn in providers}
        for future in as_completed(future_map, timeout=_PROVIDER_TIMEOUT):
            name = future_map[future]
            try:
                result = future.result(timeout=1)
                if result and result.get("requires_login"):
                    login_hint = True
                    continue
                if result and result.get("play_url"):
                    _score_result(result)
                    logger.info("Facebook provider=%s score=%s", name, result.get("provider_score"))
                    results.append(result)
            except Exception as exc:
                logger.debug("Facebook provider=%s failed: %s", name, exc)

    if results:
        results.sort(key=lambda r: int(r.get("provider_score") or 0), reverse=True)
        best = results[0]
        best["provider_candidates"] = [
            {"source": r.get("source"), "score": r.get("provider_score"), "probe": r.get("cdn_probe")}
            for r in results
        ]
        best["engine_profile"] = "yt-dlp+html+mobile+cdn-probe"
        logger.info("FacebookEnginePro selected source=%s score=%s", best.get("source"), best.get("provider_score"))
        return best

    if login_hint:
        return {
            "platform": "Facebook",
            "media_type": "video",
            "url": url,
            "webpage_url": url,
            "requires_login": True,
            "error_reason": "login_required_or_private",
            "message": "Facebook content requires cookies/login or is unavailable/private.",
            "source": "facebook_pro",
            "provider_candidates": [],
        }
    return None
