"""
Production rate limiter for downloads — powered by the `limits` library (v5.x).

Algorithms (2026 industry default for abuse control):
  - Moving Window for short cooldown / burst
  - Moving Window for hourly quota
  - Moving Window for daily quota

Storage backend (auto-selected):
  - redis://... or rediss://...  → distributed, atomic, multi-worker safe
  - memory:// (default)          → single-process; SQLite daily/hourly backstop
    still enforces persistent quotas across restarts

Admin / owner bypass all layers.

API:
  check_rate_limit(user_id) -> (allowed, wait_seconds)
  check_rate_limit_detailed(user_id) -> RateLimitResult
  mark_download(user_id)  # consume windows after a download is accepted
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from limits import parse
from limits.storage import storage_from_string
from limits.strategies import MovingWindowRateLimiter

from config.settings import (
    RATE_LIMIT_SECONDS,
    RATE_LIMIT_BURST,
    HOURLY_DOWNLOAD_LIMIT,
    DAILY_DOWNLOAD_LIMIT,
)
from middlewares.auth import is_admin

logger = logging.getLogger(__name__)


@dataclass
class RateLimitResult:
    allowed: bool
    wait_seconds: int = 0
    reason: str = ""  # "" | "cooldown" | "hourly" | "daily"
    remaining: int = -1


def _build_storage():
    """
    Prefer Redis when REDIS_URL is set (multi-instance production).
    Fall back to in-process memory store for single-worker deploys.
    """
    redis_url = (
        os.getenv("REDIS_URL")
        or os.getenv("RATE_LIMIT_REDIS_URL")
        or ""
    ).strip()
    if redis_url:
        try:
            storage = storage_from_string(redis_url)
            # Probe connectivity early so we can fall back cleanly.
            if hasattr(storage, "check") and not storage.check():
                raise RuntimeError("Redis storage health check failed")
            logger.info("Rate limiter storage: Redis (%s)", redis_url.split("@")[-1])
            return storage, True
        except Exception as e:
            logger.error("Redis rate-limit backend unavailable (%s); using memory", e)
    logger.info("Rate limiter storage: memory:// (set REDIS_URL for distributed limits)")
    return storage_from_string("memory://"), False


_storage, _using_redis = _build_storage()
_limiter = MovingWindowRateLimiter(_storage)

# Moving-window items (limits grammar: "N per X seconds|minute|hour|day")
# Cooldown window: allow RATE_LIMIT_BURST hits inside RATE_LIMIT_SECONDS.
_COOLDOWN_ITEM = parse(f"{max(1, RATE_LIMIT_BURST)}/{max(1, RATE_LIMIT_SECONDS)} seconds")
_HOURLY_ITEM = parse(f"{max(1, HOURLY_DOWNLOAD_LIMIT)}/hour")
_DAILY_ITEM = parse(f"{max(1, DAILY_DOWNLOAD_LIMIT)}/day")


def _key(user_id: int, scope: str) -> str:
    return f"dl:{scope}:{user_id}"


def _wait_from_stats(item, key: str) -> int:
    try:
        stats = _limiter.get_window_stats(item, key)
        reset = float(getattr(stats, "reset_time", 0) or 0)
        wait = int(reset - time.time()) + 1
        return max(1, wait)
    except Exception:
        return max(1, RATE_LIMIT_SECONDS)


def _sqlite_count(user_id: int, seconds: int) -> int:
    """Persistent backstop when not on Redis (memory store dies on restart)."""
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
    result = check_rate_limit_detailed(user_id)
    return result.allowed, result.wait_seconds


def check_rate_limit_detailed(user_id: int) -> RateLimitResult:
    """Non-consuming multi-layer check (Moving Window via `limits`)."""
    if is_admin(user_id):
        return RateLimitResult(allowed=True)

    # --- Daily ---
    daily_key = _key(user_id, "day")
    if not _limiter.test(_DAILY_ITEM, daily_key):
        return RateLimitResult(
            allowed=False,
            wait_seconds=_wait_from_stats(_DAILY_ITEM, daily_key),
            reason="daily",
            remaining=0,
        )
    # SQLite backstop when memory store cannot survive restarts
    if not _using_redis:
        if _sqlite_count(user_id, 86400) >= DAILY_DOWNLOAD_LIMIT:
            return RateLimitResult(allowed=False, wait_seconds=0, reason="daily", remaining=0)

    # --- Hourly ---
    hourly_key = _key(user_id, "hour")
    if not _limiter.test(_HOURLY_ITEM, hourly_key):
        return RateLimitResult(
            allowed=False,
            wait_seconds=_wait_from_stats(_HOURLY_ITEM, hourly_key),
            reason="hourly",
            remaining=0,
        )
    if not _using_redis:
        if _sqlite_count(user_id, 3600) >= HOURLY_DOWNLOAD_LIMIT:
            return RateLimitResult(allowed=False, wait_seconds=0, reason="hourly", remaining=0)

    # --- Cooldown / burst window ---
    cool_key = _key(user_id, "cool")
    if not _limiter.test(_COOLDOWN_ITEM, cool_key):
        return RateLimitResult(
            allowed=False,
            wait_seconds=_wait_from_stats(_COOLDOWN_ITEM, cool_key),
            reason="cooldown",
            remaining=0,
        )

    try:
        stats = _limiter.get_window_stats(_COOLDOWN_ITEM, cool_key)
        remaining = int(getattr(stats, "remaining", -1))
    except Exception:
        remaining = -1

    return RateLimitResult(allowed=True, remaining=remaining)


def mark_download(user_id: int) -> bool:
    """
    Atomically consume one hit on all windows after a download is accepted.

    Returns False if the hit was rejected (should not happen if check passed,
    but closes the race under concurrent callbacks).
    """
    if is_admin(user_id):
        return True

    cool_key = _key(user_id, "cool")
    hourly_key = _key(user_id, "hour")
    daily_key = _key(user_id, "day")

    # Hit cooldown first (strictest short window)
    if not _limiter.hit(_COOLDOWN_ITEM, cool_key):
        logger.warning("rate limit race: cooldown hit failed for user %s", user_id)
        return False
    if not _limiter.hit(_HOURLY_ITEM, hourly_key):
        logger.warning("rate limit race: hourly hit failed for user %s", user_id)
        return False
    if not _limiter.hit(_DAILY_ITEM, daily_key):
        logger.warning("rate limit race: daily hit failed for user %s", user_id)
        return False
    return True


def reset_rate_limit(user_id: int) -> None:
    """Clear moving-window counters for a user (admin/debug)."""
    for scope, item in (
        ("cool", _COOLDOWN_ITEM),
        ("hour", _HOURLY_ITEM),
        ("day", _DAILY_ITEM),
    ):
        try:
            _limiter.clear(item, _key(user_id, scope))
        except Exception as e:
            logger.debug("reset_rate_limit clear failed for %s/%s: %s", user_id, scope, e)
