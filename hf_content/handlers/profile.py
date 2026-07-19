import random
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database.users import get_user, claim_daily, add_points
from database.achievements import get_user_achievements, check_and_award
from middlewares.subscription_gate import require_subscription
from middlewares.auth import is_admin
from locales import t
from config.settings import ACHIEVEMENTS, POINTS_DAILY, LUCKY_WHEEL_PRIZES, LUCKY_WHEEL_COOLDOWN, get_achievement_name
from utils.helpers import get_level
from database.db import db_cursor


def _claim_wheel(user_id: int) -> tuple[bool, int, int, int]:
    """Try to spin the wheel. Returns (success, hours_left, minutes_left, prize_won)."""
    with db_cursor() as c:
        c.execute("SELECT last_wheel FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row and row["last_wheel"]:
            try:
                last = datetime.fromisoformat(row["last_wheel"])
            except Exception:
                last = None
            if last:
                diff = datetime.utcnow() - last
                if diff.total_seconds() < LUCKY_WHEEL_COOLDOWN:
                    remaining = LUCKY_WHEEL_COOLDOWN - diff.total_seconds()
                    hours   = int(remaining // 3600)
                    minutes = int((remaining % 3600) // 60)
                    return False, hours, minutes, 0

        prize = random.choice(LUCKY_WHEEL_PRIZES)
        c.execute(
            "UPDATE users SET last_wheel = datetime('now'), points = points + ? WHERE user_id = ?",
            (prize, user_id)
        )
        c.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
        total = c.fetchone()["points"]
        return True, 0, 0, prize


@require_subscription
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    join_date = str(db_user.get("join_date", ""))[:10]
    points = db_user.get("points", 0)
    level = get_level(points, lang)

    from database.downloads import get_user_download_stats
    dl_stats = get_user_download_stats(user.id)
    earned_ids = get_user_achievements(user.id)

    text = t(lang, "profile_text",
             user_id=user.id,
             downloads=db_user.get("downloads", 0),
             video_downloads=dl_stats["video"],
             audio_downloads=dl_stats["audio"],
             referrals=db_user.get("referrals", 0),
             points=points,
             level=level,
             achievements=len(earned_ids),
             join_date=join_date)

    if is_admin(user.id):
        text += t(lang, "profile_admin_badge")

    keyboard = [
        [
            InlineKeyboardButton(t(lang, "profile_history_btn"),   callback_data="prof_history"),
            InlineKeyboardButton(t(lang, "profile_favorites_btn"), callback_data="prof_favorites"),
        ],
        [
            InlineKeyboardButton(t(lang, "profile_wheel_btn"),     callback_data="prof_wheel"),
            InlineKeyboardButton(t(lang, "settings_lang_btn"),     callback_data="settings_lang"),
        ],
    ]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    from services.subscription import check_subscription
    subscribed = await check_subscription(context.bot, user.id)
    if not subscribed:
        await query.answer(t(lang, "not_subscribed"), show_alert=True)
        return

    await query.answer()

    if query.data == "prof_history":
        from database.downloads import get_user_history
        history = get_user_history(user.id, 10)
        if not history:
            await query.message.reply_text(t(lang, "history_empty"))
            return
        text = t(lang, "history_title")
        for item in history:
            date = str(item.get("created_at", ""))[:10]
            text += t(lang, "history_item",
                      title=item.get("title", "Unknown")[:40],
                      platform=item.get("platform", ""),
                      quality=item.get("quality", ""),
                      date=date)
        await query.message.reply_text(text, parse_mode="HTML")

    elif query.data == "prof_favorites":
        from database.favorites import get_favorites
        from utils.helpers import truncate_title
        favs = get_favorites(user.id)
        if not favs:
            await query.message.reply_text(t(lang, "favorites_empty"))
            return
        text = t(lang, "favorites_title")
        for i, fav in enumerate(favs[:10]):
            title = truncate_title(fav.get("title", "Unknown"), 35)
            platform = fav.get("platform", "")
            date = str(fav.get("added_at", ""))[:10]
            text += f"{i+1}. <b>{title}</b> — {platform} ({date})\n"
        await query.message.reply_text(text, parse_mode="HTML")

    elif query.data == "prof_wheel":
        await _handle_wheel_spin(query, lang, user.id)


async def _handle_wheel_spin(query, lang: str, user_id: int):
    """Execute lucky wheel spin from callback."""
    success, hours, minutes, prize = _claim_wheel(user_id)
    if not success:
        await query.message.reply_text(
            t(lang, "wheel_cooldown", hours=hours, minutes=minutes)
        )
        return

    fresh = get_user(user_id)
    total = fresh.get("points", 0) if fresh else 0

    if prize == 10:
        msg = t(lang, "wheel_jackpot", total=total)
    else:
        msg = t(lang, "wheel_win", points=prize, total=total)

    await query.message.reply_text(msg, parse_mode="HTML")

    # Award wheel achievement if first time
    check_and_award(user_id,
                    fresh.get("downloads", 0) if fresh else 0,
                    fresh.get("referrals", 0) if fresh else 0)


@require_subscription
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    from database.downloads import get_user_history
    history = get_user_history(user.id, 10)

    if not history:
        await update.message.reply_text(t(lang, "history_empty"))
        return

    text = t(lang, "history_title")
    for item in history:
        date = str(item.get("created_at", ""))[:10]
        text += t(lang, "history_item",
                  title=item.get("title", "Unknown")[:40],
                  platform=item.get("platform", ""),
                  quality=item.get("quality", ""),
                  date=date)

    await update.message.reply_text(text, parse_mode="HTML")


@require_subscription
async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    code = db_user.get("referral_code", "")
    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=REF_{code}"

    from database.referrals import get_referrer_stats, get_referral_history
    from config.settings import POINTS_REFERRAL
    stats = get_referrer_stats(user.id)
    history = get_referral_history(user.id, 5)

    history_text = ""
    if history:
        history_text = "\n\n" + t(lang, "referral_history_title")
        for entry in history:
            name = entry.get("first_name") or entry.get("username") or f"#{entry['referred_id']}"
            if entry["status"] == "completed":
                history_text += t(lang, "referral_history_done", name=name[:20])
            else:
                history_text += t(lang, "referral_history_pending", name=name[:20])
    else:
        history_text = "\n\n" + t(lang, "referral_history_empty")

    main_text = t(lang, "referral_text",
                  link=ref_link,
                  count=stats["completed"],
                  points_per_referral=POINTS_REFERRAL)
    stats_text = t(lang, "referral_stats",
                   total=stats["total"],
                   completed=stats["completed"],
                   pending=stats["pending"])

    share_url = f"https://t.me/share/url?url={ref_link}&text=Join PrimeDownloader!"
    keyboard = [[
        InlineKeyboardButton(t(lang, "share_button"), url=share_url)
    ]]

    await update.message.reply_text(
        main_text + "\n\n" + stats_text + history_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


@require_subscription
async def points_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    from config.settings import REWARDS
    rewards_text = ""
    keyboard = []
    for cost, reward in REWARDS.items():
        if reward["type"] == "vip":
            continue
        rewards_text += f"• {cost} pts → {reward['name']}\n"
        keyboard.append([InlineKeyboardButton(
            t(lang, "redeem_button", cost=cost, name=reward["name"]),
            callback_data=f"redeem_{cost}"
        )])

    await update.message.reply_text(
        t(lang, "points_text", points=db_user.get("points", 0), rewards=rewards_text),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def redeem_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    from services.subscription import check_subscription
    subscribed = await check_subscription(context.bot, user.id)
    if not subscribed:
        await query.answer(t(lang, "not_subscribed"), show_alert=True)
        return

    cost_str = query.data.replace("redeem_", "")
    if not cost_str.isdigit():
        await query.answer()
        return
    cost = int(cost_str)

    from config.settings import REWARDS
    from database.users import deduct_points, set_vip
    from database.db import db_cursor

    reward = REWARDS.get(cost)
    if not reward:
        await query.answer()
        return

    if reward["type"] == "vip":
        await query.answer("⏳ VIP rewards are coming soon. Stay tuned!", show_alert=True)
        return

    live_user = get_user(user.id)
    current_points = live_user.get("points", 0) if live_user else 0
    if current_points < cost:
        await query.answer(t(lang, "redeem_not_enough", cost=cost, have=current_points), show_alert=True)
        return

    success = deduct_points(user.id, cost)
    if not success:
        refetched = get_user(user.id)
        actual = refetched.get("points", 0) if refetched else 0
        await query.answer(t(lang, "redeem_not_enough", cost=cost, have=actual), show_alert=True)
        return

    await query.answer()

    if reward["type"] == "vip":
        set_vip(user.id, reward["value"])
    elif reward["type"] == "points":
        add_points(user.id, reward["value"])

    with db_cursor() as c:
        c.execute(
            "INSERT INTO rewards_log (user_id, reward_cost, reward_name) VALUES (?, ?, ?)",
            (user.id, cost, reward["name"])
        )

    await query.edit_message_text(t(lang, "redeem_success", name=reward["name"]), parse_mode="HTML")


@require_subscription
async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    success, hours, total = claim_daily(user.id)
    if success:
        await update.message.reply_text(
            t(lang, "daily_claimed", points=POINTS_DAILY, total=total),
            parse_mode="HTML"
        )
    else:
        minutes = total
        await update.message.reply_text(
            t(lang, "daily_already", hours=hours, minutes=minutes)
        )


@require_subscription
async def wheel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lucky Wheel command — /wheel."""
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    keyboard = [[
        InlineKeyboardButton(t(lang, "wheel_spin_btn"), callback_data="wheel_spin")
    ]]
    await update.message.reply_text(
        t(lang, "wheel_spin"),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def wheel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle wheel spin button."""
    query = update.callback_query
    user = query.from_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    from services.subscription import check_subscription
    subscribed = await check_subscription(context.bot, user.id)
    if not subscribed:
        await query.answer(t(lang, "not_subscribed"), show_alert=True)
        return

    await query.answer(t(lang, "wheel_spinning"))
    await _handle_wheel_spin(query, lang, user.id)


@require_subscription
async def achievements_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    earned_ids = get_user_achievements(user.id)
    text = t(lang, "achievements_title")
    for ach in ACHIEVEMENTS:
        icon = "✅" if ach["id"] in earned_ids else "🔒"
        name = get_achievement_name(ach, lang)
        text += f"{icon} {name}\n"

    await update.message.reply_text(text, parse_mode="HTML")


@require_subscription
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    keyboard = [[
        InlineKeyboardButton(t(lang, "weekly_tab"), callback_data="lb_weekly"),
        InlineKeyboardButton(t(lang, "monthly_tab"), callback_data="lb_monthly"),
    ]]

    from database.users import get_top_referrers
    users = get_top_referrers(period="all", limit=10)
    text = t(lang, "leaderboard_title")
    if not users:
        text += t(lang, "leaderboard_empty")
    else:
        for i, u in enumerate(users, 1):
            name = u.get("first_name") or u.get("username") or f"User{u['user_id']}"
            text += t(lang, "leaderboard_item", rank=i, name=name, count=u["referrals"])

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    from services.subscription import check_subscription
    subscribed = await check_subscription(context.bot, user.id)
    if not subscribed:
        await query.answer(t(lang, "not_subscribed"), show_alert=True)
        return

    await query.answer()

    period = query.data.replace("lb_", "")
    from database.users import get_top_referrers
    users = get_top_referrers(period=period, limit=10)

    text = t(lang, "leaderboard_title")
    if not users:
        text += t(lang, "leaderboard_empty")
    else:
        for i, u in enumerate(users, 1):
            name = u.get("first_name") or u.get("username") or f"User{u['user_id']}"
            text += t(lang, "leaderboard_item", rank=i, name=name, count=u["referrals"])

    keyboard = [[
        InlineKeyboardButton(t(lang, "weekly_tab"), callback_data="lb_weekly"),
        InlineKeyboardButton(t(lang, "monthly_tab"), callback_data="lb_monthly"),
    ]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


@require_subscription
async def goals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    from database.users import get_total_users
    from config.settings import COMMUNITY_GOALS
    total = get_total_users()

    goals_text = ""
    next_done = False
    for goal in COMMUNITY_GOALS:
        th = goal["threshold"]
        rw = goal["reward"]
        if total >= th:
            goals_text += t(lang, "goal_item_done", threshold=th, reward=rw)
        elif not next_done:
            goals_text += t(lang, "goal_item_next", threshold=th, reward=rw, remaining=th - total)
            next_done = True
        else:
            goals_text += t(lang, "goal_item_future", threshold=th, reward=rw)

    await update.message.reply_text(
        t(lang, "community_goals", total=total, goals=goals_text),
        parse_mode="HTML"
    )


@require_subscription
async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    from database.downloads import get_top_downloads
    items = get_top_downloads(10)

    if not items:
        await update.message.reply_text(t(lang, "top_downloads_empty"))
        return

    text = t(lang, "top_downloads_title")
    for i, item in enumerate(items, 1):
        title    = item.get("title", "Unknown")[:50]
        platform = item.get("platform", "")
        count    = item.get("download_count", 0)
        text += t(lang, "top_downloads_item", rank=i, title=title,
                  count=count, platform=platform)

    await update.message.reply_text(text, parse_mode="HTML")
