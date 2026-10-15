"""Owner Control Center — security + behavior tests.

The owner panel's promise: OWNER-ONLY IN CODE (not just hidden buttons),
every sensitive action audited, bans carry reasons, and the runtime admin
registry can never grant ownership.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import init_db
from database.users import create_user, get_user, ban_user, unban_user, get_banned_users
from database.bot_settings import (
    set_setting, get_setting, delete_setting,
    add_extra_admin, remove_extra_admin, is_extra_admin,
)
from database.admin_audit import log_admin_action, get_admin_audit
import middlewares.auth as auth
from handlers.owner_panel import _owner_gate


class _User:
    def __init__(self, uid):
        self.id = uid


class _Chat:
    type = "private"


class _Update:
    def __init__(self, uid):
        self.effective_user = _User(uid)
        self.effective_chat = _Chat()


def test_owner_gate_allows_only_owner(monkeypatch):
    monkeypatch.setattr(auth, "OWNER_ID", 555000)
    assert _owner_gate(_Update(555000)) is True
    # An admin (env or granted) is STILL not the owner.
    monkeypatch.setattr(auth, "ADMIN_IDS", [777000])
    assert _owner_gate(_Update(777000)) is False
    assert _owner_gate(_Update(1)) is False


def test_owner_gate_denies_non_private_chat(monkeypatch):
    monkeypatch.setattr(auth, "OWNER_ID", 555000)
    upd = _Update(555000)
    upd.effective_chat.type = "group"
    assert _owner_gate(upd) is False


def test_denied_owner_access_is_audited(monkeypatch):
    init_db()
    monkeypatch.setattr(auth, "OWNER_ID", 555000)
    _owner_gate(_Update(999))
    entries = get_admin_audit(5)
    assert any(e.get("action") == "owner_panel_denied" for e in entries)


def test_ban_stores_reason_and_date():
    init_db()
    create_user(66100001, "u1", "هدف", "", language="ar")
    ban_user(66100001, "مخالفة قواعد الاستخدام")
    user = get_user(66100001)
    assert user["is_banned"] == 1
    assert user["ban_reason"] == "مخالفة قواعد الاستخدام"
    assert user["banned_at"]
    banned = get_banned_users(50)
    assert any(b["user_id"] == 66100001 for b in banned)
    unban_user(66100001)
    user = get_user(66100001)
    assert user["is_banned"] == 0
    assert user["ban_reason"] == ""


def test_extra_admin_registry_grants_admin_never_owner(monkeypatch):
    init_db()
    monkeypatch.setattr(auth, "OWNER_ID", 555000)
    monkeypatch.setattr(auth, "ADMIN_IDS", [])
    add_extra_admin(66200002)
    assert is_extra_admin(66200002)
    assert auth.is_admin(66200002) is True
    assert auth.is_owner(66200002) is False  # ownership can never be granted
    remove_extra_admin(66200002)
    assert auth.is_admin(66200002) is False


def test_referral_target_override_roundtrip():
    init_db()
    from services import referral as R
    original = R.get_effective_target()
    try:
        R.set_target_override(42)
        assert R.get_effective_target() == 42
        assert get_setting("referral_target") == "42"
    finally:
        R.set_target_override(None)
        delete_setting("referral_target")
    assert R.get_effective_target() == original
