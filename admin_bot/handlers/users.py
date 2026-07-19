from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from database.db import (
    get_user_by_id, get_users_by_username, get_user_download_count,
    ban_user, unban_user, add_points, remove_points,
    count_all_users, get_all_users_paginated,
    get_admin_lang,
)
from locales import t
from utils.logger import action_logger, error_logger

PAGE_SIZE = 50


def users_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(lang, "btn_search_id"), callback_data="adm_users_sid"),
            InlineKeyboardButton(t(lang, "btn_search_un"), callback_data="adm_users_sun"),
        ],
        [InlineKeyboardButton(t(lang, "btn_browse"),       callback_data="adm_users_list_0")],
        [InlineKeyboardButton(t(lang, "btn_back"),         callback_data="adm_menu")],
    ])


def user_action_keyboard(uid: int, is_banned: bool, lang: str = "en") -> InlineKeyboardMarkup:
    ban_btn = (
        InlineKeyboardButton("✅ Unban" if lang == "en" else "✅ رفع الحظر",
                             callback_data=f"adm_users_unban_{uid}")
        if is_banned else
        InlineKeyboardButton("🚫 Ban" if lang == "en" else "🚫 حظر",
                             callback_data=f"adm_users_ban_{uid}")
    )
    add_lbl = "➕ Add Points" if lang == "en" else "➕ إضافة نقاط"
    rem_lbl = "➖ Remove Points" if lang == "en" else "➖ خصم نقاط"
    back_lbl = t(lang, "btn_back")
    return InlineKeyboardMarkup([
        [ban_btn],
        [
            InlineKeyboardButton(add_lbl, callback_data=f"adm_users_ap_{uid}"),
            InlineKeyboardButton(rem_lbl, callback_data=f"adm_users_rp_{uid}"),
        ],
        [InlineKeyboardButton(back_lbl, callback_data="adm_users")],
    ])


def format_user_profile(u: dict, lang: str = "en") -> str:
    uname     = f"@{u['username']}" if u.get("username") else ("no username" if lang == "en" else "بدون معرف")
    join_date = (u.get("join_date") or "")[:10]
    last_seen = (u.get("last_seen") or "")[:16]
    dl_count  = get_user_download_count(u["user_id"])
    if lang == "ar":
        banned = "🚫 نعم" if u.get("is_banned") else "✅ لا"
        return (
            f"👤 <b>ملف المستخدم</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🆔 ID:          <code>{u['user_id']}</code>\n"
            f"📛 الاسم:       {u.get('first_name', '')} {u.get('last_name', '')}\n"
            f"🔖 المعرف:      {uname}\n"
            f"🌐 اللغة:       {u.get('language', '?')}\n"
            f"🚫 محظور:       {banned}\n"
            f"⭐ النقاط:      {u.get('points', 0):,}\n"
            f"📥 التحميلات:   {dl_count:,}\n"
            f"🔗 الدعوات:     {u.get('referrals', 0):,}\n"
            f"📅 الانضمام:    {join_date}\n"
            f"🕐 آخر ظهور:   {last_seen}\n"
            f"━━━━━━━━━━━━━━━━"
        )
    banned = "🚫 Yes" if u.get("is_banned") else "✅ No"
    return (
        f"👤 <b>User Profile</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🆔 ID:          <code>{u['user_id']}</code>\n"
        f"📛 Name:        {u.get('first_name', '')} {u.get('last_name', '')}\n"
        f"🔖 Username:    {uname}\n"
        f"🌐 Language:    {u.get('language', '?')}\n"
        f"🚫 Banned:      {banned}\n"
        f"⭐ Points:      {u.get('points', 0):,}\n"
        f"📥 Downloads:   {dl_count:,}\n"
        f"🔗 Referrals:   {u.get('referrals', 0):,}\n"
        f"📅 Joined:      {join_date}\n"
        f"🕐 Last Seen:   {last_seen}\n"
        f"━━━━━━━━━━━━━━━━"
    )


async def show_users_menu(query, context):
    context.user_data.pop("adm_state", None)
    uid   = query.from_user.id
    lang  = get_admin_lang(uid)
    total = count_all_users()
    await query.edit_message_text(
        f"{t(lang, 'users_title')}  —  <b>{total:,}</b>\n\n{t(lang, 'users_subtitle')}",
        parse_mode="HTML",
        reply_markup=users_menu_keyboard(lang)
    )


