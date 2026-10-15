"""Referral & clone-instance reward engine.

Core rules (exactly as specified):
  * Every Telegram account counts ONCE — ``referrals.referred_id`` is UNIQUE.
  * Nobody can refer themselves, and a binding can never be moved to another
    referrer after the fact.
  * The user must actually START the bot (binding only happens on /start with
    a ref_ code, for brand-new accounts).
  * A referral becomes QUALIFIED when the referred user either
      (a) completes their FIRST successful download, or
      (b) uses the bot on 2 different UTC days.
  * Anti-fraud: binding velocity per referrer is capped per hour (excess
    bindings are marked ``suspicious`` and never count), banned users are
    rejected, and reward claims are atomic (a reward can never be granted
    twice even if the button is pressed 10 times).

Tokens are secrets with full bot privileges:
  * stored ONLY encrypted (Fernet/AES128-CBC+HMAC via ``cryptography``),
  * a sha256 hash guarantees a token is never linked to two instances,
  * the plaintext never appears in logs, and callers delete the user's
    message immediately after capture.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import secrets
import string
from datetime import datetime, timezone, timedelta

from database import pg
from config.settings import (
    BOT_TOKEN, MAIN_BOT_USERNAME, REFERRAL_TARGET, BOT_SECRETS_KEY,
    CLONE_PLAN_BASIC_DAYS, CLONE_PLAN_PRO_DAYS, CLONE_PRO_TARGET_MULTIPLIER,
)

logger = logging.getLogger(__name__)

# ---- milestone notifications ---------------------------------------------------
_notifier_bot = None
_notifier_loop = None


def set_notifier(bot, loop=None) -> None:
    """Register the main bot so the engine can push milestone notifications."""
    global _notifier_bot, _notifier_loop
    _notifier_bot = bot
    _notifier_loop = loop


def notify_user(user_id: int, text: str) -> bool:
    """Best-effort push of a milestone message through the main bot."""
    if not _notifier_bot:
        return False
    try:
        coro = _notifier_bot.send_message(
            chat_id=int(user_id),
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if _notifier_loop is not None:
            asyncio.run_coroutine_threadsafe(coro, _notifier_loop)
        return True
    except Exception:
        logger.exception("milestone notification failed uid=%s", user_id)
        return False


# ---- anti-fraud knobs --------------------------------------------------------
MAX_BINDS_PER_HOUR = int(os.getenv("REFERRAL_MAX_BINDS_PER_HOUR", "20"))
CODE_LEN = 6
_CODE_ALPHABET = string.ascii_uppercase + string.digits

_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


# ---- clone plans & validity -------------------------------------------------
# Every activated clone runs on a time-limited plan:
#   basic -> CLONE_PLAN_BASIC_DAYS days (default 30)
#   pro   -> CLONE_PLAN_PRO_DAYS days   (default 40)
# Pro is earned at activation by reaching pro_target() qualified referrals.
# Expiry is enforced three ways: the periodic worker (expire_due_instances in
# instance_manager), the spawn guard (an expired clone NEVER boots), and the
# stage machine (users see "expired" instead of "active").

def clone_plan_days(plan) -> int:
    """Validity window length for a plan, in days."""
    return CLONE_PLAN_PRO_DAYS if (plan or "basic") == "pro" else CLONE_PLAN_BASIC_DAYS


def pro_target() -> int:
    """Qualified referrals required at activation time to earn the Pro plan."""
    return max(1, int(get_effective_target() * CLONE_PRO_TARGET_MULTIPLIER))


def _utcnow() -> datetime:
    """Naive UTC now - matches how CURRENT_TIMESTAMP is stored on both backends."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_ts(value):
    """Parse a TIMESTAMP coming from either backend (datetime or str)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value)[:26], fmt)
        except ValueError:
            continue
    return None


def instance_days_left(inst) -> int | None:
    """Days remaining on the instance's plan, or None when no expiry is set."""
    exp = _parse_ts((inst or {}).get("expires_at"))
    if exp is None:
        return None
    return max(0, (exp - _utcnow()).days)


