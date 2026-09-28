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
