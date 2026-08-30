import asyncio
import os
import re
import tempfile
import aiohttp
import requests
from typing import Callable, Optional
import yt_dlp
from config.settings import TEMP_DIR, MAX_FILE_SIZE_BYTES, DOWNLOAD_TIMEOUT
from utils.helpers import sanitize_filename, format_duration, format_size, get_platform
from utils.logger import download_logger, error_logger
from utils.ffmpeg_check import FFMPEG_AVAILABLE, FFMPEG_PATH
from middlewares.concurrency import get_executor

# Platforms that primarily serve images/carousels
IMAGE_PLATFORMS = {"Pinterest", "Instagram"}
# Platforms that serve audio
AUDIO_PLATFORMS = {"SoundCloud", "Spotify"}
# Multi-item posts (carousels / sidecars) — extract full entry list, not only first item.
# Telegram media groups accept at most 10 items, so we hard-cap extraction at 10.
CAROUSEL_PLATFORMS = {
    "Instagram", "TikTok", "Facebook", "Twitter/X", "Twitter", "X",
    "Threads", "Reddit", "Pinterest",
}
ALBUM_MAX_ITEMS = 10
# Pinterest-specific image URL patterns
PINTEREST_IMAGE_PATTERNS = [
    "i.pinimg.com",
    "s.pinimg.com",
    "pinimg.com",
    "pinterest.com/pin/",
    "pin.it/",
]
PINTEREST_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "svg", "bmp"}

# Always prefer a stream that contains both video and audio (merge with ffmpeg)
BEST_FORMAT_WITH_AUDIO = (
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
    "bestvideo+bestaudio/"
    "best[ext=mp4]/"
    "best"
)

class FileTooLargeError(Exception):
    """Raised when a downloaded file exceeds MAX_FILE_SIZE_BYTES."""

    def __init__(self, size_bytes: int = 0, limit_bytes: int = 0):
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes or MAX_FILE_SIZE_BYTES
        super().__init__(
            f"File too large: {size_bytes} bytes (limit {self.limit_bytes})"
        )


def _enforce_max_file_size(path: str | None) -> str | None:
    """
    Guard every successful download return path.

    - If path is missing / empty → return as-is (caller handles failure).
    - If file exceeds MAX_FILE_SIZE_BYTES → delete it and raise FileTooLargeError.
    - Otherwise return the same path unchanged.
    """
    if not path:
        return path
    try:
        size = os.path.getsize(path)
    except OSError:
        return path
    if size > MAX_FILE_SIZE_BYTES:
        error_logger.error(
            f"File exceeds size limit: {path} size={size} limit={MAX_FILE_SIZE_BYTES}"
        )
        try:
            os.remove(path)
        except OSError:
            pass
        raise FileTooLargeError(size_bytes=size, limit_bytes=MAX_FILE_SIZE_BYTES)
    return path


def _safe_impersonate():
    """Return ImpersonateTarget only when curl_cffi version is supported by yt-dlp."""
    try:
        import curl_cffi
        parts = [int(x) for x in curl_cffi.__version__.split(".")[:3] if x.isdigit()]
        ver = tuple(parts + [0] * (3 - len(parts)))
        # yt-dlp 2026.07.x marks curl_cffi >= 0.16 as unsupported
        if ver >= (0, 16, 0):
            return None
        return yt_dlp.networking.impersonate.ImpersonateTarget("chrome")
    except Exception:
        return None


