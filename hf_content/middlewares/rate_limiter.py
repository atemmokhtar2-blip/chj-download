import time
from collections import defaultdict
from config.settings import RATE_LIMIT_SECONDS

_last_download: dict[int, float] = defaultdict(float)


def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """Returns (allowed, seconds_remaining)"""
    now = time.time()
    last = _last_download[user_id]
    elapsed = now - last
    if elapsed < RATE_LIMIT_SECONDS:
        remaining = int(RATE_LIMIT_SECONDS - elapsed) + 1
        return False, remaining
    return True, 0


def mark_download(user_id: int):
    _last_download[user_id] = time.time()


def reset_rate_limit(user_id: int):
    _last_download[user_id] = 0.0
