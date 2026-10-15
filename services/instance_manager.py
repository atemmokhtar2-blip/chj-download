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
    # Validity guard: an expired clone NEVER boots — not on process restart,
    # not on a manual owner "start". It must be renewed first (extend_instance).
    if referral.is_instance_expired(instance):
        referral.mark_instance_expired(instance_id)
        logger.info("Clone instance %s refused to boot: plan validity expired.", instance_id)
        return False
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


async def expire_due_instances() -> int:
    """Stop + expire every ACTIVE clone whose plan validity has ended, and
    notify its owner. Called every 15 min by workers.cleanup.clone_expiry_worker.

    An expired clone only returns to 'active' through an owner-approved
    renewal (referral.extend_instance from the owner panel).
    """
    if not ENABLE_CLONE_INSTANCES:
        return 0
    try:
        due = referral.list_expired_due_instances()
    except Exception:
        logger.exception("Could not list expired-due instances")
        return 0
    count = 0
    for inst in due:
        inst_id = int(inst["id"])
        try:
            await stop_instance(inst_id)
            referral.mark_instance_expired(inst_id)
            plan_label = "\U0001F451 Pro" if (inst.get("plan") or "basic") == "pro" else "\U0001F193 Basic"
            referral.notify_user(
                int(inst.get("owner_id")),
                f"\u231b <b>\u0627\u0646\u062a\u0647\u062a \u0645\u062f\u0629 \u0646\u0633\u062e\u062a\u0643 \u0627\u0644\u062e\u0627\u0635\u0629</b> @{inst.get('bot_username') or ''}\n\n"
                f"\U0001F4E6 \u062e\u0637\u062a\u0643: {plan_label}\n"
                "\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0644\u0646\u0633\u062e\u0629 \u062a\u0644\u0642\u0627\u0626\u064a\u064b\u0627 \u0644\u0627\u0646\u062a\u0647\u0627\u0621 \u0645\u062f\u0629 \u0627\u0644\u062e\u0637\u0629.\n"
                "\u0627\u0644\u062a\u062c\u062f\u064a\u062f \u064a\u062a\u0645 \u0628\u0642\u0631\u0627\u0631 \u0627\u0644\u0645\u0627\u0644\u0643 \u2014 \u0627\u0641\u062a\u062d \u062a\u0630\u0643\u0631\u0629 \u0645\u0646 \U0001F6DF \u0627\u0644\u062f\u0639\u0645 \u0627\u0644\u0641\u0646\u064a "
                "\u062f\u0627\u062e\u0644 \u0627\u0644\u0628\u0648\u062a \u0627\u0644\u0623\u0633\u0627\u0633\u064a \u0644\u0637\u0644\u0628 \u0627\u0644\u062a\u062c\u062f\u064a\u062f.",
            )
            logger.info("Clone instance %s expired and stopped.", inst_id)
            count += 1
        except Exception:
            logger.exception("Expiry enforcement failed id=%s", inst_id)
    if count:
        logger.info("Expired %s clone instance(s).", count)
    return count


def running_instance_ids() -> list[int]:
    return list(_running.keys())
