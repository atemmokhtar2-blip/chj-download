"""Central multi-bot instance manager.

One process, one shared codebase, many logical bots: every ACTIVE clone
(instance earned through referrals) runs as its own python-telegram-bot
``Application`` inside the main event loop, polling with its own token.

Isolation model (v1, central):
  * Each clone has its own token, username, ``bot_settings`` rows and audit
    trail in ``bot_instances`` / ``bot_secrets`` / ``bot_settings``.
  * Clones share the media engine and the referral network — new users who
    start a clone are registered with ``source="clone:<bot_id>"`` so the main
    bot's growth is attributed, and every clone shows a permanent
    "العودة إلى البوت الأساسي" button.
  * A crashing clone never takes the main bot down: spawn errors are caught,
    recorded in ``bot_instances.last_error`` and skipped.
"""

from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application

from config.settings import ENABLE_CLONE_INSTANCES
from services import referral

logger = logging.getLogger(__name__)

_running: dict[int, Application] = {}


def is_clone_bot(bot_username: str | None) -> bool:
    """True when the running bot is a clone instance, not the main bot."""
    from config.settings import MAIN_BOT_USERNAME
    if not MAIN_BOT_USERNAME or not bot_username:
        return False
    return str(bot_username).lower().lstrip("@") != str(MAIN_BOT_USERNAME).lower().lstrip("@")


async def spawn_instance(instance: dict) -> bool:
    """Build + start ONE clone Application from its DB row. Idempotent."""
    if not ENABLE_CLONE_INSTANCES:
        return False
    instance_id = int(instance["id"])
    if instance_id in _running:
        return True
    token = referral.load_instance_token(instance_id)
    if not token:
        referral.set_instance_error(instance_id, "missing_or_undecryptable_token")
        return False
    try:
        # Lazy import: bot.py imports this module, avoid circular import at load.
        from bot import build_application
        app = build_application(token=token, clone=True)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        _running[instance_id] = app
        logger.info("Clone instance %s (@%s) started.", instance_id, instance.get("bot_username"))
        referral.set_instance_error(instance_id, "")
        return True
    except Exception as exc:
        # Only the exception CLASS is persisted: ptb errors can embed request
        # URLs that contain the bot token, and last_error is readable in the DB.
        logger.error("Failed to start clone instance %s: %s", instance_id, type(exc).__name__)
        referral.set_instance_error(instance_id, type(exc).__name__[:120])
        return False


async def start_active_instances() -> None:
    """Boot every active clone after the main bot is up (called from post_init)."""
    if not ENABLE_CLONE_INSTANCES:
        return
    # Give the main bot a moment to settle before adding pollers.
    await asyncio.sleep(5)
    try:
        instances = referral.list_active_instances()
    except Exception:
        logger.exception("Could not list active instances")
        return
    started = 0
    for inst in instances:
        try:
            if await spawn_instance(inst):
                started += 1
        except Exception:
            logger.exception("Instance spawn crashed id=%s", inst.get("id"))
    if instances:
        logger.info("Clone instances: %s/%s started.", started, len(instances))


async def stop_instance(instance_id: int) -> None:
    app = _running.pop(int(instance_id), None)
    if app is None:
        return
    try:
        await app.stop()
        await app.shutdown()
    except Exception:
        logger.exception("Error shutting down instance %s", instance_id)


def running_instance_ids() -> list[int]:
    return list(_running.keys())
