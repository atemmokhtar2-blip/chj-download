"""
Instagram post/reel/carousel extractor — production cascade (2026).

Order (strongest first):
  1. gallery-dl  — best for public carousels without login
  2. instaloader — Post.from_shortcode (structured sidecar nodes)
  3. yt-dlp info extract (caller also runs this)

Returns analyze_url-compatible dict.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_SHORTCODE_RE = re.compile(
    r"(?:instagram\.com|instagr\.am)/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)"
)


def extract_shortcode(url: str) -> str | None:
    if not url:
        return None
    m = _SHORTCODE_RE.search(url)
    return m.group(1) if m else None


def _normalize_ig_url(url: str) -> str:
    url = (url or "").strip()
    # strip tracking
    if "?" in url:
        base, _, _ = url.partition("?")
        url = base
    if not url.endswith("/"):
        url += "/"
    return url


def _from_gallery_dl(url: str) -> dict | None:
    try:
        from services.gallery_extractor import extract_gallery_items
        items = extract_gallery_items(url, max_items=10)
        if not items:
            return None
        shortcode = extract_shortcode(url) or ""
        if len(items) >= 2:
            album = []
            for i, g in enumerate(items):
                album.append({
                    "title": g.get("title") or f"slide_{i + 1}",
                    "url": g["url"],
                    "thumbnail": g.get("thumbnail") or g["url"],
                    "image_url": g["url"] if g.get("type") == "image" else None,
                    "type": g.get("type") or "image",
                    "id": str(g.get("id") or i),
                })
            return {
                "title": items[0].get("title") or "Instagram Album",
                "uploader": "Instagram User",
                "duration": "Unknown",
                "duration_secs": 0,
                "thumbnail": album[0]["url"],
                "platform": "Instagram",
                "media_type": "album",
                "qualities": [],
                "audio_formats": [],
                "image_url": None,
                "album_items": album,
                "url": url,
                "webpage_url": url,
                "ext": "jpg",
                "media_id": shortcode,
                "source": "gallery_dl",
            }
        g = items[0]
        is_vid = g.get("type") == "video"
        return {
            "title": g.get("title") or "Instagram Media",
            "uploader": "Instagram User",
            "duration": "Unknown",
            "duration_secs": 0,
            "thumbnail": g.get("thumbnail") or g["url"],
            "platform": "Instagram",
            "media_type": "video" if is_vid else "image",
            "qualities": [{"label": "Best", "format_id": "best", "has_audio": True}] if is_vid else [],
            "audio_formats": [],
            "image_url": None if is_vid else g["url"],
            "album_items": [],
            "url": url,
            "webpage_url": url,
            "ext": "mp4" if is_vid else "jpg",
            "media_id": shortcode,
            "play_url": g["url"] if is_vid else None,
            "source": "gallery_dl",
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
        logger.warning("instaloader not installed")
        return None
    try:
        L = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
        )
        # Optional session from cookies.txt (Netscape) if present
        try:
            import os
            if os.path.exists("cookies.txt"):
                L.load_session_from_file(None, filename="cookies.txt")
        except Exception:
            pass

        post = Post.from_shortcode(L.context, shortcode)
        title = (post.caption or "Instagram Post").split("\n")[0][:200]
        uploader = post.owner_username or "Instagram User"
        typename = post.typename  # GraphImage | GraphVideo | GraphSidecar

        album_items: list[dict] = []
        image_url = None
        play_url = None
        media_type = "image"
        duration_secs = 0

        if typename == "GraphSidecar":
            media_type = "album"
            for i, node in enumerate(post.get_sidecar_nodes()):
                if node.is_video:
                    u = node.video_url
                    album_items.append({
                        "title": f"{title}_{i + 1}",
                        "url": u,
                        "thumbnail": node.display_url,
                        "image_url": None,
                        "type": "video",
                        "id": str(i),
                        "play_url": u,
                    })
                else:
                    u = node.display_url
                    album_items.append({
                        "title": f"{title}_{i + 1}",
                        "url": u,
                        "thumbnail": u,
                        "image_url": u,
                        "type": "image",
                        "id": str(i),
                    })
            if len(album_items) < 2:
                # demote
                if album_items and album_items[0]["type"] == "video":
                    media_type = "video"
                    play_url = album_items[0]["url"]
                    album_items = []
                elif album_items:
                    media_type = "image"
                    image_url = album_items[0]["image_url"]
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
            media_type = "image"
            image_url = post.url

        return {
            "title": title,
            "uploader": uploader,
            "duration": f"{duration_secs}s" if duration_secs else "Unknown",
            "duration_secs": duration_secs,
            "thumbnail": image_url or (album_items[0]["thumbnail"] if album_items else ""),
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
            "source": "instaloader",
        }
    except Exception as e:
        logger.info("instaloader failed for %s: %s", shortcode, e)
        return None


def scrape_instagram(url: str) -> dict | None:
    if not url or "instagram." not in url and "instagr.am" not in url:
        return None
    url = _normalize_ig_url(url)

    # 1) gallery-dl (strong for public multi-image without login)
    g = _from_gallery_dl(url)
    if g and (g.get("image_url") or g.get("play_url") or g.get("album_items")):
        logger.info("Instagram gallery-dl ok type=%s", g.get("media_type"))
        return g

    # 2) instaloader
    il = _from_instaloader(url)
    if il and (il.get("image_url") or il.get("play_url") or il.get("album_items")):
        logger.info("Instagram instaloader ok type=%s", il.get("media_type"))
        return il

    logger.warning("Instagram extract failed for %s", url)
    return None
