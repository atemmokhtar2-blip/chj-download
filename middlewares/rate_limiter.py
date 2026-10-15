from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from collections import defaultdict, deque

from config.settings import RATE_LIMIT_SECONDS, RATE_LIMIT_BURST, HOURLY_DOWNLOAD_LIMIT, DAILY_DOWNLOAD_LIMIT

_recent: dict[int, deque[float]] = defaultdict(deque)
_downloads_hour: dict[int, deque[float]] = defaultdict(deque)
_downloads_day: dict[int, deque[float]] = defaultdict(deque)
# Check and record requests atomically: Telegram updates may be handled concurrently.
_lock = threading.RLock()

# Per-user deques only trim on that user's NEXT access; a one-time user would
# otherwise linger in memory forever. Once the tracker grows past this cap,
# entries whose windows have fully expired are evicted — dropping them is a
# no-op for enforcement (they can never be "counted" again outside their
# window) but keeps the three dicts bounded on a long-running bot.
_MAX_TRACKED_USERS = 100_000


def _evict_stale_entries(now: float | None = None) -> int:
    """Drop idle, fully-expired user entries when the dicts are oversized.

    MUST be called with ``_lock`` held. Returns the number of user ids evicted
    from the burst window (the other two buckets are swept alongside it).
    """
    if len(_recent) <= _MAX_TRACKED_USERS:
        return 0
    current = time.monotonic() if now is None else now
    stale: set[int] = set()
    for uid, q in _recent.items():
        if not q or current - q[-1] >= 86400:
            stale.add(uid)
    for uid in stale:
        _recent.pop(uid, None)
        _downloads_hour.pop(uid, None)
        _downloads_day.pop(uid, None)
    return len(stale)


@dataclass
class RateLimitResult:
    allowed: bool
    reason: str = ""
    wait_seconds: int = 0


def _trim(q: deque[float], window: int, now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    while q and current - q[0] >= window:
        q.popleft()


def _check_rate_limit_locked(user_id: int, now: float) -> RateLimitResult:
    _evict_stale_entries(now)
    recent = _recent[user_id]
    hour = _downloads_hour[user_id]
    day = _downloads_day[user_id]
    _trim(recent, RATE_LIMIT_SECONDS, now)
    _trim(hour, 3600, now)
    _trim(day, 86400, now)

    if len(day) >= DAILY_DOWNLOAD_LIMIT:
        return RateLimitResult(False, "daily", max(1, int(86400 - (now - day[0]))))
    if len(hour) >= HOURLY_DOWNLOAD_LIMIT:
        return RateLimitResult(False, "hourly", max(1, int(3600 - (now - hour[0]))))
    if len(recent) >= RATE_LIMIT_BURST:
        return RateLimitResult(False, "burst", max(1, int(RATE_LIMIT_SECONDS - (now - recent[0]))))
    return RateLimitResult(True)


def check_rate_limit_detailed(user_id: int) -> RateLimitResult:
    with _lock:
        return _check_rate_limit_locked(user_id, time.monotonic())


def mark_download(user_id: int) -> bool:
    # Keep the lock held through the check and all three appends. Otherwise two
    # concurrent requests can both pass the check and exceed the configured limit.
    with _lock:
        now = time.monotonic()
        result = _check_rate_limit_locked(user_id, now)
        if not result.allowed:
            return False
        _recent[user_id].append(now)
        _downloads_hour[user_id].append(now)
        _downloads_day[user_id].append(now)
        return True
