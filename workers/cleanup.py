import asyncio
import os
import time
from config.settings import TEMP_DIR


async def cleanup_temp_files(max_age_seconds: int = 3600):
    while True:
        try:
            now = time.time()
            for name in os.listdir(TEMP_DIR):
                path = os.path.join(TEMP_DIR, name)
                if os.path.isfile(path) and now - os.path.getmtime(path) > max_age_seconds:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        except Exception:
            pass
        await asyncio.sleep(900)


async def cleanup_old_cache():
    while True:
        await asyncio.sleep(3600)


async def cleanup_old_history():
    """Apply each user's history-retention preference periodically."""
    while True:
        try:
            from database.user_settings import prune_expired_history
            removed = prune_expired_history()
            if removed:
                import logging
                logging.getLogger(__name__).info("Pruned %s expired history rows", removed)
        except Exception:
            pass
        await asyncio.sleep(3600)


async def clone_expiry_worker():
    """Enforce clone-plan validity windows.

    Every 15 minutes: any ACTIVE clone whose ``expires_at`` has passed is
    stopped, marked ``expired`` in the DB, and its owner is notified. An
    expired clone never boots again until an owner-approved renewal.
    """
    await asyncio.sleep(60)
    while True:
        try:
            from services.instance_manager import expire_due_instances
            await expire_due_instances()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Clone expiry worker failed")
        await asyncio.sleep(900)
