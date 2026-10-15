from __future__ import annotations

from config.settings import ADMIN_IDS, OWNER_ID
from database.users import get_user


def is_admin(user_id: int | None) -> bool:
    """Admin = env OWNER/ADMIN_IDS + admins granted at runtime by the owner.

    The runtime registry (``extra_admins``) can only ever grant the ADMIN
    role — ownership itself is fixed by env and can never be granted here.
    """
    if not user_id:
        return False
    if int(user_id) in set([OWNER_ID, *ADMIN_IDS]):
        return True
    try:
        from database.bot_settings import is_extra_admin
        return is_extra_admin(int(user_id))
    except Exception:
        return False


def is_owner(user_id: int | None) -> bool:
    """Owner-only gate. FIXED to env OWNER_ID — never grantable at runtime."""
    return bool(user_id) and int(user_id) == int(OWNER_ID)


def is_banned(user_id: int | None) -> bool:
    if not user_id:
        return True
    try:
        user = get_user(int(user_id))
        return bool(user and user.get("is_banned"))
    except Exception:
        return False
