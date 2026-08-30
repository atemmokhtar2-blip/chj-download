"""
Instagram extractor — multi-engine cascade tuned for 2026.

Public posts try, in order:
  1. Instagram web GraphQL (query_hash / doc_id) + Chrome TLS (curl_cffi)
  2. Embed page parse (video_url / display_url / og tags)
  3. gallery-dl (cookies.txt if present)
  4. instaloader (optional INSTAGRAM_SESSIONID / cookies)

Auth boosters:
  INSTAGRAM_SESSIONID — browser sessionid cookie
  cookies.txt — Netscape jar
"""
from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import quote

logger = logging.getLogger(__name__)

_SHORTCODE_RE = re.compile(
    r"(?:instagram\.com|instagr\.am)/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)"
)
_IG_APP_ID = "936619743392459"
_GQL_QUERY_HASHES = (
    "9f8827793ef34641b2fb195d4a97f096",
    "2b0673e0dc4580674a88d32bac63519",
)
_GQL_DOC_IDS = ("10015918", "8845758582119845")


def extract_shortcode(url: str) -> str | None:
    if not url:
        return None
    m = _SHORTCODE_RE.search(url)
    return m.group(1) if m else None


def _normalize_ig_url(url: str) -> str:
    url = (url or "").strip()
    if "?" in url:
        url = url.split("?", 1)[0]
    if not url.endswith("/"):
        url += "/"
    return url.replace("://m.instagram.com", "://www.instagram.com")


def _sessionid() -> str | None:
    return (os.getenv("INSTAGRAM_SESSIONID") or os.getenv("IG_SESSIONID") or "").strip() or None


def _http_get(url: str, headers: dict | None = None, timeout: int = 20):
    hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "X-IG-App-ID": _IG_APP_ID,
        "X-ASBD-ID": "129477",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if headers:
        hdrs.update(headers)
    sid = _sessionid()
    if sid:
        hdrs["Cookie"] = f"sessionid={sid};"
    try:
        from curl_cffi import requests as creq
        return creq.get(url, headers=hdrs, impersonate="chrome", timeout=timeout)
    except Exception:
        pass
    import requests
    return requests.get(url, headers=hdrs, timeout=timeout)


def _unescape(u: str) -> str:
    if not u:
        return u
    try:
        return u.encode("utf-8").decode("unicode_escape").replace("\\/", "/")
    except Exception:
        return u.replace("\\/", "/").replace("\\u0026", "&")


