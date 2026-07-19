from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import (
    get_banned_users, get_reports,
    get_suspicious_users, get_spam_reporters,
    get_admin_lang,
)
from utils.logger import error_logger


def _t(lang: str, key: str) -> str:
    strings = {
        "sec_title":          {"en": "🔒 <b>Security</b>\n\nMonitor and manage security:",
                               "ar": "🔒 <b>الأمان</b>\n\nمراقبة وإدارة الأمان:"},
        "sec_banned":         {"en": "🚫 Banned Users",        "ar": "🚫 المحظورون"},
        "sec_reports":        {"en": "📋 Abuse Reports",       "ar": "📋 بلاغات الإساءة"},
        "sec_susp":           {"en": "⚠️ Suspicious Activity", "ar": "⚠️ نشاط مشبوه"},
        "sec_spam":           {"en": "🔁 Spam Reporters",      "ar": "🔁 المُبلِّغون المتكررون"},
        "btn_refresh":        {"en": "🔄 Refresh",             "ar": "🔄 تحديث"},
        "btn_back":           {"en": "⬅️ Back",                "ar": "⬅️ رجوع"},
        "banned_title_empty": {"en": "🚫 <b>Banned Users</b>\n\n✅ No banned users.",
                               "ar": "🚫 <b>المحظورون</b>\n\n✅ لا يوجد مستخدمون محظورون."},
        "banned_title":       {"en": "🚫 <b>Banned Users ({count})</b>\n",
                               "ar": "🚫 <b>المحظورون ({count})</b>\n"},
        "reports_empty":      {"en": "📋 <b>Abuse Reports</b>\n\n✅ No open reports.",
                               "ar": "📋 <b>بلاغات الإساءة</b>\n\n✅ لا توجد بلاغات مفتوحة."},
        "reports_title":      {"en": "📋 <b>Open Abuse Reports ({count})</b>\n",
                               "ar": "📋 <b>البلاغات المفتوحة ({count})</b>\n"},
        "susp_empty":         {"en": "⚠️ <b>Suspicious Activity</b>\n\n✅ No suspicious activity detected.",
                               "ar": "⚠️ <b>نشاط مشبوه</b>\n\n✅ لم يُرصد أي نشاط مشبوه."},
        "susp_title":         {"en": "⚠️ <b>Suspicious Activity (last 24h)</b>\n",
                               "ar": "⚠️ <b>نشاط مشبوه (آخر 24 ساعة)</b>\n"},
        "susp_item":          {"en": "• <code>{uid}</code> — <b>{cnt}</b> downloads in 24h",
                               "ar": "• <code>{uid}</code> — <b>{cnt}</b> تحميل في 24 ساعة"},
        "spam_empty":         {"en": "🔁 <b>Spam Reporters</b>\n\n✅ No repeat reporters.",
                               "ar": "🔁 <b>المُبلِّغون المتكررون</b>\n\n✅ لا يوجد مُبلِّغون متكررون."},
        "spam_title":         {"en": "🔁 <b>Repeat Reporters ({count})</b>\n",
                               "ar": "🔁 <b>المُبلِّغون المتكررون ({count})</b>\n"},
        "spam_item":          {"en": "• <code>{uid}</code> ({uname}) — <b>{cnt}</b> reports",
                               "ar": "• <code>{uid}</code> ({uname}) — <b>{cnt}</b> بلاغ"},
    }
    entry = strings.get(key, {})
    return entry.get(lang, entry.get("en", key))


def security_menu_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(_t(lang, "sec_banned"),  callback_data="adm_sec_banned"),
            InlineKeyboardButton(_t(lang, "sec_reports"), callback_data="adm_sec_reports"),
        ],
        [
            InlineKeyboardButton(_t(lang, "sec_susp"),    callback_data="adm_sec_susp"),
            InlineKeyboardButton(_t(lang, "sec_spam"),    callback_data="adm_sec_spam"),
        ],
        [InlineKeyboardButton(_t(lang, "btn_back"), callback_data="adm_menu")],
    ])


async def show_security_menu(query, context):
    lang = get_admin_lang(query.from_user.id)
    await query.edit_message_text(
        _t(lang, "sec_title"),
        parse_mode="HTML",
        reply_markup=security_menu_keyboard(lang)
    )


async def show_banned_users(query, context):
    lang  = get_admin_lang(query.from_user.id)
    users = get_banned_users(limit=20)
    if not users:
        text = _t(lang, "banned_title_empty")
    else:
        title = _t(lang, "banned_title").format(count=len(users))
        lines = [title]
        for u in users:
            uname = f"@{u['username']}" if u.get("username") else ("no username" if lang == "en" else "بدون معرف")
            name  = u.get("first_name", "?")
            lines.append(f"• <code>{u['user_id']}</code> — {name} ({uname})")
        text = "\n".join(lines)

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(_t(lang, "btn_refresh"), callback_data="adm_sec_banned")],
            [InlineKeyboardButton(_t(lang, "btn_back"),    callback_data="adm_sec")],
        ])
    )


async def show_abuse_reports(query, context):
    lang    = get_admin_lang(query.from_user.id)
    reports = get_reports(status="open", limit=20)
    if not reports:
        text = _t(lang, "reports_empty")
    else:
        title = _t(lang, "reports_title").format(count=len(reports))
        lines = [title]
        for r in reports:
            uname = f"@{r.get('username','?')}"
            plat  = r.get("platform", "?")
            date  = (r.get("created_at") or "")[:10]
            msg   = (r.get("message") or "")[:60]
            lines.append(
                f"🔴 <b>#{r['id']}</b> | {uname} | {plat} | {date}\n"
                f"   <i>{msg}</i>\n"
            )
        text = "\n".join(lines)

    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(_t(lang, "btn_refresh"), callback_data="adm_sec_reports")],
            [InlineKeyboardButton(_t(lang, "btn_back"),    callback_data="adm_sec")],
        ])
    )


async def show_suspicious_activity(query, context):
    lang  = get_admin_lang(query.from_user.id)
    users = get_suspicious_users(limit=10)
    if not users:
        text = _t(lang, "susp_empty")
    else:
        lines = [_t(lang, "susp_title")]
        for u in users:
            lines.append(_t(lang, "susp_item").format(uid=u["user_id"], cnt=u["cnt"]))
        text = "\n".join(lines)

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(_t(lang, "btn_refresh"), callback_data="adm_sec_susp")],
            [InlineKeyboardButton(_t(lang, "btn_back"),    callback_data="adm_sec")],
        ])
    )


async def show_spam_reporters(query, context):
    lang  = get_admin_lang(query.from_user.id)
    users = get_spam_reporters(limit=10)
    if not users:
        text = _t(lang, "spam_empty")
    else:
        title = _t(lang, "spam_title").format(count=len(users))
        lines = [title]
        for u in users:
            uname = f"@{u.get('username','?')}"
            lines.append(_t(lang, "spam_item").format(
                uid=u["user_id"], uname=uname, cnt=u["cnt"]
            ))
        text = "\n".join(lines)

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(_t(lang, "btn_refresh"), callback_data="adm_sec_spam")],
            [InlineKeyboardButton(_t(lang, "btn_back"),    callback_data="adm_sec")],
        ])
    )
