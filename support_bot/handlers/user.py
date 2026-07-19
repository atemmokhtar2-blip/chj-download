from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from database.db import is_main_bot_user
from database.tickets import (
    create_ticket, add_message, get_user_open_ticket,
    get_user_tickets, get_ticket_messages
)
from config.settings import ADMIN_IDS, OWNER_ID
from utils.logger import system_logger, tickets_logger, error_logger

# Try to detect user language from main DB
def _get_user_lang(user_id: int) -> str:
    try:
        from database.users import get_user
        db_user = get_user(user_id)
        if db_user:
            return db_user.get("language", "en")
    except Exception:
        pass
    return "en"


# Bilingual button labels
_BTN_NEW    = {"en": "📩 New Ticket",   "ar": "📩 تذكرة جديدة"}
_BTN_STATUS = {"en": "📋 My Tickets",  "ar": "📋 تذاكري"}


def _t(lang: str, key: str, **kwargs) -> str:
    """Simple bilingual string lookup."""
    STRINGS = {
        "en": {
            "access_denied": (
                "🚫 <b>Access Denied</b>\n\n"
                "You must use the main PrimeDownloader bot first before accessing support.\n"
                "Please start the bot and try again."
            ),
            "welcome_open_ticket": (
                "👋 <b>Welcome back!</b>\n\n"
                "You have open ticket <b>#{ticket_id}</b>.\n"
                "Any message you send will be added to it.\n\n"
                "Use <b>{btn_status}</b> to see all your tickets."
            ),
            "welcome": (
                "👋 <b>Welcome to PrimeDownloader Support</b>\n\n"
                "How can we help you today?\n\n"
                "<b>📋 FAQ:</b>\n"
                "• <b>How to download?</b> — Send any supported link to the main bot.\n"
                "• <b>Supported platforms?</b> — YouTube, TikTok, Instagram, Facebook, Twitter/X, Threads, Reddit, Pinterest, Snapchat, Vimeo, Dailymotion, SoundCloud, Telegram.\n"
                "• <b>How to earn points?</b> — Daily reward (/daily), Lucky Wheel (/wheel), Referrals (/referral).\n"
                "• <b>Each download costs 1 point.</b> You start with 10 free points.\n"
                "• <b>File too large?</b> — Max file size is 50MB via Telegram.\n\n"
                "Tap <b>{btn_new}</b> to open a support ticket."
            ),
            "already_open": (
                "⚠️ You already have open ticket <b>#{ticket_id}</b>.\n\n"
                "Any message you send will be added to it. "
                "Please wait for an admin reply before opening a new one."
            ),
            "new_ticket_prompt": (
                "📝 <b>New Support Ticket</b>\n\n"
                "Please describe your issue in detail.\n"
                "Send /cancel to go back."
            ),
            "empty_message": "❌ Please send a text message.",
            "ticket_created": (
                "✅ <b>Ticket #{ticket_id} Created</b>\n\n"
                "Your message has been sent to our support team.\n"
                "We'll reply here as soon as possible.\n\n"
                "You can continue sending messages — they'll be added to this ticket."
            ),
            "added_to_ticket": "📨 Message added to your open ticket.",
            "no_open_ticket": (
                "💬 You don't have an open ticket.\n\nTap <b>{btn_new}</b> to start one."
            ),
            "cancelled": "❌ Cancelled.",
            "no_tickets": (
                "📭 You have no tickets yet.\n\nTap <b>{btn_new}</b> to open one."
            ),
            "tickets_title": "📋 <b>Your Tickets</b>\n",
            "ticket_line": "{emoji} <b>Ticket #{ticket_id}</b> — {status} — {date}\n   💬 {count} message(s)",
            "admin_new_ticket": (
                "🎫 <b>New Support Ticket #{ticket_id}</b>\n\n"
                "👤 User: {name} ({uname})\n"
                "🆔 User ID: <code>{user_id}</code>\n"
                "🕐 Time: {now}\n\n"
                "📝 <b>Message:</b>\n{text}\n\n"
                "━━━━━━━━━━━━━━━━"
            ),
            "admin_new_message": (
                "💬 <b>New Message on Ticket #{ticket_id}</b>\n\n"
                "👤 {name} ({uname})\n"
                "🆔 <code>{user_id}</code>\n\n"
                "📝 {text}\n\n"
                "━━━━━━━━━━━━━━━━"
            ),
        },
        "ar": {
            "access_denied": (
                "🚫 <b>وصول مرفوض</b>\n\n"
                "يجب عليك استخدام بوت PrimeDownloader الرئيسي أولاً قبل الوصول للدعم.\n"
                "يرجى تشغيل البوت والمحاولة مجدداً."
            ),
            "welcome_open_ticket": (
                "👋 <b>مرحباً بعودتك!</b>\n\n"
                "لديك تذكرة مفتوحة <b>#{ticket_id}</b>.\n"
                "أي رسالة تُرسلها ستُضاف إليها.\n\n"
                "استخدم <b>{btn_status}</b> لرؤية جميع تذاكرك."
            ),
            "welcome": (
                "👋 <b>مرحباً بك في دعم PrimeDownloader</b>\n\n"
                "كيف يمكننا مساعدتك اليوم؟\n\n"
                "<b>📋 الأسئلة الشائعة:</b>\n"
                "• <b>كيف أُحمّل؟</b> — أرسل أي رابط مدعوم للبوت الرئيسي.\n"
                "• <b>المنصات المدعومة؟</b> — YouTube، TikTok، Instagram، Facebook، Twitter/X، Threads، Reddit، Pinterest، Snapchat، Vimeo، Dailymotion، SoundCloud، Telegram.\n"
                "• <b>كيف أكسب نقاطاً؟</b> — المكافأة اليومية (/daily)، عجلة الحظ (/wheel)، الدعوات (/referral).\n"
                "• <b>كل تحميل يكلف نقطة واحدة.</b> تبدأ بـ 10 نقاط مجانية.\n"
                "• <b>الملف كبير جداً؟</b> — الحد الأقصى هو 50MB عبر تيليغرام.\n\n"
                "اضغط <b>{btn_new}</b> لفتح تذكرة دعم."
            ),
            "already_open": (
                "⚠️ لديك تذكرة مفتوحة <b>#{ticket_id}</b>.\n\n"
                "أي رسالة تُرسلها ستُضاف إليها. "
                "يرجى الانتظار حتى رد أحد المشرفين قبل فتح تذكرة جديدة."
            ),
            "new_ticket_prompt": (
                "📝 <b>تذكرة دعم جديدة</b>\n\n"
                "يرجى وصف مشكلتك بالتفصيل.\n"
                "أرسل /cancel للرجوع."
            ),
            "empty_message": "❌ يرجى إرسال رسالة نصية.",
            "ticket_created": (
                "✅ <b>تم إنشاء التذكرة #{ticket_id}</b>\n\n"
                "تم إرسال رسالتك لفريق الدعم.\n"
                "سنرد عليك هنا في أقرب وقت ممكن.\n\n"
                "يمكنك الاستمرار في إرسال الرسائل — ستُضاف إلى هذه التذكرة."
            ),
            "added_to_ticket": "📨 تمت إضافة رسالتك إلى التذكرة المفتوحة.",
            "no_open_ticket": (
                "💬 ليس لديك تذكرة مفتوحة.\n\nاضغط <b>{btn_new}</b> لفتح واحدة."
            ),
            "cancelled": "❌ تم الإلغاء.",
            "no_tickets": (
                "📭 لا توجد تذاكر بعد.\n\nاضغط <b>{btn_new}</b> لفتح تذكرة."
            ),
            "tickets_title": "📋 <b>تذاكرك</b>\n",
            "ticket_line": "{emoji} <b>تذكرة #{ticket_id}</b> — {status} — {date}\n   💬 {count} رسالة",
            "admin_new_ticket": (
                "🎫 <b>تذكرة دعم جديدة #{ticket_id}</b>\n\n"
                "👤 المستخدم: {name} ({uname})\n"
                "🆔 ID: <code>{user_id}</code>\n"
                "🕐 الوقت: {now}\n\n"
                "📝 <b>الرسالة:</b>\n{text}\n\n"
                "━━━━━━━━━━━━━━━━"
            ),
            "admin_new_message": (
                "💬 <b>رسالة جديدة على التذكرة #{ticket_id}</b>\n\n"
                "👤 {name} ({uname})\n"
                "🆔 <code>{user_id}</code>\n\n"
                "📝 {text}\n\n"
                "━━━━━━━━━━━━━━━━"
            ),
        },
    }
    s = STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))
    try:
        return s.format(**kwargs)
    except Exception:
        return s


