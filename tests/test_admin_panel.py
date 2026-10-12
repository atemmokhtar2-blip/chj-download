from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import init_db
from database.users import create_user, get_users_page, get_top_users, get_active_24h
from database.downloads import get_downloads_24h, get_total_data_bytes
from database.admin_audit import log_admin_action, get_admin_audit
from middlewares.auth import is_owner
import handlers.admin as admin_module


def _seed_user(uid: int, first_name: str = "مستخدم", username: str = "user"):
    init_db()
    create_user(uid, username, first_name, "", language="ar")


def test_users_page_returns_real_profile_fields():
    _seed_user(81000001, "احمد", "ahmed_real")
    page = get_users_page(offset=0, limit=20)
    row = next(r for r in page if r["user_id"] == 81000001)
    for key in ("language", "last_seen", "join_date", "first_name", "downloads"):
        assert key in row, f"missing {key}"


def test_top_users_returns_rows_with_downloads():
    init_db()
    rows = get_top_users(limit=5)
    assert isinstance(rows, list)
    for row in rows:
        assert "downloads" in row


def test_user_detail_and_back_button_render():
    _seed_user(81000002, "سارة", "sara_real")
    assert isinstance(admin_module.ADMIN_LAST_PAGE, dict)
    rendered = admin_module._render_user_detail(81000002)
    assert rendered is not None
    text, keyboard = rendered
    assert "ملف المستخدم الكامل" in text
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("رجوع للمستخدمين" in (label or "") for label in labels)


def test_24h_and_data_volume_stats_are_real():
    init_db()
    assert isinstance(get_active_24h(), int)
    assert isinstance(get_downloads_24h(), int)
    assert isinstance(get_total_data_bytes(), int)


def test_admin_audit_trail_records_actions():
    init_db()
    log_admin_action(123, "ban_user", 81000003, "test")
    entries = get_admin_audit(5)
    assert entries
    assert entries[0]["action"] == "ban_user"


def test_owner_gate_exists():
    from config.settings import OWNER_ID
    assert is_owner(int(OWNER_ID)) is True
    assert is_owner(99999999) is False


if __name__ == "__main__":
    test_users_page_returns_real_profile_fields()
    test_top_users_returns_rows_with_downloads()
    test_user_detail_and_back_button_render()
    test_24h_and_data_volume_stats_are_real()
    test_admin_audit_trail_records_actions()
    test_owner_gate_exists()
    print("admin panel tests passed")