def _node_to_result(node: dict, url: str, shortcode: str, source: str) -> dict | None:
    if not isinstance(node, dict):
        return None
    typename = node.get("__typename") or ""
    title = ""
    try:
        edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
        if edges:
            title = (edges[0].get("node") or {}).get("text") or ""
    except Exception:
        pass
    title = (title or "Instagram Media").split("\n")[0][:200]
    owner = (node.get("owner") or {}).get("username") or "Instagram User"

    album_items: list[dict] = []
    image_url = None
    play_url = None
    media_type = "image"
    duration_secs = 0

    children = (node.get("edge_sidecar_to_children") or {}).get("edges") or []
    if children:
        media_type = "album"
        for i, edge in enumerate(children):
            c = edge.get("node") or edge
            if not isinstance(c, dict):
                continue
            if c.get("is_video") or c.get("__typename") == "GraphVideo":
                vu = _unescape(c.get("video_url") or "")
                if not vu:
                    continue
                album_items.append({
                    "title": f"{title}_{i + 1}",
                    "url": vu,
                    "thumbnail": _unescape(c.get("display_url") or ""),
                    "image_url": None,
                    "type": "video",
                    "id": str(i),
                    "play_url": vu,
                })
            else:
                du = _unescape(c.get("display_url") or "")
                resources = c.get("display_resources") or []
                if resources:
                    try:
                        du = _unescape(sorted(
                            resources, key=lambda r: int(r.get("config_width") or 0)
                        )[-1].get("src") or du)
                    except Exception:
                        pass
                if not du:
                    continue
                album_items.append({
                    "title": f"{title}_{i + 1}",
                    "url": du,
                    "thumbnail": du,
                    "image_url": du,
                    "type": "image",
                    "id": str(i),
                })
        if len(album_items) < 2:
            if album_items and album_items[0]["type"] == "video":
                media_type, play_url = "video", album_items[0]["url"]
                image_url = album_items[0].get("thumbnail")
                album_items = []
            elif album_items:
                media_type, image_url = "image", album_items[0]["image_url"]
                album_items = []
    elif node.get("is_video") or typename == "GraphVideo":
        media_type = "video"
        play_url = _unescape(node.get("video_url") or "")
        image_url = _unescape(node.get("display_url") or "")
        try:
            duration_secs = int(float(node.get("video_duration") or 0))
        except Exception:
            duration_secs = 0
    else:
        media_type = "image"
        image_url = _unescape(node.get("display_url") or "")
        resources = node.get("display_resources") or []
        if resources:
            try:
                image_url = _unescape(sorted(
                    resources, key=lambda r: int(r.get("config_width") or 0)
                )[-1].get("src") or image_url)
            except Exception:
                pass

    if not (image_url or play_url or album_items):
        return None

    return {
        "title": title,
        "uploader": owner,
        "duration": f"{duration_secs}s" if duration_secs else "Unknown",
        "duration_secs": duration_secs,
        "thumbnail": image_url or (album_items[0].get("thumbnail") if album_items else ""),
        "platform": "Instagram",
        "media_type": media_type,
        "qualities": [{"label": "Best", "format_id": "best", "has_audio": True}] if media_type == "video" else [],
        "audio_formats": [],
        "image_url": image_url if media_type == "image" else None,
        "album_items": album_items if media_type == "album" else [],
        "url": url,
        "webpage_url": f"https://www.instagram.com/p/{shortcode}/",
        "ext": "mp4" if media_type == "video" else "jpg",
        "media_id": shortcode,
        "play_url": play_url,
        "source": source,
    }


def _from_graphql(shortcode: str, url: str) -> dict | None:
    variables = json.dumps(
        {"shortcode": shortcode, "fetch_tagged_user_count": None,
         "hoisted_comment_id": None, "hoisted_reply_id": None},
        separators=(",", ":"),
    )
    for qh in _GQL_QUERY_HASHES:
        try:
            endpoint = (
                "https://www.instagram.com/graphql/query/"
                f"?query_hash={qh}&variables={quote(variables)}"
            )
            r = _http_get(endpoint, timeout=18)
            if getattr(r, "status_code", 0) != 200:
                continue
            data = r.json()
            node = (
                (data.get("data") or {}).get("shortcode_media")
                or (data.get("data") or {}).get("xdt_shortcode_media")
            )
            result = _node_to_result(node, url, shortcode, "graphql")
            if result:
                return result
        except Exception as e:
            logger.debug("IG graphql hash %s: %s", qh, e)
    for doc_id in _GQL_DOC_IDS:
        try:
            endpoint = (
                "https://www.instagram.com/graphql/query/"
                f"?doc_id={doc_id}&variables={quote(variables)}"
            )
            r = _http_get(endpoint, timeout=18)
            if getattr(r, "status_code", 0) != 200:
                continue
            data = r.json()
            node = (
                (data.get("data") or {}).get("xdt_shortcode_media")
                or (data.get("data") or {}).get("shortcode_media")
            )
            result = _node_to_result(node, url, shortcode, "graphql_doc")
            if result:
                return result
        except Exception as e:
            logger.debug("IG graphql doc %s: %s", doc_id, e)
    return None


