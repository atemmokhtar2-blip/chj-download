"""
Global concurrency control for simultaneous downloads.

- Per-user: only 1 active download (existing active_downloads dict)
- Global: MAX_CONCURRENT_DOWNLOADS across all users (semaphore)
- Thread pool: bounded executor so yt-dlp / scraper threads cannot explode RAM
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from config.settings import MAX_CONCURRENT_DOWNLOADS

# Global slot limit (how many downloads may run at once process-wide)
_download_semaphore: asyncio.Semaphore | None = None

# Bounded thread pool for blocking yt-dlp / HTTP work
_DOWNLOAD_WORKERS = max(4, min(int(MAX_CONCURRENT_DOWNLOADS) * 2, 16))
_executor: ThreadPoolExecutor | None = None


def get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=_DOWNLOAD_WORKERS,
            thread_name_prefix="dl-worker",
        )
    return _executor


def get_download_semaphore() -> asyncio.Semaphore:
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(max(1, int(MAX_CONCURRENT_DOWNLOADS)))
    return _download_semaphore


def active_global_slots() -> tuple[int, int]:
    """Return (in_use, max) for monitoring."""
    sem = get_download_semaphore()
    # asyncio.Semaphore stores value privately as _value
    max_slots = max(1, int(MAX_CONCURRENT_DOWNLOADS))
    free = getattr(sem, "_value", max_slots)
    return max_slots - free, max_slots


@asynccontextmanager
async def download_slot(timeout: float = 120.0):
    """
    Acquire a global download slot. Waits up to `timeout` seconds.
    Raises asyncio.TimeoutError if the queue is full too long.
    """
    sem = get_download_semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        raise
    try:
        yield
    finally:
        sem.release()
