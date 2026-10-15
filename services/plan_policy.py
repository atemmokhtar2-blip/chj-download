"""Server-side plan policy — the ONE source of truth for Basic vs Pro.

Spec (enforced HERE, on the server, on every operation — hiding buttons is
cosmetics, this module is the enforcement):

    الميزة                     Basic        Pro
    تحميلات يومية/مستخدم        70           150
    روابط الدفعة الواحدة        3            10
    جودة الفيديو                ≤ 720p       أعلى جودة متاحة
    سجل التحميلات               7 أيام       90 يومًا
    لوحة إدارة خاصة             لا           نعم
    إدارة مستخدمي النسخة        لا           نعم
    الإذاعة                     لا           نعم
    إدارة الإداريين             لا           نعم
    إدارة الأكواد/النسخ الأخرى  لا           لا (للمالك الرئيسي فقط)

Every clone instance carries ``instance_id`` + ``plan`` in the database
(``bot_instances``); counters come from the database (never memory alone) so
restarts cannot reset a user's daily usage. The main bot has NO plan caps —
the owner's own configuration and the global config limits apply there.
"""

from __future__ import annotations

import logging
import time

from database import pg
from database.db import db_cursor

logger = logging.getLogger(__name__)

PLAN_BASIC = "basic"
PLAN_PRO = "pro"
PLAN_MAIN = "main"

PLAN_LIMITS: dict[str, dict] = {
    PLAN_BASIC: {
        "daily_downloads": 70,
        "batch_urls": 3,
        "max_video_height": 720,      # 720p ceiling when available
        "audio_cap_kbps": 192,        # MP3 bitrate ceiling
        "history_days": 7,
        "admin_panel": False,
    },
    PLAN_PRO: {
        "daily_downloads": 150,
        "batch_urls": 10,
        "max_video_height": None,     # highest available
        "audio_cap_kbps": None,       # full 320kbps, no ceiling
        "history_days": 90,
        "admin_panel": True,
    },
}

PLAN_LABELS = {PLAN_BASIC: "\U0001F193 Basic", PLAN_PRO: "\U0001F451 Pro"}

ADMIN_PERMS = ("can_ban", "can_broadcast", "can_stats")

_instance_cache: dict[int, tuple[float, dict | None]] = {}
_CACHE_TTL = 60.0  # plan changes apply within a minute without a restart


# --------------------------------------------------------------------------
# Plan resolution
# --------------------------------------------------------------------------

def normalize_plan(plan) -> str:
    """Unknown / None / garbage -> Basic. Fails closed to the STRICTER plan."""
    return PLAN_PRO if str(plan or "").strip().lower() == PLAN_PRO else PLAN_BASIC


def limits(plan) -> dict:
    """Policy dict for a plan name (copy — callers can't mutate the table)."""
    return dict(PLAN_LIMITS[normalize_plan(plan)])


# --------------------------------------------------------------------------
# User-facing plan visibility (what exists / what doesn't — shown, not hidden)
# --------------------------------------------------------------------------

def plan_comparison_lines() -> list[str]:
    """Arabic HTML lines comparing Basic vs Pro, rendered FROM PLAN_LIMITS so
    what users SEE can never drift from what the server ENFORCES."""
    b = PLAN_LIMITS[PLAN_BASIC]
    p = PLAN_LIMITS[PLAN_PRO]
    return [
        f"• تحميلات يومية لكل مستخدم: 🆓 <b>{b['daily_downloads']}</b> — 👑 <b>{p['daily_downloads']}</b>",
        f"• روابط في الدفعة الواحدة: 🆓 <b>{b['batch_urls']}</b> — 👑 <b>{p['batch_urls']}</b>",
        f"• جودة الفيديو: 🆓 <b>{b['max_video_height']}p كحد أقصى</b> — 👑 <b>أعلى جودة متاحة</b>",
        f"• نقاء الصوت (MP3): 🆓 <b>{b['audio_cap_kbps']}kbps كحد أقصى</b> — 👑 <b>320kbps كامل</b>",
        f"• سجل التحميلات: 🆓 <b>{b['history_days']} أيام</b> — 👑 <b>{p['history_days']} يومًا</b>",
        "• لوحة إدارة خاصة: 🆓 ❌ غير موجودة — 👑 ✅ موجودة",
        "• إدارة مستخدمي النسخة: 🆓 ❌ — 👑 ✅",
        "• الإذاعة داخل النسخة: 🆓 ❌ — 👑 ✅",
        "• إدارة الإداريين والصلاحيات: 🆓 ❌ — 👑 ✅",
    ]