def is_instance_expired(inst) -> bool:
    """True when the instance's validity window has passed."""
    exp = _parse_ts((inst or {}).get("expires_at"))
    return exp is not None and exp <= _utcnow()


# ---- token crypto ------------------------------------------------------------

def _fernet_key() -> bytes:
    """32-byte urlsafe key. Prefers BOT_SECRETS_KEY; else derives from the
    main bot token (never logged)."""
    if BOT_SECRETS_KEY:
        digest = hashlib.sha256(BOT_SECRETS_KEY.encode()).digest()
    else:
        digest = hashlib.sha256(("chj::" + (BOT_TOKEN or "dev")).encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_token(plaintext: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_fernet_key()).encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_fernet_key()).decrypt(ciphertext.encode()).decode()


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.strip().encode()).hexdigest()


def looks_like_token(text: str) -> bool:
    return bool(_TOKEN_RE.match((text or "").strip()))


# ---- codes & links ------------------------------------------------------------

def get_or_create_code(owner_id: int) -> str:
    """Permanent per-user referral code (created once, reused forever)."""
    row = pg.fetchone("SELECT code FROM referral_codes WHERE owner_id = ?", (owner_id,))
    if row:
        return row["code"]
    for _ in range(8):
        code = "ref_" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LEN))
        try:
            pg.execute(
                "INSERT INTO referral_codes (owner_id, code) VALUES (?, ?)",
                (owner_id, code),
            )
            return code
        except Exception:
            # UNIQUE collision on either owner or code — re-read / retry.
            row = pg.fetchone("SELECT code FROM referral_codes WHERE owner_id = ?", (owner_id,))
            if row:
                return row["code"]
    raise RuntimeError("Could not allocate a referral code")


def referral_link(owner_id: int, bot_username: str | None = None) -> str:
    username = (bot_username or MAIN_BOT_USERNAME or "").strip().lstrip("@")
    code = get_or_create_code(owner_id)
    if username:
        return f"https://t.me/{username}?start={code}"
    return code  # username unknown (clone without MAIN_BOT_USERNAME set)


# ---- user registry & activity --------------------------------------------------

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def register_user(tg_user, is_new: bool, source: str = "main") -> None:
    """Upsert the referral-network user mirror + record today's activity."""
    uid = int(tg_user.id)
    today = _today()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = pg.fetchone("SELECT activity_days FROM ref_users WHERE telegram_id = ?", (uid,))
    if existing is None:
        pg.execute(
            "INSERT INTO ref_users (telegram_id, username, first_name, source, registered_at, last_active_at, activity_days) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)",
            (uid, (tg_user.username or "")[:64], (tg_user.first_name or "")[:64], source[:40], now_iso, today),
        )
    else:
        days = set(d for d in (existing.get("activity_days") or "").split(",") if d)
        days.add(today)
        days_str = ",".join(sorted(days))[-400:]
        pg.execute(
            "UPDATE ref_users SET last_active_at = ?, activity_days = ?, username = ?, first_name = ? WHERE telegram_id = ?",
            (now_iso, days_str, (tg_user.username or "")[:64], (tg_user.first_name or "")[:64], uid),
        )
    if not is_new:
        # Returning user: 2 distinct activity days can qualify a pending referral.
        _maybe_qualify_by_activity(uid)


def _is_banned(user_id: int) -> bool:
    try:
        from database.users import get_user
        u = get_user(user_id)
        return bool(u and u.get("is_banned"))
    except Exception:
        return False


