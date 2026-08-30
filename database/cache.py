"""
Telegram media file_id cache — 3-tier production architecture (2026).

  Request → L1 in-process TTLCache (cachetools, µs)
         → L2 Redis (optional, multi-worker, ms)
         → L3 SQLite (durable, TTL + LRU eviction)

Also supports album/media-group entries as ordered JSON lists of file_ids.

Stale file_ids: handlers call invalidate_* on TelegramError and re-upload.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from cachetools import TTLCache

from config.settings import CACHE_TTL_SECONDS, CACHE_MAX_SIZE
from .db import db_cursor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL canonicalization
# ---------------------------------------------------------------------------
_STRIP_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "igshid", "igsh", "si", "feature",
    "ref", "ref_src", "ref_url", "s", "t", "tt_from", "share_app_id",
    "share_link_id", "timestamp", "context", "entry_point", "spm",
}


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        scheme = (p.scheme or "https").lower()
        netloc = (p.netloc or "").lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        for prefix in ("m.", "mobile."):
            if netloc.startswith(prefix) and netloc.count(".") >= 2:
                netloc = netloc[len(prefix):]
                break
        path = re.sub(r"/+", "/", p.path or "/")
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        # YouTube: keep only v= for watch URLs
        if "youtube.com" in netloc and path == "/watch":
            qs = dict(parse_qsl(p.query, keep_blank_values=True))
            v = qs.get("v")
            query = f"v={v}" if v else ""
        else:
            pairs = [
                (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                if k.lower() not in _STRIP_QUERY_KEYS
            ]
            query = urlencode(sorted(pairs), doseq=True)
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url.strip()


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def _compound_key(url: str, quality: str, media_type: str) -> str:
    return f"{url_hash(url)}|{quality}|{media_type}"


# ---------------------------------------------------------------------------
# L1 — in-process (shared across threads in one worker)
# ---------------------------------------------------------------------------
_L1_MAX = max(256, min(CACHE_MAX_SIZE or 1000, 5000))
_L1_TTL = max(30, min(CACHE_TTL_SECONDS, 3600))  # L1 shorter than durable TTL
_l1: TTLCache = TTLCache(maxsize=_L1_MAX, ttl=_L1_TTL)
_l1_lock = threading.RLock()

# Stampede locks: one writer per compound key
_write_locks: dict[str, threading.Lock] = {}
_write_locks_guard = threading.Lock()


def _write_lock_for(key: str) -> threading.Lock:
    with _write_locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _write_locks[key] = lock
            # Bound the map size
            if len(_write_locks) > 10_000:
                for k in list(_write_locks.keys())[:1000]:
                    _write_locks.pop(k, None)
        return lock


def _l1_get(key: str):
    with _l1_lock:
        return _l1.get(key)


def _l1_set(key: str, value) -> None:
    with _l1_lock:
        _l1[key] = value


def _l1_del(key: str) -> None:
    with _l1_lock:
        _l1.pop(key, None)


# ---------------------------------------------------------------------------
# L2 — Redis
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
        client = redis.Redis.from_url(
            redis_url, decode_responses=True, socket_connect_timeout=1.5
        )
        client.ping()
        _redis = client
        logger.info("Media cache L2: Redis enabled")
    except Exception as e:
        logger.warning("Media cache L2: Redis unavailable (%s)", e)
        _redis = None
    return _redis


def _redis_key(h: str, quality: str, media_type: str) -> str:
    return f"tgcache:v2:{h}:{quality}:{media_type}"


# ---------------------------------------------------------------------------
# L3 helpers — SQLite
# ---------------------------------------------------------------------------
def _sqlite_get(h: str, quality: str, media_type: str) -> str | None:
    with db_cursor() as c:
        c.execute(
            """
            SELECT file_id FROM file_cache
            WHERE url_hash = ? AND quality = ? AND media_type = ?
              AND created_at >= datetime('now', ?)
            """,
            (h, quality, media_type, f"-{int(CACHE_TTL_SECONDS)} seconds"),
        )
        row = c.fetchone()
        if not row:
            # Expired or missing — purge if expired row exists
            c.execute(
                """
                DELETE FROM file_cache
                WHERE url_hash = ? AND quality = ? AND media_type = ?
                  AND created_at < datetime('now', ?)
                """,
                (h, quality, media_type, f"-{int(CACHE_TTL_SECONDS)} seconds"),
            )
            return None
        c.execute(
            """
            UPDATE file_cache SET hits = hits + 1
            WHERE url_hash = ? AND quality = ? AND media_type = ?
            """,
            (h, quality, media_type),
        )
        return row["file_id"]


def _sqlite_set(
    h: str, quality: str, media_type: str, file_id: str,
    title: str, platform: str,
) -> None:
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
        if CACHE_MAX_SIZE > 0:
            c.execute("SELECT COUNT(*) AS cnt FROM file_cache")
            cnt = int(c.fetchone()["cnt"])
            if cnt > CACHE_MAX_SIZE:
                c.execute(
                    """
                    DELETE FROM file_cache WHERE id IN (
                        SELECT id FROM file_cache
                        ORDER BY hits ASC, created_at ASC
                        LIMIT ?
                    )
                    """,
                    (cnt - CACHE_MAX_SIZE,),
                )


def _sqlite_delete(h: str, quality: str | None = None, media_type: str | None = None) -> int:
    with db_cursor() as c:
        if quality is not None and media_type is not None:
            c.execute(
                "DELETE FROM file_cache WHERE url_hash = ? AND quality = ? AND media_type = ?",
                (h, quality, media_type),
            )
        else:
            c.execute("DELETE FROM file_cache WHERE url_hash = ?", (h,))
        return c.rowcount if c.rowcount and c.rowcount > 0 else 0


# ---------------------------------------------------------------------------
# Public single-item API
# ---------------------------------------------------------------------------
def get_cached(url: str, quality: str, media_type: str) -> str | None:
    """Read-through L1 → L2 → L3. Promotes hits upward."""
    key = _compound_key(url, quality, media_type)
    h = url_hash(url)

    # L1
    val = _l1_get(key)
    if isinstance(val, str) and val:
        return val

    # L2
    r = _get_redis()
    if r is not None:
        try:
            raw = r.get(_redis_key(h, quality, media_type))
            if raw:
                _l1_set(key, raw)
                return raw
        except Exception:
            pass

    # L3
    try:
        file_id = _sqlite_get(h, quality, media_type)
    except Exception as e:
        logger.debug("SQLite cache get failed: %s", e)
        file_id = None

    if not file_id:
        return None

    # Promote
    _l1_set(key, file_id)
    if r is not None:
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
    """Write-through to L1 + L2 + L3 under a per-key lock (stampede-safe)."""
    if not file_id:
        return
    key = _compound_key(url, quality, media_type)
    h = url_hash(url)
    with _write_lock_for(key):
        _l1_set(key, file_id)
        r = _get_redis()
        if r is not None:
            try:
                r.setex(_redis_key(h, quality, media_type), max(60, CACHE_TTL_SECONDS), file_id)
            except Exception:
                pass
        try:
            _sqlite_set(h, quality, media_type, file_id, title, platform)
        except Exception as e:
            logger.warning("SQLite cache set failed: %s", e)


def invalidate_cache(
    url: str, quality: str | None = None, media_type: str | None = None
) -> int:
    """Drop from all tiers (stale file_id recovery)."""
    h = url_hash(url)
    if quality is not None and media_type is not None:
        _l1_del(_compound_key(url, quality, media_type))
    else:
        # Drop all L1 keys for this hash prefix
        with _l1_lock:
            for k in list(_l1.keys()):
                if isinstance(k, str) and k.startswith(h):
                    _l1.pop(k, None)

    r = _get_redis()
    if r is not None:
        try:
            if quality is not None and media_type is not None:
                r.delete(_redis_key(h, quality, media_type))
            else:
                for rk in r.scan_iter(match=f"tgcache:v2:{h}:*"):
                    r.delete(rk)
        except Exception:
            pass

    try:
        deleted = _sqlite_delete(h, quality, media_type)
    except Exception:
        deleted = 0
    if deleted:
        logger.info("Cache invalidated %s entr(y/ies) hash=%s", deleted, h[:12])
    return deleted


# ---------------------------------------------------------------------------
# Album / media-group API (ordered list of file_ids)
# ---------------------------------------------------------------------------
def get_cached_album(url: str) -> list[dict] | None:
    """
    Returns list of {file_id, type} or None.
    Stored under quality='album', media_type='album' as JSON.
    """
    raw = get_cached(url, "album", "album")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list) and data:
            return data
    except Exception:
        invalidate_cache(url, "album", "album")
    return None


def set_cache_album(url: str, items: list[dict], title: str = "", platform: str = "") -> None:
    """
    items: [{file_id: str, type: 'image'|'video'}, ...]
    """
    clean = []
    for it in items:
        fid = it.get("file_id")
        if not fid:
            continue
        clean.append({"file_id": fid, "type": it.get("type") or "image"})
    if len(clean) < 2:
        return
    set_cache(url, "album", "album", json.dumps(clean, separators=(",", ":")), title, platform)


# ---------------------------------------------------------------------------
# Metrics / maintenance
# ---------------------------------------------------------------------------
def get_cache_count() -> int:
    try:
        with db_cursor() as c:
            c.execute("SELECT COUNT(*) as cnt FROM file_cache")
            return int(c.fetchone()["cnt"])
    except Exception:
        return 0


def get_cache_hits() -> int:
    try:
        with db_cursor() as c:
            c.execute("SELECT COALESCE(SUM(hits), 0) as total FROM file_cache")
            return int(c.fetchone()["total"])
    except Exception:
        return 0


def get_cache_stats() -> dict:
    with _l1_lock:
        l1_size = len(_l1)
        l1_max = _l1.maxsize
    return {
        "l1_size": l1_size,
        "l1_max": l1_max,
        "l1_ttl": _L1_TTL,
        "l2_redis": _get_redis() is not None,
        "l3_rows": get_cache_count(),
        "l3_hits": get_cache_hits(),
        "ttl_seconds": CACHE_TTL_SECONDS,
        "max_size": CACHE_MAX_SIZE,
    }


def cleanup_old_cache(days: int | None = None) -> int:
    if days is None:
        days = max(1, int(CACHE_TTL_SECONDS / 86400) or 1)
    try:
        with db_cursor() as c:
            c.execute(
                """
                DELETE FROM file_cache
                WHERE created_at < datetime('now', '-' || ? || ' days')
                """,
                (int(days),),
            )
            n = c.rowcount if c.rowcount and c.rowcount > 0 else 0
    except Exception:
        n = 0
    with _l1_lock:
        _l1.clear()
    return n


# ---------------------------------------------------------------------------
# Content fingerprint — cross-URL dedup (same YouTube id, different share links)
# ---------------------------------------------------------------------------
def make_fingerprint(platform: str, media_id: str, quality: str, media_type: str) -> str:
    """
    Stable content key independent of URL.
    Same TikTok/YouTube id + quality + type → same fingerprint → shared file_id.
    """
    if not media_id:
        return ""
    raw = f"{(platform or '').strip().lower()}|{str(media_id).strip()}|{(quality or '').strip()}|{(media_type or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached_by_fingerprint(fingerprint: str, quality: str, media_type: str) -> dict | None:
    """
    Lookup by content fingerprint.
    Returns {file_id, vault_chat_id, vault_message_id, url_hash} or None.
    """
    if not fingerprint:
        return None
    # L1
    l1k = f"fp|{fingerprint}|{quality}|{media_type}"
    hit = _l1_get(l1k)
    if isinstance(hit, dict) and hit.get("file_id"):
        return hit

    # L2
    r = _get_redis()
    if r is not None:
        try:
            raw = r.get(f"tgfp:v1:{fingerprint}:{quality}:{media_type}")
            if raw:
                data = json.loads(raw)
                _l1_set(l1k, data)
                return data
        except Exception:
            pass

    # L3 index table
    try:
        with db_cursor() as c:
            c.execute(
                """
                SELECT file_id, vault_chat_id, vault_message_id, url_hash
                FROM media_fingerprint_index
                WHERE fingerprint = ? AND quality = ? AND media_type = ?
                """,
                (fingerprint, quality, media_type),
            )
            row = c.fetchone()
            if not row or not row["file_id"]:
                return None
            data = {
                "file_id": row["file_id"],
                "vault_chat_id": row["vault_chat_id"],
                "vault_message_id": row["vault_message_id"],
                "url_hash": row["url_hash"],
            }
            _l1_set(l1k, data)
            if r is not None:
                try:
                    r.setex(
                        f"tgfp:v1:{fingerprint}:{quality}:{media_type}",
                        max(60, CACHE_TTL_SECONDS),
                        json.dumps(data),
                    )
                except Exception:
                    pass
            return data
    except Exception as e:
        logger.debug("fingerprint lookup failed: %s", e)
        return None


def index_fingerprint(
    fingerprint: str,
    quality: str,
    media_type: str,
    url: str,
    file_id: str,
    vault_chat_id: int | None = None,
    vault_message_id: int | None = None,
) -> None:
    if not fingerprint or not file_id:
        return
    h = url_hash(url)
    data = {
        "file_id": file_id,
        "vault_chat_id": vault_chat_id,
        "vault_message_id": vault_message_id,
        "url_hash": h,
    }
    l1k = f"fp|{fingerprint}|{quality}|{media_type}"
    _l1_set(l1k, data)
    r = _get_redis()
    if r is not None:
        try:
            r.setex(
                f"tgfp:v1:{fingerprint}:{quality}:{media_type}",
                max(60, CACHE_TTL_SECONDS),
                json.dumps(data),
            )
        except Exception:
            pass
    try:
        with db_cursor() as c:
            c.execute(
                """
                INSERT INTO media_fingerprint_index
                    (fingerprint, quality, media_type, url_hash, file_id,
                     vault_chat_id, vault_message_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(fingerprint, quality, media_type) DO UPDATE SET
                    url_hash = excluded.url_hash,
                    file_id = excluded.file_id,
                    vault_chat_id = COALESCE(excluded.vault_chat_id, media_fingerprint_index.vault_chat_id),
                    vault_message_id = COALESCE(excluded.vault_message_id, media_fingerprint_index.vault_message_id),
                    updated_at = datetime('now')
                """,
                (fingerprint, quality, media_type, h, file_id, vault_chat_id, vault_message_id),
            )
            # Also stamp fingerprint on the primary file_cache row when present
            c.execute(
                """
                UPDATE file_cache SET fingerprint = ?,
                    vault_chat_id = COALESCE(?, vault_chat_id),
                    vault_message_id = COALESCE(?, vault_message_id)
                WHERE url_hash = ? AND quality = ? AND media_type = ?
                """,
                (fingerprint, vault_chat_id, vault_message_id, h, quality, media_type),
            )
    except Exception as e:
        logger.warning("fingerprint index write failed: %s", e)


def set_cache_with_meta(
    url: str,
    quality: str,
    media_type: str,
    file_id: str,
    *,
    title: str = "",
    platform: str = "",
    media_id: str = "",
    vault_chat_id: int | None = None,
    vault_message_id: int | None = None,
) -> None:
    """Write URL cache + content-fingerprint index + optional vault coords."""
    set_cache(url, quality, media_type, file_id, title, platform)
    fp = make_fingerprint(platform, media_id, quality, media_type)
    if fp:
        index_fingerprint(
            fp, quality, media_type, url, file_id,
            vault_chat_id=vault_chat_id, vault_message_id=vault_message_id,
        )


def resolve_cached_delivery(
    url: str,
    quality: str,
    media_type: str,
    *,
    platform: str = "",
    media_id: str = "",
) -> dict | None:
    """
    Unified lookup: URL key first, then content fingerprint.
    Returns {file_id, vault_chat_id, vault_message_id, source} or None.
    """
    fid = get_cached(url, quality, media_type)
    if fid:
        # Try enrich with vault coords from fingerprint row
        fp = make_fingerprint(platform, media_id, quality, media_type)
        meta = get_cached_by_fingerprint(fp, quality, media_type) if fp else None
        return {
            "file_id": fid,
            "vault_chat_id": (meta or {}).get("vault_chat_id"),
            "vault_message_id": (meta or {}).get("vault_message_id"),
            "source": "url",
        }
    fp = make_fingerprint(platform, media_id, quality, media_type)
    meta = get_cached_by_fingerprint(fp, quality, media_type) if fp else None
    if meta and meta.get("file_id"):
        # Promote into URL cache for next time
        set_cache(url, quality, media_type, meta["file_id"], platform=platform)
        return {
            "file_id": meta["file_id"],
            "vault_chat_id": meta.get("vault_chat_id"),
            "vault_message_id": meta.get("vault_message_id"),
            "source": "fingerprint",
        }
    return None