async def show_all_users(query, context, offset: int):
    uid   = query.from_user.id
    lang  = get_admin_lang(uid)
    total = count_all_users()
    users = get_all_users_paginated(offset, PAGE_SIZE)

    page_num   = offset // PAGE_SIZE + 1
    page_total = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    title = "All Users" if lang == "en" else "جميع المستخدمين"
    lines = [f"👥 <b>{title}</b>  —  {page_num}/{page_total}  ({total:,})\n"]
    for u in users:
        uname  = f"@{u['username']}" if u.get("username") else "no @"
        banned = "🚫" if u.get("is_banned") else "✅"
        lines.append(f"{banned} <code>{u['user_id']}</code> — {u.get('first_name', '')} {uname}")

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            t(lang, "btn_prev"), callback_data=f"adm_users_list_{offset - PAGE_SIZE}"
        ))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(
            t(lang, "btn_next"), callback_data=f"adm_users_list_{offset + PAGE_SIZE}"
        ))

    rows = []
    if nav:
        rows.append(nav)
    for u in users:
        label = f"{u.get('first_name', '')} @{u['username']}" if u.get("username") else f"{u.get('first_name', str(u['user_id']))}"
        rows.append([InlineKeyboardButton(label, callback_data=f"adm_users_view_{u['user_id']}")])

    rows.append([InlineKeyboardButton(t(lang, "btn_back"), callback_data="adm_users")])

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…"

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows[:50])
    )


async def prompt_search_id(query, context):
    uid  = query.from_user.id
    lang = get_admin_lang(uid)
    context.user_data["adm_state"] = "search_id"
    prompt = "🔍 <b>Search by User ID</b>\n\nSend the user ID:" if lang == "en" else "🔍 <b>بحث بالـ ID</b>\n\nأرسل الـ ID:"
    await query.edit_message_text(
        prompt, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="adm_users")
        ]])
    )


async def prompt_search_username(query, context):
    uid  = query.from_user.id
    lang = get_admin_lang(uid)
    context.user_data["adm_state"] = "search_un"
    prompt = "🔍 <b>Search by Username</b>\n\nSend the username:" if lang == "en" else "🔍 <b>بحث باليوزرنيم</b>\n\nأرسل اليوزرنيم:"
    await query.edit_message_text(
        prompt, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="adm_users")
        ]])
    )


async def handle_search_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    lang = get_admin_lang(uid)
    text = update.message.text.strip()
    if not text.isdigit():
        msg = "❌ Please send a valid numeric user ID." if lang == "en" else "❌ أرسل رقم ID صحيح."
        await update.message.reply_text(msg)
        return
    target_uid = int(text)
    user = get_user_by_id(target_uid)
    context.user_data.pop("adm_state", None)
    if not user:
        not_found = f"❌ User ID <code>{target_uid}</code> not found." if lang == "en" else f"❌ المستخدم <code>{target_uid}</code> غير موجود."
        await update.message.reply_text(
            not_found, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(lang, "btn_back"), callback_data="adm_users")
            ]])
        )
        return
    await update.message.reply_text(
        format_user_profile(user, lang), parse_mode="HTML",
        reply_markup=user_action_keyboard(target_uid, bool(user.get("is_banned")), lang)
    )


async def handle_search_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    lang  = get_admin_lang(uid)
    text  = update.message.text.strip()
    users = get_users_by_username(text)
    context.user_data.pop("adm_state", None)
    if not users:
        not_found = f"❌ No users found matching <code>{text}</code>." if lang == "en" else f"❌ لا يوجد مستخدمون مطابقون لـ <code>{text}</code>."
        await update.message.reply_text(
            not_found, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(lang, "btn_back"), callback_data="adm_users")
            ]])
        )
        return
    if len(users) == 1:
        u = users[0]
        await update.message.reply_text(
            format_user_profile(u, lang), parse_mode="HTML",
            reply_markup=user_action_keyboard(u["user_id"], bool(u.get("is_banned")), lang)
        )
        return
    title  = "👥 <b>Search Results</b>\n" if lang == "en" else "👥 <b>نتائج البحث</b>\n"
    lines  = [title]
    buttons = []
    for u in users:
        uname = f"@{u['username']}" if u.get("username") else "no username"
        lines.append(f"• <code>{u['user_id']}</code> — {u.get('first_name', '')} ({uname})")
        buttons.append([InlineKeyboardButton(
            f"{u.get('first_name', '')} ({uname})",
            callback_data=f"adm_users_view_{u['user_id']}"
        )])
    buttons.append([InlineKeyboardButton(t(lang, "btn_back"), callback_data="adm_users")])
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_user_profile(query, context, uid: int):
    admin_lang = get_admin_lang(query.from_user.id)
    user = get_user_by_id(uid)
    if not user:
        not_found = f"❌ User <code>{uid}</code> not found." if admin_lang == "en" else f"❌ المستخدم <code>{uid}</code> غير موجود."
        await query.edit_message_text(
            not_found, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(admin_lang, "btn_back"), callback_data="adm_users")
            ]])
        )
        return
    await query.edit_message_text(
        format_user_profile(user, admin_lang), parse_mode="HTML",
        reply_markup=user_action_keyboard(uid, bool(user.get("is_banned")), admin_lang)
    )