def bind_referral(code: str | None, new_user_id: int) -> str:
    """Attribute a BRAND-NEW user to a referrer. Returns a status string:
    bound | suspicious | self | dup | bad_code | banned | flood | no_code
    """
    if not code:
        return "no_code"
    code = str(code).strip()
    if not code.startswith("ref_"):
        return "bad_code"
    row = pg.fetchone("SELECT owner_id FROM referral_codes WHERE code = ?", (code,))
    if not row:
        return "bad_code"
    referrer_id = int(row["owner_id"])
    if referrer_id == int(new_user_id):
        return "self"
    if _is_banned(referrer_id) or _is_banned(new_user_id):
        return "banned"

    # Velocity guard: too many fresh bindings per hour -> suspicious (never counted).
    recent = pg.fetchone(
        "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id = ? AND bound_at >= CURRENT_TIMESTAMP - INTERVAL '1 hour'",
        (referrer_id,),
    ) if pg.BACKEND == "postgres" else pg.fetchone(
        "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id = ? AND bound_at >= datetime('now', '-1 hour')",
        (referrer_id,),
    )
    status = "pending"
    if recent and int(recent["cnt"]) >= MAX_BINDS_PER_HOUR:
        status = "suspicious"
        log_activation(referrer_id, None, "referral_flood_flag",
                       f"referred={new_user_id} binds_last_hour={recent['cnt']}")

    try:
        pg.execute(
            "INSERT INTO referrals (referrer_id, referred_id, code, status) VALUES (?, ?, ?, ?)",
            (referrer_id, int(new_user_id), code, status),
        )
    except Exception:
        return "dup"  # referred_id UNIQUE — account already attributed once.
    _ensure_reward_row(referrer_id)
    return status


# ---- qualification -------------------------------------------------------------

