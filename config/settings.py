import os
from dotenv import load_dotenv

load_dotenv()

# Prefer environment variables in production. Never print or expose the token.
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "7631249810").split(",") if x.strip().isdigit()]
OWNER_ID = int(os.getenv("OWNER_ID", "7631249810")) if os.getenv("OWNER_ID", "7631249810").isdigit() else 7631249810

def _resolve_database_path() -> str:
    """Persistent SQLite location across redeploys/restarts.

    Hosts like Railway / Render mount persistent disks at ``/data``
    while the app directory itself is rebuilt from git on every deploy, so a
    default ``database/bot.db`` next to the code is wiped each time and users
    "disappear". Priority:
      1. ``DATABASE_PATH`` env var (explicit, any host)
      2. ``DATA_DIR`` env var (a directory that is already a persistent volume)
      3. ``/data`` when the platform exposes it
      4. the legacy repo-relative path (local development)
    """
    explicit = os.getenv("DATABASE_PATH", "").strip()
    if explicit:
        return explicit

    data_dir = os.getenv("DATA_DIR", "").strip()
    if data_dir:
        return os.path.join(data_dir, "bot.db")

    if os.path.isdir("/data"):
        return os.path.join("/data", "bot.db")

    return os.path.join(os.getcwd(), "database", "bot.db")


DATABASE_PATH = _resolve_database_path()
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
# Batch handling: how many URLs may be processed in a single multi-link message.
BATCH_MAX_URLS = min(int(os.getenv("BATCH_MAX_URLS", "10")), 50)
# Audio MP3 extraction bitrate (LAME VBR-style numeric target, e.g. 128/192/320).
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "192").strip() or "192"
AUDIO_BITRATE_HIGH = os.getenv("AUDIO_BITRATE_HIGH", "320").strip() or "320"
# When a video exceeds Telegram's upload limit, split it into parts instead of
# refusing the download. Disable with AUTO_SPLIT_LARGE_VIDEOS=0.
AUTO_SPLIT_LARGE_VIDEOS = os.getenv("AUTO_SPLIT_LARGE_VIDEOS", "1") == "1"
# Optional: redis://[:password]@host:6379/0  (also REDIS_URL / RATE_LIMIT_REDIS_URL)
REDIS_URL = os.getenv("REDIS_URL", os.getenv("RATE_LIMIT_REDIS_URL", "")).strip()
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID", os.getenv("MEDIA_VAULT_CHANNEL_ID", "")).strip()
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5"))

# Brand shown on every delivered media caption ("downloaded by …").
BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "〆 𝑿𝑬𝑳𝑽𝑶𝑹").strip()

# Advanced download engine knobs. Leave empty unless needed.
# YTDLP_COOKIES_FILE: path to Netscape cookies.txt for Instagram/Facebook/YouTube restricted media.
# YTDLP_COOKIES_FROM_BROWSER: e.g. chrome, firefox, edge (only works where browser profiles exist).
# DOWNLOAD_PROXY: http/socks proxy for regions where a platform blocks the server IP.
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "cookies.txt").strip()
YTDLP_COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
DOWNLOAD_PROXY = os.getenv("DOWNLOAD_PROXY", "").strip()
YTDLP_CLIENT = os.getenv("YTDLP_CLIENT", "android").strip()

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", "1000"))

# ---- Referral network & clone instances ------------------------------------
# MAIN_BOT_USERNAME: the official bot's @username (no @). Used for referral
# deep links and the "العودة إلى البوت الأساسي" button inside every clone.
MAIN_BOT_USERNAME = os.getenv("MAIN_BOT_USERNAME", "").strip().lstrip("@")
# Qualified referrals required to earn a clone instance.
REFERRAL_TARGET = int(os.getenv("REFERRAL_TARGET", "70"))
# Encryption key for stored clone bot tokens (any long random string).
# If empty, a key is derived from TELEGRAM_BOT_TOKEN — setting this explicitly
# is strongly recommended in production.
BOT_SECRETS_KEY = os.getenv("BOT_SECRETS_KEY", "").strip()
# Master switch for running earned clone instances in this process.
ENABLE_CLONE_INSTANCES = os.getenv("ENABLE_CLONE_INSTANCES", "1") == "1"

# ---- Clone plans & validity -------------------------------------------------
# Every clone runs on a time-limited plan:
#   🆓 basic = CLONE_PLAN_BASIC_DAYS days   👑 pro = CLONE_PLAN_PRO_DAYS days.
# Pro is earned automatically at activation by reaching CLONE_PRO_TARGET_MULTIPLIER
# × the referral target in QUALIFIED referrals. When a clone's validity window
# ends it is stopped automatically and marked 'expired'; it NEVER boots again
# until an owner-approved renewal/extension (owner panel → إدارة النسخ).
CLONE_PLAN_BASIC_DAYS = int(os.getenv("CLONE_PLAN_BASIC_DAYS", "30"))
CLONE_PLAN_PRO_DAYS = int(os.getenv("CLONE_PLAN_PRO_DAYS", "40"))
CLONE_PRO_TARGET_MULTIPLIER = float(os.getenv("CLONE_PRO_TARGET_MULTIPLIER", "2"))
# Neon/Postgres DSN is resolved in database/pg.py from NEON_DATABASE_URL /
# POSTGRES_URL / DATABASE_URL (postgres:// prefix). Without one, the referral
# tables live in the same SQLite file as the rest of the bot.

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
    "likee.video", "likee.com", "l.likee.video", "mobile.likee.video",
    "kwai.com", "kw.ai", "s.kw.ai", "m.kwai.com", "kwai-video.com",
    "twitch.tv", "clips.twitch.tv", "m.twitch.tv", "player.twitch.tv",
    "bilibili.com", "b23.tv", "bilibili.tv", "m.bilibili.com",
    "vk.com", "vkvideo.ru", "vk.cc", "m.vk.com", "vkvideo.cc",
    "tumblr.com", "at.tumblr.com", "tmblr.co",
    "imgur.com", "i.imgur.com", "m.imgur.com", "stack.imgur.com",
    "streamable.com",
    "coub.com",
    "ok.ru", "m.ok.ru", "odnoklassniki.ru", "m.odnoklassniki.ru",
    "rumble.com", "www.rumble.com",
    "odysee.com", "www.odysee.com",
    "nicovideo.jp", "nico.ms", "www.nicovideo.jp",
    "douyin.com", "v.douyin.com", "www.douyin.com", "m.douyin.com",
    "xiaohongshu.com", "xhslink.com", "www.xiaohongshu.com", "m.xiaohongshu.com",
]

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
