"""Behavior tests for the referral extras:

  * milestone notifications (50 / 60 / target) are idempotent + secret-free,
  * the smart «نسختي» stage machine drives the right button per state,
  * support tickets: open -> admin reply -> view -> close -> reopen,
  * no notifier is registered yet -> auto-notify remains a safe no-op.
"""

from __future__ import annotations

import base64
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

os.environ["REFERRAL_TARGET"] = "70"

# --- stub dotenv if unavailable (plain-script parity with the smoke test) ---
try:
    import dotenv  # noqa: F401
except ImportError:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = dotenv

# --- stub cryptography.fernet with reversible b64 ---
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

# PostgreSQL is the ONLY backend. Under pytest, tests/conftest.py has already
# booted the disposable test server and pointed NEON_DATABASE_URL at the
# suite database before any project module was imported; a plain-script run
# (python3 tests/test_referral_extras.py) bootstraps its own server here.
# Rows are scrubbed by ID range below, so no dedicated database is needed.
if "pytest" not in sys.modules:
    pgtest.bootstrap()

from database import pg  # noqa: E402
from database import db as dbm  # noqa: E402

pg.init_referral_schema()
dbm.init_db()


def _scrub_test_rows() -> None:
    """Remove this suite's users so tests are re-runnable on a shared DB."""
    try:
        pg.execute(
            "DELETE FROM referrals WHERE referrer_id BETWEEN 900000 AND 940000 "
            "OR referred_id BETWEEN 900000 AND 940000"
        )
        pg.execute("DELETE FROM referral_rewards WHERE user_id BETWEEN 900000 AND 940000")
        pg.execute("DELETE FROM bot_instances WHERE owner_id BETWEEN 900000 AND 940000")
    except Exception:
        pass


_scrub_test_rows()

import config.settings as cs  # noqa: E402
import services.referral as R  # noqa: E402
from database import support  # noqa: E402


@pytest.fixture(autouse=True)
def _stable_target():
    cs.REFERRAL_TARGET = 70
    R.REFERRAL_TARGET = 70
    yield


def _insert_qualified(referrer, referred):
    pg.execute(
        "INSERT INTO referrals (referrer_id, referred_id, code, status) "
        "VALUES (?, ?, 'ref_TEST9', 'qualified')",
        (referrer, referred),
    )


def test_milestones_progress_and_idempotency():
    uid = 910001
    R._ensure_reward_row(uid)
    pg.execute("UPDATE referral_rewards SET notified_milestone = 0 WHERE user_id = ?", (uid,))

    for i in range(1, 51):
        _insert_qualified(uid, 900000 + i)
    assert R.claim_new_milestones(uid) == [50]
    assert R.claim_new_milestones(uid) == []  # idempotent

    for i in range(51, 61):
        _insert_qualified(uid, 900000 + i)
    assert R.claim_new_milestones(uid) == [60]

    for i in range(61, 71):
        _insert_qualified(uid, 900000 + i)
    R._recount_and_update_reward(uid)
    assert R.claim_new_milestones(uid) == [70]
    stats = R.get_stats(uid)
    assert stats["qualified"] == 70
    assert stats["reward_status"] == "eligible"


def test_auto_notify_is_safe_without_notifier():
    uid = 920001
    R._ensure_reward_row(uid)
    pg.execute("UPDATE referral_rewards SET notified_milestone = 0 WHERE user_id = ?", (uid,))
    for i in range(1, 51):
        _insert_qualified(uid, 920000 + i)
    R._auto_notify_milestones(uid)  # notifier None -> must not raise
    assert R.claim_new_milestones(uid) == []  # consumed by the auto path


def test_milestone_text_never_exposes_secrets():
    t = R.milestone_text(70)
    assert "70" in t and "مؤهل" in t
    assert "token" not in t.lower() and "ghp" not in t.lower()


def test_referrer_stage_smarts():
    assert R.referrer_stage({"reward_status": "progress", "instance": None}) == "progress"
    assert R.referrer_stage({"reward_status": "eligible", "instance": None}) == "eligible"
    assert R.referrer_stage({"reward_status": "awaiting_token", "instance": None}) == "awaiting_token"
    assert R.referrer_stage(
        {"reward_status": "claimed", "instance": {"status": "active", "bot_username": "x_bot"}}
    ) == "active"
    assert R.referrer_stage(
        {"reward_status": "claimed", "instance": {"status": "pending_token", "bot_username": ""}}
    ) == "awaiting_token"


def test_support_ticket_lifecycle():
    with dbm.db_cursor() as c:
        c.execute("DELETE FROM support_ticket_messages")
        c.execute("DELETE FROM support_tickets")

    t = support.open_ticket(930001, "token", "التوكن مرفوض", "مشكلة في التفعيل")
    assert t["ticket_no"].startswith("TKT-")
    assert t["status"] == "open"
    assert support.count_tickets("open") == 1
    assert support.get_ticket_for_user(t["id"], 930001)["ticket_no"] == t["ticket_no"]
    assert support.get_ticket_for_user(t["id"], 999999) is None

    support.add_admin_reply(t["id"], "أعد إنشاء البوت وأرسل التوكن الجديد")
    assert support.get_ticket(t["id"])["status"] == "answered"
    msgs = support.get_messages(t["id"])
    assert len(msgs) == 2 and msgs[-1]["sender"] == "admin"

    support.close_ticket(t["id"])
    assert support.get_ticket(t["id"])["status"] == "closed"
    support.reopen_ticket(t["id"])
    assert support.get_ticket(t["id"])["status"] == "open"

    dumped = repr(msgs) + repr(t)
    assert "ghp" not in dumped and "555" not in dumped


def test_support_ticket_numbers_are_unique():
    with dbm.db_cursor() as c:
        c.execute("DELETE FROM support_ticket_messages")
        c.execute("DELETE FROM support_tickets")

    nos = set()
    for _ in range(5):
        nos.add(support.open_ticket(940001, "other", "تجربة", "").get("ticket_no", ""))
    assert len(nos) == 5
    assert all(n.startswith("TKT-") for n in nos)
