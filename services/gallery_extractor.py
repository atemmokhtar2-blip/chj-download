"""
Gallery / carousel extraction via gallery-dl (industry standard 2026).

yt-dlp alone fails on image-only Instagram carousels and TikTok photo-mode
posts. gallery-dl is the maintained tool production downloaders route those
URLs through (see ig-dl, InstaHam, etc.).

API used: gallery_dl.job.DataJob — extracts direct media URLs without relying
on fragile HTML regex.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

ALBUM_MAX_ITEMS = 10

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "heic", "avif"}
VIDEO_EXTS = {"mp4", "webm", "mov", "mkv", "m4v"}


def _guess_type(url: str, meta: dict) -> str:
    ext = (meta.get("extension") or meta.get("ext") or "").lower().lstrip(".")
    if not ext and url:
        path = url.split("?")[0].rsplit(".", 1)
        if len(path) == 2:
            ext = path[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    # Instagram / TikTok CDN heuristics
    u = (url or "").lower()
    if any(x in u for x in ("cdninstagram", "fbcdn", "scontent", "pinimg", "tiktokcdn", "image")):
        if any(x in u for x in (".mp4", "video", "/v/")):
            return "video"
        return "image"
    return "image"


def extract_gallery_items(url: str, max_items: int = ALBUM_MAX_ITEMS) -> list[dict[str, Any]]:
    """
    Extract direct media URLs for a post/carousel using gallery-dl.

    Returns list of:
      {url, type ("image"|"video"), title, extension, id}
    Empty list on failure (caller falls back to yt-dlp path).
    """
    if not url:
        return []

    try:
        from gallery_dl import config, job
        from gallery_dl.extractor.message import Message
    except ImportError:
        logger.warning("gallery-dl not installed; skipping gallery extraction")
        return []

    try:
        config.clear()
        config.set(("extractor",), "base-directory", os.path.join(os.getcwd(), "temp"))
        # Cap how many files the extractor yields for a single post.
        config.set(("extractor",), "image-range", f"1-{max_items}")
        config.set(("extractor",), "post-range", "1")
        if os.path.exists("cookies.txt"):
            config.set(("extractor",), "cookies", os.path.abspath("cookies.txt"))

        data_job = job.DataJob(url, file=None)
        data_job.file = None  # do not dump JSON to stdout
        data_job.run()

        if data_job.exception:
            logger.info("gallery-dl extract error for %s: %s", url, data_job.exception)

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in data_job.data or []:
            if not row or row[0] != Message.Url:
                continue
            # Message.Url payload: (3, media_url, metadata_dict)
            if len(row) < 3:
                continue
            media_url = row[1]
            meta = row[2] if isinstance(row[2], dict) else {}
            if not media_url or not str(media_url).startswith("http"):
                continue
            if media_url in seen:
                continue
            seen.add(media_url)
            ext = (meta.get("extension") or "").lower()
            items.append({
                "url": media_url,
                "image_url": media_url if _guess_type(media_url, meta) == "image" else None,
                "type": _guess_type(media_url, meta),
                "title": meta.get("title") or meta.get("description") or meta.get("id") or "media",
                "extension": ext,
                "id": str(meta.get("id") or meta.get("media_id") or len(items)),
                "thumbnail": meta.get("thumbnail") or "",
            })
            if len(items) >= max_items:
                break

        logger.info("gallery-dl extracted %d item(s) from %s", len(items), url)
        return items
    except Exception as e:
        logger.error("gallery-dl extract failed for %s: %s", url, e)
        return []
