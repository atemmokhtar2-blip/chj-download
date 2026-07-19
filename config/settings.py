import os
from dotenv import load_dotenv

load_dotenv()

try:
    with open("/home/user/app/token.txt", "r") as f:
        BOT_TOKEN = f.read().strip()
except:
    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID", "").isdigit() else 0

SUPPORT_BOT_USERNAME = os.getenv("SUPPORT_BOT_USERNAME", "").strip().lstrip("@")
ADMIN_BOT_USERNAME   = os.getenv("ADMIN_BOT_USERNAME",  "").strip().lstrip("@")
UPDATE_BOT_USERNAME  = os.getenv("UPDATE_BOT_USERNAME", "TitanXvv_bot").strip().lstrip("@")

DATABASE_PATH = "/home/user/app/database/bot.db"
TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "temp")
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "10"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5"))

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", "1000"))

# Points system: users START with 10, each download COSTS 1 point
POINTS_REGISTRATION   = 10
POINTS_DOWNLOAD       = 1    # deducted per download
POINTS_REFERRAL       = 2    # NOT 50
POINTS_FIRST_DOWNLOAD = 10   # bonus on very first download
POINTS_DAILY          = 5

LUCKY_WHEEL_PRIZES    = [1, 2, 3, 5, 10]
LUCKY_WHEEL_COOLDOWN  = 86400  # 24 hours in seconds

REWARDS = {
    50:  {"name": "Extra Downloads Pack (5x)", "type": "points",   "value": 5},
    100: {"name": "Extra Downloads Pack (15x)", "type": "points",  "value": 15},
    250: {"name": "VIP Day",  "type": "vip", "value": 1},
    500: {"name": "VIP Week", "type": "vip", "value": 7},
}

# Bilingual achievements — each has name_en and name_ar
ACHIEVEMENTS = [
    {"id": "first_download",   "name_en": "🏆 First Download",   "name_ar": "🏆 أول تحميل",        "condition": "downloads",  "threshold": 1},
    {"id": "ten_downloads",    "name_en": "🏆 10 Downloads",     "name_ar": "🏆 10 تحميلات",       "condition": "downloads",  "threshold": 10},
    {"id": "fifty_downloads",  "name_en": "🏆 50 Downloads",     "name_ar": "🏆 50 تحميلاً",       "condition": "downloads",  "threshold": 50},
    {"id": "hundred_downloads","name_en": "🏆 100 Downloads",    "name_ar": "🏆 100 تحميل",        "condition": "downloads",  "threshold": 100},
    {"id": "first_referral",   "name_en": "🌟 First Referral",   "name_ar": "🌟 أول دعوة",         "condition": "referrals",  "threshold": 1},
    {"id": "five_referrals",   "name_en": "🌟 5 Referrals",      "name_ar": "🌟 5 دعوات",          "condition": "referrals",  "threshold": 5},
    {"id": "fifty_referrals",  "name_en": "🌟 50 Referrals",     "name_ar": "🌟 50 دعوة",          "condition": "referrals",  "threshold": 50},
    {"id": "daily_streak_7",   "name_en": "📅 7-Day Streak",     "name_ar": "📅 7 أيام متواصلة",   "condition": "daily",      "threshold": 7},
    {"id": "wheel_winner",     "name_en": "🎰 Lucky Winner",     "name_ar": "🎰 الفائز المحظوظ",   "condition": "wheel",      "threshold": 1},
]

def get_achievement_name(ach: dict, lang: str) -> str:
    if lang == "ar":
        return ach.get("name_ar", ach.get("name_en", ach["id"]))
    return ach.get("name_en", ach["id"])

COMMUNITY_GOALS = [
    {"threshold": 500,  "reward": "Unlock Snapchat Support"},
    {"threshold": 1000, "reward": "Unlock Spotify Audio"},
    {"threshold": 2500, "reward": "Unlock HD Priority Queue"},
    {"threshold": 5000, "reward": "Unlock 4K Downloads"},
]

# All supported domains for URL validation
SUPPORTED_DOMAINS = [
    # YouTube
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    # TikTok
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    # Instagram
    "instagram.com", "instagr.am",
    # Facebook
    "facebook.com", "fb.watch", "fb.com",
    # Twitter / X
    "twitter.com", "x.com", "t.co",
    # Threads
    "threads.net",
    # Reddit
    "reddit.com", "redd.it", "v.redd.it",
    # Pinterest
    "pinterest.com", "pin.it", "pinterest.fr", "pinterest.co.uk",
    # Snapchat
    "snapchat.com", "story.snapchat.com",
    # Vimeo
    "vimeo.com",
    # Dailymotion
    "dailymotion.com", "dai.ly",
    # SoundCloud
    "soundcloud.com",
    # Spotify
    "spotify.com", "open.spotify.com",
    # Telegram
    "t.me", "telegram.me",
]

BROADCAST_BATCH_SIZE = 30
BROADCAST_DELAY = 0.3

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
