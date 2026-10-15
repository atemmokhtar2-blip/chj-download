"""🎟️ Redemption codes — owner-issued clone activation codes.

Built ON TOP of the existing clone/plan system (bot_instances + plan +
expires_at + the expiry/renewal workers) — no parallel timing mechanism.

Lifecycle (two-phase, concurrency-safe):

  1. ``create_code``   — OWNER only (gate lives in the handler); generates a
     unique code with plan / runtime days / max uses / pre-use validity.
  2. ``redeem_code``   — user submits the code. All validation runs first,
     then ONE atomic statement reserves a use:

         UPDATE redemption_codes SET uses_consumed = uses_consumed + 1
          WHERE id = ? AND status = 'active' AND uses_consumed < max_uses
            AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)

     rowcount == 0 ⇒ the code was revoked/expired/exhausted (possibly by a
     simultaneous redemption) — nothing is consumed and a precise reason is
     returned. On success a ``pending_token`` instance is created and a
     'reserved' audit row is written.
  3. ``confirm_use``   — called ONLY after the clone was fully created and
     linked (token verified, instance activated). A failed or cancelled
     activation NEVER consumes a use: ``release_reservation`` decrements the
     counter and logs 'released'.

Stale reservations (user redeemed then vanished) are swept automatically on
the next redemption attempt of the same code after
``CODE_RESERVATION_TTL_MINUTES`` (default 30).

Secrecy: the full code text and bot tokens NEVER appear in any log row —
audit details carry ids, the code prefix and safe metadata only.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta

from database import pg

logger = logging.getLogger(__name__)

# Unambiguous alphabet (no 0/O, 1/I) — codes are read by humans in chat.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_PREFIX = "CHJ"

PLAN_CHOICES = ("basic", "pro")
MAX_RUNTIME_DAYS = 365
MAX_USES = 50
MAX_VALID_DAYS = 365

RESERVATION_TTL_MINUTES = int(os.getenv("CODE_RESERVATION_TTL_MINUTES", "30"))


# ---- helpers -----------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.utcnow()


def _parse_ts(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def normalize_code(text: str) -> str:
    """Canonical stored form: uppercase, no spaces, no dashes, no Arabic
    diacritic. Users may type ``chj abcd efgh`` / ``CHJ-ABCD-EFGH`` /
    ``chjabcdefgh`` — all normalize to the same canonical ``CHJABCDEFGH``."""
    return ("".join((text or "").split()).upper()
            .replace("ـ", "").replace("-", "").replace("‌", ""))


def format_code(code: str) -> str:
    """Human display form: ``CHJ-XXXX-XXXX`` (grouped for readability)."""
    c = normalize_code(code)
    if len(c) == 11:
        return f"{c[:3]}-{c[3:7]}-{c[7:]}"
    return c


def _generate_code() -> str:
    """Canonical (dash-free) code text — this is what gets STORED."""
    body = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{CODE_PREFIX}{body}"


def code_prefix(code: str) -> str:
    """Safe display fragment for logs/lists (never the full code)."""
    c = normalize_code(code)
    return c[:8] if c else ""


def mask_code(code: str) -> str:
    c = normalize_code(code)
    if len(c) <= 8:
        return c[:4] + "••••"
    return c[:8] + "-••••"


def _log(code_id: int, action: str, user_id: int | None = None,
         instance_id: int | None = None, details: str = "") -> None:
    """Append-only audit row. NEVER include code text or tokens in details."""
    try:
        pg.execute(
            "INSERT INTO redemption_log (code_id, action, user_id, instance_id, details) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(code_id), action[:40], user_id, instance_id, (details or "")[:300]),
        )
    except Exception:
        logger.exception("redemption log failed action=%s", action)


# ---- owner operations ----------------------------------------------------------

def create_code(plan: str, runtime_days: int, max_uses: int, valid_days: int,
                note: str = "", created_by: int | None = None) -> dict:
    """Create one redemption code. Returns {ok, code, code_id, ...}.

    The FULL code text is returned exactly once (for the owner to copy);
    afterwards only masked fragments are ever displayed.
    """
    plan = "pro" if (plan or "").strip().lower() == "pro" else "basic"
    runtime_days = int(runtime_days)
    max_uses = int(max_uses)
    valid_days = int(valid_days)
    if not 1 <= runtime_days <= MAX_RUNTIME_DAYS:
        return {"ok": False, "reason": "runtime_days_range"}
    if not 1 <= max_uses <= MAX_USES:
        return {"ok": False, "reason": "max_uses_range"}
    if not 1 <= valid_days <= MAX_VALID_DAYS:
        return {"ok": False, "reason": "valid_days_range"}

    created = _utcnow()
    expires = created + timedelta(days=valid_days)
    # Collision-safe insert (26^8 space — retries are theoretical).
    for _attempt in range(5):
        code = _generate_code()
        code_id = pg.insert_returning_id(
            "INSERT INTO redemption_codes "
            "(code, plan, runtime_days, max_uses, valid_days, note, created_by, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (code, plan, runtime_days, max_uses, valid_days,
             (note or "")[:200], created_by, _ts(expires)),
        )
        if code_id:
            _log(code_id, "created", user_id=created_by,
                 details=f"plan={plan} runtime_days={runtime_days} max_uses={max_uses} "
                         f"valid_days={valid_days} prefix={code_prefix(code)}")
            return {"ok": True, "code_id": code_id, "code": format_code(code),
                    "plan": plan,
                    "runtime_days": runtime_days, "max_uses": max_uses,
                    "valid_days": valid_days, "expires_at": _ts(expires)}
    return {"ok": False, "reason": "collision"}


def get_code(code_id: int) -> dict | None:
    return pg.fetchone("SELECT * FROM redemption_codes WHERE id = ?", (int(code_id),))


def find_code(code_text: str) -> dict | None:
    return pg.fetchone(
        "SELECT * FROM redemption_codes WHERE code = ?", (normalize_code(code_text),)
    )


def list_codes(limit: int = 15) -> list[dict]:
    return pg.fetchall(
        "SELECT id, code, plan, runtime_days, max_uses, uses_consumed, valid_days, "
        "status, expires_at, created_at FROM redemption_codes ORDER BY id DESC LIMIT ?",
        (int(limit),),
    )


def revoke_code(code_id: int, by_owner: int | None = None) -> tuple[bool, str]:
    """Immediate deactivation — only an atomic 'active'->'revoked' flip wins,
    so double-revokes are idempotent and never re-log."""
    affected = pg.execute(
        "UPDATE redemption_codes SET status = 'revoked' WHERE id = ? AND status <> 'revoked'",
        (int(code_id),),
    )
    if not affected:
        row = get_code(code_id)
        if not row:
            return False, "not_found"
        return False, f"already_{row.get('status', 'inactive')}"
    _log(code_id, "revoked", user_id=by_owner)
    return True, "revoked"


# ---- reservation lifecycle ------------------------------------------------------

def _sweep_stale_reservations(code_id: int) -> None:
    """Release 'reserved' slots older than the TTL whose owner never
    completed (no later 'used'/'released' row for the same user+code)."""
    try:
        stale = pg.fetchall(
            "SELECT r.user_id FROM redemption_log r "
            "WHERE r.code_id = ? AND r.action = 'reserved' "
            "AND r.created_at < CURRENT_TIMESTAMP - (? * INTERVAL '1 minute') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM redemption_log x WHERE x.code_id = r.code_id "
            "  AND x.user_id = r.user_id AND x.id > r.id "
            "  AND x.action IN ('used', 'released'))",
            (int(code_id), RESERVATION_TTL_MINUTES),
        )
        for row in stale:
            pg.execute(
                "UPDATE redemption_codes SET uses_consumed = GREATEST(uses_consumed - 1, 0) "
                "WHERE id = ?", (int(code_id),),
            )
            _log(code_id, "released", user_id=row.get("user_id"),
                 details="stale_reservation_sweep")
    except Exception:
        logger.exception("stale reservation sweep failed code=%s", code_id)


def _active_reservation(code_id: int, user_id: int) -> bool:
    row = pg.fetchone(
        "SELECT 1 AS ok FROM redemption_log r "
        "WHERE r.code_id = ? AND r.user_id = ? AND r.action = 'reserved' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM redemption_log x WHERE x.code_id = r.code_id "
        "  AND x.user_id = r.user_id AND x.id > r.id "
        "  AND x.action IN ('used', 'released')) LIMIT 1",
        (int(code_id), int(user_id)),
    )
    return bool(row)


def redeem_code(user_id: int, code_text: str) -> dict:
    """Validate + atomically reserve one use. Returns:

      {ok: True, code_id, plan, runtime_days, instance_id}
      {ok: False, reason}  reasons: shape | not_found | revoked | expired |
                           exhausted | limit_reached | already_used |
                           has_active_instance | race_lost
    """
    uid = int(user_id)
    norm = normalize_code(code_text)
    if not norm or len(norm) < 6:
        return {"ok": False, "reason": "shape"}

    row = find_code(norm)
    if not row:
        return {"ok": False, "reason": "not_found"}
    code_id = int(row["id"])

    if row.get("status") == "revoked":
        return {"ok": False, "reason": "revoked"}

    expires = _parse_ts(row.get("expires_at"))
    if expires and _utcnow() > expires:
        return {"ok": False, "reason": "expired"}

    # Reclaim abandoned slots BEFORE judging availability.
    _sweep_stale_reservations(code_id)
    row = get_code(code_id) or row

    used_before = pg.fetchone(
        "SELECT 1 AS ok FROM redemption_log WHERE code_id = ? AND user_id = ? "
        "AND action = 'used' LIMIT 1", (code_id, uid),
    )
    if used_before:
        return {"ok": False, "reason": "already_used"}

    # One clone per user: an ACTIVE instance blocks a second redemption.
    active_inst = pg.fetchone(
        "SELECT id FROM bot_instances WHERE owner_id = ? AND status = 'active' LIMIT 1",
        (uid,),
    )
    if active_inst:
        return {"ok": False, "reason": "has_active_instance"}

    if int(row.get("uses_consumed") or 0) >= int(row.get("max_uses") or 0):
        return {"ok": False, "reason": "exhausted"}

    # THE concurrency guard: one atomic statement; rowcount 0 means someone
    # else took the last slot (or the code was revoked/expired mid-flight).
    affected = pg.execute(
        "UPDATE redemption_codes SET uses_consumed = uses_consumed + 1 "
        "WHERE id = ? AND status = 'active' AND uses_consumed < max_uses "
        "AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)",
        (code_id,),
    )
    if not affected:
        fresh = get_code(code_id) or {}
        if fresh.get("status") == "revoked":
            return {"ok": False, "reason": "revoked"}
        exp2 = _parse_ts(fresh.get("expires_at"))
        if exp2 and _utcnow() > exp2:
            return {"ok": False, "reason": "expired"}
        return {"ok": False, "reason": "limit_reached"}

    plan = row.get("plan") or "basic"
    runtime_days = int(row.get("runtime_days") or 30)
    instance_id = pg.insert_returning_id(
        "INSERT INTO bot_instances (owner_id, status, plan) VALUES (?, 'pending_token', ?)",
        (uid, plan),
    )
    _log(code_id, "reserved", user_id=uid, instance_id=instance_id,
         details=f"plan={plan} runtime_days={runtime_days}")
    return {"ok": True, "code_id": code_id, "plan": plan,
            "runtime_days": runtime_days, "instance_id": instance_id}


def release_reservation(user_id: int, code_id: int, reason: str = "") -> bool:
    """Give the reserved slot back (failed/cancelled activation). Only fires
    when THIS user holds a live reservation for THIS code."""
    uid, cid = int(user_id), int(code_id)
    if not _active_reservation(cid, uid):
        return False
    pg.execute(
        "UPDATE redemption_codes SET uses_consumed = GREATEST(uses_consumed - 1, 0) "
        "WHERE id = ?", (cid,),
    )
    _log(cid, "released", user_id=uid, details=(reason or "activation_not_completed")[:120])
    return True


def confirm_use(user_id: int, code_id: int, instance_id: int,
                bot_username: str = "") -> None:
    """Called ONLY after the clone is fully created + activated. Marks the
    reservation as consumed and flags the code exhausted when full."""
    uid, cid, iid = int(user_id), int(code_id), int(instance_id)
    _log(cid, "used", user_id=uid, instance_id=iid,
         details=f"bot=@{(bot_username or '')[:40]}")
    row = pg.fetchone(
        "SELECT max_uses FROM redemption_codes WHERE id = ? AND status = 'active'",
        (cid,),
    )
    if not row:
        return
    used_cnt = pg.fetchone(
        "SELECT COUNT(*) AS cnt FROM redemption_log "
        "WHERE code_id = ? AND action = 'used'", (cid,),
    )["cnt"]
    if int(used_cnt) >= int(row["max_uses"]):
        pg.execute(
            "UPDATE redemption_codes SET status = 'exhausted' "
            "WHERE id = ? AND status = 'active'", (cid,),
        )


# ---- owner visibility ------------------------------------------------------------

def code_audit_trail(code_id: int, limit: int = 20) -> list[dict]:
    return pg.fetchall(
        "SELECT action, user_id, instance_id, details, created_at "
        "FROM redemption_log WHERE code_id = ? ORDER BY id DESC LIMIT ?",
        (int(code_id), int(limit)),
    )
