import os
from dotenv import load_dotenv

load_dotenv()

try:
    with open("/home/user/app/token.txt", "r") as f:
        BOT_TOKEN = f.read().strip()
except:
    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "7631249810").split(",") if x.strip().isdigit()]
OWNER_ID = int(os.getenv("OWNER_ID", "7631249810")) if os.getenv("OWNER_ID", "7631249810").isdigit() else 7631249810

DATABASE_PATH = os.path.join(os.getcwd(), "database", "bot.db")
TEMP_DIR = os.path.join(os.getcwd(), "temp")
LOGS_DIR = os.path.join(os.getcwd(), "logs")

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "10"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5"))

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", "1000"))

SUPPORTED_DOMAINS = [
    "youtube.com", "youtu.be", "youtube-nocookie.com", "m.youtube.com",
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com",
    "instagram.com", "instagr.am", "l.instagram.com",
    "facebook.com", "fb.watch", "fb.com", "m.facebook.com",
    "twitter.com", "x.com", "t.co", "nitter.net",
    "threads.net", "www.threads.net",
    "reddit.com", "redd.it", "v.redd.it", "old.reddit.com",
    "pinterest.com", "pin.it", "pinterest.fr", "pinterest.co.uk",
    "pinterest.de", "pinterest.jp", "pinterest.ca", "pinterest.es",
    "pinterest.it", "pinterest.com.au", "pinterest.com.mx",
    "pinterest.ca", "pinterest.nz",
    "snapchat.com", "story.snapchat.com",
    "vimeo.com", "player.vimeo.com",
    "dailymotion.com", "dai.ly", "www.dailymotion.com",
    "soundcloud.com", "m.soundcloud.com",
    "spotify.com", "open.spotify.com", "play.spotify.com",
    "t.me", "telegram.me", "telegram.org",
    "likee.video", "likee.com",
]

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
