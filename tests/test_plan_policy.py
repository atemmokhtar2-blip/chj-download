"""📦 Clone plan policy — Basic vs Pro enforcement on real PostgreSQL.

Runs as a pytest module (dedicated database via tests/pgtest.py, pinned
through pg.PG_DSN like the other referral modules) AND as a plain script
(`python3 tests/test_plan_policy.py`).

Covers the spec contract:
  * the limits TABLE itself (Basic 70/day • 3 links • 720p ceiling • 7-day
    history • no panel; Pro 150/day • 10 links • highest quality • 90-day
    history • panel) — the exact numbers the product spec promises;
  * fail-closed plan resolution (missing/garbage plan -> Basic);
  * plan_context resolved from the RUNNING bot — never from input;
  * DB-backed daily counters (survive restarts) scoped per instance;
  * batch caps per plan; quality clamping at download time (auto/size/
    highest/1080p all become 720p on Basic, never on Pro);
  * clone bans scoped to ONE instance;
  * clone admins + granular permissions (can_ban/can_broadcast/can_stats);
  * the security rule that matters most: global OWNER_ID / ADMIN_IDS grant
    NOTHING on a clone, and a clone owner has no access to another clone;
  * clone settings, broadcast log, failure tracking, admin audit log + CSV;
  * per-plan history retention purge (7 vs 90 days);
  * the clone user directory (ref_users source='clone:<bot_id>').
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

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

if "pytest" not in sys.modules:
    pgtest.bootstrap()

from database import pg  # noqa: E402

_ORIGINAL_PG_DSN = pg.PG_DSN
_DB_NAME = "xd_plan_policy"
pg.PG_DSN = pgtest.create_test_database(_DB_NAME)

from database import db as legacy_db  # noqa: E402

# Production order: core tables FIRST (users / downloads / caches), then the
# referral+plan schema — init_referral_schema ALTERs downloads to add the
# instance_id column, so downloads must already exist (same order as bot.py).
legacy_db.init_db()
pg.init_referral_schema()

import config.settings as cs  # noqa: E402
from services import plan_policy as pp  # noqa: E402

# Deterministic environment for cap comparisons.
cs.MAIN_BOT_USERNAME = "xd_main_bot"
cs.BATCH_MAX_URLS = 10

BASIC_OWNER = 4001
PRO_OWNER = 4002
BASIC_BOT_ID = 900111
PRO_BOT_ID = 900222
USER_A = 4101
USER_B = 4102
CLONE_ADMIN = 4201
OUTSIDER = 4999

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        _failures.append(label)


def _ts(days_ago: float = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _mk_instance(owner: int, bot_id: int, username: str, plan: str) -> dict:
    inst_id = pg.insert_returning_id(
        "INSERT INTO bot_instances (owner_id, bot_id, bot_username, status, plan, "
        "activated_at, expires_at) "
        "VALUES (?, ?, ?, 'active', ?, CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP + INTERVAL '30 days')",
        (owner, bot_id, username, plan))
    return pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (inst_id,))


def _mk_user(uid: int, username: str = "", first: str = "") -> None:
    pg.execute(
        "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id) DO NOTHING", (uid, username, first))


def _mk_download(inst_id: int, uid: int, platform: str = "youtube",
                 size: int = 1000, days_ago: float = 0) -> None:
    pg.execute(
        "INSERT INTO downloads (user_id, url, title, platform, quality, media_type, "
        "file_size, instance_id, created_at) "
        "VALUES (?, ?, 't', ?, '720p', 'video', ?, ?, ?)",
        (uid, f"https://example.com/{uid}/{platform}", platform, size,
         int(inst_id), _ts(days_ago)))


def _setup_module_checks() -> tuple[dict, dict]:
    print("== limits table matches the product spec ==")
    b = pp.limits("basic")
    p = pp.limits("pro")
    check("basic: 70 daily downloads", b["daily_downloads"] == 70)
    check("basic: 3 links per batch", b["batch_urls"] == 3)
    check("basic: 720p ceiling", b["max_video_height"] == 720)
    check("basic: 7-day history", b["history_days"] == 7)
    check("basic: NO admin panel", b["admin_panel"] is False)
    check("pro: 150 daily downloads", p["daily_downloads"] == 150)
    check("pro: 10 links per batch", p["batch_urls"] == 10)
    check("pro: highest quality (no ceiling)", p["max_video_height"] is None)
    check("pro: 90-day history", p["history_days"] == 90)
    check("pro: admin panel", p["admin_panel"] is True)
    check("limits() returns a copy (callers can't mutate the table)",
          b is not pp.PLAN_LIMITS["basic"])

    print("== fail-closed plan resolution ==")
    check("None -> basic", pp.normalize_plan(None) == pp.PLAN_BASIC)
    check("garbage -> basic", pp.normalize_plan("premium") == pp.PLAN_BASIC)
    check("empty -> basic", pp.normalize_plan("") == pp.PLAN_BASIC)
    check("'PRO' -> pro", pp.normalize_plan("PRO") == pp.PLAN_PRO)
    check("' pro ' -> pro", pp.normalize_plan(" pro ") == pp.PLAN_PRO)

    print("== quality clamping (server-side ceiling) ==")
    check("basic: best -> 720p", pp.clamp_quality("best", "basic") == "720p")
    check("basic: highest -> 720p", pp.clamp_quality("highest", "basic") == "720p")
    check("basic: auto -> 720p (open-ended can't exceed ceiling)",
          pp.clamp_quality("auto", "basic") == "720p")
    check("basic: size -> 720p", pp.clamp_quality("size", "basic") == "720p")
    check("basic: '' -> 720p", pp.clamp_quality("", "basic") == "720p")
    check("basic: 1080p -> 720p", pp.clamp_quality("1080p", "basic") == "720p")
    check("basic: 2160p -> 720p", pp.clamp_quality("2160p", "basic") == "720p")
    check("basic: 480p stays", pp.clamp_quality("480p", "basic") == "480p")
    check("basic: 720p stays", pp.clamp_quality("720p", "basic") == "720p")
    check("pro: 1080p untouched", pp.clamp_quality("1080p", "pro") == "1080p")
    check("pro: auto untouched", pp.clamp_quality("auto", "pro") == "auto")
    check("pro: best untouched", pp.clamp_quality("best", "pro") == "best")
    check("main: untouched", pp.clamp_quality("1080p", "main") == "1080p")

    print("== settings-menu option filter ==")
    check("basic can't SELECT highest", pp.quality_option_allowed("highest", "basic") is False)
    check("basic can't SELECT 1080p", pp.quality_option_allowed("1080p", "basic") is False)
    check("basic CAN select 720p", pp.quality_option_allowed("720p", "basic") is True)
    check("basic CAN select 480p", pp.quality_option_allowed("480p", "basic") is True)
    check("pro can select anything", pp.quality_option_allowed("highest", "pro") is True
          and pp.quality_option_allowed("1080p", "pro") is True)

    print("== batch + daily caps from context ==")
    main_ctx = {"is_clone": False, "instance_id": 0, "plan": pp.PLAN_MAIN,
                "limits": None, "instance": None}
    basic_ctx = {"is_clone": True, "instance_id": 1, "plan": pp.PLAN_BASIC,
                 "limits": pp.limits("basic"), "instance": {}}
    pro_ctx = {"is_clone": True, "instance_id": 2, "plan": pp.PLAN_PRO,
               "limits": pp.limits("pro"), "instance": {}}
    check("main: batch = global setting", pp.batch_cap(main_ctx) == cs.BATCH_MAX_URLS)
    check("main: no daily plan cap", pp.daily_cap(main_ctx) is None)
    check("basic: batch capped to 3", pp.batch_cap(basic_ctx) == 3)
    check("basic: daily cap 70", pp.daily_cap(basic_ctx) == 70)
    check("pro: batch capped to 10", pp.batch_cap(pro_ctx) == 10)
    check("pro: daily cap 150", pp.daily_cap(pro_ctx) == 150)

    # ------------------------------------------------------------------
    print("== instances + plan_context resolved from the RUNNING bot ==")
    inst_basic = _mk_instance(BASIC_OWNER, BASIC_BOT_ID, "xd_clone_basic_bot", "basic")
    inst_pro = _mk_instance(PRO_OWNER, PRO_BOT_ID, "xd_clone_pro_bot", "pro")
    check("instances created", bool(inst_basic) and bool(inst_pro))

    pp.invalidate_instance_cache()

    class _FakeBot:
        def __init__(self, bot_id, username):
            self.id = bot_id
            self.username = username

    ctx_basic = pp.plan_context(_FakeBot(BASIC_BOT_ID, "xd_clone_basic_bot"))
    check("clone bot -> is_clone", ctx_basic["is_clone"] is True)
    check("clone bot -> its instance id", ctx_basic["instance_id"] == int(inst_basic["id"]))
    check("clone bot -> stored plan", ctx_basic["plan"] == "basic")
    check("clone bot -> basic limits attached",
          ctx_basic["limits"]["daily_downloads"] == 70)

    ctx_main = pp.plan_context(_FakeBot(1, "xd_main_bot"))
    check("main bot -> NOT clone", ctx_main["is_clone"] is False)
    check("main bot -> plan 'main', no caps",
          ctx_main["plan"] == pp.PLAN_MAIN and ctx_main["limits"] is None)

    ctx_orphan = pp.plan_context(_FakeBot(999999, "xd_clone_ghost_bot"))
    check("clone with MISSING row -> fail closed to basic",
          ctx_orphan["is_clone"] is True and ctx_orphan["plan"] == pp.PLAN_BASIC
          and ctx_orphan["instance"] is None)

    # ------------------------------------------------------------------
    print("== DB-backed daily counters, scoped per instance ==")
    _mk_user(USER_A, "user_a", "أحمد")
    _mk_user(USER_B, "user_b", "سارة")
    _mk_download(inst_basic["id"], USER_A, "youtube", 1000)
    _mk_download(inst_basic["id"], USER_A, "youtube", 1000)
    check("today's count = 2", pp.count_downloads_today(int(inst_basic["id"]), USER_A) == 2)
    check("below cap -> not reached",
          pp.daily_limit_reached(int(inst_basic["id"]), USER_A, "basic") is False)
    for _ in range(68):
        _mk_download(inst_basic["id"], USER_A, "youtube", 1000)
    check("70 downloads -> basic daily limit REACHED",
          pp.count_downloads_today(int(inst_basic["id"]), USER_A) == 70
          and pp.daily_limit_reached(int(inst_basic["id"]), USER_A, "basic") is True)
    _mk_download(inst_pro["id"], USER_A, "instagram", 2000)
    _mk_download(inst_pro["id"], USER_A, "instagram", 2000)
    check("counter is INSTANCE-scoped (pro sees only its own 2)",
          pp.count_downloads_today(int(inst_pro["id"]), USER_A) == 2)
    check("same user still under the pro cap",
          pp.daily_limit_reached(int(inst_pro["id"]), USER_A, "pro") is False)
    check("other user unaffected",
          pp.count_downloads_today(int(inst_basic["id"]), USER_B) == 0)

    # ------------------------------------------------------------------
    print("== clone bans (scoped to ONE instance) ==")
    check("ban applied", pp.ban_user(int(inst_basic["id"]), USER_B, BASIC_OWNER, "spam"))
    check("banned on THIS clone", pp.is_user_banned(int(inst_basic["id"]), USER_B) is True)
    check("NOT banned on the other clone",
          pp.is_user_banned(int(inst_pro["id"]), USER_B) is False)
    ban_row = pp.get_ban(int(inst_basic["id"]), USER_B)
    check("ban row keeps the actor", ban_row and int(ban_row["banned_by"]) == BASIC_OWNER)
    check("unban applied", pp.unban_user(int(inst_basic["id"]), USER_B))
    check("ban lifted", pp.is_user_banned(int(inst_basic["id"]), USER_B) is False)

    # ------------------------------------------------------------------
    print("== clone admins + granular permissions ==")
    check("owner access = 'owner'", pp.clone_access(inst_basic, BASIC_OWNER) == "owner")
    check("outsider access = 'none'", pp.clone_access(inst_basic, OUTSIDER) == "none")
    check("owner passes every permission",
          all(pp.perm_allows(inst_basic, BASIC_OWNER, p) for p in pp.ADMIN_PERMS))
    check("outsider passes nothing",
          not any(pp.perm_allows(inst_basic, OUTSIDER, p) for p in pp.ADMIN_PERMS))

    pp.add_clone_admin(int(inst_basic["id"]), CLONE_ADMIN, BASIC_OWNER)
    row = pp.get_clone_admin(int(inst_basic["id"]), CLONE_ADMIN)
    check("new admin: stats-only by default",
          row and bool(row["can_stats"]) and not row["can_ban"] and not row["can_broadcast"])
    check("admin access = 'admin'", pp.clone_access(inst_basic, CLONE_ADMIN) == "admin")
    check("admin allowed stats", pp.perm_allows(inst_basic, CLONE_ADMIN, "can_stats") is True)
    check("admin NOT allowed ban yet",
          pp.perm_allows(inst_basic, CLONE_ADMIN, "can_ban") is False)
    pp.set_admin_perm(int(inst_basic["id"]), CLONE_ADMIN, "can_ban", True)
    check("ban permission toggled on",
          pp.perm_allows(inst_basic, CLONE_ADMIN, "can_ban") is True)
    check("unknown permission rejected",
          pp.set_admin_perm(int(inst_basic["id"]), CLONE_ADMIN, "can_nuke", True) is False)
    admins = pp.list_clone_admins(int(inst_basic["id"]))
    check("admin listed", any(int(a["user_id"]) == CLONE_ADMIN for a in admins))

    print("== THE security rule: global powers grant NOTHING on a clone ==")
    check("global OWNER_ID is NOT a clone owner",
          pp.clone_access(inst_basic, cs.OWNER_ID) == "none"
          and pp.clone_access(inst_pro, cs.OWNER_ID) == "none")
    for aid in list(getattr(cs, "ADMIN_IDS", []) or [])[:3]:
        check(f"global admin {aid} has no clone access",
              pp.clone_access(inst_basic, int(aid)) == "none")
    check("basic owner has NO access to the pro clone",
          pp.clone_access(inst_pro, BASIC_OWNER) == "none")
    check("pro owner has NO access to the basic clone",
          pp.clone_access(inst_basic, PRO_OWNER) == "none")
    check("clone admin of basic has NO access to pro",
          pp.clone_access(inst_pro, CLONE_ADMIN) == "none")

    check("admin removed", pp.remove_clone_admin(int(inst_basic["id"]), CLONE_ADMIN))
    check("removed admin lost access",
          pp.clone_access(inst_basic, CLONE_ADMIN) == "none")

    # ------------------------------------------------------------------
    print("== clone settings ==")
    iid = int(inst_basic["id"])
    check("missing setting -> default",
          pp.get_clone_setting(iid, "welcome_text", "DEF") == "DEF")
    pp.set_clone_setting(iid, "welcome_text", "أهلاً بك")
    check("setting saved", pp.get_clone_setting(iid, "welcome_text") == "أهلاً بك")
    pp.set_clone_setting(iid, "welcome_text", "مرحباً")
    check("setting upserted (no duplicate)",
          pp.get_clone_setting(iid, "welcome_text") == "مرحباً")
    pp.set_clone_setting(iid, "default_quality", "480p")
    check("quality default saved", pp.get_clone_setting(iid, "default_quality") == "480p")
    check("settings are instance-scoped",
          pp.get_clone_setting(int(inst_pro["id"]), "welcome_text", "X") == "X")

    # ------------------------------------------------------------------
    print("== failure tracking (safe reasons only) ==")
    pp.log_download_failure(USER_A, iid, "youtube", "timeout")
    pp.log_download_failure(USER_A, iid, "youtube", "timeout")
    pp.log_download_failure(USER_A, iid, "instagram", "analysis_failed")
    check("failures today = 3", pp.count_failures_today(iid) == 3)
    check("failures for user = 3", pp.count_failures_for_user(iid, USER_A) == 3)
    check("failures for other user = 0", pp.count_failures_for_user(iid, USER_B) == 0)
    check("other instance sees none", pp.count_failures_today(int(inst_pro["id"])) == 0)
    stats = pp.failure_stats(iid, 30)
    check("failure stats total", stats["total"] == 3)
    check("top failure reason = timeout x2",
          stats["top_reasons"] and str(stats["top_reasons"][0]["reason"]) == "timeout"
          and int(stats["top_reasons"][0]["cnt"]) == 2)
    recent = pp.recent_failures(iid, 3)
    check("recent failures listed newest-first",
          len(recent) == 3 and str(recent[0]["platform"]) == "instagram")

    # ------------------------------------------------------------------
    print("== broadcast log ==")
    pp.log_broadcast(iid, BASIC_OWNER, 10, 8, 2)
    pp.log_broadcast(iid, BASIC_OWNER, 5, 5, 0)
    bcs = pp.last_broadcasts(iid, 3)
    check("broadcasts logged newest-first",
          len(bcs) == 2 and int(bcs[0]["sent"]) == 5 and int(bcs[1]["failed"]) == 2)

    # ------------------------------------------------------------------
    print("== admin audit log + CSV export ==")
    pp.log_admin_action(iid, BASIC_OWNER, "ban_user", str(USER_B))
    pp.log_admin_action(iid, BASIC_OWNER, "set_default_quality", "480p")
    pp.log_admin_action(iid, BASIC_OWNER, "broadcast", "total=5 sent=5 failed=0")
    rows = pp.list_admin_log(iid, 15)
    check("audit rows recorded", len(rows) == 3)
    check("audit newest-first", str(rows[0]["action"]) == "broadcast")
    csv_text = pp.admin_log_csv(iid, 500)
    csv_lines = [l for l in csv_text.splitlines() if l.strip()]
    check("csv = header + 3 rows", len(csv_lines) == 4)
    check("csv mentions the actions",
          "ban_user" in csv_text and "broadcast" in csv_text)
    check("audit log scoped to instance",
          len(pp.list_admin_log(int(inst_pro["id"]), 15)) == 0)

    # ------------------------------------------------------------------
    print("== clone user directory (ref_users source) ==")
    src = f"clone:{BASIC_BOT_ID}"
    for uid, uname, fname in ((USER_A, "user_a", "أحمد"), (USER_B, "user_b", "سارة")):
        pg.execute(
            "INSERT INTO ref_users (telegram_id, username, first_name, source, "
            "registered_at, last_active_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            "ON CONFLICT (telegram_id) DO UPDATE SET source = EXCLUDED.source, "
            "last_active_at = CURRENT_TIMESTAMP", (uid, uname, fname, src))
    pg.execute(
        "INSERT INTO ref_users (telegram_id, username, first_name, source) "
        "VALUES (?, 'main_user', 'MAIN', 'main') ON CONFLICT DO NOTHING", (5555,))
    check("clone users counted (main users excluded)",
          pp.clone_users_count(inst_basic) == 2)
    check("active users (7d) counted", pp.clone_active_users_count(inst_basic, 7) == 2)
    listed = pp.list_clone_users(inst_basic, 8, 0)
    check("user list scoped to the clone", len(listed) == 2)
    by_name = pp.search_clone_user(inst_basic, "user_a")
    check("search by username works", len(by_name) == 1
          and int(by_name[0]["telegram_id"]) == USER_A)
    by_id = pp.search_clone_user(inst_basic, str(USER_B))
    check("search by telegram id works", len(by_id) == 1
          and int(by_id[0]["telegram_id"]) == USER_B)
    check("search never matches other-clone/main users",
          len(pp.search_clone_user(inst_basic, "main_user")) == 0)
    detail = pp.clone_user_detail(inst_basic, USER_A)
    check("detail: downloads match the counter",
          detail["downloads"] == pp.count_downloads_for_user(iid, USER_A) == 70)
    check("detail: failures match the counter", detail["failures"] == 3)
    check("detail: not banned", detail["banned"] is False)
    pp.ban_user(iid, USER_A, BASIC_OWNER, "abuse")
    check("detail: ban reflected", pp.clone_user_detail(inst_basic, USER_A)["banned"] is True)
    pp.unban_user(iid, USER_A)

    summary = pp.clone_today_summary(inst_basic)
    check("today summary consistent",
          summary["downloads_today"] == 70 and summary["failures_today"] == 3)
    ds = pp.clone_download_stats(inst_basic)
    # NOTE: stats run BEFORE the retention section adds USER_B's rows, and
    # every figure is INSTANCE-scoped (the pro instance's 2 downloads for
    # USER_A must never show up here).
    check("stats: today/week/month consistent",
          ds["today"] == 70 and ds["week"] >= 70 and ds["month"] >= 70)
    check("stats: total bytes summed (instance-scoped)",
          ds["total_bytes"] >= 70 * 1000)
    check("stats: top platforms include youtube",
          any(str(p["platform"]) == "youtube" for p in ds["top_platforms"]))
    check("stats: failure reasons surfaced", ds["failures_30d"] == 3)

    # ------------------------------------------------------------------
    print("== per-plan history retention ==")
    _mk_download(iid, USER_B, "tiktok", 500, days_ago=8)   # beyond basic 7d
    _mk_download(iid, USER_B, "tiktok", 500, days_ago=2)   # inside basic 7d
    removed = pp.purge_history_for_plan(iid, "basic")
    check("basic purge removed the 8-day-old row", removed == 1)
    check("basic purge kept the 2-day-old row",
          pp.count_downloads_for_user(iid, USER_B) == 1)
    pid = int(inst_pro["id"])
    _mk_download(pid, USER_A, "twitter", 700, days_ago=91)  # beyond pro 90d
    _mk_download(pid, USER_A, "twitter", 700, days_ago=30)  # inside pro 90d
    removed_pro = pp.purge_history_for_plan(pid, "pro")
    check("pro purge removed the 91-day-old row", removed_pro == 1)
    check("pro purge kept the 30-day-old row",
          pp.count_downloads_range(pid, 90) >= 3)
    total = pp.purge_all_clone_history()
    check("global sweep runs cleanly", total >= 0)

    return inst_basic, inst_pro


# --- effective_plan: an expired Pro downgrades to Basic IMMEDIATELY,
# without waiting for the 15-min expiry worker (fail closed).
from datetime import datetime, timedelta
_fut = (datetime.utcnow() + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
_past = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
check("active pro keeps pro",
      pp.effective_plan({"plan": "pro", "expires_at": _fut}) == pp.PLAN_PRO)
check("open-ended pro keeps pro",
      pp.effective_plan({"plan": "pro", "expires_at": None}) == pp.PLAN_PRO)
check("expired pro downgrades to basic",
      pp.effective_plan({"plan": "pro", "expires_at": _past}) == pp.PLAN_BASIC)
check("expired status downgrades to basic",
      pp.effective_plan({"plan": "pro", "status": "expired"}) == pp.PLAN_BASIC)
check("garbage plan is basic",
      pp.effective_plan({"plan": "PRO123"}) == pp.PLAN_BASIC)
check("missing row is basic", pp.effective_plan(None) == pp.PLAN_BASIC)


_INSTANCES = _setup_module_checks()


def test_plan_policy():
    assert not _failures, "FAILED CHECKS:\n" + "\n".join(_failures)


if __name__ == "__main__":
    print()
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print("  -", f)
        sys.exit(1)
    print("ALL PLAN-POLICY CHECKS PASSED ✅")
