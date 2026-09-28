from __future__ import annotations

from config.settings import ADMIN_IDS, OWNER_ID
from database.users import get_user


def is_admin(user_id: int | None) -> bool:
    return bool(user_id) and int(user_id) in set([OWNER_ID, *ADMIN_IDS])


def is_banned(user_id: int | None) -> bool:
    if not user_id:
        return True
    try:
        user = get_user(int(user_id))
        return bool(user and user.get("is_banned"))
    except Exception:
        return False