def _ensure_reward_row(user_id: int) -> None:
    pg.execute(
        "INSERT INTO referral_rewards (user_id, qualified_count, status) VALUES (?, 0, 'progress') "
        "ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )


def _recount_and_update_reward(referrer_id: int) -> dict:
    row = pg.fetchone(
        "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id = ? AND status = 'qualified'",
        (referrer_id,),
    )
    count = int(row["cnt"]) if row else 0
    _ensure_reward_row(referrer_id)
    reward = pg.fetchone("SELECT status FROM referral_rewards WHERE user_id = ?", (referrer_id,))
    new_status = (reward or {}).get("status", "progress")
    if new_status == "progress" and count >= REFERRAL_TARGET:
        new_status = "eligible"
        log_activation(referrer_id, None, "reward_eligible", f"qualified={count}")
    pg.execute(
        "UPDATE referral_rewards SET qualified_count = ?, status = ? WHERE user_id = ? AND status IN ('progress', 'eligible')",
        (count, new_status, referrer_id),
    )
    return {"qualified": count, "status": new_status}


def _qualify_referral(referred_id: int, reason: str) -> bool:
    """Move a pending referral to qualified (once), then refresh the reward."""
    row = pg.fetchone("SELECT referrer_id, status FROM referrals WHERE referred_id = ?", (int(referred_id),))
    if not row or row["status"] != "pending":
        return False
    affected = pg.execute(
        "UPDATE referrals SET status = 'qualified', qualified_at = CURRENT_TIMESTAMP "
        "WHERE referred_id = ? AND status = 'pending'",
        (int(referred_id),),
    )
    if not affected:
        return False
    referrer_id = int(row["referrer_id"])
    result = _recount_and_update_reward(referrer_id)
    log_activation(referrer_id, None, "referral_qualified",
                   f"referred={referred_id} reason={reason} total={result['qualified']}")
    _auto_notify_milestones(referrer_id)
    return True


def on_successful_download(user_id: int) -> None:
    """Rule (a): first successful download qualifies the referral."""
    try:
        _qualify_referral(user_id, "first_download")
    except Exception:
        logger.exception("referral qualify (download) failed uid=%s", user_id)


def _maybe_qualify_by_activity(user_id: int) -> None:
    """Rule (b): activity on 2+ distinct UTC days qualifies the referral."""
    try:
        row = pg.fetchone("SELECT activity_days FROM ref_users WHERE telegram_id = ?", (int(user_id),))
        if not row:
            return
        days = [d for d in (row.get("activity_days") or "").split(",") if d]
        if len(days) >= 2:
            _qualify_referral(user_id, "two_active_days")
    except Exception:
        logger.exception("referral qualify (activity) failed uid=%s", user_id)


# ---- stats ------------------------------------------------------------------------

def get_stats(user_id: int) -> dict:
    uid = int(user_id)
    _ensure_reward_row(uid)
    reward = pg.fetchone("SELECT * FROM referral_rewards WHERE user_id = ?", (uid,)) or {}
    qualified = pg.fetchone(
        "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id = ? AND status = 'qualified'", (uid,)
    )["cnt"]
    pending = pg.fetchone(
        "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id = ? AND status = 'pending'", (uid,)
    )["cnt"]
    suspicious = pg.fetchone(
        "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id = ? AND status = 'suspicious'", (uid,)
    )["cnt"]
    qualified = int(qualified or 0)
    target = get_effective_target()
    pct = min(100.0, round(qualified * 100.0 / target, 1)) if target else 0.0
    instance = pg.fetchone(
        "SELECT id, bot_username, status, plan, activated_at, expires_at "
        "FROM bot_instances WHERE owner_id = ? ORDER BY id DESC LIMIT 1", (uid,)
    )
    if instance:
        instance = dict(instance)
        instance["days_left"] = instance_days_left(instance)
    return {
        "qualified": qualified,
        "pending": int(pending or 0),
        "suspicious": int(suspicious or 0),
        "target": target,
        "percent": pct,
        "reward_status": reward.get("status", "progress"),
        "instance": instance,
        "code": get_or_create_code(uid),
    }


def get_referrals_list(user_id: int, limit: int = 10, offset: int = 0) -> list[dict]:
    return pg.fetchall(
        "SELECT referred_id, status, bound_at, qualified_at FROM referrals "
        "WHERE referrer_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (int(user_id), limit, offset),
    )


# ---- owner-level network intelligence ------------------------------------------

def get_effective_target() -> int:
    """The referral target actually in force.

    The owner can override REFERRAL_TARGET at runtime from the owner panel;
    the override lives in the main bot_settings table. Falls back to the env
    value (module attribute, so tests that patch it keep working).
    """
    try:
        from database.bot_settings import get_setting
        raw = get_setting("referral_target", "")
        if str(raw).strip().isdigit() and int(raw) > 0:
            return int(raw)
    except Exception:
        pass
    return int(REFERRAL_TARGET)


def set_target_override(value: int | None) -> None:
    """Persist (or clear with None) the owner's referral-target override."""
    from database.bot_settings import set_setting, delete_setting
    if value is None:
        delete_setting("referral_target")
    else:
        set_setting("referral_target", str(int(value)))


def get_network_overview() -> dict:
    """Whole-network counters for the owner's referral center."""
    def _cnt(sql: str, params: tuple = ()) -> int:
        row = pg.fetchone(sql, params)
        return int((row or {}).get("cnt") or 0)

    by_status = {
        row["status"]: int(row["cnt"])
        for row in pg.fetchall(
            "SELECT status, COUNT(*) AS cnt FROM referrals GROUP BY status"
        )
    }
    rewards = {
        row["status"]: int(row["cnt"])
        for row in pg.fetchall(
            "SELECT status, COUNT(*) AS cnt FROM referral_rewards GROUP BY status"
        )
    }
    instances = {
        row["status"]: int(row["cnt"])
        for row in pg.fetchall(
            "SELECT status, COUNT(*) AS cnt FROM bot_instances GROUP BY status"
        )
    }
    total_refs = sum(by_status.values())
    qualified = by_status.get("qualified", 0)
    return {
        "total": total_refs,
        "by_status": by_status,
        "codes": _cnt("SELECT COUNT(*) AS cnt FROM referral_codes"),
        "rewards": rewards,
        "instances": instances,
        "instances_total": sum(instances.values()),
        "conversion": round(qualified * 100.0 / total_refs, 1) if total_refs else 0.0,
        "target": get_effective_target(),
    }


def get_top_referrers(limit: int = 10) -> list[dict]:
    """Leaderboard: most qualified referrals first."""
    return pg.fetchall(
        "SELECT referrer_id, COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'qualified' THEN 1 ELSE 0 END) AS qualified, "
        "SUM(CASE WHEN status = 'suspicious' THEN 1 ELSE 0 END) AS suspicious "
        "FROM referrals GROUP BY referrer_id "
        "ORDER BY qualified DESC, total DESC LIMIT ?",
        (limit,),
    )


def list_all_instances(limit: int = 30) -> list[dict]:
    """Every clone instance ever created (owner's instance-management screen)."""
    return pg.fetchall(
        "SELECT id, owner_id, bot_id, bot_username, status, last_error, created_at, activated_at "
        "FROM bot_instances ORDER BY id DESC LIMIT ?",
        (limit,),
    )


def set_instance_status(instance_id: int, status: str) -> None:
    pg.execute(
        "UPDATE bot_instances SET status = ? WHERE id = ?",
        (status[:30], int(instance_id)),
    )


def get_referrer_of(user_id: int) -> dict | None:
    """Who referred this user (owner's user-profile 'الإحالات' tab)."""
    return pg.fetchone(
        "SELECT referrer_id, code, status, bound_at FROM referrals WHERE referred_id = ?",
        (int(user_id),),
    )


def get_referrer_counts(user_id: int) -> dict:
    row = pg.fetchone(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'qualified' THEN 1 ELSE 0 END) AS qualified, "
        "SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending, "
        "SUM(CASE WHEN status = 'suspicious' THEN 1 ELSE 0 END) AS suspicious "
        "FROM referrals WHERE referrer_id = ?",
        (int(user_id),),
    ) or {}
    return {k: int(row.get(k) or 0) for k in ("total", "qualified", "pending", "suspicious")}


