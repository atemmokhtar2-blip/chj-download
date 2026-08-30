"""
Telegram file_id media cache — production grade (2026).

Why file_id caching matters:
  Re-sending by file_id skips re-upload entirely (bandwidth + latency).
  Telegram bot file_ids stay valid long-term while the bot has seen the file;
  they can still go stale → must invalidate + re-upload on send failure.

Layers:
  1. URL normalization before hashing (stable keys across tracking params)
  2. SQLite persistent store with TTL (CACHE_TTL_SECONDS)
  3. Optional Redis hot layer when REDIS_URL is set (multi-worker)
  4. Stale-file_id invalidation API used by handlers on TelegramError
  5. Soft max-size eviction (lowest hits, then oldest)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from config.settings import CACHE_TTL_SECONDS, CACHE_MAX_SIZE
from .db import db_cursor

logger = logging.getLogger(__name__)

# Tracking / share junk stripped so the same media shares one cache key.
_STRIP_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "igshid", "igsh", "si", "feature",
    "ref", "ref_src", "ref_url", "s", "t", "tt_from", "share_app_id",
    "share_link_id", "timestamp", "context", "entry_point",
}


def normalize_url(url: str) -> str:
    """Canonical form for cache keys — host lowercased, tracking params dropped."""
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        scheme = (p.scheme or "https").lower()
        netloc = (p.netloc or "").lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        # Drop mobile subdomains that point at the same content
        for prefix in ("m.", "mobile.", "vm.", "vt."):
            if netloc.startswith(prefix) and netloc.count(".") >= 2:
                # keep tiktok short hosts as-is (vm/vt are real hosts)
                if not netloc.startswith(("vm.tiktok.", "vt.tiktok.")):
                    netloc = netloc[len(prefix):]
                break
        path = re.sub(r"/+", "/", p.path or "/")
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        query_pairs = [
            (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in _STRIP_QUERY_KEYS
        ]
        query = urlencode(sorted(query_pairs), doseq=True)
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url.strip()


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Optional Redis hot layer
# ---------------------------------------------------------------------------
_redis = None
_redis_checked = False


def _get_redis():
    global _redis, _redis_checked
    if _redis_checked:
        return _redis
    _redis_checked = True
    redis_url = (
        os.getenv("REDIS_URL")
        or os.getenv("RATE_LIMIT_REDIS_URL")
        or ""
    ).strip()
    if not redis_url:
        return None
    try:
        import redis
        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=1.5)
        client.ping()
        _redis = client
        logger.info("Media cache: Redis hot layer enabled")
    except Exception as e:
        logger.warning("Media cache: Redis unavailable (%s); SQLite only", e)
        _redis = None
    return _redis


def _redis_key(h: str, quality: str, media_type: str) -> str:
    return f"tgcache:{h}:{quality}:{media_type}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_cached(url: str, quality: str, media_type: str) -> str | None:
    """Return a live file_id or None. Honors TTL and bumps hit counter."""
    h = url_hash(url)
    r = _get_redis()
    if r is not None:
        try:
            raw = r.get(_redis_key(h, quality, media_type))
            if raw:
                return raw
        except Exception:
            pass

    with db_cursor() as c:
        c.execute(
            """
            SELECT file_id, created_at FROM file_cache
            WHERE url_hash = ? AND quality = ? AND media_type = ?
            """,
            (h, quality, media_type),
        )
        row = c.fetchone()
        if not row:
            return None

        # TTL enforcement (CACHE_TTL_SECONDS)
        try:
            c.execute(
                """
                SELECT file_id FROM file_cache
                WHERE url_hash = ? AND quality = ? AND media_type = ?
                  AND created_at >= datetime('now', ?)
                """,
                (h, quality, media_type, f"-{int(CACHE_TTL_SECONDS)} seconds"),
            )
            live = c.fetchone()
        except Exception:
            live = row

        if not live:
            c.execute(
                "DELETE FROM file_cache WHERE url_hash = ? AND quality = ? AND media_type = ?",
                (h, quality, media_type),
            )
            return None

        c.execute(
            """
            UPDATE file_cache SET hits = hits + 1
            WHERE url_hash = ? AND quality = ? AND media_type = ?
            """,
            (h, quality, media_type),
        )
        file_id = live["file_id"]

    if r is not None and file_id:
        try:
            r.setex(_redis_key(h, quality, media_type), max(60, CACHE_TTL_SECONDS), file_id)
        except Exception:
            pass
    return file_id


def set_cache(
    url: str,
    quality: str,
    media_type: str,
    file_id: str,
    title: str = "",
    platform: str = "",
) -> None:
    if not file_id:
        return
    h = url_hash(url)
    with db_cursor() as c:
        c.execute(
            """
            INSERT INTO file_cache
                (url_hash, quality, media_type, file_id, title, platform, hits, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, datetime('now'))
            ON CONFLICT(url_hash, quality, media_type) DO UPDATE SET
                file_id = excluded.file_id,
                title = excluded.title,
                platform = excluded.platform,
                created_at = datetime('now')
            """,
            (h, quality, media_type, file_id, title, platform),
        )
        # Soft max-size: evict coldest entries beyond CACHE_MAX_SIZE
        if CACHE_MAX_SIZE > 0:
            c.execute("SELECT COUNT(*) AS cnt FROM file_cache")
            cnt = int(c.fetchone()["cnt"])
            if cnt > CACHE_MAX_SIZE:
                overflow = cnt - CACHE_MAX_SIZE
                c.execute(
                    """
                    DELETE FROM file_cache WHERE id IN (
                        SELECT id FROM file_cache
                        ORDER BY hits ASC, created_at ASC
                        LIMIT ?
                    )
                    """,
                    (overflow,),
                )

    r = _get_redis()
    if r is not None:
        try:
            r.setex(_redis_key(h, quality, media_type), max(60, CACHE_TTL_SECONDS), file_id)
        except Exception:
            pass


def invalidate_cache(url: str, quality: str | None = None, media_type: str | None = None) -> int:
    """
    Drop cache entries for a URL (or a specific quality/type).
    Call this when Telegram rejects a file_id as invalid/unavailable.
    """
    h = url_hash(url)
    deleted = 0
    with db_cursor() as c:
        if quality is not None and media_type is not None:
            c.execute(
                "DELETE FROM file_cache WHERE url_hash = ? AND quality = ? AND media_type = ?",
                (h, quality, media_type),
            )
        else:
            c.execute("DELETE FROM file_cache WHERE url_hash = ?", (h,))
        deleted = c.rowcount if c.rowcount and c.rowcount > 0 else 0

    r = _get_redis()
    if r is not None:
        try:
            if quality is not None and media_type is not None:
                r.delete(_redis_key(h, quality, media_type))
            else:
                for key in r.scan_iter(match=f"tgcache:{h}:*"):
                    r.delete(key)
        except Exception:
            pass
    if deleted:
        logger.info("Invalidated %s cache entr(y/ies) for %s", deleted, h[:12])
    return deleted


def get_cache_count() -> int:
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) as cnt FROM file_cache")
        return int(c.fetchone()["cnt"])


def get_cache_hits() -> int:
    with db_cursor() as c:
        c.execute("SELECT COALESCE(SUM(hits), 0) as total FROM file_cache")
        return int(c.fetchone()["total"])


def cleanup_old_cache(days: int | None = None) -> int:
    """Delete entries older than `days` (default from CACHE_TTL_SECONDS)."""
    if days is None:
        days = max(1, int(CACHE_TTL_SECONDS / 86400) or 1)
    with db_cursor() as c:
        c.execute(
            """
            DELETE FROM file_cache
            WHERE created_at < datetime('now', '-' || ? || ' days')
            """,
            (int(days),),
        )
        return c.rowcount if c.rowcount and c.rowcount > 0 else 0
