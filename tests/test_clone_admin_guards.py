"""Guard-retention tests: HIDING BUTTONS IS NOT PROTECTION.

Every ca:* action in the clone admin panel must re-verify — server-side,
on EVERY callback press and EVERY text capture:

  1. identity      (clone_access computed from the DB: owner/admin/none),
  2. instance id   (resolved from the RUNNING bot, never from input),
  3. plan          (Pro required for the whole panel, fail-closed),
  4. permission    (can_ban / can_broadcast / can_stats, owner-only for
                    admin management, per-action re-checks everywhere).

These tests scan the dispatcher SOURCE so a future refactor cannot drop a
check without failing the suite. They complement test_plan_policy.py,
which unit-tests the permission primitives themselves (clone_access /
perm_allows / effective_plan) against a live test database.
"""
from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_src = (_ROOT / "handlers" / "clone_admin.py").read_text(encoding="utf-8")
_adm = (_ROOT / "handlers" / "admin.py").read_text(encoding="utf-8")
_own = (_ROOT / "handlers" / "owner_panel.py").read_text(encoding="utf-8")

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        _failures.append(label)


def branch(marker: str) -> str:
    """Dispatcher source from `marker` up to the next elif/else branch."""
    i = _src.find(marker)
    if i < 0:
        return ""
    j = _src.find("elif data", i + len(marker))
    k = _src.find("else:", i + len(marker))
    end = min(x for x in (j, k, len(_src)) if x > 0)
    return _src[i:end]


def fn_body(name: str, next_name: str) -> str:
    i = _src.find(name)
    j = _src.find(next_name, i + 1) if i >= 0 else -1
    return _src[i:j] if i >= 0 else ""


print("== entry gate: identity + instance + plan BEFORE any action ==")
gate_idx = _src.find("gated = _gate(context")
plan_idx = _src.find('if ctx["plan"] != pp.PLAN_PRO:')
disp_idx = _src.find('parts = data.split(":")')
check("entry: _gate runs before dispatch", 0 < gate_idx < disp_idx)
check("entry: Pro plan check runs before dispatch", 0 < plan_idx < disp_idx)
check("_gate resolves the instance from the RUNNING bot",
      "pp.plan_context(context.bot)" in fn_body("def _gate(", "def _plan_blocked_text"))
check("_gate denies non-clones and missing rows (fail closed)",
      'if not ctx.get("is_clone")' in fn_body("def _gate(", "def _plan_blocked_text"))
check("identity computed from the DB, never from input",
      "pp.clone_access(inst, user_id)" in fn_body("def _gate(", "def _plan_blocked_text"))

print("== admin management: owner-only on callbacks AND text capture ==")
check("ca:admins owner-only", 'acc != "owner"' in branch('elif data == "ca:admins":'))
check("ca:admins:add owner-only",
      'acc != "owner"' in branch('elif data == "ca:admins:add":'))
check("ca:admin:* (remove/perms/toggle) owner-only",
      'acc != "owner"' in branch('elif data.startswith("ca:admin:")'))
check("perm toggle whitelisted to ADMIN_PERMS",
      "parts[4] in pp.ADMIN_PERMS" in branch('elif data.startswith("ca:admin:")'))
_cap = _src[_src.find("async def try_handle_capture"):]
_add = _cap[_cap.find('kind == "add_admin"'):]
check("add_admin text capture re-checks owner",
      'acc != "owner"' in _add[:_add.find("return True") + 400])

print("== broadcast: can_broadcast re-checked at new + confirm + capture ==")
check("broadcast:new can_broadcast",
      "can_broadcast" in branch('elif data == "ca:broadcast:new":'))
check("broadcast:confirm can_broadcast",
      "can_broadcast" in branch('elif data == "ca:broadcast:confirm":'))
check("broadcast:confirm requires a fresh draft (TTL)",
      "_drafts.pop" in branch('elif data == "ca:broadcast:confirm":'))
_bcap = _cap[_cap.find('kind == "broadcast"'):]
check("broadcast text capture re-checks can_broadcast",
      "can_broadcast" in _bcap[:_bcap.find("return True") + 400])

print("== users/ban/log/stats: per-action permission re-checks ==")
check("ban/unban ACTION re-checks can_ban",
      "can_ban" in branch('elif data.startswith("ca:user:")'))
_branch_user = branch('elif data.startswith("ca:user:")')
check("ban ACTION consults ban_guard BEFORE applying (owner/admins protected)",
      "pp.ban_guard(inst_id, uid, user.id)" in _branch_user)
check("users browse gated (can_stats|can_ban)",
      "_allows_users" in branch('elif data.startswith("ca:users:")'))
check("user detail gated",
      "_allows_users" in branch('elif data.startswith("ca:user:")'))
check("user search gated",
      "_allows_users" in branch('elif data.startswith("ca:users:search")'))
_ucap = _cap[_cap.find('kind == "user_search"'):]
check("user_search capture gated",
      "_allows_users" in _ucap[:_ucap.find("return True") + 200])
check("stats render checks can_stats",
      "can_stats" in fn_body("async def _render_stats", "async def _render_settings"))
check("audit log gated by can_stats",
      "can_stats" in branch('elif data == "ca:log":'))
check("audit log CSV gated by can_stats",
      "can_stats" in branch('elif data == "ca:log:csv":'))
check("broadcast render checks can_broadcast",
      "can_broadcast" in fn_body("async def _render_broadcast", "async def _render_admins"))

print("== plan ceiling + renew cooldown ==")
check("clone default quality plan-gated",
      "quality_option_allowed" in branch('elif data.startswith("ca:settings:quality:")'))
check("renew request DB-backed cooldown (no owner spam)",
      "recent_renew_request" in branch('elif data == "ca:renew":'))

print("== text capture re-gates EVERY message ==")
_head = _cap[:_cap.find("kind = pend.get")]
check("capture re-runs _gate (identity+instance)", "_gate(context" in _head)
check("capture re-checks Pro plan", 'ctx["plan"] != pp.PLAN_PRO' in _head)
check("capture state keyed by (bot.id, chat.id) — per-instance isolation",
      "key = (context.bot.id, msg.chat_id)" in _cap)
check("capture TTL expiry", "_CAPTURE_TTL" in _head)

print("== sibling panels keep their own gates ==")
check("admin.py rejects clone bots (ca:* vs admin_* isolation)",
      "is_clone_bot(context.bot.username" in _adm)
check("admin.py re-checks is_admin on EVERY callback",
      "if not is_admin(user.id):" in _adm)
check("admin.py owner-only callbacks re-check is_owner",
      "if data in owner_only_callbacks and not is_owner(user.id):" in _adm)
check("owner panel gates EVERY callback through _owner_gate",
      "_owner_gate(update)" in _own)


def test_clone_admin_guards():
    assert not _failures, "FAILED GUARD CHECKS:\n" + "\n".join(_failures)


if __name__ == "__main__":
    print()
    if _failures:
        print("FAILED GUARD CHECKS:")
        for f in _failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CLONE-ADMIN GUARD CHECKS PASSED ✅")
