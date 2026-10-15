"""Clone plans & validity tests (SQLite fallback path).

Covers the full plan lifecycle exactly as production would:
  plan-day mapping -> Pro threshold -> plan assignment at activation
  -> expiry computation -> due-expiry listing -> expiry marking -> stage
  machine -> owner renewal (the ONLY path back online) -> plan switch
  -> double-reward prevention under the plan system.

Runnable with plain `python3 tests/test_clone_plans.py` — dotenv and
cryptography are stubbed, so no extra dependencies are required anywhere.
"""
import base64
import os
import sys
import types
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, "temp", "test_clone_plans.db")

os.environ["DATABASE_PATH"] = DB
os.environ.pop("NEON_DATABASE_URL", None)
os.environ.pop("POSTGRES_URL", None)
os.environ.pop("DATABASE_URL", None)
os.environ["REFERRAL_TARGET"] = "2"                 # basic target = 2 qualified
os.environ["CLONE_PRO_TARGET_MULTIPLIER"] = "1.5"   # pro target  = 3 qualified
os.environ["CLONE_PLAN_BASIC_DAYS"] = "30"
os.environ["CLONE_PLAN_PRO_DAYS"] = "40"
os.makedirs(os.path.dirname(DB), exist_ok=True)
if os.path.exists(DB):
    os.remove(DB)

# --- stub dotenv if the real package is unavailable ---
try:
    import dotenv  # noqa: F401
except ImportError:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = dotenv

# --- stub cryptography.fernet with reversible b64 (flow test only) ---
try:
    from cryptography.fernet import Fernet as _RealFernet  # noqa: F401
except ImportError:
    cry = types.ModuleType("cryptography")
    fernet = types.ModuleType("cryptography.fernet")

    class Fernet:
        def __init__(self, key):
            self.key = key

        def encrypt(self, data):
            return b"gAAA::" + base64.urlsafe_b64encode(data)

        def decrypt(self, data):
            return base64.urlsafe_b64decode(data.replace(b"gAAA::", b"", 1))

    fernet.Fernet = Fernet
    cry.fernet = fernet
    sys.modules["cryptography"] = cry
    sys.modules["cryptography.fernet"] = fernet

sys.path.insert(0, REPO)

from database import pg  # noqa: E402
from services import referral  # noqa: E402

pg.init_referral_schema()

failures = []


def check(name: str, cond: bool) -> None:
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        failures.append(name)


# 1) plan day mapping + pro threshold --------------------------------------
check("basic plan = 30 days", referral.clone_plan_days("basic") == 30)
check("pro plan = 40 days", referral.clone_plan_days("pro") == 40)
check("unknown plan falls back to basic", referral.clone_plan_days(None) == 30)
check("pro target = target*1.5 = 3", referral.pro_target() == 3)

# 2) BASIC activation (2 qualified < pro target 3) --------------------------
UID = 555001
referral._ensure_reward_row(UID)
pg.execute("UPDATE referral_rewards SET status='eligible' WHERE user_id=?", (UID,))
for i in (1, 2):
    pg.execute(
        "INSERT INTO referrals (referrer_id, referred_id, status) VALUES (?,?, 'qualified')",
        (UID, 900000 + i),
    )
ok, reason = referral.claim_reward(UID)
check("claim moves eligible -> awaiting_token", ok and reason == "awaiting_token")

referral._tg_get_me = lambda token: {"id": 111222333, "username": "basic_clone_bot"}
res = referral.verify_and_link_token(UID, "123456:" + "A" * 35)
check("activation ok", res.get("ok") is True)
check("plan assigned = basic", res.get("plan") == "basic")
inst_id = res["instance_id"]
inst = referral.get_instance(inst_id)
check("expires_at stored at activation", inst.get("expires_at") is not None)
left = referral.instance_days_left(inst)
check("basic days_left ~30", left is not None and 29 <= left <= 30)
check("not expired yet", referral.is_instance_expired(inst) is False)
check("stage = active", referral.referrer_stage(referral.get_stats(UID)) == "active")

# 3) expiry enforcement (data path used by the worker) ----------------------
past = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
pg.execute("UPDATE bot_instances SET expires_at=? WHERE id=?", (past, inst_id))
inst = referral.get_instance(inst_id)
check("expired window detected", referral.is_instance_expired(inst) is True)
due = referral.list_expired_due_instances()
check("due list contains the instance", any(int(r["id"]) == inst_id for r in due))
referral.mark_instance_expired(inst_id)
inst = referral.get_instance(inst_id)
check("status = expired after marking", inst.get("status") == "expired")
check("stage = expired", referral.referrer_stage(referral.get_stats(UID)) == "expired")
check("expired instance no longer in due list",
      all(int(r["id"]) != inst_id for r in referral.list_expired_due_instances()))
log = referral.get_user_activity_log(UID, 20)
check("audit: instance_expired logged", any(r.get("action") == "instance_expired" for r in log))

# 4) owner renewal = the ONLY path back online ------------------------------
referral.extend_instance(inst_id)
inst = referral.get_instance(inst_id)
check("renewal -> active", inst.get("status") == "active")
check("renewal days_left ~30", 29 <= (referral.instance_days_left(inst) or 0) <= 30)
check("audit: instance_renewed logged",
      any(r.get("action") == "instance_renewed" for r in referral.get_user_activity_log(UID, 20)))