def plan_summary_text(ctx: dict) -> str:
    """The plan card any user can open (settings → 📊): full Basic-vs-Pro
    comparison + which plan THIS bot runs under right now."""
    compare = "\n".join(plan_comparison_lines())
    text = (
        "📦 <b>خطط النسخ الخاصة — Basic مقابل Pro</b>\n\n"
        f"{compare}\n"
        "• إدارة الأكواد والنسخ الأخرى: 🆓 ❌ — 👑 ❌ (للمالك الرئيسي فقط)\n\n"
        "━━━━━━━━━━━━━━\n"
    )
    if ctx.get("is_clone"):
        plan = normalize_plan(ctx.get("plan"))
        badge = "👑 Pro" if plan == PLAN_PRO else "🆓 Basic"
        text += f"🤖 <b>هذه النسخة تعمل حاليًا بخطة:</b> {badge}\n"
        if plan == PLAN_BASIC:
            text += (
                "كل الحدود أعلاه تُطبَّق تلقائيًا داخل هذه النسخة.\n"
                "الترقية إلى 👑 Pro تتم بقرار المالك — اطلب التجديد من صفحة 🎁."
            )
        else:
            text += "لوحة الإدارة الخاصة متاحة لمالك النسخة وإدارييها (/admin)."
    else:
        text += (
            "🤖 <b>أنت الآن في البوت الأساسي — بلا حدود خطط.</b>\n"
            "الخطط أعلاه تخص النسخ الخاصة التي يكسبها المستخدمون من 🎁 الإحالات."
        )
    return text


def instance_for_bot_id(bot_id) -> dict | None:
    """The ``bot_instances`` row behind a running clone bot, resolved by the
    numeric bot id PTB derives from the token. Briefly cached."""
    if not bot_id:
        return None
    bot_id = int(bot_id)
    now = time.monotonic()
    hit = _instance_cache.get(bot_id)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    row = None
    try:
        row = pg.fetchone(
            "SELECT id, owner_id, bot_id, bot_username, status, plan, "
            "activated_at, expires_at, last_error, created_at "
            "FROM bot_instances WHERE bot_id = ?", (bot_id,))
    except Exception:
        logger.exception("instance lookup failed bot_id=%s", bot_id)
    _instance_cache[bot_id] = (now, row)
    return row


def invalidate_instance_cache(bot_id=None) -> None:
    if bot_id is None:
        _instance_cache.clear()
    else:
        _instance_cache.pop(int(bot_id), None)


def effective_plan(inst: dict | None) -> str:
    """Plan a clone row entitles RIGHT NOW — fail closed.

    Garbage/missing plan -> Basic. An expired validity window (or an
    ``expired`` status) downgrades Pro -> Basic IMMEDIATELY, without waiting
    for the 15-min expiry worker to stop the bot. Pro with no ``expires_at``
    stays Pro (open-ended grant). Any infra error resolving the window also
    downgrades to Basic — the stricter plan is always the safe default.
    """
    plan = normalize_plan((inst or {}).get("plan"))
    if plan != PLAN_PRO:
        return PLAN_BASIC
    try:
        from services.referral import is_instance_expired
        if is_instance_expired(inst):
            return PLAN_BASIC
        if str((inst or {}).get("status") or "").strip().lower() == "expired":
            return PLAN_BASIC
    except Exception:
        return PLAN_BASIC
    return PLAN_PRO


def plan_context(bot) -> dict:
    """Enforcement context for the bot serving the current update.

    Main bot -> ``plan="main"`` (no plan caps). Clone -> its stored plan;
    a clone whose row is missing resolves to Basic (fail closed).
    """
    from services.instance_manager import is_clone_bot
    username = getattr(bot, "username", None) or ""
    if not is_clone_bot(username):
        return {"is_clone": False, "instance_id": 0, "plan": PLAN_MAIN,
                "limits": None, "instance": None}
    inst = instance_for_bot_id(getattr(bot, "id", None))
    if not inst:
        return {"is_clone": True, "instance_id": 0, "plan": PLAN_BASIC,
                "limits": limits(PLAN_BASIC), "instance": None}
    plan = effective_plan(inst)
    return {"is_clone": True, "instance_id": int(inst["id"]), "plan": plan,
            "limits": limits(plan), "instance": inst}


