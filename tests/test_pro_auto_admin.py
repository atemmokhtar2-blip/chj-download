"""Pro-plan fidelity + auto-admin appointment (PostgreSQL).

Covers the owner's spec:
  * A BASIC code activates a clone with EXACTLY the Basic feature set
    (70/day, 3 urls, 720p, 192kbps, 7-day history, NO admin panel).
  * A PRO code activates a clone with EXACTLY the Pro feature set
    (150/day, 10 urls, best quality, full audio, 90-day history) AND the
    redeemer is AUTOMATICALLY appointed full-permission admin of their
    own clone ("يتعين تلقائي أدمن").
  * The appointment lives ONLY in clone_admins — never in the main bot's
    global admin registry (complete isolation).
  * A basic->pro plan switch appoints the owner instantly.
  * The Pro clone's owner/admin sees "🛠️ لوحة التحكم" on their clone's
    reply keyboard; nobody else ever does (main bot, Basic clones, and
    regular users of Pro clones all get the standard 2-row keyboard).
  * Main-bot panels (owner/admin) never open inside a clone.

Runnable with plain `python3 tests/test_pro_auto_admin.py` — dotenv and
cryptography are stubbed, so no extra dependencies are required anywhere.
"""
import base64
import os
import sys
import types
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
sys.path.insert(0, os.path.join(REPO, "tests"))
import pgtest  # noqa: E402

if "pytest" not in sys.modules:
    pgtest.bootstrap()

from database import pg  # noqa: E402

_ORIGINAL_PG_DSN = pg.PG_DSN
_DB_NAME = "xd_pro_auto_admin"
pg.PG_DSN = pgtest.create_test_database(_DB_NAME)

from database import db as legacy_db  # noqa: E402

# Production order: core tables FIRST (users / downloads / caches), then the
# referral+plan schema — init_referral_schema ALTERs downloads to add the
# instance_id column, and ensure_plan_schema (clone_admins & friends) needs
# downloads to exist too (same order as bot.py).
legacy_db.init_db()
pg.init_referral_schema()
assert pg.backend_name() == "postgres", pg.backend_name()

import config.settings as cs  # noqa: E402
import services.referral as R  # noqa: E402
import services.redemption as RD  # noqa: E402
import services.plan_policy as pp  # noqa: E402

cs.MAIN_BOT_USERNAME = "MainBot"

MAIN_TOKEN = "5550009:AATheMainBotTokenForTest0000000000000"
cs.BOT_TOKEN = R.BOT_TOKEN = MAIN_TOKEN

failures = []


def check(name, cond):
    if cond:
        print(f"  ok  {name}")
    else:
        failures.append(name)
        print(f" FAIL {name}")


def _row(uid, iid):
    return pg.fetchone(
        "SELECT * FROM clone_admins WHERE instance_id = ? AND user_id = ?",
        (iid, uid))


def _activate_pro(uid, bot_id, username, days=30):
    """Full code->redeem->link->activate pipeline on a PRO code."""
    made = RD.create_code("pro", days, 5, 7, note="t", created_by=1)
    assert made.get("ok"), made
    res = RD.redeem_code(uid, made["code"])
    assert res.get("ok"), res
    R._tg_get_me = lambda tok: {"id": bot_id, "username": username}
    out = R.verify_and_link_token(
        uid, f"{bot_id}:AAFakeProToken{uid}xxxxxxxxxxxxxxxxxxxxxxxxxx",
        instance_id=int(res["instance_id"]),
        plan=res.get("plan"),
        days=int(res.get("runtime_days") or 0) or None)
    assert out.get("ok"), out
    return out, res


def _activate_basic(uid, bot_id, username, days=30):
    made = RD.create_code("basic", days, 5, 7, note="t", created_by=1)
    assert made.get("ok"), made
    res = RD.redeem_code(uid, made["code"])
    assert res.get("ok"), res
    R._tg_get_me = lambda tok: {"id": bot_id, "username": username}
    out = R.verify_and_link_token(
        uid, f"{bot_id}:AAFakeBasicToken{uid}xxxxxxxxxxxxxxxxxxxxxxxx",
        instance_id=int(res["instance_id"]),
        plan=res.get("plan"),
        days=int(res.get("runtime_days") or 0) or None)
    assert out.get("ok"), out
    return out, res


