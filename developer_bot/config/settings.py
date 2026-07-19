import os
from dotenv import load_dotenv

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(_root, ".env"))

DEVELOPER_BOT_TOKEN = os.getenv("DEVELOPER_BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID", "").isdigit() else 0

_project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAIN_DB_PATH    = os.path.join(_root, "database", "bot.db")
SUPPORT_DB_PATH = os.path.join(_root, "database", "support.db")
DEV_DB_PATH     = os.path.join(_root, "database", "developer.db")

LOGS_DIR        = os.path.join(_root, "logs")
TEMP_DIR        = os.path.join(_root, "temp")
BACKUPS_DIR     = os.path.join(_root, "backups")

MAIN_BOT_SCRIPT    = os.path.join(_root, "bot.py")
ADMIN_BOT_SCRIPT   = os.path.join(_root, "admin_bot", "bot.py")
SUPPORT_BOT_SCRIPT = os.path.join(_root, "support_bot", "bot.py")

PROTECTED_PATHS = [
    os.path.join(_root, "database"),
    os.path.join(_root, ".env"),
    os.path.join(_root, "config", "settings.py"),
]

os.makedirs(BACKUPS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
