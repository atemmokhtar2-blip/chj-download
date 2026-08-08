"""
Robust TikTok helpers: expand short links, scrape metadata, and fetch
no-watermark play URLs via multiple strategies when yt-dlp is blocked.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
ANDROID_UA = (
    "com.ss.android.ugc.trill/340003 (Linux; U; Android 13; en_US; "
    "Pixel 7; Build/TQ3A.230805.001; Cronet/58.0.2991.0)"
)

USER_AGENTS = [MOBILE_UA, DESKTOP_UA, ANDROID_UA]


def _session(ua: str = MOBILE_UA) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
    })
    return s


def expand_tiktok_url(url: str) -> str:
    """Follow redirects for short TikTok links."""
    if not any(x in url for x in ("vt.tiktok.com", "vm.tiktok.com", "tiktok.com/t/", "tiktok.com/share")):
        return url
    for ua in USER_AGENTS:
        try:
            s = _session(ua)
            r = s.get(url, timeout=15, allow_redirects=True)
            if "tiktok.com" in r.url and "/video/" in r.url:
                return r.url.split("?")[0]
            if r.url and r.url != url:
                return r.url.split("?")[0]
        except Exception as e:
            logger.debug(f"expand_tiktok_url ua fail: {e}")
    return url


def _extract_video_id(url: str) -> Optional[str]:
    m = re.search(r"/video/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]item_id=(\d+)", url)
    if m:
        return m.group(1)
    return None


def _parse_json_blobs(html: str) -> list:
    blobs = []
    patterns = [
        r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>',
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'window\[[\'"]SIGI_STATE[\'"]\]\s*=\s*(\{.*?\});',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, re.DOTALL | re.IGNORECASE):
            raw = m.group(1).strip()
            try:
                blobs.append(json.loads(raw))
            except Exception:
                continue
    return blobs


def _walk_for_item(obj: Any, found: list) -> None:
    if isinstance(obj, dict):
        if "video" in obj and isinstance(obj.get("video"), dict):
            v = obj["video"]
            if any(k in v for k in ("playAddr", "downloadAddr", "play_addr", "download_addr")):
                found.append(obj)
        for v in obj.values():
            _walk_for_item(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_for_item(v, found)


def _addr_url(addr: Any) -> Optional[str]:
    if not addr:
        return None
    if isinstance(addr, str) and addr.startswith("http"):
        return addr
    if isinstance(addr, dict):
        url_list = addr.get("UrlList") or addr.get("url_list") or []
        if url_list:
            return url_list[0]
        u = addr.get("uri") or addr.get("Url")
        if isinstance(u, str) and u.startswith("http"):
            return u
    return None


def _from_item(item: dict) -> dict:
    video = item.get("video") or {}
    play = (
        _addr_url(video.get("playAddr"))
        or _addr_url(video.get("play_addr"))
        or _addr_url(video.get("downloadAddr"))
        or _addr_url(video.get("download_addr"))
    )
    no_wm = (
        _addr_url(video.get("downloadAddr"))
        or _addr_url(video.get("download_addr"))
        or play
    )
    author = item.get("author") or {}
    author_name = (
        author.get("nickname")
        or author.get("uniqueId")
        or item.get("authorName")
        or "TikTok User"
    )
    title = item.get("desc") or item.get("description") or item.get("title") or "TikTok Video"
    duration = video.get("duration") or item.get("duration") or 0
    try:
        duration = int(duration)
        if duration > 1000:
            duration = duration // 1000
    except Exception:
        duration = 0
    height = video.get("height") or 0
    cover = (
        _addr_url(video.get("cover"))
        or _addr_url(video.get("originCover"))
        or _addr_url(video.get("dynamicCover"))
        or ""
    )
    return {
        "title": title,
        "uploader": author_name,
        "duration": duration,
        "thumbnail": cover if isinstance(cover, str) else "",
        "play_url": no_wm or play,
        "height": height,
    }


def scrape_page_meta(url: str) -> Optional[dict]:
    for ua in USER_AGENTS:
        try:
            s = _session(ua)
            r = s.get(url, timeout=20, allow_redirects=True)
            html = r.text
            final = r.url
            if "tiktok.com" not in final:
                continue

            title = "TikTok Video"
            m = re.search(
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                html, re.I,
            )
            if m:
                title = m.group(1)

            thumb = ""
            m = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                html, re.I,
            )
            if m:
                thumb = m.group(1)

            author = "TikTok User"
            m = re.search(r'"authorName"\s*:\s*"([^"]+)"', html)
            if m:
                author = m.group(1)

            play_url = None
            items: list = []
            for blob in _parse_json_blobs(html):
                _walk_for_item(blob, items)
            if items:
                meta = _from_item(items[0])
                return {
                    "title": meta.get("title") or title,
                    "uploader": meta.get("uploader") or author,
                    "duration": meta.get("duration") or 0,
                    "thumbnail": meta.get("thumbnail") or thumb,
                    "play_url": meta.get("play_url"),
                    "height": meta.get("height") or 0,
                    "webpage_url": final.split("?")[0],
                }

            return {
                "title": title,
                "uploader": author,
                "duration": 0,
                "thumbnail": thumb,
                "play_url": None,
                "height": 0,
                "webpage_url": final.split("?")[0],
            }
        except Exception as e:
            logger.debug(f"scrape_page_meta fail: {e}")
    return None


def fetch_via_tikwm(url: str) -> Optional[dict]:
    """Public TikWM API — often works when TikTok blocks datacenter IPs."""
    endpoints = [
        f"https://www.tikwm.com/api/?url={quote(url, safe='')}&hd=1",
        f"https://tikwm.com/api/?url={quote(url, safe='')}&hd=1",
    ]
    for api in endpoints:
        try:
            s = _session(DESKTOP_UA)
            s.headers["Accept"] = "application/json"
            r = s.get(api, timeout=20)
            if r.status_code != 200:
                continue
            data = r.json()
            d = data.get("data") or {}
            if not isinstance(d, dict):
                continue
            play = d.get("hdplay") or d.get("play") or d.get("wmplay")
            if not play:
                continue
            author = d.get("author") or {}
            uploader = author.get("nickname") if isinstance(author, dict) else (d.get("author") or "TikTok User")
            return {
                "title": d.get("title") or "TikTok Video",
                "uploader": uploader or "TikTok User",
                "duration": int(d.get("duration") or 0),
                "thumbnail": d.get("cover") or d.get("origin_cover") or "",
                "play_url": play,
                "music_url": d.get("music"),
                "height": int(d.get("height") or 0),
                "webpage_url": url,
                "source": "tikwm",
            }
        except Exception as e:
            logger.debug(f"tikwm fail: {e}")
    return None


def scrape_tiktok(url: str) -> Optional[dict]:
    """
    Multi-strategy TikTok resolver.
    Returns metadata + optional direct play_url (no watermark when possible).
    """
    try:
        expanded = expand_tiktok_url(url)
        video_id = _extract_video_id(expanded)

        page = scrape_page_meta(expanded)
        api = fetch_via_tikwm(expanded) or fetch_via_tikwm(url)

        title = (api or {}).get("title") or (page or {}).get("title") or "TikTok Video"
        uploader = (api or {}).get("uploader") or (page or {}).get("uploader") or "TikTok User"
        thumb = (api or {}).get("thumbnail") or (page or {}).get("thumbnail") or ""
        play_url = (api or {}).get("play_url") or (page or {}).get("play_url")
        duration = (api or {}).get("duration") or (page or {}).get("duration") or 0
        webpage = (page or {}).get("webpage_url") or expanded or url
        height = (api or {}).get("height") or (page or {}).get("height") or 0

        qualities = [{"label": "Best", "format_id": "best", "has_audio": True}]
        if height and int(height) > 0:
            qualities.append({
                "label": f"{int(height)}p",
                "format_id": "best",
                "has_audio": True,
            })

        result = {
            "title": title,
            "uploader": uploader,
            "duration": duration if isinstance(duration, (int, float)) else 0,
            "thumbnail": thumb,
            "platform": "TikTok",
            "media_type": "video",
            "url": url,
            "webpage_url": webpage,
            "play_url": play_url,
            "music_url": (api or {}).get("music_url"),
            "qualities": qualities,
            "video_id": video_id,
            "source": (api or {}).get("source") or ("page" if page else "unknown"),
        }
        logger.info(
            f"TikTok scrape ok id={video_id} source={result['source']} "
            f"play_url={'yes' if play_url else 'no'}"
        )
        return result
    except Exception as e:
        logger.error(f"TikTok Scraper Error: {e}")
        return None


def download_tiktok_direct(play_url: str, out_path: str) -> Optional[str]:
    """Download a direct CDN URL with TikTok-friendly headers."""
    if not play_url:
        return None
    headers = {
        "User-Agent": MOBILE_UA,
        "Referer": "https://www.tiktok.com/",
        "Accept": "*/*",
    }
    try:
        s = _session(MOBILE_UA)
        with s.get(play_url, headers=headers, timeout=120, stream=True) as r:
            r.raise_for_status()
            path = out_path if out_path.endswith(".mp4") else (out_path + ".mp4")
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
            if os.path.getsize(path) > 1000:
                return path
            try:
                os.remove(path)
            except OSError:
                pass
    except Exception as e:
        logger.error(f"TikTok direct download failed: {e}")
    return None