def _from_embed(shortcode: str, url: str) -> dict | None:
    for path in (
        f"/p/{shortcode}/embed/captioned/",
        f"/reel/{shortcode}/embed/captioned/",
        f"/p/{shortcode}/embed/",
        f"/reel/{shortcode}/embed/",
    ):
        try:
            r = _http_get(f"https://www.instagram.com{path}", timeout=20)
            html = getattr(r, "text", "") or ""
            if len(html) < 500:
                continue
            video = image = None
            for pat in (
                r'"video_url"\s*:\s*"(https:[^"]+)"',
                r'video_url\\":\\"(https:[^\\]+)',
                r'property="og:video" content="(https:[^"]+)"',
                r'content="(https://[^"]+\.mp4[^"]*)"',
            ):
                m = re.search(pat, html)
                if m:
                    video = _unescape(m.group(1))
                    break
            for pat in (
                r'"display_url"\s*:\s*"(https:[^"]+)"',
                r'property="og:image" content="(https:[^"]+)"',
            ):
                m = re.search(pat, html)
                if m:
                    image = _unescape(m.group(1))
                    break
            if video:
                return {
                    "title": "Instagram Video", "uploader": "Instagram User",
                    "duration": "Unknown", "duration_secs": 0,
                    "thumbnail": image or "", "platform": "Instagram",
                    "media_type": "video",
                    "qualities": [{"label": "Best", "format_id": "best", "has_audio": True}],
                    "audio_formats": [], "image_url": None, "album_items": [],
                    "url": url, "webpage_url": url, "ext": "mp4",
                    "media_id": shortcode, "play_url": video, "source": "embed",
                }
            if image:
                return {
                    "title": "Instagram Image", "uploader": "Instagram User",
                    "duration": "Unknown", "duration_secs": 0,
                    "thumbnail": image, "platform": "Instagram", "media_type": "image",
                    "qualities": [], "audio_formats": [], "image_url": image,
                    "album_items": [], "url": url, "webpage_url": url, "ext": "jpg",
                    "media_id": shortcode, "source": "embed",
                }
        except Exception as e:
            logger.debug("IG embed %s: %s", path, e)
    return None


def _from_gallery_dl(url: str) -> dict | None:
    try:
        from services.gallery_extractor import extract_gallery_items
        items = extract_gallery_items(url, max_items=10)
        if not items:
            return None
        shortcode = extract_shortcode(url) or ""
        if len(items) >= 2:
            album = [{
                "title": g.get("title") or f"slide_{i + 1}",
                "url": g["url"], "thumbnail": g.get("thumbnail") or g["url"],
                "image_url": g["url"] if g.get("type") == "image" else None,
                "type": g.get("type") or "image", "id": str(g.get("id") or i),
            } for i, g in enumerate(items)]
            return {
                "title": items[0].get("title") or "Instagram Album",
                "uploader": "Instagram User", "duration": "Unknown", "duration_secs": 0,
                "thumbnail": album[0]["url"], "platform": "Instagram", "media_type": "album",
                "qualities": [], "audio_formats": [], "image_url": None, "album_items": album,
                "url": url, "webpage_url": url, "ext": "jpg", "media_id": shortcode,
                "source": "gallery_dl",
            }
        g = items[0]
        is_vid = g.get("type") == "video"
        return {
            "title": g.get("title") or "Instagram Media", "uploader": "Instagram User",
            "duration": "Unknown", "duration_secs": 0,
            "thumbnail": g.get("thumbnail") or g["url"], "platform": "Instagram",
            "media_type": "video" if is_vid else "image",
            "qualities": [{"label": "Best", "format_id": "best", "has_audio": True}] if is_vid else [],
            "audio_formats": [], "image_url": None if is_vid else g["url"], "album_items": [],
            "url": url, "webpage_url": url, "ext": "mp4" if is_vid else "jpg",
            "media_id": shortcode, "play_url": g["url"] if is_vid else None, "source": "gallery_dl",
        }
    except Exception as e:
        logger.info("IG gallery-dl failed: %s", e)
        return None


