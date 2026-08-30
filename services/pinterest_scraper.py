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
from typing import Any
from urllib.parse import urlparse

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


def scrape_pinterest(url: str) -> dict | None:
    """
    Full multi-strategy Pinterest extract.
    Returns a standard analyze_url-compatible dict or None.
    """
    if not url or not any(x in url for x in ("pinterest.", "pin.it")):
        return None

    session = _session()
    url = expand_pin_url(url, session)
    pin_id = extract_pin_id(url)

    # 1) Official-style PinResource API
    if pin_id:
        pin = _pin_from_api(pin_id, session)
        if pin:
            result = _result_from_pin_data(pin, url)
            if result.get("image_url") or result.get("play_url") or result.get("album_items"):
                logger.info(
                    "Pinterest PinResource ok id=%s type=%s source=%s",
                    pin_id, result.get("media_type"), result.get("source"),
                )
                return result

    # 2) gallery-dl
    g = _from_gallery_dl(url)
    if g:
        logger.info("Pinterest gallery-dl ok type=%s", g.get("media_type"))
        return g

    # 3) HTML scrape
    h = _from_html(url, session)
    if h:
        logger.info("Pinterest HTML ok type=%s", h.get("media_type"))
        return h

    logger.warning("Pinterest extract failed for %s", url)
    return None