# 5) plan switch recomputes the window --------------------------------------
referral.set_instance_plan(inst_id, "pro")
inst = referral.get_instance(inst_id)
check("plan switched to pro", inst.get("plan") == "pro")
check("pro days_left ~40 after switch", 39 <= (referral.instance_days_left(inst) or 0) <= 40)

# 6) PRO earned at activation (3 qualified >= pro target) --------------------
UID2 = 555002
referral._ensure_reward_row(UID2)
pg.execute("UPDATE referral_rewards SET status='eligible' WHERE user_id=?", (UID2,))
for i in (1, 2, 3):
    pg.execute(
        "INSERT INTO referrals (referrer_id, referred_id, status) VALUES (?,?, 'qualified')",
        (UID2, 910000 + i),
    )
ok, _ = referral.claim_reward(UID2)
check("second user claim ok", ok is True)
referral._tg_get_me = lambda token: {"id": 444555666, "username": "pro_clone_bot"}
res2 = referral.verify_and_link_token(UID2, "654321:" + "B" * 35)
check("pro activation ok", res2.get("ok") is True)
check("plan assigned = pro", res2.get("plan") == "pro")
inst2 = referral.get_instance(res2["instance_id"])
check("pro days_left ~40", 39 <= (referral.instance_days_left(inst2) or 0) <= 40)

# 7) double-reward prevention still holds under the plan system --------------
ok2, reason2 = referral.claim_reward(UID)
check("no duplicate reward", ok2 is True and reason2 in ("claimed", "awaiting_token"))
cnt = pg.fetchone("SELECT COUNT(*) AS c FROM bot_instances WHERE owner_id=?", (UID,))["c"]
check("exactly one instance per owner", int(cnt) == 1)

# 8) user-facing plans page renders the full system ---------------------------
# The 📦 page must expose BOTH plans, both targets, both validity windows,
# the expiry/renewal policy, and the user's own standing — for every state.
from handlers.referral import _plans_text  # noqa: E402

# a) user WITHOUT an instance: shows both plans + remaining counts
stats_no_inst = {"target": 2, "qualified": 1, "instance": None}
txt = _plans_text(stats_no_inst)
check("plans page shows Basic plan", "🆓" in txt and "Basic" in txt)
check("plans page shows Pro plan", "👑" in txt and "Pro" in txt)
check("plans page shows basic target", "<b>2 مستخدمًا مؤهلًا</b>" in txt)
check("plans page shows pro target (3)", "<b>3 مستخدمًا مؤهلًا</b>" in txt)
check("plans page shows basic window (30)", "30 يومًا" in txt)
check("plans page shows pro window (40)", "40 يومًا" in txt)
check("plans page explains expiry policy", "بتتوقف تلقائيًا" in txt)
check("plans page explains renewal via owner ticket", "طلب تجديد مدة نسختي" in txt)
check("plans page shows remaining-to-basic (1)", "باقي <b>1</b> مستخدمًا مؤهلًا" in txt)

# b) ACTIVE instance owner: shows current plan badge + expiry + days_left
active_inst = {
    "id": 1, "bot_username": "basic_clone_bot", "status": "active",
    "plan": "basic", "expires_at": "2030-01-15 00:00:00", "days_left": 30,
}
txt_active = _plans_text({"target": 2, "qualified": 2, "instance": active_inst})
check("active owner sees current plan", "خطتك الحالية: <b>🆓 Basic</b>" in txt_active)
check("active owner sees expiry date", "2030-01-15" in txt_active)
check("active owner sees days left", "باقي <b>30</b> يومًا" in txt_active)

# c) EXPIRED instance owner: shows expired state + renewal CTA
expired_inst = dict(active_inst, status="expired", plan="pro")
txt_exp = _plans_text({"target": 2, "qualified": 3, "instance": expired_inst})
check("expired owner sees expired state", "منتهية" in txt_exp)
check("expired owner sees renewal CTA", "طلب تجديد مدة نسختي" in txt_exp)

# d) callback wiring: the 📦 button exists in every user's home menu
from telegram import InlineKeyboardMarkup  # noqa: E402
from handlers.referral import _rows_for  # noqa: E402

for stage_stats in (
    {"target": 2, "qualified": 0, "percent": 0, "pending": 0, "suspicious": 0,
     "reward_status": "progress", "instance": None, "link": "https://x"},
    {"target": 2, "qualified": 2, "percent": 100, "pending": 0, "suspicious": 0,
     "reward_status": "eligible", "instance": None, "link": "https://x"},
    {"target": 2, "qualified": 2, "percent": 100, "pending": 0, "suspicious": 0,
     "reward_status": "claimed", "instance": active_inst, "link": "https://x"},
):
    rows = _rows_for(stage_stats, "main_bot")
    datas = {b.callback_data for row in rows for b in row}
    check(f"plans button visible (stage={stage_stats['reward_status']})", "ref:plans" in datas)

# e) rules text mentions both plans so no screen contradicts the system
from handlers.referral import _RULES_TEXT  # noqa: E402
check("rules mention Basic", "Basic" in _RULES_TEXT)
check("rules mention Pro", "Pro" in _RULES_TEXT)
check("rules point to plans page", "الخطط والباقات" in _RULES_TEXT)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL CLONE PLAN TESTS PASSED")