def get_instance_by_owner(owner_id: int) -> dict | None:
    return pg.fetchone(
        "SELECT id, bot_username, status, last_error, activated_at "
        "FROM bot_instances WHERE owner_id = ? ORDER BY id DESC LIMIT 1",
        (int(owner_id),),
    )


# ---- milestones & smart state ------------------------------------------------------

MILESTONE_STEPS = (50, 60)


def milestone_thresholds() -> list[int]:
    """Breakpoints worthy of a push notification, incl. the clone target."""
    return sorted({50, 60, max(1, REFERRAL_TARGET)})


def claim_new_milestones(user_id: int) -> list[int]:
    """Return milestone values crossed since the last notification (idempotent).

    Reading this MUTATES ``referral_rewards.notified_milestone`` so a milestone
    is announced only once. Safe to call lazily on page opens — it returns
    ``[]`` when the automatic path already notified.
    """
    uid = int(user_id)
    _ensure_reward_row(uid)
    row = pg.fetchone("SELECT notified_milestone FROM referral_rewards WHERE user_id = ?", (uid,)) or {}
    notified = int(row.get("notified_milestone") or 0)
    qualified = int(get_stats(uid)["qualified"])
    fresh = [t for t in milestone_thresholds() if qualified >= t and notified < t]
    if not fresh:
        return []
    top = max(fresh)
    pg.execute("UPDATE referral_rewards SET notified_milestone = ? WHERE user_id = ?", (top, uid))
    for t in sorted(fresh):
        log_activation(uid, None, "milestone_reached", f"qualified={qualified} milestone={t}")
    return sorted(fresh)


def milestone_text(value: int) -> str:
    v = int(value)
    if v >= REFERRAL_TARGET:
        return (
            f"🏆 <b>مبروك!</b> أكملت <b>{v}</b> مستخدمًا مؤهلًا.\n"
            "أصبحت مؤهلًا الآن لتفعيل نسختك الخاصة.\n"
            "افتح 🎁 <b>اكسب نسختك من البوت</b> واضغط «🚀 فعّل نسختك الآن»."
        )
    if v >= 60:
        return f"🔥 <b>قربت!</b> وصلت إلى <b>{v}</b> مستخدمًا مؤهلًا — باقي القليل على هدفك. 🎯"
    if v >= 50:
        return f"🎉 <b>إنجاز كبير!</b> وصلت إلى <b>{v}</b> مستخدمًا مؤهلًا — أنت في منتصف الطريق. 💪"
    return f"🎉 وصلت إلى <b>{v}</b> مستخدمًا مؤهلًا."


