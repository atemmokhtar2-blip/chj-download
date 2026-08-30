import asyncio
import os
import logging
from config.settings import TEMP_DIR

logger = logging.getLogger("system")

# Partial / failed downloads should not linger
TEMP_MAX_AGE_SECONDS = int(os.getenv("TEMP_MAX_AGE_SECONDS", "1800"))  # 30 min


async def cleanup_temp_files():
    """Delete temp files older than TEMP_MAX_AGE_SECONDS (default 30 min)."""
    while True:
        try:
            import time
            now = time.time()
            count = 0
            if not os.path.isdir(TEMP_DIR):
                os.makedirs(TEMP_DIR, exist_ok=True)
            for fname in os.listdir(TEMP_DIR):
                fpath = os.path.join(TEMP_DIR, fname)
                if not os.path.isfile(fpath):
                    continue
                try:
                    age = now - os.path.getmtime(fpath)
                    size = os.path.getsize(fpath)
                    # Empty or ancient files go first
                    if size == 0 or age > TEMP_MAX_AGE_SECONDS:
                        os.remove(fpath)
                        count += 1
                except OSError:
                    continue
            if count:
                logger.info("Cleanup: removed %s temp files.", count)
        except Exception as e:
            logger.error("Cleanup error: %s", e)
        await asyncio.sleep(900)  # every 15 min


async def cleanup_old_cache():
    """Remove file_cache entries older than configured TTL days."""
    while True:
        try:
            from database.cache import cleanup_old_cache as db_cleanup
            n = db_cleanup()
            logger.info("Cache cleanup complete (removed ~%s).", n)
        except Exception as e:
            logger.error("Cache cleanup error: %s", e)
        await asyncio.sleep(86400)
