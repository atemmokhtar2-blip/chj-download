"""
Twitter/X extractor — multi-provider resolver for videos, GIFs, photos and mixed tweets.

Providers:
  1. yt-dlp metadata (best for video variants)
  2. public mirror APIs (api.vxtwitter.com / fxtwitter.com)
  3. HTML/OpenGraph fallback

The resolver returns a unified info dict compatible with services.downloader and
PlatformEngine handlers: play_url for video/GIF, image_url for a single photo,
album_items for multi-media tweets.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_TWEET_RE = re.compile(r"(?:twitter\.com|x\.com|mobile\.twitter\.com|fxtwitter\.com|fixupx\.com|vxtwitter\.com)/([^/]+)/status(?:es)?/(\d+)")
_PROVIDER_TIMEOUT = 24
_CDN_HOST_HINTS = ("twimg.com", "video.twimg.com", "pbs.twimg.com")
_DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def extract_tweet_id(url: str) -> str | None:
    if not url:
        return None
    m = _TWEET_RE.search(url)
    return m.group(2) if m else None


def extract_username(url: str) -> str | None:
    m = _TWEET_RE.search(url or "")
    return m.group(1) if m else None


def normalize_twitter_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    # Expand common shorteners when possible.
    try:
        if "t.co/" in url:
            r = _http_get(url, timeout=10, allow_redirects=True)
            final = getattr(r, "url", "") or url
            if final and final != url:
                url = final
    except Exception:
        pass
    url = url.split("?", 1)[0]
    url = url.replace("https://mobile.twitter.com", "https://twitter.com")
    url = url.replace("https://x.com", "https://twitter.com")
    url = url.replace("https://fxtwitter.com", "https://twitter.com")
    url = url.replace("https://fixupx.com", "https://twitter.com")
    url = url.replace("https://vxtwitter.com", "https://twitter.com")
    return url


def _http_get(url: str, headers: dict | None = None, timeout: int = 18, allow_redirects: bool = True):
    hdrs = {
        "User-Agent": _DESKTOP_UA,
        "Accept": "text/html,application/json,image/*,video/*,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://twitter.com/",
    }
    if headers:
        hdrs.update(headers)
    try:
        from curl_cffi import requests as creq
        return creq.get(url, headers=hdrs, impersonate="chrome", timeout=timeout, allow_redirects=allow_redirects)
    except Exception:
        pass
    import requests
    return requests.get(url, headers=hdrs, timeout=timeout, allow_redirects=allow_redirects)


def _unescape(value: str) -> str:
    if not value:
        return value
    try:
        return value.encode("utf-8").decode("unicode_escape").replace("\\/", "/")
    except Exception:
        return value.replace("\\/", "/").replace("&amp;", "&")


def _probe_media_url(url: str | None, *, kind: str = "media") -> tuple[bool, int, str]:
    if not url or not str(url).startswith("http"):
        return False, 0, ""
    try:
        r = _http_get(url, headers={"Range": "bytes=0-2048", "Accept": "video/*,image/*,*/*"}, timeout=10)
        status = int(getattr(r, "status_code", 0) or 0)
        ctype = (getattr(r, "headers", {}) or {}).get("Content-Type", "").lower()
        try:
            size = int((getattr(r, "headers", {}) or {}).get("Content-Length") or 0)
        except Exception:
            size = 0
        if status not in (200, 206):
            return False, size, ctype
        if any(x in ctype for x in ("text/html", "application/json")):
            return False, size, ctype
        if kind == "video" and ctype and not any(x in ctype for x in ("video", "octet-stream", "binary", "mpegurl")):
            return False, size, ctype
        if kind == "image" and ctype and not ctype.startswith("image/"):
            return False, size, ctype
        return True, size, ctype
    except Exception as exc:
        logger.debug("Twitter media probe failed for %s: %s", url, exc)
        return False, 0, ""


def _best_video_format(formats: list[dict]) -> dict | None:
    candidates = []
    for fmt in formats or []:
        u = fmt.get("url")
        if not u:
            continue
        ext = (fmt.get("ext") or "").lower()
        proto = (fmt.get("protocol") or "").lower()
        if ext == "m3u8" or "m3u8" in u or proto == "m3u8_native":
            # Prefer direct mp4 for Telegram/direct download; keep m3u8 as weak fallback only.
            weight = 50
        else:
            weight = 200
        height = int(fmt.get("height") or 0)
        tbr = int(fmt.get("tbr") or fmt.get("vbr") or 0)
        candidates.append((weight + height * 3 + tbr, fmt))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: x[0], reverse=True)[0][1]


def _base_result(url: str, tweet_id: str | None, source: str) -> dict:
    return {
        "title": "Twitter/X Media",
        "uploader": extract_username(url) or "Twitter User",
        "duration": "Unknown",
        "duration_secs": 0,
        "thumbnail": "",
        "platform": "Twitter/X",
        "qualities": [],
        "audio_formats": [],
        "image_url": None,
        "album_items": [],
        "url": url,
        "webpage_url": url,
        "ext": "mp4",
        "media_id": tweet_id or "",
        "source": source,
    }


def _from_ytdlp(url: str) -> dict | None:
    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 20,
            "extractor_retries": 2,
            "http_headers": {"User-Agent": _DESKTOP_UA, "Referer": "https://twitter.com/"},
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        tweet_id = str(info.get("id") or extract_tweet_id(url) or "")
        result = _base_result(url, tweet_id, "yt-dlp")
        result.update({
            "title": (info.get("title") or info.get("description") or "Twitter/X Media").split("\n")[0][:200],
            "uploader": info.get("uploader") or info.get("channel") or result["uploader"],
            "duration_secs": int(info.get("duration") or 0),
            "duration": f"{int(info.get('duration') or 0)}s" if info.get("duration") else "Unknown",
            "thumbnail": info.get("thumbnail") or "",
            "webpage_url": info.get("webpage_url") or url,
        })
        entries = info.get("entries") or []
        if entries:
            album = []
            for i, e in enumerate(entries[:10]):
                if not isinstance(e, dict):
                    continue
                best = _best_video_format(e.get("formats") or [])
                if best and best.get("url"):
                    album.append({
                        "title": e.get("title") or f"tweet_media_{i + 1}",
                        "url": best["url"],
                        "thumbnail": e.get("thumbnail") or "",
                        "image_url": None,
                        "type": "video",
                        "id": str(e.get("id") or i),
                        "play_url": best["url"],
                    })
                elif e.get("url") or e.get("thumbnail"):
                    img = e.get("url") or e.get("thumbnail")
                    album.append({
                        "title": e.get("title") or f"tweet_media_{i + 1}",
                        "url": img,
                        "thumbnail": img,
                        "image_url": img,
                        "type": "image",
                        "id": str(e.get("id") or i),
                    })
            if len(album) >= 2:
                result["media_type"] = "album"
                result["album_items"] = album
                result["thumbnail"] = album[0].get("thumbnail") or album[0].get("url") or ""
                result["ext"] = "jpg"
                return result
        best = _best_video_format(info.get("formats") or [])
        if best and best.get("url"):
            result["media_type"] = "video"
            result["play_url"] = best["url"]
            result["ext"] = "mp4"
            result["qualities"] = [{"label": f"{best.get('height') or 'Best'}p", "format_id": str(best.get("format_id") or "best"), "has_audio": True}]
            return result
        direct = info.get("url")
        ext = (info.get("ext") or "").lower()
        if direct and ext in ("jpg", "jpeg", "png", "webp"):
            result["media_type"] = "image"
            result["image_url"] = direct
            result["thumbnail"] = direct
            result["ext"] = ext
            return result
    except Exception as exc:
        logger.info("Twitter yt-dlp failed: %s", exc)
    return None


def _from_vx_api(url: str) -> dict | None:
    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        return None
    endpoints = [
        f"https://api.vxtwitter.com/Twitter/status/{tweet_id}",
        f"https://api.vxtwitter.com/status/{tweet_id}",
        f"https://api.fxtwitter.com/status/{tweet_id}",
    ]
    for endpoint in endpoints:
        try:
            r = _http_get(endpoint, headers={"Accept": "application/json"}, timeout=16)
            if int(getattr(r, "status_code", 0) or 0) != 200:
                continue
            data = r.json()
            if not isinstance(data, dict):
                continue
            tweet = data.get("tweet") if isinstance(data.get("tweet"), dict) else data
            media = tweet.get("media_extended") or tweet.get("media") or []
            if isinstance(media, dict):
                media = list(media.values())
            title = (tweet.get("text") or tweet.get("description") or "Twitter/X Media").split("\n")[0][:200]
            uploader = ((tweet.get("author") or {}).get("screen_name") if isinstance(tweet.get("author"), dict) else None) or tweet.get("user_name") or extract_username(url) or "Twitter User"
            result = _base_result(url, tweet_id, "vxtwitter_api")
            result.update({"title": title, "uploader": uploader, "webpage_url": normalize_twitter_url(url)})
            album = []
            best_video = None
            best_score = -1
            for i, item in enumerate(media or []):
                if not isinstance(item, dict):
                    continue
                mtype = (item.get("type") or item.get("format") or "").lower()
                video_url = item.get("url") or item.get("video_url") or item.get("source")
                thumb = item.get("thumbnail_url") or item.get("thumb") or item.get("image_url") or item.get("url") or ""
                if mtype in ("video", "gif", "animated_gif") or (video_url and ".mp4" in video_url):
                    variants = item.get("variants") or []
                    if variants:
                        for v in variants:
                            vu = v.get("url") if isinstance(v, dict) else None
                            if not vu:
                                continue
                            score = int(v.get("bitrate") or 0) if isinstance(v, dict) else 0
                            if ".mp4" in vu:
                                score += 500
                            if score > best_score:
                                best_score, best_video = score, vu
                    if video_url and (best_video is None or ".mp4" in video_url):
                        best_video = video_url
                    if video_url:
                        album.append({"title": f"tweet_video_{i + 1}", "url": video_url, "thumbnail": thumb, "image_url": None, "type": "video", "id": str(i), "play_url": video_url})
                else:
                    img = item.get("url") or item.get("image_url") or item.get("media_url_https") or item.get("media_url")
                    if img:
                        if "?" not in img and "pbs.twimg.com" in img:
                            img = img + "?format=jpg&name=orig"
                        album.append({"title": f"tweet_image_{i + 1}", "url": img, "thumbnail": img, "image_url": img, "type": "image", "id": str(i)})
            if best_video:
                result["media_type"] = "video"
                result["play_url"] = best_video
                result["thumbnail"] = album[0].get("thumbnail", "") if album else ""
                result["qualities"] = [{"label": "Best", "format_id": "best", "has_audio": True}]
                return result
            if len(album) >= 2:
                result["media_type"] = "album"
                result["album_items"] = album[:10]
                result["thumbnail"] = album[0].get("thumbnail") or album[0].get("url") or ""
                result["ext"] = "jpg"
                return result
            if album:
                item = album[0]
                result["media_type"] = item["type"]
                if item["type"] == "image":
                    result["image_url"] = item["image_url"]
                    result["thumbnail"] = item["image_url"]
                    result["ext"] = "jpg"
                else:
                    result["play_url"] = item["play_url"]
                    result["thumbnail"] = item.get("thumbnail") or ""
                    result["qualities"] = [{"label": "Best", "format_id": "best", "has_audio": True}]
                return result
        except Exception as exc:
            logger.debug("Twitter mirror API failed %s: %s", endpoint, exc)
    return None


def _from_html(url: str) -> dict | None:
    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        return None
    mirror_urls = [
        url,
        url.replace("https://twitter.com", "https://fxtwitter.com"),
        url.replace("https://twitter.com", "https://vxtwitter.com"),
    ]
    for page in mirror_urls:
        try:
            r = _http_get(page, timeout=16)
            html = getattr(r, "text", "") or ""
            if len(html) < 200:
                continue
            title = "Twitter/X Media"
            m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html)
            if m:
                title = _unescape(m.group(1)).split("\n")[0][:200]
            videos = []
            images = []
            for pat in (
                r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+)',
                r'"video_url"\s*:\s*"(https:[^"\\]+)',
                r'(https://video\.twimg\.com/[^"\'<>\\]+\.mp4[^"\'<>\\]*)',
            ):
                for mm in re.finditer(pat, html):
                    videos.append(_unescape(mm.group(1)))
            for pat in (
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'(https://pbs\.twimg\.com/media/[^"\'<>\\]+)',
            ):
                for mm in re.finditer(pat, html):
                    img = _unescape(mm.group(1))
                    if "?" not in img and "pbs.twimg.com" in img:
                        img += "?format=jpg&name=orig"
                    images.append(img)
            result = _base_result(url, tweet_id, "html_meta")
            result["title"] = title
            if videos:
                result["media_type"] = "video"
                result["play_url"] = videos[0]
                result["thumbnail"] = images[0] if images else ""
                result["qualities"] = [{"label": "Best", "format_id": "best", "has_audio": True}]
                return result
            uniq_images = []
            for img in images:
                if img not in uniq_images:
                    uniq_images.append(img)
            if len(uniq_images) >= 2:
                result["media_type"] = "album"
                result["album_items"] = [{"title": f"tweet_image_{i + 1}", "url": img, "thumbnail": img, "image_url": img, "type": "image", "id": str(i)} for i, img in enumerate(uniq_images[:10])]
                result["thumbnail"] = uniq_images[0]
                result["ext"] = "jpg"
                return result
            if uniq_images:
                result["media_type"] = "image"
                result["image_url"] = uniq_images[0]
                result["thumbnail"] = uniq_images[0]
                result["ext"] = "jpg"
                return result
        except Exception as exc:
            logger.debug("Twitter HTML fallback failed for %s: %s", page, exc)
    return None


def _score_result(result: dict | None) -> int:
    if not result:
        return -1
    score = 0
    source = result.get("source") or ""
    media_type = result.get("media_type") or ""
    if source == "yt-dlp":
        score += 260
    elif source == "vxtwitter_api":
        score += 230
    elif source == "html_meta":
        score += 120
    if media_type == "video" and result.get("play_url"):
        score += 650
        ok, size, ctype = _probe_media_url(result.get("play_url"), kind="video")
        result["probe"] = {"ok": ok, "size": size, "content_type": ctype}
        if ok:
            score += 700
        host = urlparse(result.get("play_url") or "").netloc.lower()
        if any(h in host for h in _CDN_HOST_HINTS):
            score += 120
        if ".mp4" in (result.get("play_url") or ""):
            score += 100
    elif media_type == "image" and result.get("image_url"):
        score += 500
        ok, size, ctype = _probe_media_url(result.get("image_url"), kind="image")
        result["probe"] = {"ok": ok, "size": size, "content_type": ctype}
        if ok:
            score += 450
    elif media_type == "album" and result.get("album_items"):
        items = result.get("album_items") or []
        score += 420 + min(len(items), 10) * 60
        valid = 0
        for item in items[:4]:
            u = item.get("play_url") or item.get("image_url") or item.get("url")
            ok, _, _ = _probe_media_url(u, kind="video" if item.get("type") == "video" else "image")
            if ok:
                valid += 1
        score += valid * 120
    if result.get("title") and result.get("title") != "Twitter/X Media":
        score += 40
    return score


def scrape_twitter(url: str) -> dict | None:
    normalized = normalize_twitter_url(url)
    tweet_id = extract_tweet_id(normalized)
    if not tweet_id:
        return None

    providers = (
        ("yt-dlp", lambda: _from_ytdlp(normalized)),
        ("vxtwitter", lambda: _from_vx_api(normalized)),
        ("html", lambda: _from_html(normalized)),
    )
    results: list[dict] = []
    failures: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        futures = {pool.submit(fn): name for name, fn in providers}
        for future in as_completed(futures, timeout=_PROVIDER_TIMEOUT):
            name = futures[future]
            try:
                result = future.result(timeout=1)
                if result and (result.get("play_url") or result.get("image_url") or result.get("album_items")):
                    result["source"] = result.get("source") or name
                    result["provider_score"] = _score_result(result)
                    results.append(result)
                else:
                    failures.append({"provider": name, "status": "empty"})
            except Exception as exc:
                failures.append({"provider": name, "status": "error", "error": str(exc)[:160]})

    if not results:
        return {
            "platform": "Twitter/X",
            "media_type": "unknown",
            "requires_login": True,
            "error_reason": "login_required_or_private_or_deleted",
            "url": normalized,
            "webpage_url": normalized,
            "media_id": tweet_id,
            "provider_candidates": failures,
        }

    results.sort(key=lambda item: int(item.get("provider_score") or 0), reverse=True)
    best = results[0]
    best["provider_candidates"] = [
        {"provider": r.get("source"), "score": r.get("provider_score"), "media_type": r.get("media_type")}
        for r in results
    ] + failures
    best["engine_profile"] = "yt-dlp+vxtwitter+html+cdn-probe"
    logger.info(
        "Twitter/X selected provider=%s score=%s type=%s candidates=%s",
        best.get("source"), best.get("provider_score"), best.get("media_type"), len(best.get("provider_candidates") or []),
    )
    return best


__all__ = ["scrape_twitter", "normalize_twitter_url", "extract_tweet_id"]
