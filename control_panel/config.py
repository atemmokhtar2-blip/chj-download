import os
import sys

# Root of the entire project (one level up from extracted_project)
_HERE = os.path.dirname(os.path.abspath(__file__))
CONTROL_PANEL_DIR = _HERE
EXTRACTED_DIR = os.path.dirname(_HERE)
PROJECT_ROOT = os.path.dirname(EXTRACTED_DIR)

# Make extracted_project importable
if EXTRACTED_DIR not in sys.path:
    sys.path.insert(0, EXTRACTED_DIR)

# Auth
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID", "0").isdigit() else 0
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SECRET_KEY = os.getenv("SESSION_SECRET", "titanx-control-panel-secret-key-2026")

# Public URL — works in dev (REPLIT_DEV_DOMAIN) and production (REPLIT_DOMAINS)
_dev_domain    = os.getenv("REPLIT_DEV_DOMAIN", "")
_prod_domains  = os.getenv("REPLIT_DOMAINS", "")
_prod_primary  = _prod_domains.split(",")[0].strip() if _prod_domains else ""
if _dev_domain:
    PUBLIC_URL = f"https://{_dev_domain}"
elif _prod_primary:
    PUBLIC_URL = f"https://{_prod_primary}"
else:
    PUBLIC_URL = os.getenv("PUBLIC_URL", "")

# Database paths
MAIN_DB = os.path.join(EXTRACTED_DIR, "database", "bot.db")
SUPPORT_DB = os.path.join(EXTRACTED_DIR, "database", "support.db")
DEV_DB = os.path.join(EXTRACTED_DIR, "database", "developer.db")

# Directories
LOGS_DIR = os.path.join(EXTRACTED_DIR, "logs")
TEMP_DIR = os.path.join(EXTRACTED_DIR, "temp")
BACKUPS_DIR = os.path.join(EXTRACTED_DIR, "backups")

PROTECTED_NAMES = {".env", "bot.db", "developer.db", "support.db"}
PROTECTED_DIRS = {"database"}

MAX_VIEW_BYTES = 100_000
MAX_EDIT_BYTES = 200_000

# Bot scripts
BOT_SCRIPTS = {
    "main":    os.path.join(EXTRACTED_DIR, "bot.py"),
    "admin":   os.path.join(EXTRACTED_DIR, "admin_bot", "bot.py"),
    "support": os.path.join(EXTRACTED_DIR, "support_bot", "bot.py"),
    "dev":     os.path.join(EXTRACTED_DIR, "developer_bot", "bot.py"),
}

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(BACKUPS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