# --------------------------------------------------------------------------
# Download enforcement (DB-backed counters — survive restarts)
# --------------------------------------------------------------------------

def batch_cap(ctx: dict) -> int:
    """Max URLs accepted from ONE message for this bot's plan."""
    from config.settings import BATCH_MAX_URLS
    if not ctx.get("is_clone"):
        return int(BATCH_MAX_URLS)
    return min(int(BATCH_MAX_URLS), int(ctx["limits"]["batch_urls"]))


def daily_cap(ctx: dict):
    """Daily download cap for this bot's plan. None = no plan cap (main)."""
    if not ctx.get("is_clone"):
        return None
    return int(ctx["limits"]["daily_downloads"])


def count_downloads_today(instance_id: int, user_id: int) -> int:
    """DB-backed daily counter — a restart can never reset it."""
    try:
        with db_cursor() as c:
            c.execute(
                "SELECT COUNT(*) AS cnt FROM downloads "
                "WHERE instance_id = ? AND user_id = ? AND date(created_at) = date('now')",
                (int(instance_id), int(user_id)))
            row = c.fetchone()
            return int((row or {}).get("cnt") or 0)
    except Exception:
        logger.exception("daily count failed inst=%s uid=%s", instance_id, user_id)
        return 0


def daily_limit_reached(instance_id: int, user_id: int, plan) -> bool:
    return count_downloads_today(instance_id, user_id) >= int(limits(plan)["daily_downloads"])


def audio_cap(plan):
    """MP3 bitrate ceiling (kbps) for a plan. None = no cap (main bot / Pro)."""
    if not plan:
        return None
    row = PLAN_LIMITS.get(normalize_plan(plan))
    if row is None:
        return None
    cap = row.get("audio_cap_kbps")
    return int(cap) if cap else None


def clamp_audio_bitrate(value, plan):
    """Clamp an MP3 bitrate to the plan ceiling — server-side, on every press.

    A preference saved anywhere (even 320kbps on the main bot) can never lift
    a Basic clone above its cap, and the explicit "MP3 320" button is clamped
    too. Non-numeric/None values pass through untouched.
    """
    try:
        br = int(value)
    except (TypeError, ValueError):
        return value
    cap = audio_cap(plan)
    if cap is not None:
        br = min(br, cap)
    return str(br)


def count_downloads_range(instance_id: int, days: int) -> int:
    try:
        with db_cursor() as c:
            c.execute(
                "SELECT COUNT(*) AS cnt FROM downloads "
                "WHERE instance_id = ? AND created_at >= datetime('now', ?)",
                (int(instance_id), f"-{int(days)} days"))
            row = c.fetchone()
            return int((row or {}).get("cnt") or 0)
    except Exception:
        logger.exception("range count failed inst=%s", instance_id)
        return 0


def count_downloads_for_user(instance_id: int, user_id: int) -> int:
    try:
        with db_cursor() as c:
            c.execute(
                "SELECT COUNT(*) AS cnt FROM downloads WHERE instance_id = ? AND user_id = ?",
                (int(instance_id), int(user_id)))
            row = c.fetchone()
            return int((row or {}).get("cnt") or 0)
    except Exception:
        return 0


def total_bytes_for(instance_id: int) -> int:
    try:
        with db_cursor() as c:
            c.execute(
                "SELECT COALESCE(SUM(file_size), 0) AS total FROM downloads WHERE instance_id = ?",
                (int(instance_id),))
            row = c.fetchone()
            return int((row or {}).get("total") or 0)
    except Exception:
        return 0


def top_platforms_for(instance_id: int, limit: int = 5) -> list[dict]:
    try:
        with db_cursor() as c:
            c.execute(
                "SELECT platform, COUNT(*) AS cnt FROM downloads "
                "WHERE instance_id = ? GROUP BY platform "
                "ORDER BY cnt DESC LIMIT ?", (int(instance_id), int(limit)))
            return [dict(r) for r in c.fetchall()]
    except Exception:
        return []


