# PrimeDownloader Bot — Migration Guide

How to move this project to a new Replit account with the minimum number of manual steps.

---

## What is already persistent (zero manual steps)

These values are stored in `bot/.env` and travel with the project files:

| Value | Stored in | Action needed |
|---|---|---|
| `REQUIRED_CHANNEL` | `bot/.env` | None — already in project |
| `OWNER_ID` | `bot/.env` | None — already in project |
| `ADMIN_IDS` | `bot/.env` | None — already in project |

---

## What requires one manual step

| Value | Why it can't travel | Action needed |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Secret — must never be committed | Add to Replit Secrets on the new account |

---

## Migration steps (new Replit account)

### Step 1 — Import the project
Fork or import this Repl into your new Replit account.
All source files, `bot/.env`, and dependencies come with it.

### Step 2 — Add the bot token as a Replit Secret
1. Open the new Repl
2. Go to **Tools → Secrets**
3. Add one secret:
   - Key: `TELEGRAM_BOT_TOKEN`
   - Value: your token from [@BotFather](https://t.me/BotFather)

### Step 3 — Start the workflow
The `PrimeDownloader Bot` workflow is already configured.
Click **Run** or start the workflow — the bot will come online immediately.

### Step 4 — (Optional) Add the bot as admin to your channel
If you use a required-join channel (`REQUIRED_CHANNEL` in `bot/.env`):
- Open that channel in Telegram
- Add your bot as an Administrator
- No code changes needed

---

## Changing config values after migration

To update `REQUIRED_CHANNEL`, `OWNER_ID`, or `ADMIN_IDS`:
1. Edit `bot/.env` directly
2. Restart the `PrimeDownloader Bot` workflow

To rotate `TELEGRAM_BOT_TOKEN`:
1. Go to **Tools → Secrets**
2. Update the `TELEGRAM_BOT_TOKEN` value
3. Restart the `PrimeDownloader Bot` workflow

---

## Config load order (how the bot resolves values)

```
bot/.env  (project file, committed)
    ↓  loaded by load_dotenv() at startup
os.getenv() calls in config/settings.py
    ↓
Replit Secrets override .env values if the same key is set in both
```

Replit Secrets always win over `.env`. This means the token in Secrets
safely overrides any accidental token in `.env` without requiring code changes.

---

## Summary: total manual steps to go live on a new account = 1

> Set `TELEGRAM_BOT_TOKEN` in Replit Secrets. Everything else is already in the project.
