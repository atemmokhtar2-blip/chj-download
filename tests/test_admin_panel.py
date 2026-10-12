from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import init_db
from database.users import create_user, get_users_page, get_top_users
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


if __name__ == "__main__":
    test_users_page_returns_real_profile_fields()
    test_top_users_returns_rows_with_downloads()
    test_user_detail_and_back_button_render()
    print("admin panel tests passed")