def purge_history_for_plan(instance_id: int, plan) -> int:
    """Delete this instance's download rows older than the plan's retention."""
    days = int(limits(plan)["history_days"])
    try:
        with db_cursor() as c:
            c.execute(
                "DELETE FROM downloads WHERE instance_id = ? AND created_at < datetime('now', ?)",
                (int(instance_id), f"-{days} days"))
            return int(c.rowcount or 0)
    except Exception:
        logger.exception("history purge failed inst=%s", instance_id)
        return 0


def purge_all_clone_history() -> int:
    """Retention sweep for EVERY clone with recorded downloads."""
    total = 0
    try:
        with db_cursor() as c:
            c.execute("SELECT DISTINCT instance_id FROM downloads WHERE instance_id > 0")
            ids = [int(r["instance_id"]) for r in c.fetchall()]
    except Exception:
        logger.exception("history sweep listing failed")
        return 0
    for inst_id in ids:
        inst = None
        try:
            inst = pg.fetchone(
                "SELECT plan FROM bot_instances WHERE id = ?", (inst_id,))
        except Exception:
            inst = None
        total += purge_history_for_plan(inst_id, (inst or {}).get("plan"))
    return total


# --------------------------------------------------------------------------
# Quality enforcement
# --------------------------------------------------------------------------

def quality_height(label) -> int:
    try:
        return int(str(label).lower().replace("p", "").strip())
    except Exception:
        return 0


def clamp_quality(label: str, plan) -> str:
    """Clamp a quality label to the plan ceiling.

    Basic: 'best'/'highest', open-ended modes ('auto'/'size'/'') or anything
    above 720p become '720p' (the engine picks the best available format
    at-or-below that height) — an open-ended mode could otherwise resolve
    to 1080p+ and silently lift Basic above its ceiling. Pro/main: as-is.
    """
    if str(plan or "").strip().lower() == PLAN_MAIN:
        return label  # the main bot has no plan ceiling — never clamp it
    cap = limits(plan).get("max_video_height")
    if cap is None:
        return label
    low = str(label or "").strip().lower()
    if low in ("best", "highest", "max", "", "auto", "size"):
        return f"{cap}p"
    h = quality_height(low)
    if h and h > int(cap):
        return f"{cap}p"
    return label


def quality_option_allowed(option: str, plan) -> bool:
    """Can this plan SELECT this quality option at all (settings menu)?"""
    cap = limits(plan).get("max_video_height")
    if cap is None:
        return True
    low = str(option or "").strip().lower()
    if low in ("highest", "best", "max"):
        return False
    h = quality_height(low)
    return (not h) or h <= int(cap)


# --------------------------------------------------------------------------
# Clone bans
# --------------------------------------------------------------------------

def is_user_banned(instance_id: int, user_id: int) -> bool:
    if not instance_id:
        return False
    try:
        row = pg.fetchone(
            "SELECT 1 AS ok FROM clone_bans WHERE instance_id = ? AND user_id = ?",
            (int(instance_id), int(user_id)))
        return bool(row)
    except Exception:
        logger.exception("ban check failed inst=%s uid=%s", instance_id, user_id)
        return False


def ban_user(instance_id: int, user_id: int, banned_by: int, reason: str = "") -> bool:
    try:
        pg.execute(
            "INSERT INTO clone_bans (instance_id, user_id, banned_by, reason) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (instance_id, user_id) DO NOTHING",
            (int(instance_id), int(user_id), int(banned_by), str(reason or "")[:200]))
        return True
    except Exception:
        logger.exception("ban failed inst=%s uid=%s", instance_id, user_id)
        return False


def unban_user(instance_id: int, user_id: int) -> bool:
    try:
        removed = pg.execute(
            "DELETE FROM clone_bans WHERE instance_id = ? AND user_id = ?",
            (int(instance_id), int(user_id)))
        return int(removed or 0) > 0
    except Exception:
        logger.exception("unban failed inst=%s uid=%s", instance_id, user_id)
        return False


def get_ban(instance_id: int, user_id: int) -> dict | None:
    try:
        return pg.fetchone(
            "SELECT * FROM clone_bans WHERE instance_id = ? AND user_id = ?",
            (int(instance_id), int(user_id)))
    except Exception:
        return None


# --------------------------------------------------------------------------
# Clone admins (per-instance — NEVER the global ADMIN_IDS)
# --------------------------------------------------------------------------

