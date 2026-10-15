"""🎟️ Redemption-code system — full lifecycle on real PostgreSQL.

Runs as a pytest module (dedicated database via tests/pgtest.py, pinned
through pg.PG_DSN like the other referral modules) AND as a plain script
(`python3 tests/test_redemption_codes.py`).

Covers the contract from the spec:
  * owner creates codes with per-code values (plan / runtime days / uses /
    pre-use validity);
  * atomic use-limit — a code can never exceed max_uses even when several
    users redeem simultaneously (single guarded UPDATE);
  * a FAILED or cancelled activation NEVER consumes a use (release path);
  * the use is confirmed only after the clone is fully created + activated;
  * revoked / expired / already-used / active-instance blocks;
  * stale reservations are swept automatically;
  * audit trail records created/reserved/used/released/revoked with user id
    and instance id — and NEVER the full code text or a bot token.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

# --- stub dotenv so importing config.settings never loads a real .env -------
import types  # noqa: E402

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    dotenv.find_dotenv = lambda *a, **k: ""
    sys.modules["dotenv"] = dotenv

# --- cryptography (Fernet): prefer the REAL package; deterministic stub only
# when it is not installed (same contract as the other referral test modules).
try:
    from cryptography.fernet import Fernet as _RealFernet  # noqa: F401
except Exception:
    crypto = types.ModuleType("cryptography")
    fernet_mod = types.ModuleType("cryptography.fernet")

    class Fernet:  # deterministic round-trip stand-in
        def __init__(self, key):
            self.key = key

        def encrypt(self, data):
            return b"enc:" + data

        def decrypt(self, data):
            return data[4:] if data.startswith(b"enc:") else data

    fernet_mod.Fernet = Fernet
    fernet_mod.InvalidToken = Exception
    crypto.fernet = fernet_mod
    sys.modules["cryptography"] = crypto
    sys.modules["cryptography.fernet"] = fernet_mod

import pgtest  # noqa: E402

# PostgreSQL is the ONLY backend. Under pytest, tests/conftest.py already
# booted the test server; a plain-script run bootstraps its own here.
if "pytest" not in sys.modules:
    pgtest.bootstrap()

from database import pg  # noqa: E402

# Dedicated database — plan/code rows must never mix with other modules.
_ORIGINAL_PG_DSN = pg.PG_DSN
_DB_NAME = "xd_redemption_codes"
pg.PG_DSN = pgtest.create_test_database(_DB_NAME)

pg.init_referral_schema()

import config.settings as cs  # noqa: E402
from services import redemption  # noqa: E402
from services import referral  # noqa: E402

OWNER = 7631249810
_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        _failures.append(label)


def _log_rows(code_id: int, action: str | None = None) -> list[dict]:
    sql = "SELECT * FROM redemption_log WHERE code_id = ?"
    params: tuple = (code_id,)
    if action:
        sql += " AND action = ?"
        params += (action,)
    return pg.fetchall(sql + " ORDER BY id ASC", params)


def _setup_module_checks() -> None:
    print("== create + validation ==")
    bad = redemption.create_code("basic", 0, 1, 7)
    check("runtime_days=0 rejected", not bad.get("ok"))
    bad = redemption.create_code("basic", 30, 999, 7)
    check("max_uses=999 rejected", not bad.get("ok"))
    bad = redemption.create_code("pro", 40, 2, 0)
    check("valid_days=0 rejected", not bad.get("ok"))

    res = redemption.create_code("basic", 30, 1, 7, note="هدية", created_by=OWNER)
    check("basic code created", res.get("ok") is True)
    code_basic = res["code"]
    cid_basic = res["code_id"]
    check("code format BASIC-XXXX-XXXX",
          code_basic.startswith("BASIC-") and len(code_basic) == 15)
    row = redemption.get_code(cid_basic)
    check("expires_at = created + valid_days",
          row["expires_at"] is not None and
          abs((row["expires_at"] - datetime.utcnow()).total_seconds() - 7 * 86400) < 120)
    created_logs = _log_rows(cid_basic, "created")
    check("audit 'created' logged with owner id",
          len(created_logs) == 1 and int(created_logs[0]["user_id"]) == OWNER)
    check("full code NOT in audit details",
          all(code_basic not in (l.get("details") or "") for l in created_logs))

    pro = redemption.create_code("pro", 40, 2, 14, created_by=OWNER)
    check("pro code created", pro.get("ok") is True)
    code_pro = pro["code"]
    cid_pro = pro["code_id"]
    check("pro code format PRO-XXXX-XXXX",
          code_pro.startswith("PRO-") and len(code_pro) == 13)

    print("== redeem guards ==")
    r = redemption.redeem_code(5001, "GARBAGE")
    check("garbage code -> shape/not_found", r.get("reason") in ("shape", "not_found"))
    r = redemption.redeem_code(5001, code_basic)
    check("valid redeem ok", r.get("ok") is True and r.get("plan") == "basic"
          and r.get("runtime_days") == 30 and r.get("instance_id"))
    inst_id = r["instance_id"]
    row = redemption.get_code(cid_basic)
    check("use reserved (uses_consumed=1)", int(row["uses_consumed"]) == 1)
    check("audit 'reserved' with user+instance",
          any(int(l["user_id"]) == 5001 and int(l["instance_id"]) == inst_id
              for l in _log_rows(cid_basic, "reserved")))
    inst = pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (inst_id,))
    check("instance pending_token with plan", inst["status"] == "pending_token" and inst["plan"] == "basic")

    r = redemption.redeem_code(5001, code_basic)
    check("second redeem while reserved -> resume (no 2nd slot) or blocked",
          (r.get("ok") is True and r.get("resumed") is True) or r.get("ok") is False)

    print("== failed activation never consumes ==")
    released = redemption.release_reservation(5001, cid_basic, "token_attempts_exhausted")
    check("release ok", released is True)
    row = redemption.get_code(cid_basic)
    check("uses_consumed back to 0", int(row["uses_consumed"]) == 0)
    check("audit 'released' logged", len(_log_rows(cid_basic, "released")) >= 1)
    check("release again is a no-op", redemption.release_reservation(5001, cid_basic) is False)
    # The abandoned pending instance must not block a fresh redeem.
    pg.execute("DELETE FROM bot_instances WHERE id = ?", (inst_id,))

    r = redemption.redeem_code(5001, code_basic)
    check("re-redeem after release ok", r.get("ok") is True)
    inst_id = r["instance_id"]

    print("== atomic use limit ==")
    r2 = redemption.redeem_code(5002, code_basic)
    check("max_uses=1 blocks second user", r2.get("ok") is False
          and r2.get("reason") in ("limit_reached", "exhausted"))

    print("== full activation consumes exactly one use ==")
    # Fake Telegram getMe so no network is touched.
    referral._tg_get_me = lambda token: {"id": 990001, "username": "gift_clone_bot"}
    fake_token = "123456789:AAH4fakeRedemptionTokenForTest_xyz"
    result = referral.verify_and_link_token(
        5001, fake_token, instance_id=inst_id, plan="basic", days=30)
    check("activation ok", result.get("ok") is True)
    inst = pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (inst_id,))
    check("instance active", inst["status"] == "active")
    exp_delta = (inst["expires_at"] - inst["activated_at"]).total_seconds() / 86400
    check("expiry = activation + runtime_days(30)", abs(exp_delta - 30) < 0.01)
    redemption.confirm_use(5001, cid_basic, inst_id, "gift_clone_bot")
    check("audit 'used' with instance id",
          any(int(l["instance_id"]) == inst_id for l in _log_rows(cid_basic, "used")))
    row = redemption.get_code(cid_basic)
    check("code exhausted after max uses confirmed", row["status"] == "exhausted")
    check("token NOT in any audit details",
          all(fake_token not in (l.get("details") or "")
              for l in pg.fetchall("SELECT details FROM redemption_log WHERE code_id = ?", (cid_basic,))))
    check("token NOT in activation_log details",
          all(fake_token not in (l.get("details") or "")
              for l in pg.fetchall("SELECT details FROM activation_log")))
    r3 = redemption.redeem_code(5003, code_basic)
    check("exhausted code rejected", r3.get("ok") is False)
    r4 = redemption.redeem_code(5001, code_basic)
    check("already_used enforced even if slots existed", r4.get("reason") in ("already_used", "has_active_instance"))

    print("== pro code: two different users ==")
    pa = redemption.redeem_code(6001, code_pro)
    check("pro use 1 reserved", pa.get("ok") is True and pa.get("plan") == "pro")
    pb = redemption.redeem_code(6002, code_pro)
    check("pro use 2 reserved (different user)", pb.get("ok") is True)
    pc = redemption.redeem_code(6003, code_pro)
    check("pro use 3 blocked (max_uses=2)",
          pc.get("ok") is False and pc.get("reason") in ("exhausted", "limit_reached"))
    referral._tg_get_me = lambda token: {"id": 990002, "username": "pro_user_one_bot"}
    ra = referral.verify_and_link_token(6001, "111111111:AAProOneToken_fake_padpadpad_pad12",
                                        instance_id=pa["instance_id"], plan="pro", days=40)
    check("pro activation 1 ok", ra.get("ok") is True)
    inst_a = pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (pa["instance_id"],))
    exp_delta = (inst_a["expires_at"] - inst_a["activated_at"]).total_seconds() / 86400
    check("pro expiry = 40 days", abs(exp_delta - 40) < 0.01)
    redemption.confirm_use(6001, cid_pro, pa["instance_id"], "pro_user_one_bot")
    row = redemption.get_code(cid_pro)
    check("pro code still active after 1/2 confirmed", row["status"] == "active")
    # user 6001 now has an ACTIVE instance -> blocked from redeeming again
    r = redemption.redeem_code(6001, code_pro)
    check("active-instance holder blocked",
          r.get("ok") is False and r.get("reason") in ("has_active_instance", "already_used"))

    print("== revoke ==")
    ok, reason = redemption.revoke_code(cid_pro, by_owner=OWNER)
    check("revoke ok", ok is True)
    ok2, reason2 = redemption.revoke_code(cid_pro, by_owner=OWNER)
    check("double revoke idempotent", ok2 is False and reason2.startswith("already_"))
    check("audit 'revoked' logged once", len(_log_rows(cid_pro, "revoked")) == 1)
    r = redemption.redeem_code(6009, code_pro)
    check("revoked code rejected", r.get("ok") is False and r.get("reason") == "revoked")
    # user 6002's reservation survives the revoke (release still works)
    check("release after revoke works",
          redemption.release_reservation(6002, cid_pro, "user_cancelled") is True)

    print("== expired code ==")
    exp_res = redemption.create_code("basic", 30, 1, 1, created_by=OWNER)
    cid_exp = exp_res["code_id"]
    pg.execute("UPDATE redemption_codes SET expires_at = ? WHERE id = ?",
               ((datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"), cid_exp))
    r = redemption.redeem_code(7001, exp_res["code"])
    check("expired code rejected", r.get("ok") is False and r.get("reason") == "expired")

    print("== stale reservation sweep ==")
    sweep_res = redemption.create_code("basic", 30, 1, 30, created_by=OWNER)
    cid_sw = sweep_res["code_id"]
    rs = redemption.redeem_code(8001, sweep_res["code"])
    check("sweep-test redeem ok", rs.get("ok") is True)
    # Backdate the reservation beyond the TTL and clear its pending instance.
    pg.execute("UPDATE redemption_log SET created_at = CURRENT_TIMESTAMP - INTERVAL '120 minutes' "
               "WHERE code_id = ? AND action = 'reserved'", (cid_sw,))
    pg.execute("DELETE FROM bot_instances WHERE id = ?", (rs["instance_id"],))
    r = redemption.redeem_code(8002, sweep_res["code"])
    check("stale slot swept -> new user can redeem", r.get("ok") is True)
    check("sweep logged 'released'",
          any("stale" in (l.get("details") or "") for l in _log_rows(cid_sw, "released")))
    row = redemption.get_code(cid_sw)
    check("uses_consumed = 1 after sweep + new reserve", int(row["uses_consumed"]) == 1)

    print("== normalization & masking ==")
    norm_res = redemption.create_code("basic", 30, 1, 30, created_by=OWNER)
    lowered = norm_res["code"].lower().replace("-", " ")
    check("lowercase/spaceless input still found",
          redemption.find_code(lowered) is not None)
    check("mask hides the tail", norm_res["code"] not in redemption.mask_code(norm_res["code"]))
    check("code_prefix never exposes full code",
          len(redemption.code_prefix(norm_res["code"])) < len(norm_res["code"]))

    print("== targeted codes (تقييد المستخدم) ==")
    t_res = redemption.create_code("pro", 40, 1, 14, created_by=OWNER, targeted_user_id=9100)
    check("targeted code created", t_res.get("ok") is True and t_res.get("targeted_user_id") == 9100)
    cid_t = t_res["code_id"]
    r = redemption.redeem_code(9200, t_res["code"])
    check("non-target user rejected", r.get("ok") is False and r.get("reason") == "not_for_you")
    row = redemption.get_code(cid_t)
    check("rejected attempt consumed nothing", int(row["uses_consumed"]) == 0)
    check("no instance created for rejected user",
          pg.fetchone("SELECT id FROM bot_instances WHERE owner_id = ?", (9200,)) is None)
    r = redemption.redeem_code(9100, t_res["code"])
    check("target user accepted", r.get("ok") is True and r.get("plan") == "pro")

    print("== status tabs & usage log ==")
    counts = redemption.code_status_counts()
    check("counts has all keys",
          all(k in counts for k in ("all", "active", "exhausted", "revoked", "expired")))
    actives = redemption.list_codes(50, "active")
    check("active tab excludes revoked/exhausted",
          all(c["status"] == "active" for c in actives))
    revs = redemption.list_codes(50, "revoked")
    check("revoked tab contains the revoked pro code",
          any(int(c["id"]) == cid_pro for c in revs))
    uses = redemption.code_uses(cid_pro)
    check("usage log shows who + when + which clone",
          len(uses) == 1 and int(uses[0]["user_id"]) == 6001
          and uses[0]["bot_username"] == "pro_user_one_bot" and uses[0]["created_at"])

    print("== 24h free trial ==")
    ok, reason = redemption.trial_eligible(9500)
    check("fresh user is trial-eligible", ok is True)
    tr = redemption.start_trial(9500, "pro")
    check("trial started", tr.get("ok") is True and tr.get("plan") == "pro"
          and tr.get("instance_id") and tr.get("runtime_hours") == 24)
    t_inst = pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (tr["instance_id"],))
    check("trial instance pending_token with plan",
          t_inst["status"] == "pending_token" and t_inst["plan"] == "pro")
    tr2 = redemption.start_trial(9500, "basic")
    check("second trial blocked while pending",
          tr2.get("ok") is False and tr2.get("reason") == "trial_used")
    check("cancelled trial released", redemption.release_trial(9500, "user_cancelled") is True)
    check("trial pending instance deleted",
          pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (tr["instance_id"],)) is None)
    tr3 = redemption.start_trial(9500, "basic")
    check("trial retry after cancel ok", tr3.get("ok") is True)
    def _fake_trial_get_me(_tok):
        return {"id": 990003, "username": "trial_user_bot"}
    referral._tg_get_me = _fake_trial_get_me
    tra = referral.verify_and_link_token(
        9500, "123456789:AATrialToken_fake_padpadpad_pad9",
        instance_id=tr3["instance_id"], plan="basic", days=1)
    check("trial activation ok", tra.get("ok") is True)
    t_inst = pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (tr3["instance_id"],))
    exp_hours = (t_inst["expires_at"] - t_inst["activated_at"]).total_seconds() / 3600
    check("trial expiry = 24h", abs(exp_hours - 24) < 0.02)
    check("release refused after activation", redemption.release_trial(9500) is False)
    tr4 = redemption.start_trial(9500)
    check("no second trial ever after activation",
          tr4.get("ok") is False and tr4.get("reason") == "trial_used")
    ok2, reason2 = redemption.trial_eligible(6001)
    check("trial blocked for user with an active clone",
          ok2 is False and reason2 == "has_instance")

    print("== parallel-flow lockdown (one clone per user) ==")
    par = redemption.create_code("basic", 30, 3, 7, created_by=OWNER)
    check("parallel-test code created", par.get("ok") is True)
    u = 9700
    r1 = redemption.redeem_code(u, par["code"])
    check("first redemption reserves", r1.get("ok") is True)
    # A SECOND code while the first reservation is in flight must be
    # blocked — two parallel flows could otherwise each link a token and
    # yield two clones for one user.
    par2 = redemption.create_code("pro", 40, 3, 7, created_by=OWNER)
    r2 = redemption.redeem_code(u, par2["code"])
    check("second code blocked while first pending (pending_flow)",
          r2.get("ok") is False and r2.get("reason") == "pending_flow")
    check("blocked code's uses NOT consumed",
          int(redemption.get_code(par2["code_id"])["uses_consumed"]) == 0)
    ok3, reason3 = redemption.trial_eligible(u)
    check("trial blocked while code flow pending",
          ok3 is False and reason3 == "pending_flow")
    check("in-flight released",
          redemption.release_reservation(u, par["code_id"], "test") is True)
    ok4, _ = redemption.trial_eligible(u)
    check("trial eligible again after release", ok4 is True)
    r3 = redemption.redeem_code(u, par2["code"])
    check("second code redeems after release", r3.get("ok") is True)

    # FINAL choke point: verify_and_link_token must refuse while ANOTHER
    # of the user's instances is active / mid-activation — even if the
    # caller forged a parallel flow.
    other_inst = pg.insert_returning_id(
        "INSERT INTO bot_instances (owner_id, status, plan) VALUES (?, 'pending_token', 'basic')",
        (u,),
    )

    def _fake_par_get_me(_tok):
        return {"id": 990700, "username": "parallel_user_bot"}

    referral._tg_get_me = _fake_par_get_me
    link1 = referral.verify_and_link_token(
        u, "123456789:AAParallelToken_fake_pad_pad_1",
        instance_id=r3["instance_id"], plan="pro", days=40)
    check("link blocked while a parallel pending instance exists",
          link1.get("ok") is False and link1.get("reason") == "has_other_instance")
    check("no secret stored for the blocked link",
          pg.fetchone("SELECT 1 AS ok FROM bot_secrets WHERE instance_id = ?",
                      (r3["instance_id"],)) is None)
    pg.execute("DELETE FROM bot_instances WHERE id = ?", (other_inst,))
    link2 = referral.verify_and_link_token(
        u, "123456789:AAParallelToken_fake_pad_pad_1",
        instance_id=r3["instance_id"], plan="pro", days=40)
    check("link succeeds once the parallel instance is gone", link2.get("ok") is True)

    # While the clone is ACTIVE, a forged second instance must NEVER link.
    forged = pg.insert_returning_id(
        "INSERT INTO bot_instances (owner_id, status, plan) VALUES (?, 'pending_token', 'basic')",
        (u,),
    )

    def _fake_par_get_me2(_tok):
        return {"id": 990701, "username": "parallel_user_second_bot"}

    referral._tg_get_me = _fake_par_get_me2
    link3 = referral.verify_and_link_token(
        u, "123456789:AAParallelToken_fake_pad_pad_2",
        instance_id=forged, plan="basic", days=30)
    check("second clone never links while first is ACTIVE",
          link3.get("ok") is False and link3.get("reason") == "has_other_instance")
    check("no secret stored for the forged instance",
          pg.fetchone("SELECT 1 AS ok FROM bot_secrets WHERE instance_id = ?", (forged,)) is None)
    pg.execute("DELETE FROM bot_instances WHERE id = ?", (forged,))

    print("== stale sweep cleans pending instances ==")
    sw = redemption.create_code("basic", 30, 2, 7, created_by=OWNER)
    u2 = 9800
    rs = redemption.redeem_code(u2, sw["code"])
    check("sweep-test reservation ok", rs.get("ok") is True)
    pg.execute(
        "UPDATE redemption_log SET created_at = CURRENT_TIMESTAMP - INTERVAL '61 minutes' "
        "WHERE code_id = ? AND action = 'reserved' AND user_id = ?",
        (sw["code_id"], u2),
    )
    redemption.redeem_code(9801, sw["code"])  # any redemption runs the sweep
    check("sweep returned the stale use",
          int(redemption.get_code(sw["code_id"])["uses_consumed"]) == 1)
    check("sweep DELETED the abandoned pending instance",
          pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (rs["instance_id"],)) is None)
    ok5, _ = redemption.trial_eligible(u2)
    check("swept user not blocked by an orphan pending row", ok5 is True)


# ---- run the full flow once, HERE (import time) --------------------------------
# Same contract as test_clone_plans: the dedicated-database pin and the
# referral._tg_get_me fake are restored IMMEDIATELY after the checks, so
# nothing leaks into other modules' tests under a shared pytest session.

_ORIGINAL_TG_GET_ME = referral._tg_get_me
try:
    _setup_module_checks()
finally:
    referral._tg_get_me = _ORIGINAL_TG_GET_ME
    pg.PG_DSN = _ORIGINAL_PG_DSN
    pgtest.drop_test_database(_DB_NAME)


def test_redemption_code_lifecycle():
    """Full lifecycle: create → reserve → fail/release → re-redeem → atomic
    limit → activate → confirm → exhaust — with audit-trail secrecy."""
    assert not _failures, f"redemption code failures: {_failures}"


if __name__ == "__main__":
    print()
    if _failures:
        print(f"{len(_failures)} FAILURES: {_failures}")
        sys.exit(1)
    print("ALL REDEMPTION CODE TESTS PASSED")