def main():
    print("== Pro plan fidelity + auto-admin ==")

    # ---- 1) PRO code: full Pro features + owner auto-appointed admin ----
    pro_out, pro_res = _activate_pro(9100001, 6100001, "pro_clone_a")
    iid = int(pro_out["instance_id"])
    check("pro code stored plan=pro", pro_res["plan"] == "pro" and pro_out["plan"] == "pro")
    lim = pp.limits(pro_out["plan"])
    check("pro daily cap 150", lim["daily_downloads"] == 150)
    check("pro batch 10", lim["batch_urls"] == 10)
    check("pro quality uncapped", lim["max_video_height"] is None)
    check("pro audio uncapped", lim["audio_cap_kbps"] is None)
    check("pro history 90d", lim["history_days"] == 90)
    check("pro has admin panel", lim["admin_panel"] is True)

    row = _row(9100001, iid)
    check("owner AUTO-appointed admin on pro activation", row is not None)
    check("auto-admin has FULL perms",
          row and row["can_ban"] and row["can_broadcast"] and row["can_stats"])
    inst = pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (iid,))
    check("clone_access = owner", pp.clone_access(inst, 9100001) == "owner")
    check("perm_allows everything for owner",
          all(pp.perm_allows(inst, 9100001, p) for p in pp.ADMIN_PERMS))

    # idempotent: repeated appointment keeps ONE row, full perms
    pp.ensure_owner_admin(iid, 9100001)
    n = pg.fetchone("SELECT COUNT(*) AS c FROM clone_admins WHERE instance_id = ? "
                    "AND user_id = ?", (iid, 9100001))["c"]
    check("ensure_owner_admin idempotent (one row)", int(n) == 1)

    # ---- 2) ISOLATION: clone admin NEVER becomes a main-bot admin ----
    from middlewares.auth import is_admin, is_owner
    check("auto-admin is NOT a global admin", is_admin(9100001) is False)
    check("auto-admin is NOT the owner", is_owner(9100001) is False)
    extra = pg.fetchone(
        "SELECT COUNT(*) AS c FROM bot_instances WHERE owner_id = ?", (9100001,))
    check("exactly one instance for the pro redeemer", int(extra["c"]) == 1)

    # ---- 3) BASIC code: exact Basic features, NO panel row ----
    b_out, b_res = _activate_basic(9100002, 6100002, "basic_clone_b")
    bid = int(b_out["instance_id"])
    check("basic code stored plan=basic", b_res["plan"] == "basic" and b_out["plan"] == "basic")
    blim = pp.limits(b_out["plan"])
    check("basic daily cap 70", blim["daily_downloads"] == 70)
    check("basic batch 3", blim["batch_urls"] == 3)
    check("basic quality ceiling 720", blim["max_video_height"] == 720)
    check("basic audio ceiling 192", blim["audio_cap_kbps"] == 192)
    check("basic history 7d", blim["history_days"] == 7)
    check("basic has NO admin panel", blim["admin_panel"] is False)
    check("basic activation adds NO clone_admins row", _row(9100002, bid) is None)
    binst = pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (bid,))
    check("basic owner still has implicit owner access",
          pp.clone_access(binst, 9100002) == "owner")

    # ---- 3b) Code tier is AUTHORITATIVE — never recomputed from referrals
    # A user with enough qualified referrals to cross the Pro threshold
    # activates a BASIC code whose plan payload arrives malformed (None).
    # It must STILL activate as Basic: the code's tier rules and garbage
    # fails closed — a Basic code must never be silently upgraded to Pro
    # just because the user has many referrals ("الكود Basic = Basic بالملي").
    uid3 = 9100003
    refcode = R.get_or_create_code(uid3)
    for r in range(R.pro_target()):
        pg.execute(
            "INSERT INTO referrals (referrer_id, referred_id, code, status, bound_at) "
            "VALUES (?, ?, ?, 'qualified', CURRENT_TIMESTAMP) "
            "ON CONFLICT (referred_id) DO NOTHING",
            (uid3, 9003000 + r, refcode),
        )
    made3 = RD.create_code("basic", 30, 5, 7, note="t", created_by=1)
    assert made3.get("ok"), made3
    res3 = RD.redeem_code(uid3, made3["code"])
    assert res3.get("ok"), res3
    R._tg_get_me = lambda tok: {"id": 6100003, "username": "basic_clone_c"}
    out3 = R.verify_and_link_token(
        uid3, "6100003:AAFakeBasicCodeToken3xxxxxxxxxxxxxxxxxxxxx",
        instance_id=int(res3["instance_id"]),
        plan=None,   # malformed payload — must fail closed to Basic
        days=30)
    assert out3.get("ok"), out3
    check("malformed code-plan payload still activates as Basic (fail closed)",
          out3.get("plan") == "basic")
    check("code tier is authoritative: Basic never upgrades from referrals",
          out3.get("plan") not in ("pro", ""))
    check("basic code (malformed payload) adds NO clone_admins row",
          _row(uid3, int(out3["instance_id"])) is None)

    # ---- 4) Upgrade basic -> pro appoints the owner instantly ----
    R.set_instance_plan(bid, "pro")
    row2 = _row(9100002, bid)
    check("plan upgrade to pro auto-appoints owner", row2 is not None)
    check("upgraded appointment has FULL perms",
          row2 and row2["can_ban"] and row2["can_broadcast"] and row2["can_stats"])
    R.set_instance_plan(bid, "basic")  # downgrade keeps the row (harmless; panel stays Pro-gated)

    # ---- 5) Keyboard: panel row ONLY for Pro clone owner/admin ----
    from types import SimpleNamespace
    from handlers.start import build_main_keyboard
    from locales import t

    admin_label = t("ar", "menu_admin")
    pro_ctx = SimpleNamespace(bot=SimpleNamespace(username="pro_clone_a", id=6100001))
    basic_ctx = SimpleNamespace(bot=SimpleNamespace(username="basic_clone_b", id=6100002))
    main_ctx = SimpleNamespace(bot=SimpleNamespace(username="MainBot", id=5550009))

    kb_owner = build_main_keyboard(pro_ctx, 9100001)
    labels_owner = [b.text for r in kb_owner.keyboard for b in r]
    check("pro clone OWNER sees the panel button", admin_label in labels_owner)

    pp.add_clone_admin(iid, 9100077, added_by=9100001, can_ban=True,
                       can_broadcast=True, can_stats=True)
    kb_admin = build_main_keyboard(pro_ctx, 9100077)
    labels_admin = [b.text for r in kb_admin.keyboard for b in r]
    check("pro clone ADMIN sees the panel button", admin_label in labels_admin)

    kb_user = build_main_keyboard(pro_ctx, 9100099)
    labels_user = [b.text for r in kb_user.keyboard for b in r]
    check("regular pro-clone user does NOT see it", admin_label not in labels_user)
    check("regular user keyboard = 2 buttons", len(labels_user) == 2)

    kb_basic = build_main_keyboard(basic_ctx, 9100002)
    labels_basic = [b.text for r in kb_basic.keyboard for b in r]
    check("basic clone owner does NOT see it (no panel on basic)",
          admin_label not in labels_basic)

    kb_main = build_main_keyboard(main_ctx, 9100001)
    labels_main = [b.text for r in kb_main.keyboard for b in r]
    check("main bot keyboard never has the clone panel row",
          admin_label not in labels_main and len(labels_main) == 2)

    # ---- 6) Router + isolation wiring (source level) ----
    bot_src = (Path(REPO) / "bot.py").read_text(encoding="utf-8")
    check("router knows the admin keyboard button",
          't("ar", "menu_admin")' in bot_src and "clone_admin_command" in bot_src)
    check("MENU_BUTTONS includes the panel label", "🛠️ لوحة التحكم" in bot_src)

    start_src = (Path(REPO) / "handlers" / "start.py").read_text(encoding="utf-8")
    guard_at = start_src.find('if not is_clone_bot(context.bot.username or ""):')
    owner_at = start_src.find("if is_owner(user.id):")
    check("main panels never open inside a clone (guard before is_owner)",
          guard_at != -1 and owner_at != -1 and guard_at < owner_at)
    check("auto-appointment note wired to the isolated panel",
          "clone_panel_note_sent" in start_src and '"ca:home"' in start_src)

    ref_src = (Path(REPO) / "handlers" / "referral.py").read_text(encoding="utf-8")
    check("activation message spells out the FULL plan features",
          "مميزات خطة" in ref_src and "_pp.limits(" in ref_src)

    ca_src = (Path(REPO) / "handlers" / "clone_admin.py").read_text(encoding="utf-8")
    check("panel gate: clone-only + Pro-only + owner/admin-only",
          'if not ctx.get("is_clone")' in ca_src
          and "PLAN_PRO" in ca_src and "clone_access" in ca_src)


main()

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nALL PRO-AUTO-ADMIN CHECKS PASSED ✅")
