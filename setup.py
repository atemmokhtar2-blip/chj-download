"""
PrimeDownloader — Setup Wizard
Checks configuration, verifies database, and tests Telegram connectivity.
Run this before starting the bot: python setup.py
"""

import os
import sys
import sqlite3
import asyncio
import textwrap
from pathlib import Path

# ── Colour helpers ──────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):    return f"{GREEN}✅ {msg}{RESET}"
def fail(msg):  return f"{RED}✗  FAIL  {msg}{RESET}"
def warn(msg):  return f"{YELLOW}⚠  WARN  {msg}{RESET}"
def info(msg):  return f"{CYAN}ℹ  {msg}{RESET}"
def header(msg):return f"\n{BOLD}{CYAN}{'━'*60}\n  {msg}\n{'━'*60}{RESET}"

# ── Required variables ───────────────────────────────────────────────────────

REQUIRED_SECRETS = {
    "TELEGRAM_BOT_TOKEN": {
        "label":   "Telegram Bot Token",
        "hint":    "Get from @BotFather — looks like 123456789:ABCdef...",
        "secret":  True,
    },
}

REQUIRED_VARS = {
    "REQUIRED_CHANNEL": {
        "label":   "Required Channel",
        "hint":    "Username (e.g. @mychannel) or chat ID users must join",
        "secret":  False,
        "optional": False,
    },
    "OWNER_ID": {
        "label":   "Owner Telegram ID",
        "hint":    "Your Telegram numeric user ID — from @userinfobot",
        "secret":  False,
        "optional": False,
    },
    "ADMIN_IDS": {
        "label":   "Admin IDs (optional)",
        "hint":    "Comma-separated Telegram IDs for extra admins, e.g. 111,222",
        "secret":  False,
        "optional": True,
    },
}

# ── Step 1 — Configuration status ───────────────────────────────────────────

def check_config() -> tuple[dict, list]:
    """
    Returns (status_map, missing_required_keys).
    status_map[key] = True/False (present / missing)
    """
    status = {}
    missing = []

    for key, meta in {**REQUIRED_SECRETS, **REQUIRED_VARS}.items():
        val = os.environ.get(key, "").strip()
        present = bool(val)
        status[key] = present
        if not present and not meta.get("optional", False):
            missing.append(key)

    return status, missing


def print_config_report(status: dict):
    print(header("CONFIGURATION STATUS"))
    for key, meta in {**REQUIRED_SECRETS, **REQUIRED_VARS}.items():
        label = meta["label"]
        opt   = " (optional)" if meta.get("optional") else ""
        if status[key]:
            print(f"  {ok(key + opt)}")
        else:
            if meta.get("optional"):
                print(f"  {warn(key + opt)}")
            else:
                print(f"  {fail(key + opt)}")


# ── Step 2 — Database verification ──────────────────────────────────────────

def check_database() -> tuple[bool, str]:
    """Returns (ok, message)."""
    try:
        base = Path(__file__).parent
        db_path = base / "database" / "bot.db"

        if not (base / "database").exists():
            return False, "database/ directory not found — extract the bot files first"

        if not db_path.exists():
            return False, f"bot.db not found at {db_path}"

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        expected_tables = ["users", "downloads", "referrals", "reports",
                           "favorites", "file_cache", "achievements",
                           "feedback", "rewards_log", "referral_audit_log"]
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing = {row["name"] for row in cur.fetchall()}
        conn.close()

        missing = [t for t in expected_tables if t not in existing]
        if missing:
            return False, f"Missing tables: {', '.join(missing)}"

        return True, f"Database OK — found at {db_path.relative_to(base)}"
    except Exception as exc:
        return False, str(exc)


# ── Step 3 — Telegram connectivity ──────────────────────────────────────────

async def _test_telegram(token: str) -> tuple[bool, str]:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        data = r.json()
        if data.get("ok"):
            bot = data["result"]
            return True, f"@{bot['username']} (id {bot['id']})"
        return False, data.get("description", "Unknown error")
    except ImportError:
        try:
            import urllib.request, json
            token_safe = token.replace(":", "%3A")
            url = f"https://api.telegram.org/bot{token}/getMe"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            if data.get("ok"):
                bot = data["result"]
                return True, f"@{bot['username']} (id {bot['id']})"
            return False, data.get("description", "Unknown error")
        except Exception as exc:
            return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def check_telegram() -> tuple[bool, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False, "TELEGRAM_BOT_TOKEN not set"
    return asyncio.run(_test_telegram(token))


# ── Main report ──────────────────────────────────────────────────────────────

def run_verification():
    print(header("RUNNING VERIFICATION CHECKS"))

    results = {}

    # 1. Config
    status, missing = check_config()
    config_pass = len(missing) == 0
    results["Configuration"] = (config_pass, "All required vars present" if config_pass
                                  else f"Missing: {', '.join(missing)}")

    # 2. Database
    db_pass, db_msg = check_database()
    results["Database"] = (db_pass, db_msg)

    # 3. Telegram
    print(info("Testing Telegram API connection…"))
    tg_pass, tg_msg = check_telegram()
    results["Telegram API"] = (tg_pass, tg_msg)

    # Print results
    print()
    all_pass = True
    for check, (passed, message) in results.items():
        if passed:
            print(f"  {ok(check):<40}  {message}")
        else:
            print(f"  {fail(check):<40}  {message}")
            all_pass = False

    print()
    if all_pass:
        print(f"{BOLD}{GREEN}{'━'*60}")
        print("  ✅  ALL CHECKS PASSED — bot is ready to start")
        print(f"{'━'*60}{RESET}")
    else:
        print(f"{BOLD}{RED}{'━'*60}")
        print("  ✗   SOME CHECKS FAILED — fix the issues above before starting the bot")
        print(f"{'━'*60}{RESET}")

    return all_pass


# ── Interactive prompt (fallback if env vars missing at runtime) ─────────────

def interactive_prompt(missing_keys: list):
    """Prints clear instructions for each missing variable."""
    all_meta = {**REQUIRED_SECRETS, **REQUIRED_VARS}
    print(header("MISSING CONFIGURATION"))
    print(textwrap.dedent("""
  These variables are required before the bot can start.
  Add them via the Replit Secrets panel (🔒 padlock icon in the sidebar),
  or set them in a .env file in the project root.
    """))
    for key in missing_keys:
        meta = all_meta[key]
        label = meta["label"]
        hint  = meta["hint"]
        secret_note = "  [store as SECRET — never commit to code]" if meta.get("secret") else "  [env var]"
        print(f"  {BOLD}{key}{RESET}")
        print(f"    {label}")
        print(f"    {hint}")
        print(f"    {YELLOW}{secret_note}{RESET}")
        print()


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}{CYAN}")
    print("  ██████╗ ██████╗ ██╗███╗   ███╗███████╗")
    print("  ██╔══██╗██╔══██╗██║████╗ ████║██╔════╝")
    print("  ██████╔╝██████╔╝██║██╔████╔██║█████╗  ")
    print("  ██╔═══╝ ██╔══██╗██║██║╚██╔╝██║██╔══╝  ")
    print("  ██║     ██║  ██║██║██║ ╚═╝ ██║███████╗")
    print("  ╚═╝     ╚═╝  ╚═╝╚═╝╚═╝     ╚═╝╚══════╝")
    print(f"  PrimeDownloader — Setup Wizard{RESET}\n")

    status, missing = check_config()
    print_config_report(status)

    if missing:
        interactive_prompt(missing)
        print(f"{RED}Setup incomplete. Provide the missing values and run setup.py again.{RESET}\n")
        sys.exit(1)

    all_pass = run_verification()
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
