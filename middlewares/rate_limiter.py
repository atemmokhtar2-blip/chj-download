from __future__ import annotations

import time
from dataclasses import dataclass
from collections import defaultdict, deque

from config.settings import RATE_LIMIT_SECONDS, RATE_LIMIT_BURST, HOURLY_DOWNLOAD_LIMIT, DAILY_DOWNLOAD_LIMIT

_recent: dict[int, deque[float]] = defaultdict(deque)
_downloads_hour: dict[int, deque[float]] = defaultdict(deque)
_downloads_day: dict[int, deque[float]] = defaultdict(deque)


@dataclass
class RateLimitResult:
    allowed: bool
    reason: str = ""
    wait_seconds: int = 0


def _trim(q: deque[float], window: int) -> None:
    now = time.time()
    while q and now - q[0] > window:
        q.popleft()


def check_rate_limit_detailed(user_id: int) -> RateLimitResult:
    now = time.time()
    recent = _recent[user_id]
    hour = _downloads_hour[user_id]
    day = _downloads_day[user_id]
    _trim(recent, RATE_LIMIT_SECONDS)
    _trim(hour, 3600)
    _trim(day, 86400)
    if len(day) >= DAILY_DOWNLOAD_LIMIT:
        return RateLimitResult(False, "daily", int(86400 - (now - day[0])))
    if len(hour) >= HOURLY_DOWNLOAD_LIMIT:
        return RateLimitResult(False, "hourly", int(3600 - (now - hour[0])))
    if len(recent) >= RATE_LIMIT_BURST:
        return RateLimitResult(False, "burst", max(1, int(RATE_LIMIT_SECONDS - (now - recent[0]))))
    return RateLimitResult(True)


def mark_download(user_id: int) -> bool:
    result = check_rate_limit_detailed(user_id)
    if not result.allowed:
        return False
    now = time.time()
    _recent[user_id].append(now)
    _downloads_hour[user_id].append(now)
    _downloads_day[user_id].append(now)
    return True
