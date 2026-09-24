"""
Pinterest pin extractor — production stack (2026).

Priority order (strongest first):
  1. Pinterest PinResource JSON API  (same endpoint yt-dlp uses)
  2. gallery-dl extractor
  3. HTML / __PWS_DATA__ scrape with originals upgrade

Handles: image pins, video pins, GIFs, carousels, pin.it short links.
Always prefers i.pinimg.com/originals/ when the variant exists.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

logger = logging.getLogger(__name__)

PLACEHOLDER_HASHES = (
    "d53b014d86a6b6761bf649a0ed813c2b",
)
SIZE_PRIORITY = ("originals", "1200x", "736x", "564x", "474x", "236x")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.pinterest.com/",
    "X-Pinterest-PWS-Handler": "www/pin/[id].js",
    "X-Requested-With": "XMLHttpRequest",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def expand_pin_url(url: str, session: requests.Session | None = None) -> str:
    """Resolve pin.it / mobile / regional hosts to canonical pinterest.com/pin/<id>/."""
    if not url:
        return url
    s = session or _session()
    try:
        if "pin.it" in url or "/pin/" not in url:
            r = s.get(url, timeout=15, allow_redirects=True)
            url = r.url
    except Exception:
        pass
    # Force www host for API consistency
    url = url.replace("://pinterest.", "://www.pinterest.")
    for host in (
        "https://ar.pinterest.com", "https://uk.pinterest.com",
        "https://fr.pinterest.com", "https://de.pinterest.com",
        "https://jp.pinterest.com", "https://br.pinterest.com",
        "https://in.pinterest.com", "https://m.pinterest.com",
    ):
        if url.startswith(host):
            url = "https://www.pinterest.com" + url[len(host):]
            break
    return url


def extract_pin_id(url: str) -> str | None:
    m = re.search(r"/pin/(?:[\w-]+--)?(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"(\d{10,})", url)
    return m.group(1) if m else None


def _is_placeholder(u: str) -> bool:
    return any(h in (u or "") for h in PLACEHOLDER_HASHES)


def upgrade_to_originals(image_url: str) -> str:
    """Rewrite size bucket → originals/ (Pinterest serves 404 if missing — caller may probe)."""
    if not image_url or "i.pinimg.com" not in image_url:
        return image_url
    return re.sub(
        r"(https://i\.pinimg\.com/)(?:originals|\d+x\d*|\d+x)/",
        r"\1originals/",
        image_url,
        count=1,
    )


def _best_image_from_images_dict(images: dict) -> str | None:
    if not isinstance(images, dict):
        return None
    for key in SIZE_PRIORITY:
        node = images.get(key)
        if isinstance(node, dict) and node.get("url") and not _is_placeholder(node["url"]):
            return node["url"]
        if isinstance(node, str) and node.startswith("http") and not _is_placeholder(node):
            return node
    # any remaining
    for node in images.values():
        if isinstance(node, dict):
            u = node.get("url")
            if u and not _is_placeholder(u):
                return u
        elif isinstance(node, str) and node.startswith("http") and not _is_placeholder(node):
            return node
    return None


def _best_video_url(videos: Any) -> str | None:
    """Pick highest-quality mp4 from pin videos structure."""
    if not videos:
        return None
    candidates: list[tuple[int, str]] = []
    # Shape A: {"video_list": {"V_720P": {"url": ...}, ...}}
    video_list = None
    if isinstance(videos, dict):
        video_list = videos.get("video_list") or videos.get("items") or videos
    if isinstance(video_list, dict):
        for key, meta in video_list.items():
            if not isinstance(meta, dict):
                continue
            u = meta.get("url") or meta.get("mp4")
            if not u or not str(u).startswith("http"):
                continue
            h = int(meta.get("height") or meta.get("width") or 0)
            # Prefer non-hls
            score = h
            if ".m3u8" in u:
                score -= 10_000
            candidates.append((score, u))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    return None


def _probe_media_url(url: str, session: requests.Session | None = None, timeout: int = 10) -> dict:
    """Validate a Pinterest CDN URL without downloading the whole file."""
    result = {"ok": False, "status": None, "content_type": "", "content_length": 0}
    if not url or not str(url).startswith("http"):
        return result
    s = session or _session()
    headers = dict(_HEADERS)
    headers["Range"] = "bytes=0-1023"
    try:
        r = s.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
        result["status"] = r.status_code
        ctype = (r.headers.get("Content-Type") or "").lower()
        clen = int(r.headers.get("Content-Length") or 0)
        result["content_type"] = ctype
        result["content_length"] = clen
        if r.status_code not in (200, 206):
            return result
        if "text/html" in ctype or "application/json" in ctype:
            return result
        if any(token in ctype for token in ("image/", "video/", "application/octet-stream")):
            result["ok"] = True
            return result
        # Some Pinterest CDN responses omit a useful content-type; trust known CDN hosts.
        if any(host in url for host in ("pinimg.com", "pin.it", "pinterest.com/videos")):
            result["ok"] = True
    except Exception as exc:
        logger.info("Pinterest probe failed for %s: %s", url[:120], exc)
    return result


def _primary_media_url(result: dict) -> str | None:
    if not isinstance(result, dict):
        return None
    return result.get("play_url") or result.get("image_url") or result.get("thumbnail")


def _score_result(result: dict, session: requests.Session | None = None) -> int:
    """Rank Pinterest candidates. Probe direct URLs and prefer playable/high-res media."""
    if not isinstance(result, dict):
        return -1
    score = 0
    source = result.get("source") or ""
    media_type = result.get("media_type") or ""
    source_bonus = {
        "pinterest_downloader": 380,
        "pin_resource": 340,
        "gallery_dl": 260,
        "html_json": 200,
        "html": 120,
    }.get(source, 80)
    score += source_bonus

    if media_type == "video" and result.get("play_url"):
        score += 1000
        if ".mp4" in result.get("play_url", ""):
            score += 220
        if ".m3u8" in result.get("play_url", ""):
            score -= 180
    elif media_type == "album" and result.get("album_items"):
        score += 850 + min(len(result.get("album_items") or []), 20) * 35
    elif media_type == "image" and result.get("image_url"):
        score += 700

    media_url = _primary_media_url(result)
    if media_url:
        if "i.pinimg.com/originals/" in media_url:
            score += 180
        elif "i.pinimg.com/1200x/" in media_url or "i.pinimg.com/736x/" in media_url:
            score += 90
        if "pinimg.com" in media_url:
            score += 80
        probe = _probe_media_url(media_url, session=session)
        result["cdn_probe"] = probe
        if probe.get("ok"):
            score += 600
            ctype = probe.get("content_type") or ""
            if media_type == "video" and "video" in ctype:
                score += 120
            if media_type in ("image", "album") and "image" in ctype:
                score += 120
            size = int(probe.get("content_length") or 0)
            if size:
                score += min(size // 100_000, 120)
        else:
            score -= 260

    album = result.get("album_items") or []
    if album:
        ok_items = 0
        for item in album[:8]:
            u = item.get("url") or item.get("image_url") or item.get("thumbnail")
            if u and _probe_media_url(u, session=session, timeout=6).get("ok"):
                ok_items += 1
        result["album_probe_ok"] = ok_items
        score += ok_items * 60
        if ok_items == 0:
            score -= 220

    result["provider_score"] = score
    return score


def _pin_from_api(pin_id: str, session: requests.Session) -> dict | None:
    """Call PinResource — the same unauthenticated JSON endpoint yt-dlp uses."""
    endpoint = "https://www.pinterest.com/resource/PinResource/get/"
    options = {
        "field_set_key": "unauth_react_main_pin",
        "id": pin_id,
    }
    try:
        r = session.get(
            endpoint,
            params={"data": json.dumps({"options": options})},
            timeout=20,
        )
        if r.status_code != 200:
            logger.info("PinResource HTTP %s for %s", r.status_code, pin_id)
            return None
        body = r.json()
        data = (body.get("resource_response") or {}).get("data")
        if not isinstance(data, dict):
            return None
        return data
    except Exception as e:
        logger.info("PinResource failed for %s: %s", pin_id, e)
        return None


def _result_from_pin_data(pin: dict, url: str) -> dict:
    pin_id = str(pin.get("id") or "")
    title = (
        pin.get("title")
        or pin.get("grid_title")
        or pin.get("description")
        or "Pinterest Pin"
    )
    if isinstance(title, str):
        title = title.strip()[:200] or "Pinterest Pin"
    uploader = (
        (pin.get("pinner") or {}).get("username")
        or (pin.get("native_creator") or {}).get("username")
        or (pin.get("closeup_attribution") or {}).get("username")
        or "Pinterest User"
    )

    images = pin.get("images") or {}
    image_url = _best_image_from_images_dict(images)
    if image_url:
        upgraded = upgrade_to_originals(image_url)
        # Prefer originals when we already had an originals key
        if images.get("originals"):
            image_url = upgraded
        else:
            image_url = upgraded  # try originals path; CDN often still serves it

    video_url = _best_video_url(pin.get("videos"))
    # Story pins / idea pins carousel
    album_items: list[dict] = []
    carousel = (pin.get("carousel_data") or {}).get("carousel_slots") or []
    for i, slot in enumerate(carousel):
        if not isinstance(slot, dict):
            continue
        slot_images = slot.get("images") or {}
        slot_url = _best_image_from_images_dict(slot_images)
        if not slot_url:
            continue
        slot_url = upgrade_to_originals(slot_url)
        album_items.append({
            "title": f"{title}_{i + 1}",
            "url": slot_url,
            "thumbnail": slot_url,
            "image_url": slot_url,
            "type": "image",
            "id": str(slot.get("id") or i),
        })

    if video_url:
        media_type = "video"
        qualities = [{"label": "Best", "format_id": "best", "has_audio": True}]
    elif len(album_items) >= 2:
        media_type = "album"
        qualities = []
    else:
        media_type = "image"
        qualities = []
        if image_url and not album_items:
            album_items = []

    ext = "mp4" if media_type == "video" else "jpg"
    if image_url and "." in image_url.split("?")[0]:
        e = image_url.split("?")[0].rsplit(".", 1)[-1].lower()
        if e in ("jpg", "jpeg", "png", "webp", "gif"):
            ext = e

    return {
        "title": title,
        "uploader": str(uploader),
        "duration": "Unknown",
        "duration_secs": 0,
        "thumbnail": image_url or (album_items[0]["image_url"] if album_items else ""),
        "platform": "Pinterest",
        "media_type": media_type,
        "qualities": qualities,
        "audio_formats": [],
        "image_url": image_url if media_type == "image" else None,
        "album_items": album_items if media_type == "album" else [],
        "url": url,
        "webpage_url": f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else url,
        "ext": ext,
        "media_id": pin_id,
        "play_url": video_url,  # direct CDN for video when yt-dlp fails
        "source": "pin_resource",
    }


def _from_gallery_dl(url: str) -> dict | None:
    try:
        from services.gallery_extractor import extract_gallery_items
        items = extract_gallery_items(url, max_items=10)
        if not items:
            return None
        if len(items) >= 2:
            album = []
            for i, g in enumerate(items):
                u = g.get("url") or ""
                if "pinimg" in u:
                    u = upgrade_to_originals(u)
                album.append({
                    "title": g.get("title") or f"pin_{i + 1}",
                    "url": u,
                    "thumbnail": u,
                    "image_url": u if g.get("type") == "image" else None,
                    "type": g.get("type") or "image",
                    "id": str(g.get("id") or i),
                })
            return {
                "title": items[0].get("title") or "Pinterest Album",
                "uploader": "Pinterest User",
                "duration": "Unknown",
                "duration_secs": 0,
                "thumbnail": album[0]["url"],
                "platform": "Pinterest",
                "media_type": "album",
                "qualities": [],
                "audio_formats": [],
                "image_url": None,
                "album_items": album,
                "url": url,
                "webpage_url": url,
                "ext": "jpg",
                "media_id": extract_pin_id(url) or "",
                "source": "gallery_dl",
            }
        g = items[0]
        u = g.get("url") or ""
        if "pinimg" in u:
            u = upgrade_to_originals(u)
        is_vid = g.get("type") == "video"
        return {
            "title": g.get("title") or "Pinterest Pin",
            "uploader": "Pinterest User",
            "duration": "Unknown",
            "duration_secs": 0,
            "thumbnail": u,
            "platform": "Pinterest",
            "media_type": "video" if is_vid else "image",
            "qualities": [{"label": "Best", "format_id": "best", "has_audio": True}] if is_vid else [],
            "audio_formats": [],
            "image_url": None if is_vid else u,
            "album_items": [],
            "url": url,
            "webpage_url": url,
            "ext": "mp4" if is_vid else "jpg",
            "media_id": extract_pin_id(url) or "",
            "play_url": u if is_vid else None,
            "source": "gallery_dl",
        }
    except Exception as e:
        logger.info("gallery-dl pinterest failed: %s", e)
        return None


def _from_html(url: str, session: requests.Session) -> dict | None:
    try:
        r = session.get(url, timeout=20, allow_redirects=True)
        html = r.text
        final = r.url
        if "login" in final.lower() and "/pin/" not in final:
            return None

        # __PWS_DATA__ / rel="preload" structured JSON
        pin_id = extract_pin_id(final) or extract_pin_id(url)
        # Try embedded pin JSON
        for pattern in (
            r'<script[^>]+id="__PWS_DATA__"[^>]*>({.*?})</script>',
            r'<script[^>]+data-relay-response="true"[^>]*>({.*?})</script>',
        ):
            m = re.search(pattern, html, re.DOTALL)
            if not m:
                continue
            try:
                blob = json.loads(m.group(1))
            except Exception:
                continue
            # Walk for images.orig.url
            found = _walk_for_pin(blob)
            if found:
                found["url"] = url
                found["webpage_url"] = final
                found["media_id"] = pin_id or found.get("media_id") or ""
                found["source"] = "html_json"
                return found

        # og:image + pinimg harvest
        og = None
        for pat in (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ):
            m = re.search(pat, html, re.I)
            if m and not _is_placeholder(m.group(1)):
                og = m.group(1)
                break

        all_imgs = re.findall(
            r'https://i\.pinimg\.com/([a-zA-Z0-9x_-]+)/([a-zA-Z0-9/_.\-]+)\.(jpg|jpeg|png|webp)',
            html,
            re.I,
        )
        by_bucket: dict[str, str] = {}
        for size_bucket, file_hash, ext in all_imgs:
            full = f"https://i.pinimg.com/{size_bucket}/{file_hash}.{ext}"
            if _is_placeholder(full) or size_bucket in ("60x60", "75x75", "136x136"):
                continue
            by_bucket.setdefault(size_bucket, full)
        chosen = None
        for b in SIZE_PRIORITY:
            if b in by_bucket:
                chosen = by_bucket[b]
                break
        if not chosen and by_bucket:
            chosen = next(iter(by_bucket.values()))
        image_url = og or chosen
        if not image_url:
            return None
        image_url = upgrade_to_originals(image_url)
        title = "Pinterest Image"
        tm = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        if tm:
            title = re.sub(r"\s*\|\s*Pinterest\s*$", "", tm.group(1).strip(), flags=re.I)
        return {
            "title": title[:200],
            "uploader": "Pinterest User",
            "duration": "Unknown",
            "duration_secs": 0,
            "thumbnail": image_url,
            "platform": "Pinterest",
            "media_type": "image",
            "qualities": [],
            "audio_formats": [],
            "image_url": image_url,
            "album_items": [],
            "url": url,
            "webpage_url": final,
            "ext": "jpg",
            "media_id": pin_id or "",
            "source": "html",
        }
    except Exception as e:
        logger.info("html pinterest scrape failed: %s", e)
        return None


def _walk_for_pin(obj: Any, depth: int = 0) -> dict | None:
    if depth > 12:
        return None
    if isinstance(obj, dict):
        images = obj.get("images")
        if isinstance(images, dict) and ("orig" in images or "originals" in images or "736x" in images):
            # looks like a pin image block
            image_url = _best_image_from_images_dict(images)
            if image_url:
                video_url = _best_video_url(obj.get("videos"))
                return {
                    "title": (obj.get("title") or obj.get("grid_title") or "Pinterest Pin")[:200],
                    "uploader": "Pinterest User",
                    "duration": "Unknown",
                    "duration_secs": 0,
                    "thumbnail": image_url,
                    "platform": "Pinterest",
                    "media_type": "video" if video_url else "image",
                    "qualities": [{"label": "Best", "format_id": "best", "has_audio": True}] if video_url else [],
                    "audio_formats": [],
                    "image_url": None if video_url else upgrade_to_originals(image_url),
                    "album_items": [],
                    "url": "",
                    "webpage_url": "",
                    "ext": "mp4" if video_url else "jpg",
                    "media_id": str(obj.get("id") or ""),
                    "play_url": video_url,
                }
        for v in obj.values():
            found = _walk_for_pin(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for it in obj[:50]:
            found = _walk_for_pin(it, depth + 1)
            if found:
                return found
    return None



def _from_pinterest_downloader(url: str) -> dict | None:
    """
    Primary engine: pinterest-downloader (PyPI) — dedicated 2026 library.
    Returns structured pin with images.orig + video.formats.
    """
    try:
        from pinterest_downloader import Pinterest
    except ImportError:
        logger.warning("pinterest-downloader not installed")
        return None
    try:
        client = Pinterest()
        result = client.get_pin(url)
        if not result or not result.get("ok"):
            err = (result or {}).get("error")
            logger.info("pinterest-downloader get_pin failed: %s", err)
            return None
        pin = result.get("pin") or {}
        if not pin:
            return None

        pin_id = str(pin.get("id") or extract_pin_id(url) or "")
        title = (pin.get("title") or pin.get("description") or "Pinterest Pin")
        if isinstance(title, str):
            title = title.strip()[:200] or "Pinterest Pin"
        author = pin.get("author") or {}
        uploader = (
            author.get("username")
            or author.get("full_name")
            or "Pinterest User"
        )

        images = pin.get("images") or {}
        image_url = None
        # Prefer orig / originals
        for key in ("orig", "originals", "736x", "564x", "474x"):
            node = images.get(key)
            if isinstance(node, dict) and node.get("url") and not _is_placeholder(node["url"]):
                image_url = node["url"]
                break
            if isinstance(node, str) and node.startswith("http"):
                image_url = node
                break
        if image_url:
            image_url = upgrade_to_originals(image_url)

        # Video formats — prefer highest non-HLS MP4
        video_url = None
        duration_secs = 0
        video = pin.get("video") or {}
        formats = video.get("formats") or []
        mp4s = []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            u = fmt.get("url") or ""
            if not u.startswith("http"):
                continue
            h = int(fmt.get("height") or 0)
            if ".m3u8" in u or "hls" in (fmt.get("quality") or "").lower():
                score = h - 10_000
            else:
                score = h
            mp4s.append((score, u, fmt))
        if mp4s:
            mp4s.sort(key=lambda x: x[0], reverse=True)
            video_url = mp4s[0][1]
            try:
                duration_secs = int((mp4s[0][2].get("duration") or 0) / 1000)
            except Exception:
                duration_secs = 0

        media_type_raw = (pin.get("media_type") or "").lower()
        if video_url or media_type_raw == "video":
            media_type = "video"
            qualities = [{"label": "Best", "format_id": "best", "has_audio": True}]
            # secondary qualities from formats
            for score, u, fmt in mp4s[:4]:
                if ".m3u8" in u:
                    continue
                qlabel = fmt.get("quality") or f"{fmt.get('height') or ''}p"
                if qlabel and not any(q["label"] == qlabel for q in qualities):
                    qualities.append({
                        "label": str(qlabel),
                        "format_id": "best",
                        "has_audio": True,
                    })
        elif media_type_raw == "gif":
            media_type = "image"
            qualities = []
        else:
            media_type = "image"
            qualities = []

        ext = "mp4" if media_type == "video" else "jpg"
        if image_url and "." in image_url.split("?")[0]:
            e = image_url.split("?")[0].rsplit(".", 1)[-1].lower()
            if e in ("jpg", "jpeg", "png", "webp", "gif"):
                ext = e

        return {
            "title": title,
            "uploader": str(uploader),
            "duration": f"{duration_secs}s" if duration_secs else "Unknown",
            "duration_secs": duration_secs,
            "thumbnail": image_url or (video.get("poster") or ""),
            "platform": "Pinterest",
            "media_type": media_type,
            "qualities": qualities if media_type == "video" else [],
            "audio_formats": [],
            "image_url": image_url if media_type == "image" else None,
            "album_items": [],
            "url": pin.get("url") or url,
            "webpage_url": pin.get("url") or url,
            "ext": ext,
            "media_id": pin_id,
            "play_url": video_url,
            "source": "pinterest_downloader",
        }
    except Exception as e:
        logger.info("pinterest-downloader error: %s", e)
        return None


def scrape_pinterest(url: str) -> dict | None:
    """
    Full multi-strategy Pinterest extract.
    Returns a standard analyze_url-compatible dict or None.

    Order (strongest first):
      1. pinterest-downloader (dedicated PyPI library, 2026)
      2. PinResource JSON API
      3. gallery-dl
      4. HTML / __PWS_DATA__
    """
    if not url or not any(x in url for x in ("pinterest.", "pin.it")):
        return None

    session = _session()
    url = expand_pin_url(url, session)
    pin_id = extract_pin_id(url)

    candidates: list[dict] = []

    def add_candidate(candidate: dict | None):
        if candidate and (candidate.get("image_url") or candidate.get("play_url") or candidate.get("album_items")):
            candidate.setdefault("platform", "Pinterest")
            candidate.setdefault("url", url)
            candidate.setdefault("webpage_url", url)
            candidates.append(candidate)

    def run_pinterest_downloader():
        lib = _from_pinterest_downloader(url if pin_id is None else (url or pin_id))
        if not lib and pin_id:
            lib = _from_pinterest_downloader(pin_id)
        return lib

    def run_pin_resource():
        if not pin_id:
            return None
        pin = _pin_from_api(pin_id, session)
        return _result_from_pin_data(pin, url) if pin else None

    tasks = [run_pinterest_downloader, run_pin_resource, lambda: _from_gallery_dl(url), lambda: _from_html(url, session)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(task) for task in tasks]
        for future in as_completed(futures, timeout=28):
            try:
                add_candidate(future.result())
            except Exception as exc:
                logger.info("Pinterest provider failed: %s", exc)

    if candidates:
        for candidate in candidates:
            _score_result(candidate, session=session)
        candidates.sort(key=lambda item: item.get("provider_score", 0), reverse=True)
        best = candidates[0]
        best["provider_candidates"] = [
            {
                "source": c.get("source"),
                "media_type": c.get("media_type"),
                "score": c.get("provider_score"),
                "has_video": bool(c.get("play_url")),
                "has_image": bool(c.get("image_url")),
                "album_count": len(c.get("album_items") or []),
            }
            for c in candidates[:6]
        ]
        best["engine_profile"] = "pinterest-downloader+pin-resource+gallery-dl+html+cdn-probe"
        logger.info(
            "Pinterest best source=%s type=%s score=%s candidates=%s",
            best.get("source"), best.get("media_type"), best.get("provider_score"), len(candidates),
        )
        return best

    logger.warning("Pinterest extract failed for %s", url)
    return None
