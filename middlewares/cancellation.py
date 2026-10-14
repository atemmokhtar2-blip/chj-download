from __future__ import annotations

import threading
import time

# Cancellation registry keyed by user_id. The bot enforces one active download
# per user (middlewares/user_locks), so a single flag per user is sufficient and
# avoids threading cancel tokens through every engine/downloader signature.
#
# How it works end-to-end:
#   1. User presses the 🛑 button -> ``request_cancel(user_id)``.
#   2. Every long-running loop (yt-dlp progress hook, direct CDN chunk loop,
#      fallback-matrix attempts, album items, pre-upload checks) calls
#      ``check_cancelled(user_id)`` which raises ``DownloadCancelledError``.
#   3. The exception propagates up, temp files are cleaned, and the user sees a
#      clear "download cancelled" message.


class DownloadCancelledError(Exception):
    """Raised inside download loops when the user pressed the cancel button."""


_lock = threading.RLock()
_cancelled: dict[int, float] = {}
# Stale flags are purged after this many seconds so a missed cleanup can never
# permanently block a user's future downloads.
_TTL_SECONDS = 600


def request_cancel(user_id: int) -> bool:
    """Mark the user's active download as cancelled. Returns True if a flag was set."""
    with _lock:
        _purge_stale_locked()
        _cancelled[int(user_id)] = time.monotonic()
        return True


def clear_cancel(user_id: int) -> None:
    """Remove any pending cancel flag (called when a download starts/ends)."""
    with _lock:
        _cancelled.pop(int(user_id), None)


def is_cancel_requested(user_id: int) -> bool:
    with _lock:
        ts = _cancelled.get(int(user_id))
        if ts is None:
            return False
        if time.monotonic() - ts > _TTL_SECONDS:
            _cancelled.pop(int(user_id), None)
            return False
        return True


def check_cancelled(user_id: int) -> None:
    """Raise ``DownloadCancelledError`` when the user asked to cancel.

    Safe to call from worker threads (yt-dlp progress hooks, requests chunk
    loops) — the registry is lock-guarded and non-blocking.
    """
    if is_cancel_requested(user_id):
        raise DownloadCancelledError(f"download cancelled by user {user_id}")


def _purge_stale_locked() -> None:
    now = time.monotonic()
    stale = [uid for uid, ts in _cancelled.items() if now - ts > _TTL_SECONDS]
    for uid in stale:
        _cancelled.pop(uid, None)
