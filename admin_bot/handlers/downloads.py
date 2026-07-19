from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import (
    get_recent_downloads, get_incomplete_downloads,
    get_platform_stats, get_top_content,
    get_admin_lang,
)
from utils.logger import error_logger

PLATFORM_EMOJI = {
    "youtube": "▶️", "tiktok": "🎵", "instagram": "📸",
    "twitter": "🐦", "facebook": "👤", "reddit": "🤖",
    "soundcloud": "🎶", "vimeo": "🎬", "pinterest": "📌",
}


def _t(lang: str, key: str) -> str:
    strings = {
        "dl_menu_title":    {"en": "📥 <b>Downloads</b>\n\nSelect a view:",
                             "ar": "📥 <b>التحميلات</b>\n\nاختر طريقة العرض:"},
        "btn_last":         {"en": "🕐 Last 50 Downloads",  "ar": "🕐 آخر 50 تحميل"},
        "btn_fail":         {"en": "⚠️ Incomplete",         "ar": "⚠️ غير مكتملة"},
        "btn_plat":         {"en": "📊 By Platform",        "ar": "📊 حسب المنصة"},
        "btn_top":          {"en": "🔥 Top Content",        "ar": "🔥 الأكثر تحميلاً"},
        "btn_refresh":      {"en": "🔄 Refresh",            "ar": "🔄 تحديث"},
        "btn_back":         {"en": "⬅️ Back",               "ar": "⬅️ رجوع"},
        "last_empty":       {"en": "📥 <b>Last 50 Downloads</b>\n\n📭 No downloads yet.",
                             "ar": "📥 <b>آخر 50 تحميل</b>\n\n📭 لا توجد تحميلات بعد."},
        "last_title":       {"en": "📥 <b>Last {n} Downloads</b>\n",
                             "ar": "📥 <b>آخر {n} تحميل</b>\n"},
        "fail_empty":       {"en": "⚠️ <b>Incomplete / Failed Downloads</b>\n\n✅ None found.",
                             "ar": "⚠️ <b>التحميلات غير المكتملة</b>\n\n✅ لا توجد."},
        "fail_title":       {"en": "⚠️ <b>Incomplete Downloads ({n})</b>\n",
                             "ar": "⚠️ <b>تحميلات غير مكتملة ({n})</b>\n"},
        "plat_empty":       {"en": "📊 <b>Downloads by Platform</b>\n\n📭 No data.",
                             "ar": "📊 <b>التحميلات حسب المنصة</b>\n\n📭 لا توجد بيانات."},
        "plat_title":       {"en": "📊 <b>Downloads by Platform</b>\n",
                             "ar": "📊 <b>التحميلات حسب المنصة</b>\n"},
        "plat_total":       {"en": "\n📦 Total: <b>{n}</b>",
                             "ar": "\n📦 الإجمالي: <b>{n}</b>"},
        "top_empty":        {"en": "🔥 <b>Most Downloaded Content</b>\n\n📭 No data.",
                             "ar": "🔥 <b>الأكثر تحميلاً</b>\n\n📭 لا توجد بيانات."},
        "top_title":        {"en": "🔥 <b>Most Downloaded Content</b>\n",
                             "ar": "🔥 <b>الأكثر تحميلاً</b>\n"},
    }
    entry = strings.get(key, {})
    return entry.get(lang, entry.get("en", key))


def downloads_menu_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(_t(lang, "btn_last"), callback_data="adm_dl_last"),
            InlineKeyboardButton(_t(lang, "btn_fail"), callback_data="adm_dl_fail"),
        ],
        [
            InlineKeyboardButton(_t(lang, "btn_plat"), callback_data="adm_dl_plat"),
            InlineKeyboardButton(_t(lang, "btn_top"),  callback_data="adm_dl_top"),
        ],
        [InlineKeyboardButton(_t(lang, "btn_back"), callback_data="adm_menu")],
    ])


async def show_downloads_menu(query, context):
    lang = get_admin_lang(query.from_user.id)
    await query.edit_message_text(
        _t(lang, "dl_menu_title"),
        parse_mode="HTML",
        reply_markup=downloads_menu_keyboard(lang)
    )


async def show_recent_downloads(query, context):
    lang = get_admin_lang(query.from_user.id)
    rows = get_recent_downloads(limit=50)
    if not rows:
        text = _t(lang, "last_empty")
    else:
        lines = [_t(lang, "last_title").format(n=len(rows))]
        for d in rows[:50]:
            uname = f"@{d['username']}" if d.get("username") else f"uid:{d['user_id']}"
            title = (d.get("title") or "?")[:35]
            plat  = (d.get("platform") or "?").lower()
            emoji = PLATFORM_EMOJI.get(plat, "🔗")
            date  = (d.get("created_at") or "")[:10]
            lines.append(f"{emoji} <b>{title}</b>  — {uname}  [{date}]")
        text = "\n".join(lines)

    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(_t(lang, "btn_refresh"), callback_data="adm_dl_last")],
            [InlineKeyboardButton(_t(lang, "btn_back"),    callback_data="adm_dl")],
        ])
    )


async def show_incomplete_downloads(query, context):
    lang = get_admin_lang(query.from_user.id)
    rows = get_incomplete_downloads(limit=30)
    if not rows:
        text = _t(lang, "fail_empty")
    else:
        lines = [_t(lang, "fail_title").format(n=len(rows))]
        for d in rows:
            uname = f"@{d['username']}" if d.get("username") else f"uid:{d['user_id']}"
            url   = (d.get("url") or "?")[:50]
            date  = (d.get("created_at") or "")[:10]
            lines.append(f"• {uname} — <code>{url}</code> [{date}]")
        text = "\n".join(lines)

    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(_t(lang, "btn_back"), callback_data="adm_dl")]
        ])
    )


async def show_platform_stats(query, context):
    lang = get_admin_lang(query.from_user.id)
    rows = get_platform_stats()
    if not rows:
        text = _t(lang, "plat_empty")
    else:
        total = sum(r["cnt"] for r in rows)
        lines = [_t(lang, "plat_title")]
        for r in rows:
            plat  = (r.get("platform") or "unknown").lower()
            emoji = PLATFORM_EMOJI.get(plat, "🔗")
            pct   = (r["cnt"] / total * 100) if total else 0
            bar   = "█" * int(pct / 5)
            lines.append(f"{emoji} <b>{plat.capitalize()}</b>: {r['cnt']:,} ({pct:.1f}%) {bar}")
        lines.append(_t(lang, "plat_total").format(n=f"{total:,}"))
        text = "\n".join(lines)

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(_t(lang, "btn_refresh"), callback_data="adm_dl_plat")],
            [InlineKeyboardButton(_t(lang, "btn_back"),    callback_data="adm_dl")],
        ])
    )


async def show_top_content(query, context):
    lang = get_admin_lang(query.from_user.id)
    rows = get_top_content(limit=20)
    if not rows:
        text = _t(lang, "top_empty")
    else:
        lines = [_t(lang, "top_title")]
        for i, r in enumerate(rows, 1):
            plat  = (r.get("platform") or "?").lower()
            emoji = PLATFORM_EMOJI.get(plat, "🔗")
            title = (r.get("title") or "?")[:40]
            lines.append(f"{i}. {emoji} <b>{title}</b> — {r['cnt']:,}x")
        text = "\n".join(lines)

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(_t(lang, "btn_back"), callback_data="adm_dl")]
        ])
    )
