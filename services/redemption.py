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
# Plan-based prefixes so a code announces its own tier:
#   BASIC-7K9M-X4PQ / PRO-7K9M-X4PQ   (legacy CHJ-… codes stay redeemable).
_PLAN_PREFIX = {"basic": "BASIC", "pro": "PRO"}
_LEGACY_PREFIX = "CHJ"

PLAN_CHOICES = ("basic", "pro")
MAX_RUNTIME_DAYS = 365
MAX_USES = 50
MAX_VALID_DAYS = 365

# The free trial is always 24 hours, one per Telegram user, ever.
TRIAL_HOURS = 24

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
    """Human display form, grouped for readability:
    ``BASIC-7K9M-X4PQ`` / ``PRO-7K9M-X4PQ`` (legacy ``CHJ-XXXX-XXXX``)."""
    c = normalize_code(code)
    for prefix in ("BASIC", "PRO", _LEGACY_PREFIX):
        if c.startswith(prefix) and len(c) == len(prefix) + 8:
            body = c[len(prefix):]
            return f"{prefix}-{body[:4]}-{body[4:]}"
    return c


def _generate_code(plan: str = "basic") -> str:
    """Canonical (dash-free) code text — this is what gets STORED.

    Plan-prefixed per spec (``BASIC…`` / ``PRO…``) so a code announces its
    own tier; legacy ``CHJ…`` codes stay redeemable through find_code.
    """
    prefix = _PLAN_PREFIX.get((plan or "basic").strip().lower(), "BASIC")
    body = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{prefix}{body}"


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
                note: str = "", created_by: int | None = None,
                targeted_user_id: int | None = None) -> dict:
    """Create one redemption code. Returns {ok, code, code_id, ...}.

    ``targeted_user_id`` restricts the code to ONE Telegram user (NULL =
    open to anyone). The FULL code text is returned exactly once (for the
    owner to copy); afterwards only masked fragments are ever displayed.
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
    targeted = int(targeted_user_id) if targeted_user_id else None

    created = _utcnow()
    expires = created + timedelta(days=valid_days)
    # Collision-safe insert (26^8 space — retries are theoretical).
    for _attempt in range(5):
        code = _generate_code(plan)
        code_id = pg.insert_returning_id(
            "INSERT INTO redemption_codes "
            "(code, plan, runtime_days, max_uses, valid_days, note, created_by, "
            "targeted_user_id, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, plan, runtime_days, max_uses, valid_days,
             (note or "")[:200], created_by, targeted, _ts(expires)),
        )
        if code_id:
            _log(code_id, "created", user_id=created_by,
                 details=f"plan={plan} runtime_days={runtime_days} max_uses={max_uses} "
                         f"valid_days={valid_days} targeted={targeted or 'open'} "
                         f"prefix={code_prefix(code)}")
            return {"ok": True, "code_id": code_id, "code": format_code(code),
                    "plan": plan,
                    "runtime_days": runtime_days, "max_uses": max_uses,
                    "valid_days": valid_days, "targeted_user_id": targeted,
                    "expires_at": _ts(expires)}
    return {"ok": False, "reason": "collision"}


def get_code(code_id: int) -> dict | None:
    return pg.fetchone("SELECT * FROM redemption_codes WHERE id = ?", (int(code_id),))


def find_code(code_text: str) -> dict | None:
    return pg.fetchone(
        "SELECT * FROM redemption_codes WHERE code = ?", (normalize_code(code_text),)
    )


_CODE_COLS = ("id, code, plan, runtime_days, max_uses, uses_consumed, valid_days, "
              "status, targeted_user_id, expires_at, created_at")


def list_codes(limit: int = 15, status: str | None = None) -> list[dict]:
    """Owner list. ``status`` filters by EFFECTIVE state: 'expired' means an
    'active' row past expires_at; 'active' means not-yet-expired."""
    if status == "expired":
        return pg.fetchall(
            f"SELECT {_CODE_COLS} FROM redemption_codes "
            "WHERE status = 'active' AND expires_at IS NOT NULL "
            "AND expires_at <= CURRENT_TIMESTAMP ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
    if status == "active":
        return pg.fetchall(
            f"SELECT {_CODE_COLS} FROM redemption_codes "
            "WHERE status = 'active' AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
    if status in ("exhausted", "revoked"):
        return pg.fetchall(
            f"SELECT {_CODE_COLS} FROM redemption_codes WHERE status = ? "
            "ORDER BY id DESC LIMIT ?", (status, int(limit)),
        )
    return pg.fetchall(
        f"SELECT {_CODE_COLS} FROM redemption_codes ORDER BY id DESC LIMIT ?",
        (int(limit),),
    )


def code_status_counts() -> dict:
    """Counts per EFFECTIVE status for the owner screen tabs."""
    rows = pg.fetchall("SELECT status, expires_at FROM redemption_codes")
    counts = {"all": len(rows), "active": 0, "exhausted": 0, "expired": 0, "revoked": 0}
    now = _utcnow()
    for r in rows:
        st = r.get("status") or "active"
        if st == "active":
            exp = _parse_ts(r.get("expires_at"))
            if exp and now > exp:
                st = "expired"
        counts[st] = counts.get(st, 0) + 1
    return counts


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

def cleanup_orphan_pending_instances() -> int:
    """Self-heal pending_token shells that NO flow owns anymore.

    A crash between the slot reservation and the audit-row write (or a
    transient audit failure) can leave a pending instance no reservation
    log covers. With the one-clone-per-user gate such an orphan would block
    that user from EVERY flow forever. This deletes pending instances older
    than the reservation TTL that are:
      * not referenced by a referral reward (reward-flow instances may
        legitimately wait for a token far longer than the TTL), and
      * not covered by a live 'reserved'/'trial_reserved' audit row.
    Abandoned trial shells give the one-ever chance back (the registry row
    goes with the instance); activated trials are untouched — their
    instances left pending_token status long ago.
    """
    try:
        removed = pg.execute(
            "DELETE FROM bot_instances "
            "WHERE status = 'pending_token' "
            "AND created_at < CURRENT_TIMESTAMP - (? * INTERVAL '1 minute') "
            "AND id NOT IN (SELECT COALESCE(instance_id, 0) FROM referral_rewards) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM redemption_log x "
            "  WHERE x.instance_id = bot_instances.id "
            "  AND x.action IN ('reserved', 'trial_reserved') "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM redemption_log y "
            "    WHERE y.code_id = x.code_id AND y.user_id = x.user_id "
            "    AND y.id > x.id AND y.action IN ('used', 'released')))",
            (RESERVATION_TTL_MINUTES,),
        )
        if removed:
            logger.info("orphan pending-instance cleanup removed %s row(s)", removed)
        # Trials whose shell just got orphan-deleted: return the chance.
        pg.execute(
            "DELETE FROM clone_trials t "
            "WHERE t.instance_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM bot_instances b WHERE b.id = t.instance_id)",
        )
        return int(removed or 0)
    except Exception:
        logger.exception("orphan pending-instance cleanup failed")
        return 0


def _sweep_stale_reservations(code_id: int) -> None:
    """Release 'reserved' slots older than the TTL whose owner never
    completed (no later 'used'/'released' row for the same user+code)."""
    try:
        stale = pg.fetchall(
            "SELECT r.user_id, r.instance_id FROM redemption_log r "
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
            # The abandoned pre-created instance MUST go too — a lingering
            # pending_token row would block the user from every future flow
            # (one clone per user) long after the slot was given back.
            inst = row.get("instance_id")
            if inst:
                pg.execute(
                    "DELETE FROM bot_instances WHERE id = ? AND status = 'pending_token'",
                    (int(inst),),
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
                           not_for_you | has_active_instance | race_lost
    """
    uid = int(user_id)
    norm = normalize_code(code_text)
    # Canonical codes are exactly 11 chars (BASIC/PRO/CHJ prefix + 8 body);
    # a far longer string is garbage that should never hit the code index.
    if not norm or len(norm) < 6 or len(norm) > 64:
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

    # User restriction: a targeted code only ever works for ITS user.
    targeted = row.get("targeted_user_id")
    if targeted and int(targeted) != uid:
        return {"ok": False, "reason": "not_for_you"}

    used_before = pg.fetchone(
        "SELECT 1 AS ok FROM redemption_log WHERE code_id = ? AND user_id = ? "
        "AND action = 'used' LIMIT 1", (code_id, uid),
    )
    if used_before:
        return {"ok": False, "reason": "already_used"}

    # Re-sent while THIS user's reservation is still in flight → resume the
    # same flow (never consumes a second slot, never spawns a 2nd instance).
    in_flight = pg.fetchone(
        "SELECT instance_id FROM redemption_log r "
        "WHERE r.code_id = ? AND r.user_id = ? AND r.action = 'reserved' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM redemption_log x WHERE x.code_id = r.code_id "
        "  AND x.user_id = r.user_id AND x.id > r.id "
        "  AND x.action IN ('used', 'released')) "
        "ORDER BY r.id DESC LIMIT 1",
        (code_id, uid),
    )
    if in_flight:
        return {"ok": True, "resumed": True, "code_id": code_id,
                "plan": row.get("plan") or "basic",
                "runtime_days": int(row.get("runtime_days") or 30),
                "instance_id": in_flight.get("instance_id")}

    # One clone per user: an ACTIVE instance blocks a second redemption.
    active_inst = pg.fetchone(
        "SELECT id FROM bot_instances WHERE owner_id = ? AND status = 'active' LIMIT 1",
        (uid,),
    )
    if active_inst:
        return {"ok": False, "reason": "has_active_instance"}

    # One flow at a time: a PENDING instance means the user is mid-activation
    # somewhere else (another code, the trial, or the referral reward). The
    # same-code resume already returned above, so anything pending here is a
    # DIFFERENT flow — starting a second one could end in two clones.
    # Self-heal first: an orphaned shell from a crashed flow must not block
    # this user forever.
    cleanup_orphan_pending_instances()
    pending_other = pg.fetchone(
        "SELECT id FROM bot_instances WHERE owner_id = ? AND status = 'pending_token' LIMIT 1",
        (uid,),
    )
    if pending_other:
        return {"ok": False, "reason": "pending_flow"}

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
    when THIS user holds a live reservation for THIS code; the pre-created
    pending instance is deleted so abandoned rows never linger."""
    uid, cid = int(user_id), int(code_id)
    res_row = pg.fetchone(
        "SELECT instance_id FROM redemption_log r "
        "WHERE r.code_id = ? AND r.user_id = ? AND r.action = 'reserved' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM redemption_log x WHERE x.code_id = r.code_id "
        "  AND x.user_id = r.user_id AND x.id > r.id "
        "  AND x.action IN ('used', 'released')) "
        "ORDER BY r.id DESC LIMIT 1",
        (cid, uid),
    )
    if not res_row:
        return False
    pg.execute(
        "UPDATE redemption_codes SET uses_consumed = GREATEST(uses_consumed - 1, 0) "
        "WHERE id = ?", (cid,),
    )
    inst = res_row.get("instance_id")
    if inst:
        pg.execute("DELETE FROM bot_instances WHERE id = ? AND status = 'pending_token'",
                   (int(inst),))
    _log(cid, "released", user_id=uid, details=(reason or "activation_not_completed")[:120])
    return True


# ---- 24-hour free trial (one per Telegram user, ever) -------------------------

def trial_eligible(user_id: int) -> tuple[bool, str]:
    """(ok, reason). reasons: trial_used | pending_flow | has_instance."""
    uid = int(user_id)
    if pg.fetchone("SELECT 1 AS ok FROM clone_trials WHERE user_id = ?", (uid,)):
        return False, "trial_used"
    # Mid-activation elsewhere (code/reward flow left a pending instance) —
    # the user must finish or cancel that flow first; otherwise two parallel
    # flows could each link a token and yield two clones.
    pend = pg.fetchone(
        "SELECT id FROM bot_instances WHERE owner_id = ? AND status = 'pending_token' LIMIT 1",
        (uid,),
    )
    if pend:
        return False, "pending_flow"
    inst = pg.fetchone(
        "SELECT id FROM bot_instances WHERE owner_id = ? AND status <> 'pending_token' LIMIT 1",
        (uid,),
    )
    if inst:
        return False, "has_instance"
    return True, "ok"


def start_trial(user_id: int, plan: str = "basic") -> dict:
    """Atomically claim the one-ever trial + pre-create the pending instance.

    The INSERT's PK conflict is the concurrency guard — double-taps or two
    devices can never grant two trials. The 24h window itself starts only
    AFTER token verification (runtime_days=1 through the SAME
    verify_and_link_token pipeline codes use).
    """
    uid = int(user_id)
    plan = "pro" if (plan or "").strip().lower() == "pro" else "basic"
    ok, reason = trial_eligible(uid)
    if not ok:
        return {"ok": False, "reason": reason}
    affected = pg.execute(
        "INSERT INTO clone_trials (user_id, plan) VALUES (?, ?) "
        "ON CONFLICT (user_id) DO NOTHING",
        (uid, plan),
    )
    if not affected:
        return {"ok": False, "reason": "trial_used"}
    instance_id = pg.insert_returning_id(
        "INSERT INTO bot_instances (owner_id, status, plan) VALUES (?, 'pending_token', ?)",
        (uid, plan),
    )
    pg.execute("UPDATE clone_trials SET instance_id = ? WHERE user_id = ?", (instance_id, uid))
    _log(0, "trial_reserved", user_id=uid, instance_id=instance_id,
         details=f"plan={plan} hours={TRIAL_HOURS}")
    return {"ok": True, "instance_id": instance_id, "plan": plan,
            "runtime_days": 1, "hours": TRIAL_HOURS, "runtime_hours": TRIAL_HOURS}


def release_trial(user_id: int, reason: str = "") -> bool:
    """Cancelled/failed trial → the user KEEPS their one-ever chance: the
    registry row and the pending instance are removed. REFUSED once the
    trial was actually activated (instance left pending_token) — the trial
    is consumed forever at that point."""
    uid = int(user_id)
    row = pg.fetchone("SELECT instance_id FROM clone_trials WHERE user_id = ?", (uid,))
    if not row:
        return False
    inst = row.get("instance_id")
    if inst:
        inst_row = pg.fetchone("SELECT status FROM bot_instances WHERE id = ?", (int(inst),))
        if inst_row and (inst_row.get("status") or "") != "pending_token":
            return False
        pg.execute("DELETE FROM bot_instances WHERE id = ? AND status = 'pending_token'",
                   (int(inst),))
    pg.execute("DELETE FROM clone_trials WHERE user_id = ?", (uid,))
    _log(0, "trial_released", user_id=uid, details=(reason or "not_completed")[:120])
    return True


def code_uses(code_id: int, limit: int = 50) -> list[dict]:
    """سجل الاستخدام — who used this code, when, and which clone they got.
    Bot usernames come from the audit details; tokens never exist there."""
    rows = pg.fetchall(
        "SELECT user_id, instance_id, details, created_at FROM redemption_log "
        "WHERE code_id = ? AND action = 'used' ORDER BY id ASC LIMIT ?",
        (int(code_id), int(limit)),
    )
    out = []
    for r in rows:
        det = r.get("details") or ""
        bot = det.split("bot=@", 1)[1].split(" ", 1)[0] if "bot=@" in det else ""
        plan = None
        if r.get("instance_id"):
            inst = pg.fetchone("SELECT plan FROM bot_instances WHERE id = ?",
                               (int(r["instance_id"]),))
            plan = (inst or {}).get("plan")
        out.append({"user_id": r.get("user_id"), "instance_id": r.get("instance_id"),
                    "bot_username": bot, "plan": plan, "created_at": r.get("created_at")})
    return out


def confirm_trial(user_id: int, instance_id: int, bot_username: str = "") -> None:
    """Audit-only: the registry row from start_trial already marks the trial
    consumed forever; this records WHEN it was actually activated."""
    _log(0, "trial_used", user_id=int(user_id), instance_id=int(instance_id),
         details=f"bot=@{(bot_username or '')[:40]}")


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