def _all_admins() -> list[int]:
    return list(set(ADMIN_IDS + ([OWNER_ID] if OWNER_ID else [])))


def _main_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[_BTN_NEW[lang], _BTN_STATUS[lang]]],
        resize_keyboard=True
    )


def _ticket_admin_keyboard(ticket_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📩 Reply", callback_data=f"sup_reply_{ticket_id}_{user_id}"),
        InlineKeyboardButton("✅ Close", callback_data=f"sup_close_{ticket_id}_{user_id}"),
    ]])


async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str,
                          keyboard: InlineKeyboardMarkup | None = None):
    for admin_id in _all_admins():
        try:
            await context.bot.send_message(
                admin_id, text, parse_mode="HTML", reply_markup=keyboard
            )
        except Exception as exc:
            error_logger.error("Failed to notify admin %s: %s", admin_id, exc)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _get_user_lang(user.id)
    btn_new    = _BTN_NEW[lang]
    btn_status = _BTN_STATUS[lang]

    if not is_main_bot_user(user.id):
        await update.message.reply_text(
            _t(lang, "access_denied"),
            parse_mode="HTML"
        )
        system_logger.warning("Blocked unauthorized access: user_id=%s", user.id)
        return

    context.user_data.pop("state", None)

    open_ticket = get_user_open_ticket(user.id)
    if open_ticket:
        ticket_id = open_ticket["id"]
        await update.message.reply_text(
            _t(lang, "welcome_open_ticket", ticket_id=ticket_id, btn_status=btn_status),
            parse_mode="HTML",
            reply_markup=_main_keyboard(lang)
        )
    else:
        await update.message.reply_text(
            _t(lang, "welcome", btn_new=btn_new),
            parse_mode="HTML",
            reply_markup=_main_keyboard(lang)
        )