async def do_ban(query, context, uid: int):
    lang = get_admin_lang(query.from_user.id)
    ok   = ban_user(uid)
    action_logger.info("Admin %s BANNED user_id=%s", query.from_user.id, uid)
    text = (f"🚫 User <code>{uid}</code> has been <b>banned</b>." if lang == "en"
            else f"🚫 تم حظر المستخدم <code>{uid}</code>.") if ok else (
            f"❌ Could not ban user <code>{uid}</code>." if lang == "en"
            else f"❌ لم يتم الحظر.")
    user = get_user_by_id(uid)
    kb   = (user_action_keyboard(uid, True, lang) if user
            else InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_back"), callback_data="adm_users")]]))
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def do_unban(query, context, uid: int):
    lang = get_admin_lang(query.from_user.id)
    ok   = unban_user(uid)
    action_logger.info("Admin %s UNBANNED user_id=%s", query.from_user.id, uid)
    text = (f"✅ User <code>{uid}</code> has been <b>unbanned</b>." if lang == "en"
            else f"✅ تم رفع الحظر عن المستخدم <code>{uid}</code>.") if ok else (
            f"❌ Could not unban user <code>{uid}</code>." if lang == "en"
            else f"❌ لم يتم رفع الحظر.")
    user = get_user_by_id(uid)
    kb   = (user_action_keyboard(uid, False, lang) if user
            else InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_back"), callback_data="adm_users")]]))
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def prompt_add_points(query, context, uid: int):
    lang = get_admin_lang(query.from_user.id)
    context.user_data["adm_state"] = f"addpts_{uid}"
    prompt = (f"➕ <b>Add Points</b>\n\nSend points to add to <code>{uid}</code>:" if lang == "en"
              else f"➕ <b>إضافة نقاط</b>\n\nأرسل عدد النقاط للمستخدم <code>{uid}</code>:")
    await query.edit_message_text(
        prompt, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_cancel"), callback_data=f"adm_users_view_{uid}")
        ]])
    )


async def prompt_remove_points(query, context, uid: int):
    lang = get_admin_lang(query.from_user.id)
    context.user_data["adm_state"] = f"rmpts_{uid}"
    prompt = (f"➖ <b>Remove Points</b>\n\nSend points to remove from <code>{uid}</code>:" if lang == "en"
              else f"➖ <b>خصم نقاط</b>\n\nأرسل عدد النقاط لخصمها من <code>{uid}</code>:")
    await query.edit_message_text(
        prompt, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_cancel"), callback_data=f"adm_users_view_{uid}")
        ]])
    )


async def handle_add_points(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    lang = get_admin_lang(update.effective_user.id)
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        err = "❌ Please send a positive integer." if lang == "en" else "❌ أرسل رقماً صحيحاً موجباً."
        await update.message.reply_text(err)
        return
    amount    = int(text)
    new_total = add_points(uid, amount)
    context.user_data.pop("adm_state", None)
    action_logger.info("Admin %s added %s pts to user_id=%s (total=%s)",
                       update.effective_user.id, amount, uid, new_total)
    user = get_user_by_id(uid)
    msg  = (f"✅ Added <b>{amount:,}</b> points to <code>{uid}</code>.\nNew total: <b>{new_total:,}</b>"
            if lang == "en" else
            f"✅ تم إضافة <b>{amount:,}</b> نقطة للمستخدم <code>{uid}</code>.\nالمجموع: <b>{new_total:,}</b>")
    await update.message.reply_text(
        msg, parse_mode="HTML",
        reply_markup=user_action_keyboard(uid, bool(user.get("is_banned")), lang) if user else None
    )


async def handle_remove_points(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    lang = get_admin_lang(update.effective_user.id)
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        err = "❌ Please send a positive integer." if lang == "en" else "❌ أرسل رقماً صحيحاً موجباً."
        await update.message.reply_text(err)
        return
    amount    = int(text)
    new_total = remove_points(uid, amount)
    context.user_data.pop("adm_state", None)
    action_logger.info("Admin %s removed %s pts from user_id=%s (total=%s)",
                       update.effective_user.id, amount, uid, new_total)
    user = get_user_by_id(uid)
    msg  = (f"✅ Removed <b>{amount:,}</b> points from <code>{uid}</code>.\nNew total: <b>{new_total:,}</b>"
            if lang == "en" else
            f"✅ تم خصم <b>{amount:,}</b> نقطة من المستخدم <code>{uid}</code>.\nالمجموع: <b>{new_total:,}</b>")
    await update.message.reply_text(
        msg, parse_mode="HTML",
        reply_markup=user_action_keyboard(uid, bool(user.get("is_banned")), lang) if user else None
    )
