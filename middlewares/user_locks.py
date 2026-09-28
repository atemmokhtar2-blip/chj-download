from __future__ import annotations

from contextlib import asynccontextmanager

_active_user_locks: set[int] = set()


def is_user_busy(user_id: int) -> bool:
    """Return True when the user already has an in-flight download action."""
    return user_id in _active_user_locks


def try_acquire_user_download(user_id: int) -> bool:
    """Acquire a per-user download lock. Returns False when already busy."""
    if user_id in _active_user_locks:
        return False
    _active_user_locks.add(user_id)
    return True


def release_user_download(user_id: int) -> None:
    """Release a per-user download lock; safe to call repeatedly."""
    _active_user_locks.discard(user_id)


@asynccontextmanager
async def user_download_lock(user_id: int):
    """Small in-process guard against double-click/download races per user."""
    acquired = try_acquire_user_download(user_id)
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        release_user_download(user_id)