async def new_ticket_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _get_user_lang(user.id)

    if not is_main_bot_user(user.id):
        await update.message.reply_text(_t(lang, "access_denied"), parse_mode="HTML")
        return

    open_ticket = get_user_open_ticket(user.id)
    if open_ticket:
        await update.message.reply_text(
            _t(lang, "already_open", ticket_id=open_ticket["id"]),
            parse_mode="HTML",
            reply_markup=_main_keyboard(lang)
        )
        return

    context.user_data["state"] = "creating_ticket"
    await update.message.reply_text(
        _t(lang, "new_ticket_prompt"),
        parse_mode="HTML"
    )


async def create_ticket_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _get_user_lang(user.id)

    if not is_main_bot_user(user.id):
        context.user_data.pop("state", None)
        await update.message.reply_text(_t(lang, "access_denied"), parse_mode="HTML")
        system_logger.warning("Blocked ticket creation attempt: user_id=%s", user.id)
        return

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text(_t(lang, "empty_message"))
        return

    context.user_data.pop("state", None)

    ticket_id = create_ticket(
        user_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "",
        message=text
    )

    tickets_logger.info("Ticket #%s created by user_id=%s", ticket_id, user.id)

    uname = f"@{user.username}" if user.username else "no username"
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    admin_text = _t("en", "admin_new_ticket",
                    ticket_id=ticket_id, name=user.first_name,
                    uname=uname, user_id=user.id, now=now, text=text)
    await _notify_admins(context, admin_text, _ticket_admin_keyboard(ticket_id, user.id))

    await update.message.reply_text(
        _t(lang, "ticket_created", ticket_id=ticket_id),
        parse_mode="HTML",
        reply_markup=_main_keyboard(lang)
    )


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _get_user_lang(user.id)
    text = (update.message.text or "").strip()

    if not is_main_bot_user(user.id):
        await update.message.reply_text(_t(lang, "access_denied"), parse_mode="HTML")
        return

    open_ticket = get_user_open_ticket(user.id)
    if not open_ticket:
        await update.message.reply_text(
            _t(lang, "no_open_ticket", btn_new=_BTN_NEW[lang]),
            parse_mode="HTML",
            reply_markup=_main_keyboard(lang)
        )
        return

    ticket_id = open_ticket["id"]
    add_message(ticket_id, user.id, "user", text)
    tickets_logger.info("User %s added message to ticket #%s", user.id, ticket_id)

    uname = f"@{user.username}" if user.username else "no username"
    admin_text = _t("en", "admin_new_message",
                    ticket_id=ticket_id, name=user.first_name,
                    uname=uname, user_id=user.id, text=text)
    await _notify_admins(context, admin_text, _ticket_admin_keyboard(ticket_id, user.id))

    await update.message.reply_text(_t(lang, "added_to_ticket"))


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _get_user_lang(user.id)
    if not is_main_bot_user(user.id):
        await update.message.reply_text(_t(lang, "access_denied"), parse_mode="HTML")
        return
    context.user_data.pop("state", None)
    await update.message.reply_text(
        _t(lang, "cancelled"),
        reply_markup=_main_keyboard(lang)
    )


async def my_tickets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _get_user_lang(user.id)

    if not is_main_bot_user(user.id):
        await update.message.reply_text(_t(lang, "access_denied"), parse_mode="HTML")
        return

    tickets = get_user_tickets(user.id, limit=10)
    if not tickets:
        await update.message.reply_text(
            _t(lang, "no_tickets", btn_new=_BTN_NEW[lang]),
            parse_mode="HTML",
            reply_markup=_main_keyboard(lang)
        )
        return

    status_emoji = {"open": "🟢", "closed": "✅"}
    lines = [_t(lang, "tickets_title")]
    for tkt in tickets:
        emoji  = status_emoji.get(tkt["status"], "⚪")
        date   = tkt["created_at"][:10]
        msgs   = get_ticket_messages(tkt["id"])
        status = ("مفتوحة" if lang == "ar" else "OPEN") if tkt["status"] == "open" else ("مغلقة" if lang == "ar" else "CLOSED")
        lines.append(
            _t(lang, "ticket_line",
               emoji=emoji, ticket_id=tkt["id"],
               status=status, date=date, count=len(msgs))
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_main_keyboard(lang)
    )
