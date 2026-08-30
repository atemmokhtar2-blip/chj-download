"""
Multi-layer download rate limiter (production pattern 2026).

Layers (checked in order):
  1. Token bucket  — short cooldown + controlled burst (in-process, thread-safe)
  2. Hourly quota  — fixed window from SQLite downloads table (survives restart)
  3. Daily quota   — fixed window from SQLite downloads table (survives restart)

Admins / owner bypass all layers.

Token bucket is the industry default for user-facing APIs (allows a small
burst without boundary-spike abuse of pure fixed windows). Persistent
quotas prevent abuse across process restarts without requiring Redis.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from config.settings import (
    RATE_LIMIT_SECONDS,
    RATE_LIMIT_BURST,
    HOURLY_DOWNLOAD_LIMIT,
    DAILY_DOWNLOAD_LIMIT,
)
from middlewares.auth import is_admin


@dataclass
class RateLimitResult:
    allowed: bool
    wait_seconds: int = 0
    reason: str = ""  # "" | "cooldown" | "hourly" | "daily"


class _TokenBucket:
    """Thread-safe token bucket: capacity=burst, refill 1 token every interval_s."""

    def __init__(self, capacity: int, interval_s: float):
        self.capacity = max(1, int(capacity))
        self.interval = max(0.5, float(interval_s))
        self._tokens: dict[int, float] = {}
        self._last: dict[int, float] = {}
        self._lock = threading.Lock()

    def _refill(self, user_id: int, now: float) -> float:
        tokens = self._tokens.get(user_id, float(self.capacity))
        last = self._last.get(user_id, now)
        elapsed = max(0.0, now - last)
        tokens = min(self.capacity, tokens + elapsed / self.interval)
        self._tokens[user_id] = tokens
        self._last[user_id] = now
        return tokens

    def check(self, user_id: int) -> tuple[bool, int]:
        """Return (allowed_if_consume, wait_seconds_if_not). Does not consume."""
        now = time.monotonic()
        with self._lock:
            tokens = self._refill(user_id, now)
            if tokens >= 1.0:
                return True, 0
            need = 1.0 - tokens
            wait = int(need * self.interval) + 1
            return False, wait

    def consume(self, user_id: int) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens = self._refill(user_id, now)
            if tokens < 1.0:
                return False
            self._tokens[user_id] = tokens - 1.0
            self._last[user_id] = now
            return True

    def reset(self, user_id: int) -> None:
        with self._lock:
            self._tokens[user_id] = float(self.capacity)
            self._last[user_id] = time.monotonic()


_bucket = _TokenBucket(capacity=RATE_LIMIT_BURST, interval_s=RATE_LIMIT_SECONDS)


def _count_downloads_since(user_id: int, seconds: int) -> int:
    """Count successful downloads for user in the last `seconds` (UTC, SQLite)."""
    try:
        from database.db import db_cursor
        with db_cursor() as c:
            c.execute(
                """
                SELECT COUNT(*) AS cnt FROM downloads
                WHERE user_id = ?
                  AND created_at >= datetime('now', ?)
                """,
                (user_id, f"-{int(seconds)} seconds"),
            )
            row = c.fetchone()
            return int(row["cnt"] if row else 0)
    except Exception:
        return 0


def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """
    Backward-compatible API used by handlers.

    Returns (allowed, seconds_remaining).
    Prefer check_rate_limit_detailed() when reason is needed.
    """
    result = check_rate_limit_detailed(user_id)
    return result.allowed, result.wait_seconds


def check_rate_limit_detailed(user_id: int) -> RateLimitResult:
    """Full multi-layer check without consuming a token."""
    if is_admin(user_id):
        return RateLimitResult(allowed=True)

    # 1) Daily quota (persistent)
    daily = _count_downloads_since(user_id, 86400)
    if daily >= DAILY_DOWNLOAD_LIMIT:
        return RateLimitResult(
            allowed=False,
            wait_seconds=0,
            reason="daily",
        )

    # 2) Hourly quota (persistent)
    hourly = _count_downloads_since(user_id, 3600)
    if hourly >= HOURLY_DOWNLOAD_LIMIT:
        return RateLimitResult(
            allowed=False,
            wait_seconds=0,
            reason="hourly",
        )

    # 3) Token bucket cooldown / burst
    ok, wait = _bucket.check(user_id)
    if not ok:
        return RateLimitResult(allowed=False, wait_seconds=wait, reason="cooldown")

    return RateLimitResult(allowed=True)


def mark_download(user_id: int) -> None:
    """Consume one token after a successful download (not on failure)."""
    if is_admin(user_id):
        return
    _bucket.consume(user_id)


def reset_rate_limit(user_id: int) -> None:
    """Admin/debug helper: refill the user's token bucket."""
    _bucket.reset(user_id)
