import os
from dotenv import load_dotenv

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(_root, ".env"))

ADMIN_BOT_TOKEN      = os.getenv("ADMIN_BOT_TOKEN", "")
OWNER_ID             = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID", "").isdigit() else 0
ADMIN_IDS            = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
REQUIRED_CHANNEL     = os.getenv("REQUIRED_CHANNEL", "")
SUPPORT_BOT_USERNAME = os.getenv("SUPPORT_BOT_USERNAME", "").strip().lstrip("@")

MAIN_DB_PATH    = os.path.join(_root, "database", "bot.db")
SUPPORT_DB_PATH = os.path.join(_root, "database", "support.db")
