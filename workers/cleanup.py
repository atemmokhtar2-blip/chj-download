import asyncio
import os
import logging
from config.settings import TEMP_DIR

logger = logging.getLogger("system")


async def cleanup_temp_files():
    """Delete temp files older than 1 hour."""
    while True:
        try:
            import time
            now = time.time()
            count = 0
            for fname in os.listdir(TEMP_DIR):
                fpath = os.path.join(TEMP_DIR, fname)
                if os.path.isfile(fpath):
                    age = now - os.path.getmtime(fpath)
                    if age > 3600:
                        os.remove(fpath)
                        count += 1
            if count:
                logger.info(f"Cleanup: removed {count} temp files.")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(1800)


async def cleanup_old_cache():
    """Remove file_cache entries older than 30 days."""
    while True:
        try:
            from database.cache import cleanup_old_cache as db_cleanup
            db_cleanup(30)
            logger.info("Cache cleanup complete.")
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")
        await asyncio.sleep(86400)
