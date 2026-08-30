"""
Hardened TikTok resolver — multiple independent providers, retries, and
never-crash design so one blocked IP / dead API cannot stop the bot.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode

import requests

from config.settings import MAX_FILE_SIZE_BYTES

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

# Overall hard timeout budget per resolve attempt (seconds)
PROVIDER_TIMEOUT = 12
DOWNLOAD_TIMEOUT = 90
MAX_RETRIES = 2


def _session(ua: str = DESKTOP_UA) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
        "Connection": "keep-alive",
    })
    # Don't leak exceptions from bad SSL on some hosts
    s.verify = True
    return s


def _safe_get(url: str, **kwargs) -> Optional[requests.Response]:
    try:
        timeout = kwargs.pop("timeout", PROVIDER_TIMEOUT)
        s = kwargs.pop("session", None) or _session()
        return s.get(url, timeout=timeout, allow_redirects=True, **kwargs)
    except Exception as e:
        logger.debug(f"GET fail {url[:60]}: {e}")
        return None


def _safe_post(url: str, **kwargs) -> Optional[requests.Response]:
    try:
        timeout = kwargs.pop("timeout", PROVIDER_TIMEOUT)
        s = kwargs.pop("session", None) or _session()
        return s.post(url, timeout=timeout, allow_redirects=True, **kwargs)
    except Exception as e:
        logger.debug(f"POST fail {url[:60]}: {e}")
        return None


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def expand_tiktok_url(url: str) -> str:
    """Resolve short links (vt/vm/t/) to the canonical /@user/video/ID form."""
    if not url:
        return url
    short = any(x in url for x in (
        "vt.tiktok.com", "vm.tiktok.com", "tiktok.com/t/", "tiktok.com/share",
    ))
    if not short and "/video/" in url:
        return url.split("?")[0]
    for ua in USER_AGENTS:
        r = _safe_get(url, session=_session(ua), timeout=10)
        if r is None:
            continue
        final = r.url or url
        if "/video/" in final:
            return final.split("?")[0]
        # Sometimes the video id is only in the HTML
        m = re.search(r'https?://(?:www\.)?tiktok\.com/@[^/]+/video/(\d+)', r.text or "")
        if m:
            return f"https://www.tiktok.com/@x/video/{m.group(1)}"
    return url.split("?")[0]


def extract_video_id(url: str) -> Optional[str]:
    if not url:
        return None
    for pat in (
        r"/video/(\d+)",
        r"[?&]item_id=(\d+)",
        r"[?&]id=(\d{15,})",
        r"tiktok\.com/.*?(\d{15,})",
    ):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _result(
    play_url: str,
    *,
    title: str = "TikTok Video",
    uploader: str = "TikTok User",
    thumbnail: str = "",
    duration: int = 0,
    height: int = 0,
    source: str = "unknown",
    webpage_url: str = "",
    music_url: str | None = None,
) -> dict:
    return {
        "title": title or "TikTok Video",
        "uploader": uploader or "TikTok User",
        "duration": int(duration or 0),
        "thumbnail": thumbnail or "",
        "play_url": play_url,
        "music_url": music_url,
        "height": int(height or 0),
        "webpage_url": webpage_url,
        "source": source,
        "platform": "TikTok",
        "media_type": "video",
    }


# ---------------------------------------------------------------------------
# Provider 1: TikWM
# ---------------------------------------------------------------------------

def provider_tikwm(url: str) -> Optional[dict]:
    endpoints = [
        f"https://www.tikwm.com/api/?url={quote(url, safe='')}&hd=1",
        f"https://tikwm.com/api/?url={quote(url, safe='')}&hd=1",
        f"https://www.tikwm.com/api/?url={quote(url, safe='')}",
    ]
    for api in endpoints:
        r = _safe_get(api, session=_session(DESKTOP_UA), timeout=PROVIDER_TIMEOUT)
        if r is None or r.status_code != 200:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        d = data.get("data") if isinstance(data, dict) else None
        if not isinstance(d, dict):
            continue
        play = d.get("hdplay") or d.get("play") or d.get("wmplay")
        if not play or not str(play).startswith("http"):
            continue
        author = d.get("author") or {}
        uploader = (
            author.get("nickname") if isinstance(author, dict) else None
        ) or (str(d.get("author") or "") if not isinstance(d.get("author"), dict) else "") or "TikTok User"
        return _result(
            str(play),
            title=d.get("title") or "TikTok Video",
            uploader=uploader,
            thumbnail=d.get("cover") or d.get("origin_cover") or "",
            duration=int(d.get("duration") or 0),
            height=int(d.get("height") or 0),
            source="tikwm",
            webpage_url=url,
            music_url=d.get("music"),
        )
    return None


# ---------------------------------------------------------------------------
# Provider 2: TikDownloader / similar JSON APIs
# ---------------------------------------------------------------------------

def provider_tikdown(url: str) -> Optional[dict]:
    """tikdownloader.one style endpoints."""
    apis = [
        ("https://api.tikdownloader.net/api/download", {"url": url}),
        ("https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed/", None),
    ]
    # Generic public mirrors
    for endpoint, params in [
        (f"https://www.tiktokdownloaderapi.com/api/download?url={quote(url)}", None),
        (f"https://api.tikmate.app/api/lookup?url={quote(url)}", None),
    ]:
        r = _safe_get(endpoint, session=_session(DESKTOP_UA), timeout=PROVIDER_TIMEOUT)
        if r is None or r.status_code != 200:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        play = _deep_find_url(data, keys=("nwm_video_url", "video_url", "play", "hdplay", "download", "url"))
        if play:
            return _result(play, source="tikdown", webpage_url=url, title=_deep_find_str(data, ("title", "desc")) or "TikTok Video")
    return None


def _deep_find_url(obj: Any, keys: tuple) -> Optional[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and v.startswith("http") and any(x in v for x in (".mp4", "video", "tiktok", "byte", "akamaized", "muscdn")):
                return v
            found = _deep_find_url(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_url(v, keys)
            if found:
                return found
    return None


def _deep_find_str(obj: Any, keys: tuple) -> Optional[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and v.strip():
                return v.strip()
            found = _deep_find_str(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_str(v, keys)
            if found:
                return found
    return None


# ---------------------------------------------------------------------------
# Provider 3: MusicalDown / SSSTik style HTML form scrape
# ---------------------------------------------------------------------------

def provider_musicaldown(url: str) -> Optional[dict]:
    """Submit URL to musicaldown.com and parse download links."""
    try:
        s = _session(DESKTOP_UA)
        home = _safe_get("https://musicaldown.com/", session=s, timeout=10)
        if home is None:
            return None
        # token from page
        token = None
        m = re.search(r'name="token"\s+value="([^"]+)"', home.text or "")
        if m:
            token = m.group(1)
        data = {"url": url, "submit": "Download"}
        if token:
            data["token"] = token
        r = _safe_post(
            "https://musicaldown.com/download",
            session=s,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": "https://musicaldown.com/"},
            timeout=PROVIDER_TIMEOUT,
        )
        if r is None or r.status_code != 200:
            return None
        html = r.text or ""
        # Prefer no-watermark links
        links = re.findall(r'href="(https?://[^"]+)"[^>]*>\s*(?:Download|MP4|Without|HD|Server)', html, re.I)
        if not links:
            links = re.findall(r'href="(https?://[^"]+\.mp4[^"]*)"', html, re.I)
        for link in links:
            if "musicaldown" in link and "token" in link:
                continue
            if any(x in link for x in (".mp4", "video", "cdn", "byte", "tiktok")):
                return _result(link, source="musicaldown", webpage_url=url)
    except Exception as e:
        logger.debug(f"musicaldown: {e}")
    return None


def provider_ssstik(url: str) -> Optional[dict]:
    """ssstik.io form flow."""
    try:
        s = _session(DESKTOP_UA)
        home = _safe_get("https://ssstik.io/en", session=s, timeout=10)
        if home is None:
            return None
        # tt value
        tt = None
        m = re.search(r's_tt\s*=\s*[\'"]([^\'"]+)', home.text or "")
        if m:
            tt = m.group(1)
        data = {"id": url, "locale": "en", "tt": tt or "0"}
        r = _safe_post(
            "https://ssstik.io/abc?url=dl",
            session=s,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "HX-Request": "true",
                "HX-Target": "target",
                "HX-Current-URL": "https://ssstik.io/en",
                "Referer": "https://ssstik.io/en",
            },
            timeout=PROVIDER_TIMEOUT,
        )
        if r is None or r.status_code != 200:
            return None
        html = r.text or ""
        links = re.findall(r'href="(https?://[^"]+)"', html)
        for link in links:
            if "ssstik" in link and "/dl/" not in link and ".mp4" not in link:
                continue
            if any(x in link for x in (".mp4", "/dl/", "cdn", "video", "byte")):
                return _result(link, source="ssstik", webpage_url=url)
    except Exception as e:
        logger.debug(f"ssstik: {e}")
    return None


def provider_snaptik(url: str) -> Optional[dict]:
    """snaptik.app / snaptik.pro style."""
    for base in ("https://snaptik.app", "https://snaptik.pro"):
        try:
            s = _session(DESKTOP_UA)
            home = _safe_get(f"{base}/en2", session=s, timeout=8)
            if home is None:
                home = _safe_get(f"{base}/", session=s, timeout=8)
            token = None
            if home:
                m = re.search(r'name="token"\s+value="([^"]+)"', home.text or "")
                if m:
                    token = m.group(1)
            data = {"url": url}
            if token:
                data["token"] = token
            r = _safe_post(
                f"{base}/abc2.php",
                session=s,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"{base}/",
                },
                timeout=PROVIDER_TIMEOUT,
            )
            if r is None:
                # alternate endpoint
                r = _safe_post(
                    f"{base}/action.php",
                    session=s,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": f"{base}/"},
                    timeout=PROVIDER_TIMEOUT,
                )
            if r is None or r.status_code != 200:
                continue
            html = r.text or ""
            links = re.findall(r'href="(https?://[^"]+)"', html)
            for link in links:
                if any(x in link for x in (".mp4", "cdn", "video", "byte", "tiktokcdn", "snaptikcdn")):
                    if "snaptik" in link and "token" in link:
                        continue
                    return _result(link, source="snaptik", webpage_url=url)
        except Exception as e:
            logger.debug(f"snaptik {base}: {e}")
    return None


# ---------------------------------------------------------------------------
# Provider 4: Direct page JSON (SIGI / UNIVERSAL_DATA)
# ---------------------------------------------------------------------------

def provider_page_json(url: str) -> Optional[dict]:
    for ua in USER_AGENTS:
        r = _safe_get(url, session=_session(ua), timeout=PROVIDER_TIMEOUT)
        if r is None or r.status_code != 200:
            continue
        html = r.text or ""
        final = (r.url or url).split("?")[0]

        items: list = []
        for pat in (
            r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>',
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        ):
            for m in re.finditer(pat, html, re.DOTALL | re.I):
                try:
                    blob = json.loads(m.group(1).strip())
                    _walk_for_item(blob, items)
                except Exception:
                    continue
        if items:
            meta = _from_item(items[0])
            if meta.get("play_url"):
                meta["source"] = "page_json"
                meta["webpage_url"] = final
                return meta

        # og: tags only (metadata, no play url)
        title = "TikTok Video"
        m = re.search(r'property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
        if m:
            title = m.group(1)
        thumb = ""
        m = re.search(r'property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I)
        if m:
            thumb = m.group(1)
        # Even without play_url, return metadata so analyze still works
        if title != "TikTok Video" or thumb:
            return {
                "title": title,
                "uploader": "TikTok User",
                "duration": 0,
                "thumbnail": thumb,
                "play_url": None,
                "height": 0,
                "webpage_url": final,
                "source": "page_meta",
                "platform": "TikTok",
                "media_type": "video",
            }
    return None


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
        for key in ("UrlList", "url_list"):
            lst = addr.get(key) or []
            if lst:
                return lst[0]
        u = addr.get("uri") or addr.get("Url")
        if isinstance(u, str) and u.startswith("http"):
            return u
    return None


def _from_item(item: dict) -> dict:
    video = item.get("video") or {}
    play = (
        _addr_url(video.get("downloadAddr"))
        or _addr_url(video.get("download_addr"))
        or _addr_url(video.get("playAddr"))
        or _addr_url(video.get("play_addr"))
    )
    author = item.get("author") or {}
    author_name = (
        author.get("nickname") or author.get("uniqueId") or item.get("authorName") or "TikTok User"
    )
    title = item.get("desc") or item.get("description") or item.get("title") or "TikTok Video"
    duration = video.get("duration") or item.get("duration") or 0
    try:
        duration = int(duration)
        if duration > 1000:
            duration = duration // 1000
    except Exception:
        duration = 0
    cover = (
        _addr_url(video.get("cover"))
        or _addr_url(video.get("originCover"))
        or ""
    )
    return _result(
        play or "",
        title=title,
        uploader=author_name,
        thumbnail=cover if isinstance(cover, str) else "",
        duration=duration,
        height=int(video.get("height") or 0),
        source="page_json",
    )


# ---------------------------------------------------------------------------
# Provider 5: TikTok mobile API (aweme detail) — best-effort
# ---------------------------------------------------------------------------

def provider_aweme(url: str) -> Optional[dict]:
    vid = extract_video_id(url)
    if not vid:
        return None
    hosts = [
        "api16-normal-c-useast1a.tiktokv.com",
        "api19-normal-c-useast1a.tiktokv.com",
        "api.tiktokv.com",
    ]
    for host in hosts:
        api = (
            f"https://{host}/aweme/v1/feed/"
            f"?aweme_id={vid}&version_code=300000&"
            f"version_name=30.0.0&device_platform=android&aid=1233"
        )
        r = _safe_get(api, session=_session(ANDROID_UA), timeout=PROVIDER_TIMEOUT)
        if r is None or r.status_code != 200:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        aweme_list = data.get("aweme_list") or []
        if not aweme_list:
            # try aweme_detail shape
            detail = data.get("aweme_detail") or data.get("aweme") or {}
            if detail:
                aweme_list = [detail]
        for item in aweme_list:
            if str(item.get("aweme_id") or item.get("id") or "") != str(vid):
                # still try first item
                pass
            meta = _from_item(item)
            if meta.get("play_url"):
                meta["source"] = "aweme"
                meta["webpage_url"] = url
                return meta
    return None


# ---------------------------------------------------------------------------
# Orchestrator — run providers in parallel, first success wins
# ---------------------------------------------------------------------------

PROVIDERS: list[tuple[str, Callable[[str], Optional[dict]]]] = [
    ("tikwm", provider_tikwm),
    ("page_json", provider_page_json),
    ("aweme", provider_aweme),
    ("ssstik", provider_ssstik),
    ("snaptik", provider_snaptik),
    ("musicaldown", provider_musicaldown),
    ("tikdown", provider_tikdown),
]


def resolve_tiktok(url: str) -> Optional[dict]:
    """
    Expand short URL, then race all providers. Prefer results that have play_url.
    Never raises — returns None only if every provider fails.
    """
    try:
        expanded = expand_tiktok_url(url)
    except Exception:
        expanded = url

    best_meta_only: Optional[dict] = None
    errors: list[str] = []

    def _run(name_fn):
        name, fn = name_fn
        try:
            return name, fn(expanded)
        except Exception as e:
            return name, None

    # Parallel race (bounded workers)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_run, p): p[0] for p in PROVIDERS}
        for fut in as_completed(futures, timeout=PROVIDER_TIMEOUT + 5):
            try:
                name, result = fut.result()
            except Exception as e:
                errors.append(str(e))
                continue
            if not result:
                continue
            if result.get("play_url"):
                result["url"] = url
                result.setdefault("webpage_url", expanded)
                logger.info(f"TikTok resolve OK via {name}")
                # cancel remaining
                for f in futures:
                    f.cancel()
                return result
            if result.get("title") and best_meta_only is None:
                best_meta_only = result

    if best_meta_only:
        best_meta_only["url"] = url
        best_meta_only.setdefault("webpage_url", expanded)
        logger.info(f"TikTok meta-only via {best_meta_only.get('source')}")
        return best_meta_only

    logger.warning(f"TikTok resolve FAILED for {url} errors={errors[:3]}")
    return None


def scrape_tiktok(url: str) -> Optional[dict]:
    """
    Public entry used by downloader.analyze_url / download_video.
    Adds quality list expected by the bot UI. Never raises.
    """
    try:
        data = resolve_tiktok(url)
        if not data:
            # Minimal stub so the bot can still show a friendly error instead of crashing
            return {
                "title": "TikTok Video",
                "uploader": "TikTok User",
                "duration": 0,
                "thumbnail": "",
                "platform": "TikTok",
                "media_type": "video",
                "url": url,
                "webpage_url": url,
                "play_url": None,
                "qualities": [{"label": "Best", "format_id": "best", "has_audio": True}],
                "source": "failed",
            }

        height = int(data.get("height") or 0)
        qualities = [{"label": "Best", "format_id": "best", "has_audio": True}]
        if height >= 360:
            qualities.append({"label": f"{height}p", "format_id": "best", "has_audio": True})

        return {
            "title": data.get("title") or "TikTok Video",
            "uploader": data.get("uploader") or "TikTok User",
            "duration": data.get("duration") or 0,
            "thumbnail": data.get("thumbnail") or "",
            "platform": "TikTok",
            "media_type": "video",
            "url": data.get("webpage_url") or url,
            "webpage_url": data.get("webpage_url") or url,
            "play_url": data.get("play_url"),
            "music_url": data.get("music_url"),
            "qualities": qualities,
            "video_id": extract_video_id(data.get("webpage_url") or url),
            "source": data.get("source") or "unknown",
        }
    except Exception as e:
        logger.error(f"scrape_tiktok fatal (swallowed): {e}")
        return {
            "title": "TikTok Video",
            "uploader": "TikTok User",
            "duration": 0,
            "thumbnail": "",
            "platform": "TikTok",
            "media_type": "video",
            "url": url,
            "webpage_url": url,
            "play_url": None,
            "qualities": [{"label": "Best", "format_id": "best", "has_audio": True}],
            "source": "error",
        }


def download_tiktok_direct(play_url: str, out_path: str) -> Optional[str]:
    """
    Stream CDN bytes to disk with retries.

    Aborts mid-stream (and raises) when transferred size exceeds MAX_FILE_SIZE_BYTES
    so a large remote file never fully lands on disk. Other failures return None.
    """
    # Local import avoids hard coupling for callers that only resolve metadata.
    from services.downloader import FileTooLargeError

    if not play_url or not str(play_url).startswith("http"):
        return None
    path = out_path if out_path.endswith((".mp4", ".webm", ".mov")) else out_path + ".mp4"
    headers_list = [
        {"User-Agent": MOBILE_UA, "Referer": "https://www.tiktok.com/", "Accept": "*/*"},
        {"User-Agent": DESKTOP_UA, "Referer": "https://www.tiktok.com/", "Accept": "*/*"},
        {"User-Agent": ANDROID_UA, "Referer": "https://www.tiktok.com/", "Accept": "video/mp4,*/*"},
    ]
    for attempt in range(MAX_RETRIES + 1):
        headers = headers_list[attempt % len(headers_list)]
        try:
            s = _session(headers["User-Agent"])
            with s.get(play_url, headers=headers, timeout=DOWNLOAD_TIMEOUT, stream=True) as r:
                if r.status_code not in (200, 206):
                    logger.debug(f"direct status {r.status_code} attempt {attempt}")
                    time.sleep(0.5 * (attempt + 1))
                    continue

                # Pre-abort when CDN advertises an oversize body.
                cl = r.headers.get("Content-Length")
                if cl:
                    try:
                        if int(cl) > MAX_FILE_SIZE_BYTES:
                            raise FileTooLargeError(
                                size_bytes=int(cl),
                                limit_bytes=MAX_FILE_SIZE_BYTES,
                            )
                    except ValueError:
                        pass

                written = 0
                with open(path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_FILE_SIZE_BYTES:
                            f.close()
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                            raise FileTooLargeError(
                                size_bytes=written,
                                limit_bytes=MAX_FILE_SIZE_BYTES,
                            )
                        f.write(chunk)
                size = os.path.getsize(path) if os.path.exists(path) else 0
                if size > 1000:
                    return path
                try:
                    os.remove(path)
                except OSError:
                    pass
        except FileTooLargeError:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            raise
        except Exception as e:
            logger.debug(f"direct download attempt {attempt}: {e}")
            time.sleep(0.5 * (attempt + 1))
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
    return None
