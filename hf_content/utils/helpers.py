import re
import os
import hashlib
from urllib.parse import urlparse
from config.settings import SUPPORTED_DOMAINS


def is_valid_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme in ("http", "https"), result.netloc])
    except Exception:
        return False


def is_supported_url(url: str) -> bool:
    try:
        netloc = urlparse(url).netloc.lower().lstrip("www.")
        return any(domain in netloc for domain in SUPPORTED_DOMAINS)
    except Exception:
        return False


def get_platform(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if "youtube" in netloc or "youtu.be" in netloc:
        return "YouTube"
    if "tiktok" in netloc:
        return "TikTok"
    if "instagram" in netloc or "instagr.am" in netloc:
        return "Instagram"
    if "facebook" in netloc or "fb.watch" in netloc or "fb.com" in netloc:
        return "Facebook"
    if "twitter" in netloc or "x.com" in netloc or "t.co" in netloc:
        return "Twitter/X"
    if "threads.net" in netloc:
        return "Threads"
    if "reddit" in netloc or "redd.it" in netloc:
        return "Reddit"
    if "pinterest" in netloc or "pin.it" in netloc:
        return "Pinterest"
    if "snapchat" in netloc:
        return "Snapchat"
    if "vimeo" in netloc:
        return "Vimeo"
    if "dailymotion" in netloc or "dai.ly" in netloc:
        return "Dailymotion"
    if "soundcloud" in netloc:
        return "SoundCloud"
    if "spotify" in netloc:
        return "Spotify"
    if "t.me" in netloc or "telegram.me" in netloc:
        return "Telegram"
    return "Unknown"


def get_platform_emoji(platform: str) -> str:
    emojis = {
        "YouTube":     "🎬",
        "TikTok":      "🎵",
        "Instagram":   "📸",
        "Facebook":    "📘",
        "Twitter/X":   "🐦",
        "Threads":     "🧵",
        "Reddit":      "🤖",
        "Pinterest":   "📌",
        "Snapchat":    "👻",
        "Vimeo":       "🎥",
        "Dailymotion": "📹",
        "SoundCloud":  "🎧",
        "Spotify":     "🎶",
        "Telegram":    "✈️",
    }
    return emojis.get(platform, "🌐")


def format_duration(seconds: int) -> str:
    if not seconds:
        return "Unknown"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_size(bytes_val: int) -> str:
    if not bytes_val:
        return "Unknown"
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name[:200] or "download"


def get_display_name(user) -> str:
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    return user.first_name or user.username or str(user.id)


def truncate_title(title: str, max_len: int = 50) -> str:
    if len(title) <= max_len:
        return title
    return title[:max_len - 3] + "..."


def make_progress_bar(percent: int, length: int = 10) -> str:
    percent = max(0, min(100, percent))
    filled = int(percent / 100 * length)
    empty = length - filled
    return "█" * filled + "░" * empty


def get_level(points: int, lang: str = "en") -> str:
    if lang == "ar":
        if points >= 1000:
            return "🔱 أسطوري"
        elif points >= 500:
            return "💎 الماس"
        elif points >= 250:
            return "🥇 ذهبي"
        elif points >= 100:
            return "🥈 فضي"
        else:
            return "🥉 برونزي"
    else:
        if points >= 1000:
            return "🔱 Legend"
        elif points >= 500:
            return "💎 Diamond"
        elif points >= 250:
            return "🥇 Gold"
        elif points >= 100:
            return "🥈 Silver"
        else:
            return "🥉 Bronze"
