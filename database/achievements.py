from .db import db_cursor
from config.settings import ACHIEVEMENTS, get_achievement_name


def get_user_achievements(user_id: int) -> list[str]:
    with db_cursor() as c:
        c.execute("SELECT achievement_id FROM achievements WHERE user_id = ?", (user_id,))
        return [row["achievement_id"] for row in c.fetchall()]


def award_achievement(user_id: int, achievement_id: str) -> bool:
    with db_cursor() as c:
        try:
            c.execute("""
                INSERT OR IGNORE INTO achievements (user_id, achievement_id)
                VALUES (?, ?)
            """, (user_id, achievement_id))
            return c.rowcount > 0
        except Exception:
            return False


def check_and_award(user_id: int, downloads: int, referrals: int,
                    lang: str = "en", wheel_spun: bool = False) -> list[str]:
    existing = get_user_achievements(user_id)
    newly_earned = []
    for ach in ACHIEVEMENTS:
        if ach["id"] in existing:
            continue
        earned = False
        if ach["condition"] == "downloads" and downloads >= ach["threshold"]:
            earned = True
        elif ach["condition"] == "referrals" and referrals >= ach["threshold"]:
            earned = True
        elif ach["condition"] == "wheel" and wheel_spun:
            # Check if user has ever spun the wheel (last_wheel is set)
            with db_cursor() as c:
                c.execute("SELECT last_wheel FROM users WHERE user_id = ?", (user_id,))
                row = c.fetchone()
                if row and row["last_wheel"]:
                    earned = True

        if earned:
            if award_achievement(user_id, ach["id"]):
                newly_earned.append(get_achievement_name(ach, lang))
    return newly_earned