def is_clone_owner(inst: dict, user_id: int) -> bool:
    try:
        return int((inst or {}).get("owner_id") or 0) == int(user_id)
    except Exception:
        return False


def get_clone_admin(instance_id: int, user_id: int) -> dict | None:
    try:
        return pg.fetchone(
            "SELECT * FROM clone_admins WHERE instance_id = ? AND user_id = ?",
            (int(instance_id), int(user_id)))
    except Exception:
        return None


def add_clone_admin(instance_id: int, user_id: int, added_by: int,
                    can_ban: bool = False, can_broadcast: bool = False,
                    can_stats: bool = True) -> bool:
    try:
        pg.execute(
            "INSERT INTO clone_admins (instance_id, user_id, can_ban, can_broadcast, can_stats, added_by) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (instance_id, user_id) DO NOTHING",
            (int(instance_id), int(user_id), bool(can_ban), bool(can_broadcast),
             bool(can_stats), int(added_by)))
        return True
    except Exception:
        logger.exception("add admin failed inst=%s uid=%s", instance_id, user_id)
        return False


def remove_clone_admin(instance_id: int, user_id: int) -> bool:
    try:
        removed = pg.execute(
            "DELETE FROM clone_admins WHERE instance_id = ? AND user_id = ?",
            (int(instance_id), int(user_id)))
        return int(removed or 0) > 0
    except Exception:
        return False


def set_admin_perm(instance_id: int, user_id: int, perm: str, value: bool) -> bool:
    if perm not in ADMIN_PERMS:
        return False
    try:
        pg.execute(
            f"UPDATE clone_admins SET {perm} = ? WHERE instance_id = ? AND user_id = ?",
            (bool(value), int(instance_id), int(user_id)))
        return True
    except Exception:
        logger.exception("perm toggle failed inst=%s uid=%s perm=%s", instance_id, user_id, perm)
        return False


def list_clone_admins(instance_id: int) -> list[dict]:
    try:
        return pg.fetchall(
            "SELECT * FROM clone_admins WHERE instance_id = ? ORDER BY created_at ASC",
            (int(instance_id),))
    except Exception:
        return []


def clone_access(inst: dict | None, user_id: int) -> str:
    """'owner' | 'admin' | 'none' — computed from the DB, NEVER from input."""
    if not inst:
        return "none"
    if is_clone_owner(inst, user_id):
        return "owner"
    if get_clone_admin(int(inst["id"]), user_id):
        return "admin"
    return "none"


def perm_allows(inst: dict | None, user_id: int, perm: str) -> bool:
    acc = clone_access(inst, user_id)
    if acc == "owner":
        return True
    if acc != "admin" or perm not in ADMIN_PERMS:
        return False
    row = get_clone_admin(int(inst["id"]), user_id)
    return bool((row or {}).get(perm))


# --------------------------------------------------------------------------
# Clone admin audit log + broadcast log
# --------------------------------------------------------------------------

def recent_renew_request(instance_id: int, hours: int = 6) -> bool:
    """True when a renewal request was logged within the last N hours.

    DB-backed cooldown for ca:renew — an admin can't spam the system owner
    with renewal notifications by pressing the button repeatedly. Reads the
    PERMANENT clone_admin_log, so it survives restarts (an in-memory timer
    would reset on every deploy and reopen the spam window).
    """
    try:
        with db_cursor() as c:
            # clone_admin_log.created_at is a real TIMESTAMP column (unlike
            # downloads.created_at which is TEXT) — compare timestamps
            # directly instead of the portable datetime('now', ?) shorthand,
            # which translates to a to_char() TEXT and would raise
            # "operator does not exist: timestamp >= text" on PG.
            c.execute(
                "SELECT COUNT(*) AS cnt FROM clone_admin_log "
                "WHERE instance_id = ? AND action = 'renew_requested' "
                "AND created_at >= (now() at time zone 'utc' + (?)::interval)",
                (int(instance_id), f"-{int(hours)} hours"))
            row = c.fetchone()
            return int((row or {}).get("cnt") or 0) > 0
    except Exception:
        logger.exception("renew cooldown check failed inst=%s", instance_id)
        return False