def _from_instaloader(url: str) -> dict | None:
    shortcode = extract_shortcode(url)
    if not shortcode:
        return None
    try:
        import instaloader
        from instaloader import Post
    except ImportError:
        return None
    try:
        L = instaloader.Instaloader(
            download_pictures=False, download_videos=False,
            download_video_thumbnails=False, download_geotags=False,
            download_comments=False, save_metadata=False, compress_json=False, quiet=True,
        )
        sid = _sessionid()
        if sid:
            try:
                L.context._session.cookies.set("sessionid", sid, domain=".instagram.com")
            except Exception:
                pass
        if os.path.exists("cookies.txt"):
            try:
                L.load_session_from_file(None, filename="cookies.txt")
            except Exception:
                pass
        post = Post.from_shortcode(L.context, shortcode)
        title = (post.caption or "Instagram Post").split("\n")[0][:200]
        uploader = post.owner_username or "Instagram User"
        typename = post.typename
        album_items: list[dict] = []
        image_url = play_url = None
        media_type = "image"
        duration_secs = 0
        if typename == "GraphSidecar":
            media_type = "album"
            for i, node in enumerate(post.get_sidecar_nodes()):
                if node.is_video:
                    album_items.append({
                        "title": f"{title}_{i + 1}", "url": node.video_url,
                        "thumbnail": node.display_url, "image_url": None,
                        "type": "video", "id": str(i), "play_url": node.video_url,
                    })
                else:
                    album_items.append({
                        "title": f"{title}_{i + 1}", "url": node.display_url,
                        "thumbnail": node.display_url, "image_url": node.display_url,
                        "type": "image", "id": str(i),
                    })
            if len(album_items) < 2:
                if album_items and album_items[0]["type"] == "video":
                    media_type, play_url = "video", album_items[0]["url"]
                    album_items = []
                elif album_items:
                    media_type, image_url = "image", album_items[0]["image_url"]
                    album_items = []
        elif typename == "GraphVideo" or post.is_video:
            media_type = "video"
            play_url = post.video_url
            image_url = post.url
            try:
                duration_secs = int(post.video_duration or 0)
            except Exception:
                duration_secs = 0
        else:
            image_url = post.url
        return {
            "title": title, "uploader": uploader,
            "duration": f"{duration_secs}s" if duration_secs else "Unknown",
            "duration_secs": duration_secs,
            "thumbnail": image_url or (album_items[0]["thumbnail"] if album_items else ""),
            "platform": "Instagram", "media_type": media_type,
            "qualities": [{"label": "Best", "format_id": "best", "has_audio": True}] if media_type == "video" else [],
            "audio_formats": [], "image_url": image_url if media_type == "image" else None,
            "album_items": album_items if media_type == "album" else [],
            "url": url, "webpage_url": f"https://www.instagram.com/p/{shortcode}/",
            "ext": "mp4" if media_type == "video" else "jpg", "media_id": shortcode,
            "play_url": play_url, "source": "instaloader",
        }
    except Exception as e:
        logger.info("instaloader failed for %s: %s", shortcode, e)
        return None


def scrape_instagram(url: str) -> dict | None:
    if not url or ("instagram." not in url and "instagr.am" not in url):
        return None
    url = _normalize_ig_url(url)
    shortcode = extract_shortcode(url)
    if not shortcode:
        logger.warning("IG: cannot parse shortcode from %s", url)
        return None

    g = _from_graphql(shortcode, url)
    if g:
        logger.info("Instagram GraphQL ok type=%s", g.get("media_type"))
        return g

    e = _from_embed(shortcode, url)
    if e:
        logger.info("Instagram embed ok type=%s", e.get("media_type"))
        return e

    gd = _from_gallery_dl(url)
    if gd:
        logger.info("Instagram gallery-dl ok type=%s", gd.get("media_type"))
        return gd

    il = _from_instaloader(url)
    if il:
        logger.info("Instagram instaloader ok type=%s", il.get("media_type"))
        return il

    logger.warning(
        "Instagram extract failed for %s (set INSTAGRAM_SESSIONID or cookies.txt)", url
    )
    return None
