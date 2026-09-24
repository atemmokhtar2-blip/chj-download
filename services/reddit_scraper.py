from __future__ import annotations

import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from utils.logger import download_logger, error_logger

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
TIMEOUT = 14
CDN_HINTS = ("v.redd.it", "redd.it", "redditmedia.com", "reddituploads.com", "preview.redd.it", "i.redd.it")


def _headers(referer: str = "https://www.reddit.com/") -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Referer": referer,
    }


def _get(url: str, *, mobile: bool = False) -> requests.Response:
    headers = _headers()
    if mobile:
        headers["User-Agent"] = MOBILE_UA
    return requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)


def normalize_reddit_url(url: str) -> str:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    try:
        resp = requests.get(url, headers=_headers(), timeout=8, allow_redirects=True, stream=True)
        if resp.url:
            url = resp.url
        resp.close()
    except Exception:
        pass
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")
    if host in {"old.reddit.com", "new.reddit.com", "np.reddit.com", "m.reddit.com"}:
        parsed = parsed._replace(netloc="www.reddit.com")
    if host == "redd.it":
        # Keep short URL if expansion did not happen; Reddit JSON handles it poorly, yt-dlp fallback can still use it.
        return urlunparse(parsed._replace(scheme="https"))
    return urlunparse(parsed._replace(scheme="https"))


def _json_url(url: str) -> str:
    clean = url.split("?", 1)[0].rstrip("/")
    if clean.endswith(".json"):
        return clean
    return clean + ".json?raw_json=1"


def _unescape_url(value: Any) -> str | None:
    if not value:
        return None
    s = html.unescape(str(value)).replace("\\/", "/")
    s = s.replace("&amp;", "&")
    if s.startswith("//"):
        s = "https:" + s
    return s if s.startswith("http") else None


def _probe_media_url(url: str | None) -> dict[str, Any]:
    if not url:
        return {"ok": False, "reason": "empty"}
    headers = _headers()
    headers["Range"] = "bytes=0-1023"
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=10, allow_redirects=True)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        clen = int(resp.headers.get("Content-Length") or 0)
        ok = resp.status_code in (200, 206) and not any(x in ctype for x in ("text/html", "application/json"))
        if resp.status_code in (403, 404, 410):
            ok = False
        if not ok and resp.status_code in (200, 206):
            try:
                chunk = next(resp.iter_content(256), b"")
                if b"<html" in chunk.lower() or b"error" in chunk.lower():
                    ok = False
            except Exception:
                pass
        resp.close()
        return {"ok": ok, "status": resp.status_code, "content_type": ctype, "size": clen, "final_url": resp.url}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:120]}


def _best_preview_image(data: dict[str, Any]) -> str | None:
    candidates: list[str] = []
    for image in (data.get("preview") or {}).get("images") or []:
        src = (image.get("source") or {}).get("url")
        if src:
            candidates.append(src)
        for res in image.get("resolutions") or []:
            if res.get("url"):
                candidates.append(res["url"])
    if data.get("url_overridden_by_dest"):
        candidates.append(data["url_overridden_by_dest"])
    if data.get("thumbnail") and str(data.get("thumbnail")).startswith("http"):
        candidates.append(data["thumbnail"])
    return _unescape_url(candidates[-1]) if candidates else None


def _extract_gallery(data: dict[str, Any]) -> list[dict[str, Any]]:
    gallery_data = data.get("gallery_data") or {}
    metadata = data.get("media_metadata") or {}
    items: list[dict[str, Any]] = []
    for item in gallery_data.get("items") or []:
        media_id = item.get("media_id")
        meta = metadata.get(media_id) or {}
        url = None
        media_type = "image"
        if meta.get("s", {}).get("u"):
            url = _unescape_url(meta["s"]["u"])
        elif meta.get("s", {}).get("gif"):
            url = _unescape_url(meta["s"]["gif"])
            media_type = "video"
        elif meta.get("s", {}).get("mp4"):
            url = _unescape_url(meta["s"]["mp4"])
            media_type = "video"
        if not url and media_id:
            ext = "jpg"
            mime = meta.get("m") or ""
            if "png" in mime:
                ext = "png"
            elif "gif" in mime:
                ext = "gif"
            url = f"https://i.redd.it/{media_id}.{ext}"
        if url:
            items.append({"url": url, "type": media_type, "width": meta.get("s", {}).get("x"), "height": meta.get("s", {}).get("y")})
    return items


