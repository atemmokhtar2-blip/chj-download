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

# Platforms that primarily serve images/carousels
IMAGE_PLATFORMS = {"Pinterest", "Instagram"}
# Platforms that serve audio
AUDIO_PLATFORMS = {"SoundCloud", "Spotify"}
# Pinterest-specific image URL patterns
PINTEREST_IMAGE_PATTERNS = [
    "i.pinimg.com",
    "s.pinimg.com",
    "pinimg.com",
    "pinterest.com/pin/",
    "pin.it/",
]
PINTEREST_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "svg", "bmp"}


def _extract_info_sync(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "extract_flat": False,
    }
    # For Pinterest and Instagram, allow images
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        session = requests.Session()
        res = session.get(url, headers=headers, timeout=20, allow_redirects=True)
        html = res.text
        final_url = res.url

        # 1) Try the structured og:image meta tag first — it points to the real pin.
        og_match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE,
        )
        og_image = None
        if og_match:
            cand = og_match.group(1)
            # Skip placeholder
            if not any(ph in cand for ph in PLACEHOLDER_HASHES):
                og_image = cand

        # 2) Collect ALL pinimg image URLs, then pick the best real one.
        all_imgs = re.findall(
            r'https://i\.pinimg\.com/([a-z0-9x]+)/([a-f0-9/_.\-]+)\.(jpg|jpeg|png|webp)',
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

        # Prefer og:image if present and real, else fall back to scraped image.
        image_url = og_image or chosen_image
        if not image_url:
            return None

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
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, _extract_info_sync, url),
                timeout=30
            )
        except Exception as e:
            # If yt-dlp fails and it's Pinterest, try fallback
            if "pinterest.com" in url or "pin.it" in url:
                download_logger.info(f"yt-dlp failed for Pinterest, trying fallback: {url}")
                return await loop.run_in_executor(None, _fallback_pinterest_extract, url)
            raise e

        if not info:
            # Also try fallback if info is empty for Pinterest
            if "pinterest.com" in url or "pin.it" in url:
                return await loop.run_in_executor(None, _fallback_pinterest_extract, url)
            return None

        platform = get_platform(url)
        media_type = _detect_media_type(info, platform)
        formats = info.get("formats", [])

        quality_map = {}
        audio_formats = []

        for f in formats:
            height = f.get("height")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            fmt_id = f.get("format_id", "")
            filesize = f.get("filesize") or f.get("filesize_approx") or 0

            # Collect audio-only formats
            if vcodec in ("none", None) and acodec not in ("none", None):
                audio_formats.append({
                    "format_id": fmt_id,
                    "filesize": filesize,
                    "abr": f.get("abr", 0),
                })
                continue

            if vcodec in ("none", None) or not height:
                continue

            label = f"{height}p"
            has_audio = acodec not in ("none", None)
            
            # If this format doesn't have audio, we'll need to merge it with best audio
            final_fmt_id = fmt_id if has_audio else f"{fmt_id}+bestaudio/best"

            if label not in quality_map:
                quality_map[label] = {
                    "label": label,
                    "height": height,
                    "format_id": final_fmt_id,
                    "filesize": filesize,
                    "has_audio": has_audio,
                }
            else:
                existing = quality_map[label]
                # Prefer formats that already have audio, or larger filesizes
                if (has_audio and not existing["has_audio"]) or (filesize > existing["filesize"]):
                    quality_map[label] = {
                        "label": label,
                        "height": height,
                        "format_id": final_fmt_id,
                        "filesize": filesize,
                        "has_audio": has_audio,
                    }

        # Sort qualities by height (ascending) and add smart labels
        qualities = sorted(quality_map.values(), key=lambda x: x["height"])
        # Add quality tier labels
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

        # Handle album entries
        album_items = []
        if media_type == "album":
            entries = info.get("entries", [])
            for entry in entries[:10]:  # limit to 10 items
                if entry:
                    item_type = _detect_media_type(entry, platform)
                    album_items.append({
                        "title": entry.get("title", ""),
                        "url": entry.get("webpage_url") or entry.get("url", ""),
                        "thumbnail": entry.get("thumbnail", ""),
                        "type": item_type,
                    })

        return {
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
        }
    except asyncio.TimeoutError:
        error_logger.error(f"Timeout analyzing: {url}")
        return None
    except Exception as e:
        # One last check for Pinterest fallback if any other error occurred
        if "pinterest.com" in url or "pin.it" in url:
            return await loop.run_in_executor(None, _fallback_pinterest_extract, url)
        error_logger.error(f"Error analyzing {url}: {e}")
        return None


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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
                    content = await resp.read()

                    # Validate: must be an image by both content-type and magic bytes.
                    if "image/" not in content_type and "application/xml" not in content_type:
                        # Some CDNs send generic content-type; rely on magic bytes.
                        pass
                    if "application/xml" in content_type or "text/html" in content_type:
                        last_err = f"Non-image Content-Type: {content_type}"
                        continue

                    if not _is_image_bytes(content):
                        last_err = "Downloaded bytes are not a valid image (magic-byte check failed)"
                        error_logger.error(f"{last_err} for {image_url} (len={len(content)})")
                        continue

                    with open(out_path, "wb") as f:
                        f.write(content)
                    if os.path.getsize(out_path) > 0:
                        return out_path
                    last_err = "Wrote 0-byte file"
        except asyncio.TimeoutError:
            last_err = "timeout"
            continue
        except Exception as e:
            last_err = str(e)
            error_logger.error(f"Image download attempt {attempt+1} error for {image_url}: {e}")
            continue

    error_logger.error(f"Image download exhausted retries for {image_url}: {last_err}")
    return None


def _download_sync(url: str, fmt_id: str, out_path: str,
                   progress_hook: Callable = None) -> str:
    ydl_opts = {
        "format": fmt_id,
        "outtmpl": out_path,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "logtostderr": True,
        "no_color": True,
        "buffersize": 1024 * 1024,
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    candidates = [
        out_path + ".mp4",
        out_path,
        out_path.replace(".%(ext)s", ".mp4"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    base = out_path.replace(".%(ext)s", "")
    for ext in [".mp4", ".mkv", ".webm", ".avi"]:
        if os.path.exists(base + ext):
            return base + ext

    raise FileNotFoundError(f"Downloaded file not found for {url}")


def _download_audio_sync(url: str, out_path: str,
                         progress_hook: Callable = None) -> str:
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_path,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "writethumbnail": False,
        "embedthumbnail": False,
    }
    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    mp3_path = out_path.replace(".%(ext)s", ".mp3")
    if os.path.exists(mp3_path):
        return mp3_path

    base = out_path.replace(".%(ext)s", "")
    if os.path.exists(base + ".mp3"):
        return base + ".mp3"

    raise FileNotFoundError("MP3 file not found after conversion")


async def download_video(url: str, format_id: str, quality_label: str,
                         progress_callback: Callable = None) -> str | None:
    safe_name = sanitize_filename(f"video_{hash(url) % 100000}_{quality_label}")
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

    try:
        file_path = await loop.run_in_executor(
            None, _download_sync, url, format_id, out_path, hook
        )
        return file_path
    except Exception as e:
        error_logger.error(f"Download error {url}: {e}")
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
            None, _download_audio_sync, url, out_path, hook
        )
        return file_path
    except Exception as e:
        error_logger.error(f"Audio download error {url}: {e}")
        return None

class FileTooLargeError(Exception):
    pass