def _auto_notify_milestones(user_id: int) -> None:
    try:
        for m in claim_new_milestones(user_id):
            notify_user(user_id, milestone_text(m))
    except Exception:
        logger.exception("milestone auto-notify failed uid=%s", user_id)


def referrer_stage(stats: dict) -> str:
    """Pure state machine for the smart «نسختي» button.

    Returns:
      ``active``          — clone linked and active,
      ``awaiting_token``  — reward claimed but token not yet linked,
      ``eligible``        — reached target, clone not requested yet,
      ``progress``        — still collecting qualified referrals.
    """
    inst = stats.get("instance") or {}
    if inst.get("status") == "expired":
        return "expired"
    if inst.get("status") == "active" and inst.get("bot_username"):
        return "active"
    reward = stats.get("reward_status", "progress")
    if reward in ("awaiting_token", "claimed"):
        return "awaiting_token"
    if reward == "eligible":
        return "eligible"
    return "progress"


def get_user_activity_log(user_id: int, limit: int = 12) -> list[dict]:
    return pg.fetchall(
        "SELECT action, details, instance_id, created_at FROM activation_log "
        "WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
        (int(user_id), limit),
    )


def get_instance(instance_id: int) -> dict | None:
    return pg.fetchone(
        "SELECT id, owner_id, bot_id, bot_username, status, last_error, activated_at, "
        "plan, expires_at FROM bot_instances WHERE id = ?",
        (int(instance_id),),
    )


# ---- reward claim (atomic, single-shot) --------------------------------------------

def claim_reward(user_id: int) -> tuple[bool, str]:
    """Atomically move eligible -> awaiting_token. Only ONE call ever wins,
    even under double-taps or retries. Returns (ok, reason)."""
    uid = int(user_id)
    _ensure_reward_row(uid)
    reward = pg.fetchone("SELECT status, qualified_count FROM referral_rewards WHERE user_id = ?", (uid,)) or {}
    status = reward.get("status", "progress")
    if status in ("awaiting_token", "claimed"):
        return True, status  # already in the flow — idempotent
    if status != "eligible":
        return False, "not_eligible"
    affected = pg.execute(
        "UPDATE referral_rewards SET status = 'awaiting_token' WHERE user_id = ? AND status = 'eligible'",
        (uid,),
    )
    if not affected:
        # Lost a race against another press — re-read; treat as already claimed.
        reward = pg.fetchone("SELECT status FROM referral_rewards WHERE user_id = ?", (uid,)) or {}
        return True, reward.get("status", "awaiting_token")
    instance_id = pg.insert_returning_id(
        "INSERT INTO bot_instances (owner_id, status) VALUES (?, 'pending_token')", (uid,)
    )
    pg.execute(
        "UPDATE referral_rewards SET instance_id = ? WHERE user_id = ?", (instance_id, uid)
    )
    log_activation(uid, instance_id, "reward_claimed", f"qualified={reward.get('qualified_count')}")
    return True, "awaiting_token"


# ---- token verification & instance activation ----------------------------------------