def _base_ydl_opts(extra: dict | None = None) -> dict:
    """Build base yt-dlp options. Impersonate is added only when supported."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "ignoreerrors": False,
        # Abort as soon as Content-Length exceeds the bot upload limit.
        # Post-download _enforce_max_file_size remains the safety net when
        # Content-Length is missing (chunked transfer / some HLS paths).
        "max_filesize": MAX_FILE_SIZE_BYTES,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "http_chunk_size": 10485760,
    }
    impersonate = _safe_impersonate()
    if impersonate is not None:
        opts["impersonate"] = impersonate
    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = "cookies.txt"
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    if extra:
        opts.update(extra)
    return opts


def _is_max_filesize_abort(err: BaseException) -> bool:
    """True when yt-dlp aborted because the remote file exceeds max_filesize."""
    msg = str(err).lower()
    return (
        "max-filesize" in msg
        or "max_filesize" in msg
        or "larger than max-filesize" in msg
        or "file is larger than max-filesize" in msg
    )


def _filesize_progress_guard(d: dict) -> None:
    """
    yt-dlp progress hook: abort mid-download when transferred bytes exceed the
    limit. Covers cases where Content-Length is absent (chunked / some HLS).
    Raising from a progress hook stops the download.
    """
    if d.get("status") != "downloading":
        return
    downloaded = d.get("downloaded_bytes") or 0
    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
    if downloaded > MAX_FILE_SIZE_BYTES or (total and total > MAX_FILE_SIZE_BYTES):
        raise FileTooLargeError(
            size_bytes=int(max(downloaded, total or 0)),
            limit_bytes=MAX_FILE_SIZE_BYTES,
        )


def _extract_info_sync(url: str) -> dict:
    platform = get_platform(url)
    # Carousel posts are playlists in yt-dlp. Global noplaylist=True would collapse
    # them to a single item — disable it here and hard-cap the entry count.
    extract_extra = {
        "skip_download": True,
        "extract_flat": False,
    }
    if platform in CAROUSEL_PLATFORMS:
        extract_extra["noplaylist"] = False
        extract_extra["playlistend"] = ALBUM_MAX_ITEMS
        extract_extra["yes_playlist"] = True

    ydl_opts = _base_ydl_opts(extract_extra)

    # Advanced TikTok/Platform bypass
    if "tiktok.com" in url:
        ydl_opts["referer"] = "https://www.tiktok.com/"
        ydl_opts["headers"] = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Mode": "navigate",
        }
        ydl_opts["extractor_args"] = {
            "tiktok": {"api_hostname": "api16-normal-c-useast1a.tiktokv.com"}
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def _is_pinterest_image_url(url_str: str) -> bool:
    """Check if a URL is a Pinterest image source."""
    url_lower = url_str.lower()
    if any(pattern in url_lower for pattern in PINTEREST_IMAGE_PATTERNS):
        clean = url_str.split("?")[0].lower()
        return any(clean.endswith(f".{ext}") for ext in PINTEREST_IMAGE_EXTS)
    return False


def _detect_media_type(info: dict, platform: str) -> str:
    """Detect content type: video, audio, image, album."""
    # Check if it's a playlist/album
    entries = info.get("entries")
    if entries:
        return "album"

    formats = info.get("formats", [])

    # Check for audio-only platforms
    if platform in AUDIO_PLATFORMS:
        return "audio"

    # Check formats for video
    has_video = any(
        f.get("vcodec", "none") not in ("none", None) and f.get("height")
        for f in formats
    )
    has_audio_only = any(
        f.get("vcodec", "none") in ("none", None) and
        f.get("acodec", "none") not in ("none", None)
        for f in formats
    )

    # Pinterest-specific image detection (before format checks)
    if platform == "Pinterest":
        # Check direct URL from yt-dlp info
        direct_url = info.get("url", "")
        if direct_url and ("i.pinimg.com" in direct_url or _is_pinterest_image_url(direct_url)):
            return "image"
        # Check if the page is an image pin (no video codec)
        if not has_video:
            ext = (info.get("ext") or "").lower()
            if ext in PINTEREST_IMAGE_EXTS:
                return "image"
            thumbnail = info.get("thumbnail", "")
            if thumbnail and ("i.pinimg.com" in thumbnail or _is_pinterest_image_url(thumbnail)):
                return "image"
            # If it's Pinterest and no video is found, it's almost certainly an image
            if not formats or len(formats) <= 1:
                return "image"

    # If no formats at all, try to detect from other fields
    if not formats:
        # Instagram/Pinterest images come with a direct URL
        ext = (info.get("ext") or "").lower()
        if ext in ("jpg", "jpeg", "png", "webp", "gif"):
            return "image"
        url_field = info.get("url", "")
        if any(x in url_field.lower() for x in [".jpg", ".jpeg", ".png", ".webp"]):
            return "image"
        # SoundCloud/Spotify audio
        if platform in AUDIO_PLATFORMS:
            return "audio"
        return "video"

    if has_video:
        return "video"
    if has_audio_only:
        return "audio"

    # Fallback: Pinterest image
    if platform == "Pinterest":
        return "image"

    return "video"


def _fallback_pinterest_extract(url: str) -> dict | None:
    """Fallback extraction for Pinterest using requests and regex when yt-dlp fails.

    This is the critical path for Pinterest image pins: yt-dlp frequently fails on
    Pinterest, so we scrape the pin HTML and find the *real* pin image. The key
    gotcha is that Pinterest HTML always contains a *placeholder* image
    (the hash "d53b014d86a6b6761bf649a0ed813c2b") that must be skipped, otherwise
    we would hand the user a non-existent image that returns 403 on download.
    """
    # The well-known Pinterest placeholder/og-default image hash that appears on
    # every Pinterest page and must NEVER be used as the pin image.
    PLACEHOLDER_HASHES = (
        "d53b014d86a6b6761bf649a0ed813c2b",  # generic Pinterest placeholder PNG
    )
    # Preferred size buckets in priority order: highest quality first.
    PREFERRED_SIZES = ("originals", "1200x", "736x", "564x", "474x", "236x")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pinterest.com/",
    }

    try:
        session = requests.Session()
        res = session.get(url, headers=headers, timeout=20, allow_redirects=True)
        html = res.text
        final_url = res.url
        
        # If we got redirected to login, the HTML is useless
        if "login" in final_url.lower():
             download_logger.error(f"Pinterest Scraper: Redirected to login for {url}")
             return None


        # 1) Try the structured og:image meta tag first — it points to the real pin.
        # Robust regex for og:image (property and content can be in any order)
        og_image = None
        og_patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]
        for pattern in og_patterns:
            og_match = re.search(pattern, html, re.IGNORECASE)
            if og_match:
                cand = og_match.group(1)
                if not any(ph in cand for ph in PLACEHOLDER_HASHES):
                    og_image = cand
                    break

        # 2) Collect ALL pinimg image URLs, then pick the best real one.
        # More flexible regex for pinimg URLs
        all_imgs = re.findall(
            r'https://i\.pinimg\.com/([a-zA-Z0-9x_-]+)/([a-zA-Z0-9/_.\-]+)\.(jpg|jpeg|png|webp)',
            html,
            re.IGNORECASE,
        )
        # Map size-bucket -> full URL for the real pin image (not the placeholder).
        by_bucket: dict[str, str] = {}
        for size_bucket, file_hash, ext in all_imgs:
            full = f"https://i.pinimg.com/{size_bucket}/{file_hash}.{ext}"
            # Skip the placeholder / Pinterest logo images.
            if any(ph in full for ph in PLACEHOLDER_HASHES):
                continue
            # Skip tiny favicons / board thumbnails that aren't the pin itself.
            if size_bucket in ("60x60", "136x136", "75x75"):
                continue
            # Prefer the first occurrence per bucket.
            if size_bucket not in by_bucket:
                by_bucket[size_bucket] = full

        # Pick the highest-quality bucket available.
        chosen_image = None
        for bucket in PREFERRED_SIZES:
            if bucket in by_bucket:
                chosen_image = by_bucket[bucket]
                break
        # If no preferred bucket, take whatever real image we found.
        if not chosen_image and by_bucket:
            chosen_image = next(iter(by_bucket.values()))

        # 3) Try to find image in JSON blobs (e.g. __PWS_DATA__)
        json_image = None
        # Pinterest sometimes uses different script types or IDs for data
        json_blobs = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        for blob in json_blobs:
            if "i.pinimg.com" not in blob:
                continue
            # Look for "originals":{"url":"..."}
            m = re.search(r'["\']originals["\']\s*:\s*\{\s*["\']url["\']\s*:\s*["\'](https?://i\.pinimg\.com/[^"\']+)["\']', blob)
            if m:
                json_image = m.group(1).replace("\\/", "/")
                break
            # Look for any large image bucket
            m = re.search(r'["\'](https?://i\.pinimg\.com/(?:originals|736x|564x|736x|474x)/[^"\']+)["\']', blob)
            if m:
                cand = m.group(1).replace("\\/", "/")
                if not any(ph in cand for ph in PLACEHOLDER_HASHES):
                    json_image = cand
                    break
            # Very broad fallback for any pinimg URL
            m = re.search(r'(https?://i\.pinimg\.com/[^"\']+\.(?:jpg|png|webp))', blob)
            if m:
                cand = m.group(1).replace("\\/", "/")
                if not any(ph in cand for ph in PLACEHOLDER_HASHES):
                    json_image = cand
                    break

        # Prefer og:image, then JSON image, then scraped image.
        image_url = og_image or json_image or chosen_image
        if not image_url:
            download_logger.error(f"Pinterest Scraper: No image URL found in HTML for {url}")
            return None
        download_logger.info(f"Pinterest Scraper: Found image {image_url}")

        # Normalize: convert any size bucket to /originals/ for maximum quality,
        # but only if it exists in our by_bucket map; otherwise keep the bucket we have.
        # (We avoid blind string-replacement to /originals/ because not every pin has
        #  an originals variant — that's what causes silent 403s downstream.)

        # Try to extract a real title from <title> or og:title.
        title = "Pinterest Image"
        title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if title_match:
            title = title_match.group(1).strip()
            title = re.sub(r"\s*\|\s*Pinterest\s*$", "", title, flags=re.IGNORECASE)
        og_title = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE,
        )
        if og_title:
            title = og_title.group(1).strip()

        ext = image_url.rsplit(".", 1)[-1].lower() if "." in image_url else "jpg"
        # yt-dlp sometimes returns 'jpeg' for .jpg; keep it simple.
        if ext not in ("jpg", "jpeg", "png", "webp", "gif", "bmp", "svg"):
            ext = "jpg"

        return {
            "title": title,
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
            "webpage_url": final_url,
            "ext": ext,
        }
    except Exception as e:
        error_logger.error(f"Fallback Pinterest error for {url}: {e}")
        return None


async def analyze_url(url: str) -> dict | None:
    try:
        loop = asyncio.get_event_loop()
        tk_info = None

        # TikTok: expand short links + multi-strategy scrape BEFORE yt-dlp
        if any(x in url for x in ("tiktok.com", "vt.tiktok.com", "vm.tiktok.com")):
            from .tiktok_scraper import scrape_tiktok
            tk_info = await loop.run_in_executor(get_executor(), scrape_tiktok, url)
            if tk_info and tk_info.get("webpage_url"):
                url = tk_info["webpage_url"]

        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(get_executor(), _extract_info_sync, url),
                timeout=30
            )
        except Exception as e:
            if "pinterest.com" in url or "pin.it" in url:
                download_logger.info(f"yt-dlp failed for Pinterest, trying fallback: {url}")
                return await loop.run_in_executor(get_executor(), _fallback_pinterest_extract, url)

            # TikTok blocked / yt-dlp failed → use scraper result (may include direct play_url)
            if tk_info:
                download_logger.info(f"yt-dlp failed for TikTok, using scraper source={tk_info.get('source')}")
                return _normalize_tiktok_info(tk_info, url)
            if any(x in url for x in ("tiktok.com", "vt.tiktok.com", "vm.tiktok.com")):
                from .tiktok_scraper import scrape_tiktok
                tk_info = await loop.run_in_executor(get_executor(), scrape_tiktok, url)
                if tk_info:
                    return _normalize_tiktok_info(tk_info, url)
            raise e

        if not info:
            if "pinterest.com" in url or "pin.it" in url:
                return await loop.run_in_executor(get_executor(), _fallback_pinterest_extract, url)
            if tk_info:
                return _normalize_tiktok_info(tk_info, url)
            return None

        platform = get_platform(url)
        media_type = _detect_media_type(info, platform)
        formats = info.get("formats", [])

        quality_map = {}
        audio_formats = []

        # Platforms where numeric format_ids are fragile / expire quickly.
        # Always use height-based selectors with audio merge + progressive fallbacks.
        SOCIAL_PLATFORMS = {
            "Instagram", "TikTok", "Facebook", "Twitter", "X",
            "Threads", "Reddit", "Snapchat", "Pinterest",
        }

        def _robust_fmt(height: int, fmt_id: str, has_audio: bool) -> str:
            """Build a format string that almost always succeeds with audio."""
            h = int(height)
            # Height-capped chain: merged A/V → progressive → any best ≤ H → absolute best
            height_chain = (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={h}]+bestaudio/"
                f"best[height<={h}][ext=mp4]/"
                f"best[height<={h}]/"
                f"bestvideo[height<={h}]+bestaudio/best/"
                f"best"
            )
            if platform in SOCIAL_PLATFORMS:
                return height_chain
            # YouTube / Vimeo etc: keep specific id when it already has audio,
            # but still append safe fallbacks so the button never hard-fails.
            if has_audio and fmt_id:
                return f"{fmt_id}/{height_chain}"
            if fmt_id:
                return f"{fmt_id}+bestaudio/best/{height_chain}"
            return height_chain

        for f in formats:
            # Prefer the smaller dimension as "height" so vertical videos
            # (1080x1920) show as 1080p not 1920p.
            h_raw = f.get("height") or 0
            w_raw = f.get("width") or 0
            try:
                h_raw, w_raw = int(h_raw or 0), int(w_raw or 0)
            except Exception:
                h_raw, w_raw = 0, 0
            if h_raw and w_raw:
                height = min(h_raw, w_raw)
            else:
                height = h_raw or w_raw
            if not height or height < 144:
                continue

            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            fmt_id = str(f.get("format_id") or "")
            filesize = f.get("filesize") or f.get("filesize_approx") or 0

            if vcodec in ("none", None) and acodec not in ("none", None):
                audio_formats.append({
                    "format_id": fmt_id,
                    "filesize": filesize,
                    "abr": f.get("abr", 0),
                })
                continue

            if vcodec in ("none", None):
                continue

            # Snap to common quality buckets to avoid 854p / 960p / 1280p noise
            if height >= 2000:
                bucket = 2160
            elif height >= 1400:
                bucket = 1440
            elif height >= 1000:
                bucket = 1080
            elif height >= 700:
                bucket = 720
            elif height >= 450:
                bucket = 480
            elif height >= 340:
                bucket = 360
            else:
                bucket = 240

            label = f"{bucket}p"
            has_audio = acodec not in ("none", None)
            final_fmt_id = _robust_fmt(bucket, fmt_id, has_audio)

            if label not in quality_map:
                quality_map[label] = {
                    "label": label,
                    "height": bucket,
                    "format_id": final_fmt_id,
                    "filesize": filesize,
                    "has_audio": has_audio,
                }
            else:
                existing = quality_map[label]
                if (has_audio and not existing["has_audio"]) or (filesize > existing["filesize"]):
                    quality_map[label] = {
                        "label": label,
                        "height": bucket,
                        "format_id": final_fmt_id,
                        "filesize": filesize,
                        "has_audio": has_audio,
                    }

        # Sort ascending; keep only meaningful steps (max 5)
        qualities = sorted(quality_map.values(), key=lambda x: x["height"])
        # Drop qualities whose known size already exceeds Telegram/bot limit
        qualities = [
            q for q in qualities
            if not (q.get("filesize") and q["filesize"] > MAX_FILE_SIZE_BYTES)
        ]
        if len(qualities) > 5:
            # keep lowest, some mids, and highest
            qualities = [qualities[0]] + qualities[1:-1][-3:] + [qualities[-1]]
        for q in qualities:
            h = q["height"]
            if h >= 2160:
                q["tier"] = "4K"
            elif h >= 1440:
                q["tier"] = "1440p"
            elif h >= 1080:
                q["tier"] = "Full HD"
            elif h >= 720:
                q["tier"] = "HD"
            elif h >= 480:
                q["tier"] = "SD"
            else:
                q["tier"] = "Low"

        thumbnail = info.get("thumbnail", "")
        duration_secs = info.get("duration", 0)

        # Handle image type — extract direct image URL
        image_url = None
        if media_type == "image":
            image_url = _extract_image_url(info)

        # Handle album / carousel entries (Instagram sidecar, multi-media posts, …)
        album_items = []
        if media_type == "album":
            entries = [e for e in (info.get("entries") or []) if e]
            for entry in entries[:ALBUM_MAX_ITEMS]:
                item_type = _detect_media_type(entry, platform)
                item_image_url = None
                if item_type == "image":
                    item_image_url = _extract_image_url(entry)
                direct = entry.get("url") or ""
                if item_type == "image" and not item_image_url and direct:
                    if any(direct.lower().split("?")[0].endswith(x) for x in (
                        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"
                    )) or "pinimg.com" in direct or "cdninstagram.com" in direct:
                        item_image_url = direct
                album_items.append({
                    "title": entry.get("title") or info.get("title") or "",
                    "url": entry.get("webpage_url") or entry.get("original_url") or entry.get("url") or url,
                    "thumbnail": entry.get("thumbnail") or "",
                    "image_url": item_image_url,
                    "type": item_type,
                    "id": str(entry.get("id") or entry.get("display_id") or ""),
                })

        # gallery-dl fallback / upgrade for carousel platforms.
        # yt-dlp 2026 often returns null entries for image-only Instagram carousels;
        # gallery-dl is the production standard for those posts.
        needs_gallery = (
            platform in CAROUSEL_PLATFORMS
            and (
                media_type == "album"
                and (not album_items or all(not (it.get("image_url") or it.get("url")) for it in album_items))
                or media_type in ("image", "video")
                and platform in ("Instagram", "Reddit", "Pinterest", "TikTok")
            )
        )
        # Always try gallery-dl for Instagram posts — it is stronger for multi-image.
        if platform in ("Instagram", "Reddit", "Pinterest") or (
            platform == "TikTok" and ("/photo/" in url or media_type == "album")
        ):
            needs_gallery = True

        if needs_gallery:
            from .gallery_extractor import extract_gallery_items
            g_items = await loop.run_in_executor(
                get_executor(), extract_gallery_items, url, ALBUM_MAX_ITEMS
            )
            if g_items:
                album_items = [
                    {
                        "title": g.get("title") or info.get("title") or "",
                        "url": g["url"],
                        "thumbnail": g.get("thumbnail") or "",
                        "image_url": g.get("image_url") or (g["url"] if g.get("type") == "image" else None),
                        "type": g.get("type") or "image",
                        "id": str(g.get("id") or ""),
                    }
                    for g in g_items
                ]
                if len(album_items) >= 2:
                    media_type = "album"
                elif len(album_items) == 1:
                    media_type = album_items[0]["type"]
                    if media_type == "image":
                        image_url = album_items[0].get("image_url") or album_items[0]["url"]

        # TikTok photo-mode images from our multi-provider scraper
        if platform == "TikTok" and tk_info and tk_info.get("images"):
            imgs = tk_info["images"][:ALBUM_MAX_ITEMS]
            if len(imgs) >= 2:
                album_items = [
                    {
                        "title": tk_info.get("title") or f"photo_{i + 1}",
                        "url": img_url,
                        "thumbnail": img_url,
                        "image_url": img_url,
                        "type": "image",
                        "id": str(i),
                    }
                    for i, img_url in enumerate(imgs)
                    if img_url and str(img_url).startswith("http")
                ]
                media_type = "album"

        if media_type == "album" and not album_items:
            media_type = _detect_media_type(
                {k: v for k, v in info.items() if k != "entries"},
                platform,
            )
            if media_type == "album":
                media_type = "video"

        media_id = str(
            info.get("id")
            or info.get("display_id")
            or info.get("extractor_key", "") + ":" + str(info.get("id") or "")
            or ""
        )
        result = {
            "title": info.get("title", "Unknown"),
            "uploader": info.get("uploader") or info.get("channel") or "Unknown",
            "duration": format_duration(int(duration_secs)) if duration_secs else "Unknown",
            "duration_secs": duration_secs,
            "thumbnail": thumbnail,
            "platform": platform,
            "media_type": media_type,
            "qualities": qualities,
            "audio_formats": audio_formats,
            "image_url": image_url,
            "album_items": album_items,
            "url": url,
            "webpage_url": info.get("webpage_url", url),
            "ext": info.get("ext", ""),
            "media_id": media_id,
        }
        if tk_info and tk_info.get("play_url"):
            result["play_url"] = tk_info["play_url"]
        return result
    except asyncio.TimeoutError:
        error_logger.error(f"Timeout analyzing: {url}")
        return None
    except Exception as e:
        if "pinterest.com" in url or "pin.it" in url:
            return await loop.run_in_executor(get_executor(), _fallback_pinterest_extract, url)
        error_logger.error(f"Error analyzing {url}: {e}")
        return None


def _normalize_tiktok_info(tk: dict, original_url: str) -> dict:
    """Build a standard analyze_url result from the multi-strategy TikTok scraper."""
    duration_secs = tk.get("duration") or 0
    try:
        duration_secs = int(duration_secs)
    except Exception:
        duration_secs = 0
    qualities = tk.get("qualities") or [
        {"label": "Best", "format_id": "best", "has_audio": True}
    ]
    images = [
        u for u in (tk.get("images") or [])
        if u and str(u).startswith("http")
    ][:ALBUM_MAX_ITEMS]
    if len(images) >= 2:
        media_type = "album"
        album_items = [
            {
                "title": tk.get("title") or f"photo_{i + 1}",
                "url": img,
                "thumbnail": img,
                "image_url": img,
                "type": "image",
                "id": str(i),
            }
            for i, img in enumerate(images)
        ]
    elif len(images) == 1:
        media_type = "image"
        album_items = []
    else:
        media_type = "video"
        album_items = []

    return {
        "title": tk.get("title") or "TikTok Video",
        "uploader": tk.get("uploader") or "TikTok User",
        "duration": format_duration(duration_secs) if duration_secs else "Unknown",
        "duration_secs": duration_secs,
        "thumbnail": tk.get("thumbnail") or (images[0] if images else ""),
        "platform": "TikTok",
        "media_type": media_type,
        "qualities": qualities if media_type == "video" else [],
        "audio_formats": [],
        "image_url": images[0] if len(images) == 1 else None,
        "album_items": album_items,
        "url": tk.get("webpage_url") or original_url,
        "webpage_url": tk.get("webpage_url") or original_url,
        "play_url": tk.get("play_url"),
        "ext": "mp4",
    }


def _extract_image_url(info: dict) -> str | None:
    """Extract direct image URL from info dict (Pinterest, Instagram images)."""
    # Try direct url field
    direct_url = info.get("url", "")
    if direct_url:
        ext = direct_url.split("?")[0].lower()
        if any(ext.endswith(x) for x in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"]):
            return direct_url
        # Pinterest-specific: URLs from i.pinimg.com are always images
        if "i.pinimg.com" in direct_url:
            # Try to get original version from any pinimg URL
            return direct_url.replace("/236x/", "/originals/").replace("/474x/", "/originals/").replace("/736x/", "/originals/").replace("/564x/", "/originals/")
        if _is_pinterest_image_url(direct_url):
            return direct_url

    # Try formats - look for image formats
    formats = info.get("formats", [])
    for f in formats:
        furl = f.get("url", "")
        if furl:
            ext = furl.split("?")[0].lower()
            if any(ext.endswith(x) for x in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
                return furl
            # Pinterest image URL in formats
            if _is_pinterest_image_url(furl):
                return furl

    # Pinterest: try to extract from original image metadata
    # yt-dlp sometimes stores the original image in the 'original_url' or similar fields
    original = info.get("original_url") or info.get("image")
    if original and _is_pinterest_image_url(original):
        return original

    # Try thumbnail as fallback (Pinterest often puts image in thumbnail)
    thumbnail = info.get("thumbnail", "")
    if thumbnail:
        # Clean up thumbnail URL - Pinterest thumbnails can be smaller, try to get original
        if "i.pinimg.com" in thumbnail:
            return thumbnail.replace("/236x/", "/originals/").replace("/474x/", "/originals/").replace("/736x/", "/originals/").replace("/564x/", "/originals/")
        return thumbnail

    # Try searching in all thumbnails if available
    thumbnails = info.get("thumbnails", [])
    for t in reversed(thumbnails): # Usually higher quality are at the end
        t_url = t.get("url", "")
        if "i.pinimg.com" in t_url:
            return t_url.replace("/236x/", "/originals/").replace("/474x/", "/originals/").replace("/736x/", "/originals/").replace("/564x/", "/originals/")
    

    return info.get("url") or None


async def download_image(url: str, image_url: str) -> str | None:
    """Download an image from a direct URL with retries and integrity checks.

    This is the second critical fix for the Pinterest problem: even after we
    extract the *correct* image URL, the actual HTTP download can still fail
    (403, timeout, partial body). Previously the code returned None silently,
    and the handler told the user "download complete" — a lie. Now we:
      * Retry up to 3 times with a short backoff.
      * Validate the HTTP status and Content-Type.
      * Validate the first bytes (magic bytes) to ensure we got a real image
        and not an XML/HTML error page (e.g. a 403 body returned with 200).
      * Use a longer per-attempt timeout and follow redirects.
    """
    # Headers that work reliably against i.pinimg.com / Pinterest CDN.
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pinterest.com/",
    }

    safe_name = sanitize_filename(f"image_{hash(url) % 100000}")
    clean_url = image_url.split("?")[0]
    ext = ".jpg"
    for e in (".png", ".webp", ".gif", ".jpeg", ".bmp", ".svg"):
        if clean_url.lower().endswith(e):
            ext = e
            break
    out_path = os.path.join(TEMP_DIR, f"{safe_name}{ext}")

    # Known image magic-byte signatures.
    def _is_image_bytes(data: bytes) -> bool:
        if not data or len(data) < 12:
            return False
        return (
            data[:8] == b"\x89PNG\r\n\x1a\n"            # PNG
            or data[:3] == b"\xff\xd8\xff"               # JPEG
            or data[:6] in (b"GIF87a", b"GIF89a")        # GIF
            or data[:4] == b"RIFF" and data[8:12] == b"WEBP"  # WEBP
            or data[:4] == b"BM "                        # BMP
        )

    last_err = None
    for attempt in range(3):
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(image_url, timeout=timeout, allow_redirects=True) as resp:
                    if resp.status != 200:
                        last_err = f"HTTP {resp.status}"
                        # 403/404 are permanent — don't retry on those.
                        if resp.status in (403, 404, 410):
                            error_logger.error(f"Image download {resp.status} for {image_url}")
                            return None
                        # Otherwise retry (5xx, network) on next attempt.
                        continue

                    content_type = (resp.headers.get("Content-Type", "") or "").lower()
                    if "application/xml" in content_type or "text/html" in content_type:
                        last_err = f"Non-image Content-Type: {content_type}"
                        continue

                    # Pre-abort when Content-Length already exceeds the limit.
                    cl_header = resp.headers.get("Content-Length")
                    if cl_header:
                        try:
                            if int(cl_header) > MAX_FILE_SIZE_BYTES:
                                raise FileTooLargeError(
                                    size_bytes=int(cl_header),
                                    limit_bytes=MAX_FILE_SIZE_BYTES,
                                )
                        except ValueError:
                            pass

                    # Stream body so we never hold a huge buffer in RAM and can
                    # abort mid-transfer when size crosses the limit.
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > MAX_FILE_SIZE_BYTES:
                            raise FileTooLargeError(
                                size_bytes=received,
                                limit_bytes=MAX_FILE_SIZE_BYTES,
                            )
                        chunks.append(chunk)
                    content = b"".join(chunks)

                    if not _is_image_bytes(content):
                        last_err = "Downloaded bytes are not a valid image (magic-byte check failed)"
                        error_logger.error(f"{last_err} for {image_url} (len={len(content)})")
                        continue

                    with open(out_path, "wb") as f:
                        f.write(content)
                    if os.path.getsize(out_path) > 0:
                        return _enforce_max_file_size(out_path)
                    last_err = "Wrote 0-byte file"
        except FileTooLargeError:
            raise
        except asyncio.TimeoutError:
            last_err = "timeout"
            continue
        except Exception as e:
            last_err = str(e)
            error_logger.error(f"Image download attempt {attempt+1} error for {image_url}: {e}")
            continue

    error_logger.error(f"Image download exhausted retries for {image_url}: {last_err}")
    return None


def _height_format_chain(h: int) -> str:
    """Universal height-capped format chain with audio + progressive fallbacks."""
    return (
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={h}]+bestaudio/"
        f"best[height<={h}][ext=mp4]/"
        f"best[height<={h}]/"
        f"bestvideo[height<={h}]+bestaudio/best/"
        f"best"
    )


def _resolve_format(fmt_id: str) -> str:
    """Map UI labels / empty values to a format that always includes audio."""
    if fmt_id is None:
        return BEST_FORMAT_WITH_AUDIO
    fmt_id = str(fmt_id).strip()
    if not fmt_id or fmt_id.lower() in ("best", "best_quality"):
        return BEST_FORMAT_WITH_AUDIO
    # Pure height label like "1080p"
    if fmt_id.endswith("p") and fmt_id[:-1].isdigit():
        return _height_format_chain(int(fmt_id[:-1]))
    # Already a chain — ensure ultimate fallback to best
    if "/" in fmt_id or "+" in fmt_id:
        if not fmt_id.rstrip("/").endswith("best"):
            return f"{fmt_id}/best"
        return fmt_id
    # Bare numeric format id → append safe fallbacks
    return f"{fmt_id}+bestaudio/best/{BEST_FORMAT_WITH_AUDIO}"


def _find_downloaded_file(out_path: str) -> str | None:
    candidates = [
        out_path + ".mp4",
        out_path,
        out_path.replace(".%(ext)s", ".mp4"),
    ]
    for c in candidates:
        if os.path.exists(c) and os.path.getsize(c) > 0:
            return c
    base = out_path.replace(".%(ext)s", "")
    for ext in [".mp4", ".mkv", ".webm", ".avi", ".mov"]:
        p = base + ext
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None


def _download_sync(url: str, fmt_id: str, out_path: str,
                   progress_hook: Callable = None) -> str:
    resolved = _resolve_format(fmt_id)
    # Primary format, then safer fallbacks so quality buttons rarely hard-fail
    format_attempts = []
    for a in (resolved, BEST_FORMAT_WITH_AUDIO, "best"):
        if a not in format_attempts:
            format_attempts.append(a)

    last_err = None
    for attempt_fmt in format_attempts:
        ydl_opts = _base_ydl_opts({
            "format": attempt_fmt,
            "outtmpl": out_path,
            "merge_output_format": "mp4",
            "logtostderr": True,
            "no_color": True,
            "buffersize": 1024 * 1024,
            "prefer_ffmpeg": True,
        })
        if "tiktok.com" in url:
            ydl_opts["referer"] = "https://www.tiktok.com/"
            ydl_opts["headers"] = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
            ydl_opts["extractor_args"] = {
                "tiktok": {"api_hostname": "api16-normal-c-useast1a.tiktokv.com"}
            }
        if any(x in url for x in ("facebook.com", "fb.watch", "fb.com", "instagram.com")):
            ydl_opts["headers"] = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
        hooks = [_filesize_progress_guard]
        if progress_hook:
            hooks.append(progress_hook)
        ydl_opts["progress_hooks"] = hooks
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            found = _find_downloaded_file(out_path)
            if found:
                return found
        except FileTooLargeError:
            raise
        except Exception as e:
            if _is_max_filesize_abort(e):
                raise FileTooLargeError(limit_bytes=MAX_FILE_SIZE_BYTES) from e
            last_err = e
            error_logger.error(f"format attempt failed ({str(attempt_fmt)[:50]}): {e}")
            continue

    if last_err:
        if _is_max_filesize_abort(last_err):
            raise FileTooLargeError(limit_bytes=MAX_FILE_SIZE_BYTES) from last_err
        raise last_err
    raise FileNotFoundError(f"Downloaded file not found for {url}")


def _download_audio_sync(url: str, out_path: str,
                         progress_hook: Callable = None) -> str:
    ydl_opts = _base_ydl_opts({
        "format": "bestaudio/best",
        "outtmpl": out_path,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "writethumbnail": False,
        "embedthumbnail": False,
    })

    if "tiktok.com" in url:
        ydl_opts["referer"] = "https://www.tiktok.com/"
        ydl_opts["headers"] = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    hooks = [_filesize_progress_guard]
    if progress_hook:
        hooks.append(progress_hook)
    ydl_opts["progress_hooks"] = hooks

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except FileTooLargeError:
        raise
    except Exception as e:
        if _is_max_filesize_abort(e):
            raise FileTooLargeError(limit_bytes=MAX_FILE_SIZE_BYTES) from e
        raise

    mp3_path = out_path.replace(".%(ext)s", ".mp3")
    if os.path.exists(mp3_path):
        return mp3_path

    base = out_path.replace(".%(ext)s", "")
    if os.path.exists(base + ".mp3"):
        return base + ".mp3"

    raise FileNotFoundError("MP3 file not found after conversion")


async def download_video(url: str, format_id: str, quality_label: str,
                         progress_callback: Callable = None,
                         play_url: str | None = None) -> str | None:
    safe_name = sanitize_filename(f"video_{hash(url) % 100000}_{quality_label}")
    out_path = os.path.join(TEMP_DIR, f"{safe_name}.%(ext)s")
    direct_path = os.path.join(TEMP_DIR, f"{safe_name}.mp4")

    last_percent = [0]
    loop = asyncio.get_running_loop()

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed", 0)
            eta = d.get("eta", 0)

            if total > 0:
                pct = round((downloaded / total) * 100, 1)
                if pct >= last_percent[0] + 5 or pct >= 99:
                    last_percent[0] = pct
                    if progress_callback:
                        asyncio.run_coroutine_threadsafe(
                            progress_callback({
                                "pct": pct,
                                "downloaded": downloaded,
                                "total": total,
                                "speed": speed,
                                "eta": eta
                            }),
                            loop
                        )

    is_tiktok = any(
        x in (url or "")
        for x in ("tiktok.com", "vt.tiktok.com", "vm.tiktok.com")
    )

    async def _tiktok_direct_attempts(preferred: str | None = None) -> str | None:
        """Resolve + download via multi-provider scraper. Never raises."""
        try:
            from .tiktok_scraper import scrape_tiktok, download_tiktok_direct, resolve_tiktok
        except Exception as e:
            error_logger.error(f"TikTok module import failed: {e}")
            return None

        candidates: list[str] = []
        if preferred and str(preferred).startswith("http"):
            candidates.append(preferred)

        try:
            resolved = await loop.run_in_executor(get_executor(), resolve_tiktok, url)
            if resolved and resolved.get("play_url"):
                candidates.append(resolved["play_url"])
        except Exception as e:
            error_logger.error(f"TikTok resolve_tiktok error: {e}")

        try:
            tk = await loop.run_in_executor(get_executor(), scrape_tiktok, url)
            if tk and tk.get("play_url"):
                candidates.append(tk["play_url"])
        except Exception as e:
            error_logger.error(f"TikTok scrape_tiktok error: {e}")

        seen = set()
        unique = []
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                unique.append(c)

        for i, direct in enumerate(unique):
            try:
                path = await loop.run_in_executor(
                    get_executor(), download_tiktok_direct, direct, f"{direct_path}.{i}"
                )
                if path and os.path.exists(path) and os.path.getsize(path) > 1000:
                    download_logger.info(f"TikTok direct OK via candidate #{i}")
                    return _enforce_max_file_size(path)
            except FileTooLargeError:
                raise
            except Exception as e:
                error_logger.error(f"TikTok direct candidate #{i} error: {e}")
        return None

    # 1) TikTok multi-provider direct CDN first (survives yt-dlp IP bans)
    if is_tiktok:
        path = await _tiktok_direct_attempts(play_url)
        if path:
            return _enforce_max_file_size(path)

    # 2) yt-dlp
    try:
        file_path = await loop.run_in_executor(
            get_executor(), _download_sync, url, format_id, out_path, hook
        )
        if file_path:
            return _enforce_max_file_size(file_path)
    except FileTooLargeError:
        raise
    except Exception as e:
        error_logger.error(f"Download error {url}: {e}")

    # 3) TikTok last-resort after yt-dlp failure
    if is_tiktok:
        path = await _tiktok_direct_attempts(None)
        if path:
            return _enforce_max_file_size(path)

    return None


async def download_audio(url: str, progress_callback: Callable = None) -> str | None:
    safe_name = sanitize_filename(f"audio_{hash(url) % 100000}")
    out_path = os.path.join(TEMP_DIR, f"{safe_name}.%(ext)s")

    last_percent = [0]
    loop = asyncio.get_running_loop()

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed", 0)
            eta = d.get("eta", 0)
            
            if total > 0:
                pct = round((downloaded / total) * 100, 1)
                if pct >= last_percent[0] + 10 or pct >= 99:
                    last_percent[0] = pct
                    if progress_callback:
                        asyncio.run_coroutine_threadsafe(
                            progress_callback({
                                "pct": pct,
                                "downloaded": downloaded,
                                "total": total,
                                "speed": speed,
                                "eta": eta
                            }),
                            loop
                        )

    try:
        file_path = await loop.run_in_executor(
            get_executor(), _download_audio_sync, url, out_path, hook
        )
        return _enforce_max_file_size(file_path)
    except FileTooLargeError:
        raise
    except Exception as e:
        error_logger.error(f"Audio download error {url}: {e}")
        return None


async def download_album(
    items: list[dict],
    progress_callback: Callable = None,
) -> list[dict]:
    """
    Download every item in a carousel / album (max ALBUM_MAX_ITEMS).

    Returns a list of dicts: {"path": str, "type": "image"|"video", "title": str}.
    Skips individual failures so one broken entry does not kill the whole album.
    Raises FileTooLargeError only if EVERY successful candidate exceeded the limit
    (partial success is preferred for multi-item posts).
    """
    if not items:
        return []

    results: list[dict] = []
    oversized = 0
    total = min(len(items), ALBUM_MAX_ITEMS)

    for idx, item in enumerate(items[:ALBUM_MAX_ITEMS]):
        item_type = (item.get("type") or "video").lower()
        item_url = item.get("url") or ""
        item_title = item.get("title") or f"item_{idx + 1}"
        image_url = item.get("image_url") or item.get("thumbnail") or ""

        if progress_callback:
            try:
                await progress_callback({
                    "pct": round((idx / max(total, 1)) * 100, 1),
                    "downloaded": idx,
                    "total": total,
                    "speed": 0,
                    "eta": 0,
                    "album_index": idx + 1,
                    "album_total": total,
                })
            except Exception:
                pass

        try:
            if item_type == "image":
                path = await download_image(item_url or image_url, image_url or item_url)
                if path:
                    results.append({"path": path, "type": "image", "title": item_title})
            else:
                path = await download_video(
                    item_url, "best", "best",
                    progress_callback=None,
                    play_url=item.get("play_url"),
                )
                if path:
                    results.append({"path": path, "type": "video", "title": item_title})
        except FileTooLargeError:
            oversized += 1
            error_logger.error(f"Album item {idx + 1}/{total} exceeds size limit")
        except Exception as e:
            error_logger.error(f"Album item {idx + 1}/{total} failed: {e}")

    if not results and oversized:
        raise FileTooLargeError(limit_bytes=MAX_FILE_SIZE_BYTES)
    return results

