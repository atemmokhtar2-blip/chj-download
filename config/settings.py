import os
from dotenv import load_dotenv

load_dotenv()

# Prefer environment variables in production. Fall back to local token.txt only
# for legacy/HuggingFace-style deployments. Never print or expose the token.
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    for token_path in (
        os.path.join(os.getcwd(), "token.txt"),
        "/home/user/app/token.txt",
    ):
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                BOT_TOKEN = f.read().strip()
                if BOT_TOKEN:
                    break
        except OSError:
            continue

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "7631249810").split(",") if x.strip().isdigit()]
OWNER_ID = int(os.getenv("OWNER_ID", "7631249810")) if os.getenv("OWNER_ID", "7631249810").isdigit() else 7631249810

DATABASE_PATH = os.path.join(os.getcwd(), "database", "bot.db")
TEMP_DIR = os.path.join(os.getcwd(), "temp")
LOGS_DIR = os.path.join(os.getcwd(), "logs")

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))
# Moving-window cooldown: RATE_LIMIT_BURST hits allowed per RATE_LIMIT_SECONDS.
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "15"))
RATE_LIMIT_BURST = int(os.getenv("RATE_LIMIT_BURST", "2"))
# Moving-window quotas (`limits` library; SQLite backstop without Redis).
HOURLY_DOWNLOAD_LIMIT = int(os.getenv("HOURLY_DOWNLOAD_LIMIT", "30"))
DAILY_DOWNLOAD_LIMIT = int(os.getenv("DAILY_DOWNLOAD_LIMIT", "100"))
# Optional: redis://[:password]@host:6379/0  (also REDIS_URL / RATE_LIMIT_REDIS_URL)
REDIS_URL = os.getenv("REDIS_URL", os.getenv("RATE_LIMIT_REDIS_URL", "")).strip()
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID", os.getenv("MEDIA_VAULT_CHANNEL_ID", "")).strip()
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5"))

# Advanced download engine knobs. Leave empty unless needed.
# YTDLP_COOKIES_FILE: path to Netscape cookies.txt for Instagram/Facebook/YouTube restricted media.
# YTDLP_COOKIES_FROM_BROWSER: e.g. chrome, firefox, edge (only works where browser profiles exist).
# DOWNLOAD_PROXY: http/socks proxy for regions where a platform blocks the server IP.
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "cookies.txt").strip()
YTDLP_COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
DOWNLOAD_PROXY = os.getenv("DOWNLOAD_PROXY", "").strip()
YTDLP_CLIENT = os.getenv("YTDLP_CLIENT", "curl_cffi").strip()

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", "1000"))

SUPPORTED_DOMAINS = [
    "youtube.com", "youtu.be", "youtube-nocookie.com", "m.youtube.com", "music.youtube.com",
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com",
    "instagram.com", "instagr.am", "l.instagram.com", "cdninstagram.com",
    "facebook.com", "fb.watch", "fb.com", "m.facebook.com", "mobile.facebook.com", "facebookreel.com",
    "twitter.com", "x.com", "t.co", "nitter.net", "fixupx.com", "fxtwitter.com",
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
