"""Smoke test of the referral + clone-instance engine (SQLite fallback path).

Runs the FULL business flow exactly as production would:
  code creation -> binding rules -> qualification rules -> reward counting
  -> atomic claim -> token verification (mocked getMe) -> instance activation
  -> double-claim / double-token protection -> audit trail.

Runnable with plain `python3 tests/test_referral_smoke.py` — dotenv and
cryptography are stubbed, so no extra dependencies are required anywhere.
"""
import base64
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, "temp", "test_referral_smoke.db")

os.environ["DATABASE_PATH"] = DB
os.environ.pop("NEON_DATABASE_URL", None)
os.environ.pop("POSTGRES_URL", None)
os.environ.pop("DATABASE_URL", None)
os.environ["REFERRAL_TARGET"] = "3"  # small target for the test
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

pg.init_referral_schema()
assert pg.backend_name() == "sqlite", pg.backend_name()

import config.settings as cs  # noqa: E402

cs.REFERRAL_TARGET = 3
import services.referral as R  # noqa: E402

R.REFERRAL_TARGET = 3


class FakeUser:
    def __init__(self, uid, username="u", first_name="F"):
        self.id, self.username, self.first_name = uid, username, first_name


failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def main():
    # ---- codes ----
    code_a = R.get_or_create_code(1001)
    check("code created", code_a.startswith("ref_") and len(code_a) == 10)
    check("code permanent", R.get_or_create_code(1001) == code_a)
    link = R.referral_link(1001, "MainBot")
    check("referral link shape", link == f"https://t.me/MainBot?start={code_a}")

    # ---- registration & binding rules ----
    R.register_user(FakeUser(1001), is_new=True)   # referrer
    R.register_user(FakeUser(2001), is_new=True)   # referred #1
    check("bind ok", R.bind_referral(code_a, 2001) == "pending")
    check("self-referral blocked", R.bind_referral(code_a, 1001) == "self")
    check("dup binding blocked", R.bind_referral(code_a, 2001) == "dup")
    check("bad code rejected", R.bind_referral("ref_ZZZZZZ", 2002) == "bad_code")
    check("no code ok", R.bind_referral(None, 2003) == "no_code")

    # second referrer cannot steal an already-bound user
    code_b = R.get_or_create_code(1002)
    R.register_user(FakeUser(1002), is_new=True)
    check("re-attribution blocked", R.bind_referral(code_b, 2001) == "dup")

    # ---- qualification rule (a): first successful download ----
    R.on_successful_download(2001)
    s = R.get_stats(1001)
    check("download qualifies", s["qualified"] == 1 and s["pending"] == 0)

    # ---- qualification rule (b): two distinct activity days ----
    R.register_user(FakeUser(2004), is_new=True)
    R.bind_referral(code_a, 2004)
    R.register_user(FakeUser(2005), is_new=True)
    R.bind_referral(code_a, 2005)
    pg.execute(
        "UPDATE ref_users SET activity_days = '2020-01-01,' || activity_days WHERE telegram_id = ?",
        (2004,),
    )
    R.register_user(FakeUser(2004), is_new=False)  # returning-user re-check
    s = R.get_stats(1001)
    check("2-day rule qualifies", s["qualified"] == 2)
    check("one day not enough", s["pending"] == 1)  # 2005 still pending

    # ---- reward eligibility + ATOMIC claim ----
    R.on_successful_download(2005)  # third qualified -> target 3
    s = R.get_stats(1001)
    check("reward eligible at target", s["reward_status"] == "eligible" and s["qualified"] == 3)
    ok1, st1 = R.claim_reward(1001)
    ok2, st2 = R.claim_reward(1001)  # double-tap
    check("claim succeeds", ok1 and st1 == "awaiting_token")
    check("double claim idempotent", ok2)
    n_inst = pg.fetchone(
        "SELECT COUNT(*) AS c FROM bot_instances WHERE owner_id = ?", (1001,)
    )["c"]
    check("exactly ONE instance row", int(n_inst) == 1)
    check("not-eligible cannot claim", R.claim_reward(1002) == (False, "not_eligible"))

    # ---- token verification pipeline ----
    check("shape rejected", R.verify_and_link_token(1001, "not-a-token") == {"ok": False, "reason": "shape"})
    cs.BOT_TOKEN = R.BOT_TOKEN = "5550009:AATheMainBotTokenForTest0000000000000"
    check("main-bot token rejected", R.verify_and_link_token(1001, R.BOT_TOKEN)["reason"] == "main_bot_token")
    R._tg_get_me = lambda tok: {"id": 5550001, "username": "my_clone_bot"}
    res = R.verify_and_link_token(1001, "5550001:AAFaketokenForSmokeTest1234567890abcdefgh")
    check("activation ok", res["ok"] and res["bot_username"] == "my_clone_bot")

    row = pg.fetchone(
        "SELECT token_encrypted, token_hash FROM bot_secrets WHERE instance_id = ?",
        (res["instance_id"],),
    )
    check(
        "token encrypted at rest",
        "Faketoken" not in row["token_encrypted"],
    )
    check(
        "decrypt roundtrip",
        R.decrypt_token(row["token_encrypted"]) == "5550001:AAFaketokenForSmokeTest1234567890abcdefgh",
    )

    res2 = R.verify_and_link_token(1001, "5550001:AAFaketokenForSmokeTest1234567890abcdefgh")
    check("token double-link blocked", res2["ok"] is False and res2["reason"] == "already_linked")

    inst = pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (res["instance_id"],))
    check("instance active", inst["status"] == "active" and inst["bot_id"] == 5550001)
    bset = pg.fetchall(
        "SELECT key, value FROM clone_settings WHERE instance_id = ?", (res["instance_id"],)
    )
    check("clone settings written", {r["key"] for r in bset} >= {"clone_mode", "owner_id", "back_to_main"})
    reward = pg.fetchone("SELECT status FROM referral_rewards WHERE user_id = ?", (1001,))
    check("reward marked claimed (never twice)", reward["status"] == "claimed")
    actions = [l["action"] for l in pg.fetchall("SELECT action FROM activation_log WHERE owner_id = ?", (1001,))]
    check("audit trail complete", {"reward_eligible", "reward_claimed", "instance_activated"} <= set(actions))
    check("token load works", R.load_instance_token(res["instance_id"]).startswith("5550001:"))
    check("active instances listed", any(i["id"] == res["instance_id"] for i in R.list_active_instances()))

    # ---- is_clone_bot logic ----
    cs.MAIN_BOT_USERNAME = "MainBot"
    try:
        from telegram.ext import Application  # noqa: F401
    except ImportError:
        # Minimal stub so instance_manager can be imported without ptb here;
        # is_clone_bot/spawn wiring is exercised live on the server.
        tg = sys.modules.get("telegram") or types.ModuleType("telegram")
        tg_ext = types.ModuleType("telegram.ext")
        tg_ext.Application = object
        tg.ext = tg_ext
        sys.modules["telegram"] = tg
        sys.modules["telegram.ext"] = tg_ext
    import services.instance_manager as im

    check("main bot not a clone", im.is_clone_bot("MainBot") is False)
    check("other bot is a clone", im.is_clone_bot("my_clone_bot") is True)

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        return 1
    print("ALL REFERRAL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