def _video_candidates_from_data(data: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    media_blocks = [data.get("media") or {}, data.get("secure_media") or {}]
    crosspost = (data.get("crosspost_parent_list") or [{}])[0]
    if crosspost:
        media_blocks += [crosspost.get("media") or {}, crosspost.get("secure_media") or {}]
    for media in media_blocks:
        rv = media.get("reddit_video") or {}
        for key in ("fallback_url", "scrubber_media_url", "dash_url", "hls_url"):
            u = _unescape_url(rv.get(key))
            if u and u not in candidates:
                candidates.append(u)
    direct = _unescape_url(data.get("url_overridden_by_dest") or data.get("url"))
    if direct and any(h in direct for h in CDN_HINTS):
        candidates.append(direct)
    return candidates


def _extract_post_from_listing(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if not isinstance(payload, dict):
        return None
    children = (((payload.get("data") or {}).get("children")) or [])
    if not children:
        return None
    data = children[0].get("data") or {}
    if data.get("crosspost_parent_list") and not (data.get("media") or data.get("gallery_data")):
        parent = data["crosspost_parent_list"][0]
        merged = dict(parent)
        merged.update({k: v for k, v in data.items() if k in ("title", "permalink", "subreddit", "author") and v})
        return merged
    return data


def _from_reddit_json(url: str) -> dict[str, Any] | None:
    resp = _get(_json_url(url))
    if resp.status_code in (401, 403, 451):
        return {"requires_login": True, "error_reason": "login_required_or_private", "source": "reddit-json"}
    if resp.status_code >= 400:
        return None
    try:
        payload = resp.json()
    except Exception:
        return None
    data = _extract_post_from_listing(payload)
    if not data:
        return None

    title = data.get("title") or "Reddit media"
    webpage_url = "https://www.reddit.com" + data.get("permalink", "") if data.get("permalink") else url
    base = {
        "platform": "Reddit",
        "title": title,
        "uploader": data.get("author"),
        "subreddit": data.get("subreddit"),
        "webpage_url": webpage_url,
        "thumbnail": _best_preview_image(data),
        "source": "reddit-json",
        "raw_kind": data.get("post_hint") or data.get("domain"),
    }

    gallery = _extract_gallery(data)
    if gallery:
        return base | {
            "media_type": "album",
            "album_items": gallery,
            "images": [x["url"] for x in gallery if x.get("type") == "image"],
            "image_url": gallery[0]["url"],
        }

    videos = _video_candidates_from_data(data)
    playable: list[dict[str, Any]] = []
    for u in videos:
        probe = _probe_media_url(u)
        if probe.get("ok") or any(ext in u for ext in (".mp4", ".m3u8", ".mpd")):
            playable.append({"url": u, "probe": probe})
    if playable:
        def rank(item: dict[str, Any]) -> tuple[int, int]:
            u = item["url"]
            score = 0
            if ".mp4" in u:
                score += 500
            if "DASH_" in u or "fallback" in u:
                score += 220
            if ".m3u8" in u:
                score += 60
            if ".mpd" in u:
                score += 40
            score += int(item.get("probe", {}).get("size") or 0) // 1024
            height = 0
            m = re.search(r"DASH_(\d+)\.mp4", u)
            if m:
                height = int(m.group(1))
                score += height
            return score, height
        best = sorted(playable, key=rank, reverse=True)[0]
        return base | {
            "media_type": "video",
            "play_url": best["url"],
            "formats": [{"format_id": "reddit_direct", "url": x["url"], "ext": "mp4" if ".mp4" in x["url"] else "web", "quality": "direct"} for x in playable],
            "cdn_probe": best.get("probe"),
        }

    image = _best_preview_image(data)
    direct = _unescape_url(data.get("url_overridden_by_dest") or data.get("url"))
    if direct and re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", direct, re.I):
        image = direct
    if image:
        return base | {"media_type": "image", "image_url": image, "images": [image]}
    if data.get("is_self"):
        return base | {"requires_login": False, "error_reason": "text_post_no_media"}
    return base


def _from_oembed(url: str) -> dict[str, Any] | None:
    try:
        endpoint = "https://www.reddit.com/oembed?url=" + requests.utils.quote(url, safe="")
        resp = _get(endpoint)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        thumb = _unescape_url(data.get("thumbnail_url"))
        if thumb:
            return {
                "platform": "Reddit",
                "title": data.get("title") or "Reddit media",
                "thumbnail": thumb,
                "image_url": thumb,
                "images": [thumb],
                "media_type": "image",
                "source": "reddit-oembed",
                "webpage_url": url,
            }
    except Exception:
        return None
    return None


def _from_ytdlp(url: str) -> dict[str, Any] | None:
    try:
        import yt_dlp
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": TIMEOUT,
            "http_headers": _headers(),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        formats = info.get("formats") or []
        best_url = info.get("url")
        best_score = -1
        for f in formats:
            u = f.get("url")
            if not u:
                continue
            score = int(f.get("height") or 0) + int(f.get("tbr") or 0)
            if f.get("ext") == "mp4":
                score += 500
            if score > best_score:
                best_score = score
                best_url = u
        thumbs = info.get("thumbnails") or []
        thumb = info.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)
        if best_url:
            return {
                "platform": "Reddit",
                "title": info.get("title") or "Reddit video",
                "uploader": info.get("uploader") or info.get("channel"),
                "duration": info.get("duration"),
                "thumbnail": thumb,
                "play_url": best_url,
                "formats": formats,
                "media_type": "video",
                "source": "yt-dlp",
                "webpage_url": info.get("webpage_url") or url,
            }
    except Exception as exc:
        error_logger.debug("Reddit yt-dlp provider failed: %s", exc)
    return None


def _score_result(result: dict[str, Any] | None) -> int:
    if not result:
        return -1
    if result.get("requires_login"):
        return 5
    score = 0
    src = result.get("source")
    score += {"reddit-json": 280, "yt-dlp": 230, "reddit-oembed": 90}.get(src, 20)
    mt = result.get("media_type")
    if mt == "video" and result.get("play_url"):
        score += 900
        u = result["play_url"]
        if ".mp4" in u:
            score += 260
        if "v.redd.it" in u:
            score += 140
        if result.get("cdn_probe", {}).get("ok"):
            score += 250
    elif mt == "album" and result.get("album_items"):
        score += 780 + len(result["album_items"]) * 35
    elif mt == "image" and result.get("image_url"):
        score += 620
    if result.get("thumbnail"):
        score += 30
    return score


def scrape_reddit(url: str) -> dict[str, Any] | None:
    started = time.time()
    normalized = normalize_reddit_url(url)
    providers = [
        ("reddit-json", _from_reddit_json, normalized),
        ("yt-dlp", _from_ytdlp, normalized),
        ("reddit-oembed", _from_oembed, normalized),
    ]
    results: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_map = {executor.submit(func, arg): name for name, func, arg in providers}
        for future in as_completed(future_map, timeout=TIMEOUT + 8):
            name = future_map[future]
            try:
                result = future.result()
                score = _score_result(result)
                candidates.append({"provider": name, "ok": bool(result), "score": score, "media_type": result.get("media_type") if result else None, "error": result.get("error_reason") if result else None})
                if result:
                    result.setdefault("source", name)
                    result["provider_score"] = score
                    results.append(result)
            except Exception as exc:
                candidates.append({"provider": name, "ok": False, "score": -1, "error": str(exc)[:120]})

    if not results:
        return None
    best = sorted(results, key=_score_result, reverse=True)[0]
    best["provider_score"] = _score_result(best)
    best["provider_candidates"] = sorted(candidates, key=lambda x: x.get("score", -1), reverse=True)
    best["engine_profile"] = "reddit-json+ytdlp+oembed+cdn-probe"
    best["resolved_url"] = normalized
    download_logger.info("RedditEnginePro selected source=%s score=%s elapsed=%.2fs", best.get("source"), best.get("provider_score"), time.time() - started)
    return best
