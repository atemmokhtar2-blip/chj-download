from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from config.settings import MAX_CONCURRENT_DOWNLOADS

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
_executor = ThreadPoolExecutor(max_workers=max(2, MAX_CONCURRENT_DOWNLOADS))
_active = 0


def get_executor() -> ThreadPoolExecutor:
    """Shared bounded executor for blocking scraper/downloader helpers."""
    return _executor


def active_global_slots() -> tuple[int, int]:
    return _active, MAX_CONCURRENT_DOWNLOADS


@asynccontextmanager
async def download_slot(timeout: int = 180):
    global _active
    await asyncio.wait_for(_semaphore.acquire(), timeout=timeout)
    _active += 1
    try:
        yield
    finally:
        _active = max(0, _active - 1)
        _semaphore.release()
