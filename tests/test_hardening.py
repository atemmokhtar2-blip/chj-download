"""🔒 Hardening suite — edge cases nobody should be able to exploit.

Covers the gaps a dedicated security pass found:

  * clone-ban privilege abuse: an admin with ``can_ban`` must NOT be able to
    ban the instance owner, another clone admin, or themselves — bans are
    enforced at /start and download time, so banning the owner would lock
    them out of their own clone;
  * forged validity windows: a negative/huge ``days`` at activation can no
    longer produce a clone that is already expired or a near-permanent one;
  * oversized token / redemption-code inputs never reach getMe or the code
    index;
  * the in-memory rate limiter evicts idle users instead of growing without
    bound on a long-running bot.
"""
from __future__ import annotations

import pytest

from database import pg
from services import plan_policy as pp
from services import referral
from services import redemption


# ---------------------------------------------------------------------------
# Ban target protection
# ---------------------------------------------------------------------------

def _prepare_schema() -> None:
    """Create the referral/clone tables (bot_instances, clone_admins, …).

    ``isolated_db`` only bootstraps the MAIN schema; the clone plan tables
    come from ``init_referral_schema`` (what bot.py runs at boot).
    """
    pg.init_referral_schema()


def _mk_instance(iid: int, owner_id: int, plan: str = "pro") -> None:
    _prepare_schema()
    pg.execute(
        "INSERT INTO bot_instances (id, owner_id, status, plan) "
        "VALUES (?, ?, 'active', ?)",
        (int(iid), int(owner_id), plan),
    )


def test_ban_guard_protects_owner(isolated_db) -> None:
    _mk_instance(1, 100)
    assert pp.ban_guard(1, 100, 200) == "owner"
    assert pp.ban_user(1, 100, 200, "spam") is False
    assert pp.is_user_banned(1, 100) is False


def test_ban_guard_protects_admin(isolated_db) -> None:
    _mk_instance(1, 100)
    pp.add_clone_admin(1, 200, added_by=100, can_ban=True)
    # Even the owner cannot "ban" an admin — removal is the removal flow.
    assert pp.ban_guard(1, 200, 100) == "admin"
    assert pp.ban_user(1, 200, 100) is False
    assert pp.is_user_banned(1, 200) is False


def test_ban_guard_blocks_self_ban(isolated_db) -> None:
    _mk_instance(1, 100)
    pp.add_clone_admin(1, 200, added_by=100, can_ban=True)
    assert pp.ban_guard(1, 200, 200) == "self"
    assert pp.ban_user(1, 200, 200) is False


def test_ban_guard_allows_regular_user(isolated_db) -> None:
    _mk_instance(1, 100)
    assert pp.ban_guard(1, 300, 200) == ""
    assert pp.ban_user(1, 300, 200, "abuse") is True
    assert pp.is_user_banned(1, 300) is True


def test_ban_guard_fails_closed_on_missing_instance(isolated_db) -> None:
    """No instance row -> refuse the ban outright (never insert orphans)."""
    _prepare_schema()
    assert pp.ban_guard(999, 300, 200) == "error"
    assert pp.ban_user(999, 300, 200) is False


# ---------------------------------------------------------------------------
# Validity-window bounds
# ---------------------------------------------------------------------------

def test_bounded_plan_days_defaults_match_plan() -> None:
    assert referral.bounded_plan_days(None, "basic") == referral.clone_plan_days("basic")
    assert referral.bounded_plan_days(0, "pro") == referral.clone_plan_days("pro")


def test_bounded_plan_days_clamps_out_of_range() -> None:
    assert referral.bounded_plan_days(-5, "basic") == 1
    assert referral.bounded_plan_days(999999, "pro") == 365
    assert referral.bounded_plan_days(365, "pro") == 365
    assert referral.bounded_plan_days(1, "basic") == 1


def test_bounded_plan_days_tolerates_garbage() -> None:
    assert referral.bounded_plan_days("garbage", "pro") == referral.clone_plan_days("pro")
    assert referral.bounded_plan_days(None, "garbage-plan") == referral.clone_plan_days("basic")
    assert referral.bounded_plan_days("7", "basic") == 7


# ---------------------------------------------------------------------------
# Input length caps (token + redemption code)
# ---------------------------------------------------------------------------

def test_looks_like_token_accepts_valid_and_short() -> None:
    assert referral.looks_like_token("12345:" + "a" * 30) is True
    assert referral.looks_like_token("short") is False
    assert referral.looks_like_token("") is False


def test_looks_like_token_rejects_oversized() -> None:
    assert referral.looks_like_token("12345:" + "a" * 250) is False


def test_redeem_code_rejects_oversized_input(monkeypatch) -> None:
    """An absurdly long code must be rejected as 'shape' BEFORE any DB lookup."""
    import services.redemption as rmodule

    def boom(*_a, **_k):
        raise AssertionError("find_code must not run for oversized input")

    monkeypatch.setattr(rmodule, "find_code", boom)
    assert redemption.redeem_code(123, "A" * 200) == {"ok": False, "reason": "shape"}
    assert redemption.redeem_code(123, "A" * 65) == {"ok": False, "reason": "shape"}


# ---------------------------------------------------------------------------
# In-memory rate limiter: bounded memory
# ---------------------------------------------------------------------------

def test_rate_limiter_evicts_stale_entries(monkeypatch) -> None:
    from middlewares import rate_limiter as rl

    monkeypatch.setattr(rl, "_MAX_TRACKED_USERS", 2)
    with rl._lock:
        rl._recent.clear()
        rl._downloads_hour.clear()
        rl._downloads_day.clear()
        now = 1_000_000.0
        for uid in (1, 2, 3):
            rl._recent[uid].append(now - 100_000)
            rl._downloads_hour[uid].append(now - 100_000)
            rl._downloads_day[uid].append(now - 100_000)
        evicted = rl._evict_stale_entries(now)
        assert evicted == 3
        assert len(rl._recent) == 0
        assert len(rl._downloads_hour) == 0
        assert len(rl._downloads_day) == 0


def test_rate_limiter_keeps_recent_entries(monkeypatch) -> None:
    from middlewares import rate_limiter as rl

    monkeypatch.setattr(rl, "_MAX_TRACKED_USERS", 2)
    with rl._lock:
        rl._recent.clear()
        rl._downloads_hour.clear()
        rl._downloads_day.clear()
        now = 2_000_000.0
        for uid in (1, 2, 3):
            rl._recent[uid].append(now - 1)  # fresh — must survive
            rl._downloads_hour[uid].append(now - 1)
            rl._downloads_day[uid].append(now - 1)
        evicted = rl._evict_stale_entries(now)
        assert evicted == 0
        assert len(rl._recent) == 3