def _tg_get_me(token: str) -> dict:
    """Call Telegram getMe. Raises RuntimeError with a SAFE message (the token
    itself must never appear in exceptions or logs)."""
    import json
    import urllib.request
    import urllib.error
    url = f"https://api.telegram.org/bot{token}/getMe"
    req = urllib.request.Request(url, headers={"User-Agent": "chj-referral/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as he:
        try:
            detail = json.loads(he.read().decode()).get("description", "")
        except Exception:
            detail = ""
        raise RuntimeError(detail or "invalid_token")
    except Exception:
        raise RuntimeError("network_error")
    if not data.get("ok"):
        raise RuntimeError("invalid_token")
    return data["result"]


def verify_and_link_token(owner_id: int, token_text: str) -> dict:
    """Full verification pipeline. Returns a dict:
    {ok: True, instance_id, bot_username, bot_id} or {ok: False, reason}.
    Reasons: invalid_token | network_error | already_linked | not_claimed |
             main_bot_token | shape
    """
    uid = int(owner_id)
    token_text = (token_text or "").strip()
    if not looks_like_token(token_text):
        return {"ok": False, "reason": "shape"}
    if BOT_TOKEN and token_text == BOT_TOKEN.strip():
        return {"ok": False, "reason": "main_bot_token"}

    reward = pg.fetchone("SELECT status, instance_id FROM referral_rewards WHERE user_id = ?", (uid,)) or {}
    if reward.get("status") not in ("awaiting_token", "claimed"):
        return {"ok": False, "reason": "not_claimed"}
    instance_id = reward.get("instance_id")
    if not instance_id:
        return {"ok": False, "reason": "not_claimed"}

    try:
        me = _tg_get_me(token_text)
    except RuntimeError as exc:
        reason = str(exc)
        log_activation(uid, instance_id, "token_verify_failed", reason[:120])
        return {"ok": False, "reason": "invalid_token" if "token" in reason or "Unauthorized" in reason else "network_error"}

    bot_id = int(me["id"])
    bot_username = me.get("username", "")

    thash = hash_token(token_text)
    if pg.fetchone("SELECT instance_id FROM bot_secrets WHERE token_hash = ?", (thash,)):
        return {"ok": False, "reason": "already_linked"}
    existing = pg.fetchone(
        "SELECT id, owner_id FROM bot_instances WHERE bot_id = ? AND status = 'active'", (bot_id,)
    )
    if existing:
        return {"ok": False, "reason": "already_linked"}

    try:
        # Upsert: re-linking the SAME instance with a fresh token (e.g. the
        # owner revoked the old one in BotFather) must replace the secret,
        # while token_hash UNIQUE still blocks one token serving two clones.
        pg.execute(
            "INSERT INTO bot_secrets (instance_id, token_encrypted, token_hash) VALUES (?, ?, ?) "
            "ON CONFLICT (instance_id) DO UPDATE SET token_encrypted = EXCLUDED.token_encrypted, token_hash = EXCLUDED.token_hash",
            (instance_id, encrypt_token(token_text), thash),
        )
    except Exception:
        return {"ok": False, "reason": "already_linked"}

    # Plan assignment at activation: reaching the Pro threshold in QUALIFIED
    # referrals earns the longer Pro validity window; everyone else gets Basic.
    qualified_now = pg.fetchone(
        "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id = ? AND status = 'qualified'",
        (uid,),
    )["cnt"]
    plan = "pro" if int(qualified_now or 0) >= pro_target() else "basic"
    activated = _utcnow()
    expires = activated + timedelta(days=clone_plan_days(plan))
    activated_ts = activated.strftime("%Y-%m-%d %H:%M:%S")
    expires_ts = expires.strftime("%Y-%m-%d %H:%M:%S")
    pg.execute(
        "UPDATE bot_instances SET bot_id = ?, bot_username = ?, status = 'active', plan = ?, "
        "activated_at = ?, expires_at = ?, last_error = '' WHERE id = ?",
        (bot_id, bot_username[:64], plan, activated_ts, expires_ts, instance_id),
    )
    for key, value in (
        ("clone_mode", "1"),
        ("owner_id", str(uid)),
        ("back_to_main", MAIN_BOT_USERNAME or ""),
    ):
        pg.execute(
            "INSERT INTO clone_settings (instance_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (instance_id, key) DO UPDATE SET value = EXCLUDED.value",
            (instance_id, key, value),
        )
    pg.execute(
        "UPDATE referral_rewards SET status = 'claimed', claimed_at = CURRENT_TIMESTAMP WHERE user_id = ?",
        (uid,),
    )
    log_activation(uid, instance_id, "instance_activated",
                   f"bot=@{bot_username} bot_id={bot_id} plan={plan} days={clone_plan_days(plan)} expires={expires_ts}")
    return {"ok": True, "instance_id": instance_id, "bot_username": bot_username,
            "bot_id": bot_id, "plan": plan, "expires_at": expires_ts}


def load_instance_token(instance_id: int) -> str | None:
    row = pg.fetchone("SELECT token_encrypted FROM bot_secrets WHERE instance_id = ?", (int(instance_id),))
    if not row:
        return None
    try:
        token = decrypt_token(row["token_encrypted"])
        pg.execute("UPDATE bot_secrets SET last_used_at = CURRENT_TIMESTAMP WHERE instance_id = ?", (int(instance_id),))
        return token
    except Exception:
        logger.exception("Token decrypt failed for instance %s", instance_id)
        return None


def list_active_instances() -> list[dict]:
    return pg.fetchall("SELECT * FROM bot_instances WHERE status = 'active' ORDER BY id ASC")


def set_instance_error(instance_id: int, error: str) -> None:
    try:
        pg.execute("UPDATE bot_instances SET last_error = ? WHERE id = ?", (error[:300], int(instance_id)))
    except Exception:
        pass


# ---- plan validity lifecycle ------------------------------------------------

def list_expired_due_instances() -> list:
    """ACTIVE instances whose validity window has passed - the enforcement
    source for the automatic expiry worker."""
    rows = pg.fetchall("SELECT * FROM bot_instances WHERE status = 'active' ORDER BY id ASC")
    return [r for r in rows if is_instance_expired(r)]


def mark_instance_expired(instance_id: int) -> None:
    """Move an instance to 'expired' + write the audit trail."""
    pg.execute("UPDATE bot_instances SET status = 'expired' WHERE id = ?", (int(instance_id),))
    inst = get_instance(int(instance_id)) or {}
    log_activation(inst.get("owner_id"), int(instance_id), "instance_expired",
                   f"plan={inst.get('plan') or 'basic'} expires_at={inst.get('expires_at')}")


def extend_instance(instance_id: int, days: int = None) -> str:
    """Owner-approved renewal/extension: push expires_at forward from NOW and
    return the instance to 'active'. This is the ONLY path an expired clone
    comes back online. Returns the new expiry as a display string."""
    inst = get_instance(int(instance_id)) or {}
    plan = inst.get("plan") or "basic"
    delta = int(days) if days else clone_plan_days(plan)
    new_expiry = _utcnow() + timedelta(days=delta)
    ts = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
    pg.execute(
        "UPDATE bot_instances SET expires_at = ?, status = 'active', last_error = '' WHERE id = ?",
        (ts, int(instance_id)),
    )
    log_activation(inst.get("owner_id"), int(instance_id), "instance_renewed",
                   f"plan={plan} days={delta} expires_at={ts}")
    return ts


def set_instance_plan(instance_id: int, plan: str) -> str:
    """Owner plan switch (basic<->pro). Recomputes the expiry from the original
    activation when the clone is still inside its window, otherwise from now.
    Returns the new expiry as a display string."""
    plan = "pro" if plan == "pro" else "basic"
    inst = get_instance(int(instance_id)) or {}
    base = _parse_ts(inst.get("activated_at")) or _utcnow()
    if is_instance_expired(inst):
        base = _utcnow()
    new_expiry = base + timedelta(days=clone_plan_days(plan))
    ts = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
    pg.execute("UPDATE bot_instances SET plan = ?, expires_at = ? WHERE id = ?",
               (plan, ts, int(instance_id)))
    log_activation(inst.get("owner_id"), int(instance_id), "instance_plan_changed",
                   f"plan={plan} expires_at={ts}")
    return ts


# ---- audit -------------------------------------------------------------------------

def log_activation(owner_id, instance_id, action: str, details: str = "") -> None:
    """Append-only audit trail for every activation-related operation."""
    try:
        pg.execute(
            "INSERT INTO activation_log (owner_id, instance_id, action, details) VALUES (?, ?, ?, ?)",
            (owner_id, instance_id, action[:60], (details or "")[:300]),
        )
    except Exception:
        logger.exception("activation log failed action=%s", action)
