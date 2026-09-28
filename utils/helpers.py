from __future__ import annotations

import re
from urllib.parse import urlparse

from config.settings import SUPPORTED_DOMAINS


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def is_supported_url(url: str) -> bool:
    if not is_valid_url(url):
        return False
    host = urlparse(url).netloc.lower().replace("www.", "")
    return any(host == d.replace("www.", "") or host.endswith("." + d.replace("www.", "")) for d in SUPPORTED_DOMAINS)


def truncate_title(title: str, max_len: int = 90) -> str:
    title = re.sub(r"\s+", " ", str(title or "Untitled")).strip()
    return title if len(title) <= max_len else title[: max_len - 1].rstrip() + "…"


def make_progress_bar(percent: float, width: int = 10) -> str:
    pct = max(0, min(100, int(percent or 0)))
    filled = round(width * pct / 100)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def format_size(size: int | float | None) -> str:
    if not size:
        return "0 B"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def get_platform_emoji(platform: str) -> str:
    p = (platform or "").lower()
    if "youtube" in p: return "▶️"
    if "tiktok" in p: return "🎵"
    if "instagram" in p: return "📸"
    if "facebook" in p: return "📘"
    if p in {"twitter", "x"} or "twitter" in p: return "𝕏"
    if "soundcloud" in p: return "☁️"
    if "reddit" in p: return "👽"
    if "pinterest" in p: return "📌"
    return "🌐"


def get_display_name(user) -> str:
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    username = getattr(user, "username", "") or ""
    full = (first + " " + last).strip()
    return full or ("@" + username if username else str(getattr(user, "id", "User")))