def log_admin_action(instance_id: int, actor_id: int, action: str, detail: str = "") -> None:
    try:
        pg.execute(
            "INSERT INTO clone_admin_log (instance_id, actor_id, action, detail) "
            "VALUES (?, ?, ?, ?)",
            (int(instance_id), int(actor_id), str(action)[:60], str(detail or "")[:300]))
    except Exception:
        logger.exception("clone admin log failed inst=%s", instance_id)


def list_admin_log(instance_id: int, limit: int = 15) -> list[dict]:
    try:
        return pg.fetchall(
            "SELECT * FROM clone_admin_log WHERE instance_id = ? ORDER BY id DESC LIMIT ?",
            (int(instance_id), int(limit)))
    except Exception:
        return []


def admin_log_csv(instance_id: int, limit: int = 500) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at", "actor_id", "action", "detail"])
    for row in list_admin_log(instance_id, limit):
        w.writerow([row.get("id"), row.get("created_at"), row.get("actor_id"),
                    row.get("action"), row.get("detail")])
    return buf.getvalue()


def log_broadcast(instance_id: int, actor_id: int, recipients: int, sent: int, failed: int) -> None:
    try:
        pg.execute(
            "INSERT INTO clone_broadcasts (instance_id, actor_id, recipients, sent, failed) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(instance_id), int(actor_id), int(recipients), int(sent), int(failed)))
    except Exception:
        logger.exception("broadcast log failed inst=%s", instance_id)


def last_broadcasts(instance_id: int, limit: int = 3) -> list[dict]:
    try:
        return pg.fetchall(
            "SELECT * FROM clone_broadcasts WHERE instance_id = ? ORDER BY id DESC LIMIT ?",
            (int(instance_id), int(limit)))
    except Exception:
        return []


# --------------------------------------------------------------------------
# Failure tracking (safe reasons only — never URLs or tokens)
# --------------------------------------------------------------------------

def log_download_failure(user_id: int, instance_id: int, platform: str, reason: str) -> None:
    try:
        pg.execute(
            "INSERT INTO download_failures (instance_id, user_id, platform, reason) "
            "VALUES (?, ?, ?, ?)",
            (int(instance_id or 0), int(user_id), str(platform or "")[:40],
             str(reason or "")[:160]))
    except Exception:
        logger.debug("failure log failed", exc_info=True)


def count_failures_today(instance_id: int) -> int:
    try:
        row = pg.fetchone(
            "SELECT COUNT(*) AS cnt FROM download_failures "
            "WHERE instance_id = ? AND created_at >= CURRENT_DATE", (int(instance_id),))
        return int((row or {}).get("cnt") or 0)
    except Exception:
        return 0


def count_failures_for_user(instance_id: int, user_id: int) -> int:
    try:
        row = pg.fetchone(
            "SELECT COUNT(*) AS cnt FROM download_failures "
            "WHERE instance_id = ? AND user_id = ?", (int(instance_id), int(user_id)))
        return int((row or {}).get("cnt") or 0)
    except Exception:
        return 0


def failure_stats(instance_id: int, days: int = 30) -> dict:
    try:
        total = pg.fetchone(
            "SELECT COUNT(*) AS cnt FROM download_failures "
            "WHERE instance_id = ? AND created_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 day')",
            (int(instance_id), int(days)))
        top = pg.fetchall(
            "SELECT reason, COUNT(*) AS cnt FROM download_failures "
            "WHERE instance_id = ? AND created_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 day') "
            "GROUP BY reason ORDER BY cnt DESC LIMIT 5",
            (int(instance_id), int(days)))
        return {"total": int((total or {}).get("cnt") or 0), "top_reasons": top or []}
    except Exception:
        return {"total": 0, "top_reasons": []}


def recent_failures(instance_id: int, limit: int = 3) -> list[dict]:
    try:
        return pg.fetchall(
            "SELECT platform, reason, created_at FROM download_failures "
            "WHERE instance_id = ? ORDER BY id DESC LIMIT ?",
            (int(instance_id), int(limit)))
    except Exception:
        return []


# --------------------------------------------------------------------------
# Clone settings (welcome message, defaults…)
# --------------------------------------------------------------------------

def get_clone_setting(instance_id: int, key: str, default: str = "") -> str:
    try:
        row = pg.fetchone(
            "SELECT value FROM clone_settings WHERE instance_id = ? AND key = ?",
            (int(instance_id), str(key)))
        return str((row or {}).get("value") if row else default)
    except Exception:
        return default


def set_clone_setting(instance_id: int, key: str, value: str) -> None:
    pg.execute(
        "INSERT INTO clone_settings (instance_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT (instance_id, key) DO UPDATE SET value = EXCLUDED.value",
        (int(instance_id), str(key)[:60], str(value or "")[:2000]))


# --------------------------------------------------------------------------
# Clone user directory (ref_users registered with source='clone:<bot_id>')
# --------------------------------------------------------------------------

def _clone_source(inst: dict) -> str:
    return f"clone:{int((inst or {}).get('bot_id') or 0)}"


def clone_users_count(inst: dict) -> int:
    try:
        row = pg.fetchone(
            "SELECT COUNT(*) AS cnt FROM ref_users WHERE source = ?", (_clone_source(inst),))
        return int((row or {}).get("cnt") or 0)
    except Exception:
        return 0


def clone_active_users_count(inst: dict, days: int = 7) -> int:
    try:
        row = pg.fetchone(
            "SELECT COUNT(*) AS cnt FROM ref_users WHERE source = ? "
            "AND last_active_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 day')",
            (_clone_source(inst), int(days)))
        return int((row or {}).get("cnt") or 0)
    except Exception:
        return 0


def list_clone_users(inst: dict, limit: int = 8, offset: int = 0) -> list[dict]:
    try:
        return pg.fetchall(
            "SELECT telegram_id, username, first_name, registered_at, last_active_at "
            "FROM ref_users WHERE source = ? ORDER BY registered_at DESC LIMIT ? OFFSET ?",
            (_clone_source(inst), int(limit), int(offset)))
    except Exception:
        return []


def search_clone_user(inst: dict, query: str) -> list[dict]:
    q = str(query or "").strip()
    if not q:
        return []
    try:
        if q.isdigit():
            return pg.fetchall(
                "SELECT telegram_id, username, first_name, registered_at, last_active_at "
                "FROM ref_users WHERE source = ? AND telegram_id = ?",
                (_clone_source(inst), int(q)))
        like = f"%{q[:64]}%"
        return pg.fetchall(
            "SELECT telegram_id, username, first_name, registered_at, last_active_at "
            "FROM ref_users WHERE source = ? AND (username ILIKE ? OR first_name ILIKE ?) "
            "ORDER BY registered_at DESC LIMIT 10",
            (_clone_source(inst), like, like))
    except Exception:
        return []


def clone_user_detail(inst: dict, user_id: int) -> dict:
    inst_id = int((inst or {}).get("id") or 0)
    user = None
    try:
        user = pg.fetchone(
            "SELECT telegram_id, username, first_name, registered_at, last_active_at "
            "FROM ref_users WHERE source = ? AND telegram_id = ?",
            (_clone_source(inst), int(user_id)))
    except Exception:
        user = None
    return {
        "user": user,
        "downloads": count_downloads_for_user(inst_id, user_id),
        "failures": count_failures_for_user(inst_id, user_id),
        "banned": is_user_banned(inst_id, user_id),
    }


def clone_today_summary(inst: dict) -> dict:
    inst_id = int((inst or {}).get("id") or 0)
    return {
        "downloads_today": count_downloads_range(inst_id, 0) if False else _today_count(inst_id),
        "failures_today": count_failures_today(inst_id),
    }


def _today_count(instance_id: int) -> int:
    try:
        with db_cursor() as c:
            c.execute(
                "SELECT COUNT(*) AS cnt FROM downloads "
                "WHERE instance_id = ? AND date(created_at) = date('now')",
                (int(instance_id),))
            row = c.fetchone()
            return int((row or {}).get("cnt") or 0)
    except Exception:
        return 0


def clone_download_stats(inst: dict) -> dict:
    inst_id = int((inst or {}).get("id") or 0)
    fails = failure_stats(inst_id, 30)
    return {
        "today": _today_count(inst_id),
        "week": count_downloads_range(inst_id, 7),
        "month": count_downloads_range(inst_id, 30),
        "total_bytes": total_bytes_for(inst_id),
        "top_platforms": top_platforms_for(inst_id),
        "failures_30d": fails.get("total", 0),
        "top_failure_reasons": fails.get("top_reasons", []),
        "active_users": clone_active_users_count(inst, 7),
        "users": clone_users_count(inst),
    }
