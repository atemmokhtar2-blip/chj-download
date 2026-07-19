from database.users import get_user
from config.settings import ADMIN_IDS, OWNER_ID


def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    if user_id in ADMIN_IDS:
        return True
    user = get_user(user_id)
    if user and user.get("role") in ("admin", "moderator", "owner"):
        return True
    return False


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_banned(user_id: int) -> bool:
    user = get_user(user_id)
    return user is not None and bool(user.get("is_banned"))


def get_role(user_id: int) -> str:
    if user_id == OWNER_ID:
        return "owner"
    user = get_user(user_id)
    if user:
        return user.get("role", "user")
    return "guest"
