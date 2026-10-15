"""🎁 Referral center — "اكسب نسختك من البوت".

Arabic-only UI consistent with the settings menu. Pages:
  * ref home: goal, qualified/70, progress %, personal invite link, actions.
  * ref:stats: latest referrals with their qualification status.
  * ref:claim: reward claim → BotFather instructions → secure token linking.

Token capture security:
  * tokens are only accepted in PRIVATE chat while the user is in the
    awaiting-token state,
  * the user's message is DELETED immediately (before any processing),
  * the value never touches logs; it is verified via getMe, stored encrypted
    (Fernet) with a sha256 uniqueness hash, then used to spawn the clone.
"""

from __future__ import annotations

import logging
from html import escape
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config.settings import MAIN_BOT_USERNAME, REFERRAL_TARGET
from services import referral
from services.instance_manager import is_clone_bot, spawn_instance, running_instance_ids
from database import support

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    "pending": "⏳ بانتظار النشاط",
    "qualified": "✅ مؤهل",
    "suspicious": "🚩 تحت المراجعة",
}

_MAX_TOKEN_ATTEMPTS = 5

_SUPPORT_CATEGORY_LABELS = {
    "token": "🔑 التوكن مرفوض",
    "clone": "🤖 نسختي مش شغالة",
    "other": "🛠 مشكلة أخرى",
}

_SUPPORT_STATUS_LABELS = {
    "open": "🟡 قيد المراجعة",
    "answered": "🟢 تم الرد",
    "closed": "⚫ مغلقة",
}

_SUPPORT_SUBJECT_BODY = {
    "token": "المستخدم يواجه رفضًا للتوكن أثناء تفعيل نسخته.",
    "clone": "المستخدم يبلغ أن نسخته المفعّلة لا تستجيب.",
    "other": "طلب دعم فني عام.",
}

_FAQ_TEXT = (
    "❓ <b>الأسئلة الشائعة</b>\n\n"
    "🔑 <b>إيه اللي أعمله لو التوكن مرفوض؟</b>\n"
    "• تأكد إنك ناسخ التوكن كامل من @BotFather من غير مسافات أو حروف ناقصة.\n"
    "• لو ظهر «مرتبط بالفعل» يبقى التوكن مستخدم قبل كده — اعمل بوت جديد بـ <code>/newbot</code>.\n"
    "• ابعت التوكن مرة واحدة بس، وبعدها اضغط «✅ تحقق من التفعيل».\n"
    "• لو لسه مرفوض افتح تذكرة من 🛟 الدعم الفني.\n\n"
    "🤖 <b>إيه اللي أعمله لو النسخة مش شغالة؟</b>\n"
    "• اضغط «🔄 تحديث حالة نسختي» عشان النظام يعيد فحص البوت مباشرة.\n"
    "• اتأكد إنك ما عملتش <code>/revoke</code> للتوكن في @BotFather.\n"
    "• لو الفحص رجع «التوكن مرفوض» أعد الربط ببوت جديد من جديد.\n"
    "• لو استمرت المشكلة افتح تذكرة دعم.\n\n"
    "🛟 <b>إزاي أتواصل مع الدعم؟</b>\n"
    "من صفحة 🎁 اكسب نسختك اضغط «🛟 الدعم الفني» ← «فتح تذكرة جديدة» واختار نوع المشكلة.\n"
    "هتاخد رقم تذكرة خاص <code>TKT-XXXXXX</code> تتابع منه الردود وحالة المشكلة."
)

_RULES_TEXT = (
    "📜 <b>قواعد الإحالات</b>\n\n"
    "1️⃣ كل حساب تيليجرام يُحتسب <b>مرة واحدة فقط</b> ولأول شخص دعاه.\n"
    "2️⃣ ممنوع تدعو نفسك، والإحالة بعد الربط ما بتنتقلش لمُحيل تاني أبدًا.\n"
    "3️⃣ المستخدم يبقى <b>مؤهلًا</b> لما:\n"
    "   • يبدأ البوت من رابطك ويكمل <b>أول تحميل ناجح</b>، أو\n"
    "   • يستخدم البوت في <b>يومين مختلفين</b>.\n"
    "4️⃣ أي مستخدم محظور لا يُحتسب.\n"
    "5️⃣ لو حصل ربط بعدد كبير في وقت قصير جدًا، الإحالات تتعلم «🚩 تحت المراجعة» ولا تُحتسب تلقائيًا.\n"
    "6️⃣ المكافأة: <b>70 مستخدمًا مؤهلًا</b> = نسخة بوت خاصة بك.\n\n"
    "⚠️ أي غش أو حسابات وهمية ممكن توقف الأهلية نهائيًا."
)

_GUIDE_STEPS = {
    1: (
        "📖 <b>دليل التفعيل — خطوة 1 من 6</b>\n\n"
        "عشان تفعّل نسختك الخاصة، هتحتاج تنشئ بوت جديد من @BotFather وتربطه هنا.\n\n"
        "الخطوات الجاية هتوديك من الصفر لنسخة شغالة بنفس قوة البوت الأساسي."
    ),
    2: (
        "📖 <b>دليل التفعيل — خطوة 2 من 6</b>\n\n"
        "افتح <a href=\"https://t.me/BotFather\">@BotFather</a> واضغط Start.\n\n"
        "بعدها ابعت الأمر:\n<code>/newbot</code>"
    ),
    3: (
        "📖 <b>دليل التفعيل — خطوة 3 من 6</b>\n\n"
        "هيسألك BotFather عن <b>اسم</b> البوت.\n"
        "مثال: <i>Kelvor Downloads</i> — اختار أي اسم تحبه."
    ),
    4: (
        "📖 <b>دليل التفعيل — خطوة 4 من 6</b>\n\n"
        "بعدين هيسألك عن <b>اليوزرنيم</b>.\n"
        "لازم ينتهي بكلمة <code>bot</code> وميكونش مستخدم قبل كده.\n"
        "مثال: <code>my_kelvor_bot</code>"
    ),
    5: (
        "📖 <b>دليل التفعيل — خطوة 5 من 6</b>\n\n"
        "هيديك BotFather <b>توكن</b> شكله كده:\n"
        "<code>123456789:AAExample...token</code>\n\n"
        "🔐 <b>انسخه كامل من غير مسافات.</b>"
    ),
    6: (
        "📖 <b>دليل التفعيل — خطوة 6 من 6</b>\n\n"
        "ارجع هنا في المحادثة واضغط:\n"
        "«📩 أبدأ ربط التوكن» وألصق التوكن.\n\n"
        "رسالتك هتتمسح فورًا، والتوكن هيتخزن مشفرًا.\n"
        "بعدها اضغط «✅ تحقق من التفعيل» عشان تتأكد إن نسختك بقت شغالة."
    ),
}

_ACTION_LABELS_AR = {
    "reward_eligible": "🏆 أصبحت مؤهلًا للمكافأة",
    "milestone_reached": "🎉 وصلت لمحطة إحالات",
    "reward_claimed": "🎁 تم فتح طلب النسخة",
    "token_verify_failed": "🔑 فشل التحقق من التوكن",
    "instance_activated": "✅ تم تفعيل نسختك",
    "referral_flood_flag": "🚩 رصد نشاط مشبوه",
    "referral_qualified": "👥 تأهلت إحالة جديدة",
    "referral_link_failed": "⚠️ خطأ في ربط الإحالة",
}


def _main_bot_url() -> str:
    return f"https://t.me/{MAIN_BOT_USERNAME}" if MAIN_BOT_USERNAME else ""


def _rows_for(stats: dict, bot_username: str | None) -> list[list[InlineKeyboardButton]]:
    """Smart, state-driven menu: every user sees the step that fits their state."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("📋 نسخ رابط الإحالة", callback_data="ref:copy"),
            InlineKeyboardButton("📊 إحصائيات الإحالات", callback_data="ref:stats"),
        ],
    ]
    stage = referral.referrer_stage(stats)
    inst = stats.get("instance") or {}

    if stage == "active":
        rows.append([InlineKeyboardButton(
            f"🤖 نسختك جاهزة: @{inst['bot_username']}",
            url=f"https://t.me/{inst['bot_username']}",
        )])
        rows.append([
            InlineKeyboardButton("🔄 تحديث حالة نسختي", callback_data="ref:refresh_status"),
            InlineKeyboardButton("✅ تحقق من التفعيل", callback_data="ref:verify_activation"),
        ])
    elif stage == "eligible":
        rows.append([InlineKeyboardButton("🚀 فعّل نسختك الآن", callback_data="ref:claim")])
        rows.append([InlineKeyboardButton("📖 دليل التفعيل", callback_data="ref:guide")])
    elif stage == "awaiting_token":
        rows.append([InlineKeyboardButton("🔑 ربط توكن البوت", callback_data="ref:claim")])
        rows.append([
            InlineKeyboardButton("📖 دليل التفعيل", callback_data="ref:guide"),
            InlineKeyboardButton("✅ تحقق من التفعيل", callback_data="ref:verify_activation"),
        ])
    else:
        remaining = max(0, int(stats.get("target", 0)) - int(stats.get("qualified", 0)))
        rows.append([InlineKeyboardButton(f"🎁 نسختي الخاصة ({remaining} متبقي)", callback_data="ref:claim")])
        rows.append([InlineKeyboardButton("📜 قواعد الإحالات", callback_data="ref:rules")])

    rows.append([
        InlineKeyboardButton("🧾 سجل العمليات", callback_data="ref:activity"),
        InlineKeyboardButton("🛟 الدعم الفني", callback_data="ref:support"),
    ])
    rows.append([
        InlineKeyboardButton("❓ أسئلة شائعة", callback_data="ref:faq"),
        InlineKeyboardButton("📜 قواعد الإحالات", callback_data="ref:rules"),
    ])

    bottom: list[InlineKeyboardButton] = [
        InlineKeyboardButton("🔙 العودة إلى الإعدادات", callback_data="settings:home"),
    ]
    # Clone context (and any bot with a known main username) gets the
    # permanent "back to the main bot" button.
    if is_clone_bot(bot_username) and _main_bot_url():
        bottom.insert(0, InlineKeyboardButton("🏠 العودة إلى البوت الأساسي", url=_main_bot_url()))
    rows.append(bottom)
    return rows


def _home_text(stats: dict) -> str:
    bar_len = 14
    filled = int(stats["percent"] / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    link = stats.get("link", "")
    stage = referral.referrer_stage(stats)
    stage_line = {
        "active": "🤖 <b>نسختك الخاصة مفعّلة وشغالة.</b>",
        "eligible": "🎉 <b>أنت مؤهل الآن لتفعيل نسختك — اضغط «فعّل الآن».</b>",
        "awaiting_token": "🔑 <b>الربط مفتوح — أرسل توكن البوت الجديد.</b>",
        "progress": f"🚀 باقي <b>{max(0, int(stats['target']) - int(stats['qualified']))}</b> مستخدمًا مؤهلًا لتفعيل نسختك.",
    }[stage]
    return (
        "🎁 <b>اكسب نسختك من البوت</b>\n\n"
        f"🎯 هدفك: <b>{stats['target']} مستخدمًا مؤهلًا</b>\n"
        f"👥 المستخدمون المؤهلون: <b>{stats['qualified']} / {stats['target']}</b>\n"
        f"📊 التقدم: <b>{stats['percent']}%</b>  <code>{bar}</code>\n"
        f"⏳ بانتظار النشاط: {stats['pending']}\n"
        f"🚩 تحت المراجعة: {stats['suspicious']}\n\n"
        f"{stage_line}\n\n"
        "🔗 <b>رابط الدعوة الخاص بك:</b>\n"
        f"<code>{link}</code>\n\n"
        "كل مستخدم مؤهل يقربك خطوة من امتلاك نسختك الخاصة.\n"
        "يُحتسب المستخدم مؤهلًا بعد أول تحميل ناجح — أو استخدام البوت في يومين مختلفين."
    )


async def _render_home(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    uid = update.effective_user.id
    stats = referral.get_stats(uid)
    stats["link"] = referral.referral_link(uid, context.bot.username or MAIN_BOT_USERNAME)
    markup = InlineKeyboardMarkup(_rows_for(stats, context.bot.username))
    text = _home_text(stats)
    # Lazy milestone notifications (idempotent; the automatic path usually
    # already sent them — this only fills gaps on page opens).
    try:
        for m in referral.claim_new_milestones(uid):
            await context.bot.send_message(
                chat_id=uid, text=referral.milestone_text(m),
                parse_mode="HTML", disable_web_page_preview=True,
            )
    except Exception:
        pass
    if edit and update.callback_query and update.callback_query.message:
        q = update.callback_query
        try:
            if q.message.text:
                await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            else:
                await q.edit_message_caption(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def referral_page(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    """Entry point used by the settings menu (settings:referral)."""
    await _render_home(update, context, edit=edit)


_BOTFATHER_GUIDE = (
    "🤖 <b>خطوات إنشاء بوتك الجديد:</b>\n\n"
    "1️⃣ افتح <a href=\"https://t.me/BotFather\">@BotFather</a>\n"
    "2️⃣ أرسل <code>/newbot</code>\n"
    "3️⃣ اختار اسمًا للبوت (مثل: <i>Kelvor Downloads</i>)\n"
    "4️⃣ اختار يوزرنيم ينتهي بـ <code>bot</code> (مثل: <code>my_kelvor_bot</code>)\n"
    "5️⃣ هينسخ لك BotFather <b>توكن</b> شكله كده:\n"
    "<code>123456789:AAExample...token</code>\n\n"
    "🔐 <b>الآن أرسل التوكن هنا في هذه المحادثة الخاصة مباشرة.</b>\n"
    "• رسالتك هتتمسح فورًا بعد الاستلام.\n"
    "• التوكن بيتخزن مشفرًا ولا يظهر في أي سجلات.\n"
    "• never send it in any group or to anyone else.\n\n"
    "⚠️ التوكن = كلمة سر كاملة الصلاحيات لبوتك. لو اتسرب بالغلط، ارجع لـ @BotFather واضغط <code>/revoke</code>."
)


async def _edit_ref(q, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit a referral callback message; falls back to a fresh reply."""
    try:
        kwargs = {"text": text, "parse_mode": "HTML"}
        if markup is not None:
            kwargs["reply_markup"] = markup
        await q.edit_message_text(**kwargs)
    except Exception:
        try:
            kwargs = {"text": text, "parse_mode": "HTML"}
            if markup is not None:
                kwargs["reply_markup"] = markup
            await q.message.reply_text(**kwargs)
        except Exception:
            pass


def _back_home_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("🔙 رجوع لصفحة المكافأة", callback_data="ref:home")]


async def _show_faq(q) -> None:
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛟 افتح تذكرة دعم", callback_data="ref:support")],
        _back_home_row(),
    ])
    await _edit_ref(q, _FAQ_TEXT, markup)


async def _show_rules(q) -> None:
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛟 عندك مشكلة؟ الدعم الفني", callback_data="ref:support")],
        _back_home_row(),
    ])
    await _edit_ref(q, _RULES_TEXT, markup)


async def _show_activity(q) -> None:
    uid = q.from_user.id
    items = referral.get_user_activity_log(uid, limit=10)
    if not items:
        body = "لا توجد عمليات مسجلة بعد — أول حدث سيظهر هنا فور حدوثه. 🕐"
    else:
        lines = []
        for it in items:
            action = _ACTION_LABELS_AR.get(it.get("action"), it.get("action") or "عملية")
            created = (it.get("created_at") or "—")[:16]
            lines.append(f"• <b>{action}</b> — <code>{created}</code>")
        body = "\n".join(lines)
    await _edit_ref(
        q,
        f"🧾 <b>سجل العمليات</b>\n\n{body}\n\n🔒 لا نعرض هنا أي توكن أو بيانات سرية.",
        InlineKeyboardMarkup([_back_home_row()]),
    )


async def _show_guide(q, step: int) -> None:
    step = max(1, min(len(_GUIDE_STEPS), int(step or 1)))
    text = _GUIDE_STEPS[step]
    rows = []
    nav = []
    if step > 1:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"ref:guide:{step - 1}"))
    if step < len(_GUIDE_STEPS):
        nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"ref:guide:{step + 1}"))
    if nav:
        rows.append(nav)
    if step == len(_GUIDE_STEPS):
        rows.append([InlineKeyboardButton("📩 أبدأ ربط التوكن", callback_data="ref:claim")])
        rows.append([InlineKeyboardButton("✅ تحقق من التفعيل", callback_data="ref:verify_activation")])
    rows.append(_back_home_row())
    await _edit_ref(q, text, InlineKeyboardMarkup(rows))


async def _probe_instance(inst: dict) -> dict:
    """Live Telegram getMe check on a clone token (never exposes the token)."""
    from telegram import Bot
    from telegram.error import InvalidToken
    token = referral.load_instance_token(int(inst["id"]))
    if not token:
        return {"ok": False, "reason": "missing_token"}
    try:
        me = await Bot(token).get_me()
        return {"ok": True, "username": getattr(me, "username", "")}
    except InvalidToken:
        return {"ok": False, "reason": "invalid_token"}
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__[:40]}


async def _render_status_screen(q, verified_title: str) -> None:
    uid = q.from_user.id
    stats = referral.get_stats(uid)
    inst = stats.get("instance")
    stage = referral.referrer_stage(stats)

    if stage == "active" and inst:
        running = int(inst["id"]) in running_instance_ids()
        if inst.get("last_error"):
            await _edit_ref(q, f"{verified_title}\n\n{escape(inst['last_error'][:120])}",
                            InlineKeyboardMarkup([_back_home_row()]))
            return
        probe = await _probe_instance(inst)
        if probe.get("ok"):
            text = (
                f"{verified_title}\n\n"
                "🟢 <b>نسختك متصلة وتستجيب الآن.</b>\n"
                f"🤖 البوت: <a href=\"https://t.me/{inst['bot_username']}\">@{inst['bot_username']}</a>\n"
                f"⚙️ الحالة داخل النظام: <b>{'يعمل الآن' if running else 'سيعمل خلال دقائق / أول إعادة تشغيل'}</b>"
            )
            rows = [
                [InlineKeyboardButton("🤖 افتح نسختك", url=f"https://t.me/{inst['bot_username']}")],
                [InlineKeyboardButton("🔄 أعد الفحص", callback_data="ref:refresh_status")],
                _back_home_row(),
            ]
        else:
            reason = probe.get("reason", "؟")
            text = (
                f"{verified_title}\n\n"
                "🔴 <b>فشل التحقق.</b>\n"
                f"السبب: <code>{reason}</code>\n\n"
                "• اتأكد إنك ما عملتش <code>/revoke</code> للتوكن في @BotFather.\n"
                "• لو التوكن مرفوض، أعد الربط ببوت جديد.\n"
                "• لو استمرت المشكلة، افتح تذكرة دعم."
            )
            rows = [
                [InlineKeyboardButton("🔑 إعادة ربط التوكن", callback_data="ref:claim")],
                [InlineKeyboardButton("🛟 الدعم الفني", callback_data="ref:support")],
                _back_home_row(),
            ]
        await _edit_ref(q, text, InlineKeyboardMarkup(rows))
        return

    if stage == "awaiting_token":
        text = (
            f"{verified_title}\n\n"
            "🟡 <b>لسه في انتظار توكن البوت.</b>\n"
            "اضغط «ربط توكن البوت» وأرسل التوكن من @BotFather."
        )
        rows = [
            [InlineKeyboardButton("🔑 ربط توكن البوت", callback_data="ref:claim")],
            [InlineKeyboardButton("📖 دليل التفعيل", callback_data="ref:guide")],
            _back_home_row(),
        ]
    elif stage == "eligible":
        text = (
            f"{verified_title}\n\n"
            "🟢 <b>أنت مؤهل للتفعيل.</b>\n"
            "اضغط «فعّل نسختك الآن» واتبع الخطوات."
        )
        rows = [
            [InlineKeyboardButton("🚀 فعّل نسختك الآن", callback_data="ref:claim")],
            [InlineKeyboardButton("📖 دليل التفعيل", callback_data="ref:guide")],
            _back_home_row(),
        ]
    else:
        remaining = max(0, int(stats.get("target", 0)) - int(stats.get("qualified", 0)))
        text = (
            f"{verified_title}\n\n"
            f"⏳ لسه مكملتش الهدف — باقي <b>{remaining}</b> مستخدمًا مؤهلًا.\n"
            "شارك رابطك عشان توصل أسرع. 💪"
        )
        rows = [
            [InlineKeyboardButton("📋 نسخ رابط الإحالة", callback_data="ref:copy")],
            _back_home_row(),
        ]
    await _edit_ref(q, text, InlineKeyboardMarkup(rows))


async def _show_support_home(q) -> None:
    uid = q.from_user.id
    tickets = support.list_user_tickets(uid, limit=5)
    if tickets:
        lines = []
        for t in tickets:
            lines.append(
                f"• <code>{t['ticket_no']}</code> — {_SUPPORT_CATEGORY_LABELS.get(t['category'], t['category'])} "
                f"— {_SUPPORT_STATUS_LABELS.get(t['status'], t['status'])}"
            )
        body = "\n".join(lines)
    else:
        body = "لا توجد تذاكر مفتوحة عندك حاليًا."
    rows = [[InlineKeyboardButton("📨 فتح تذكرة جديدة", callback_data="ref:support:new")]]
    if tickets:
        rows.insert(0, [InlineKeyboardButton("🔄 تحديث الحالة", callback_data="ref:support")])
    rows.append(_back_home_row())
    await _edit_ref(
        q,
        "🛟 <b>الدعم الفني</b>\n\n"
        f"{body}\n\n"
        "افتح تذكرة برقم خاص وتابع الردود وحالة مشكلتك من هنا.",
        InlineKeyboardMarkup(rows),
    )


async def _show_support_new(q) -> None:
    await _edit_ref(
        q,
        "🛟 <b>فتح تذكرة جديدة</b>\n\nاختر نوع المشكلة:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 التوكن مرفوض", callback_data="ref:support:open:token")],
            [InlineKeyboardButton("🤖 نسختي مش شغالة", callback_data="ref:support:open:clone")],
            [InlineKeyboardButton("🛠 مشكلة أخرى", callback_data="ref:support:open:other")],
            _back_home_row(),
        ]),
    )


async def _open_support_ticket(q, category: str) -> None:
    uid = q.from_user.id
    label = _SUPPORT_CATEGORY_LABELS.get(category, "مشكلة أخرى")
    body = _SUPPORT_SUBJECT_BODY.get(category, "طلب دعم فني عام.")
    ticket = support.open_ticket(uid, category, label, body)
    await _render_support_ticket(q, ticket["id"], just_opened=True)


async def _render_support_ticket(q, ticket_id: int, just_opened: bool = False) -> None:
    uid = q.from_user.id
    ticket = support.get_ticket_for_user(ticket_id, uid)
    if not ticket:
        await _edit_ref(q, "❌ التذكرة غير موجودة أو لا تخص حسابك.",
                        InlineKeyboardMarkup([_back_home_row()]))
        return
    msgs = support.get_messages(ticket_id, limit=20)
    lines = []
    for m in msgs:
        who = "🛟 الدعم" if m["sender"] == "admin" else "👤 أنت"
        created = (m.get("created_at") or "—")[:16]
        lines.append(f"{who} • <code>{created}</code>\n{m['body']}")
    thread = "\n\n".join(lines) if lines else "لا توجد رسائل بعد."
    head = "🎫 <b>تم فتح تذكرتك بنجاح!</b>\n" if just_opened else "🎫 <b>تذكرة الدعم</b>\n"
    text = (
        f"{head}\n"
        f"🔢 الرقم الخاص: <code>{ticket['ticket_no']}</code>\n"
        f"🏷 النوع: {_SUPPORT_CATEGORY_LABELS.get(ticket['category'], ticket['category'])}\n"
        f"📌 الحالة: {_SUPPORT_STATUS_LABELS.get(ticket['status'], ticket['status'])}\n\n"
        f"<b>المحادثة:</b>\n{thread}"
    )
    await _edit_ref(
        q, text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 تحديث الحالة", callback_data=f"ref:support:view:{ticket_id}")],
            [InlineKeyboardButton("🛟 مركز الدعم", callback_data="ref:support")],
            _back_home_row(),
        ]),
    )


async def referral_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id
    await q.answer()

    if data == "ref:home":
        await _render_home(update, context, edit=True)
        return

    if data == "ref:copy":
        link = referral.referral_link(uid, context.bot.username or MAIN_BOT_USERNAME)
        share = f"https://t.me/share/url?url={quote(link)}&text={quote('حمّل أي فيديو أو صورة من 20+ منصة بجودة عالية — جرّب البوت ده 🔥')}"
        await q.message.reply_text(
            "🔗 <b>رابط الإحالة الخاص بك</b> — اضغط عليه نسخ من الشريط بالأسفل:\n\n"
            f"<code>{link}</code>\n\n"
            "شاركه مع أصدقائك؛ كل مستخدم مؤهل = خطوة نحو نسختك الخاصة.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 مشاركة مباشرة", url=share)],
                [InlineKeyboardButton("🔙 رجوع", callback_data="ref:home")],
            ]),
        )
        return

    if data == "ref:stats":
        stats = referral.get_stats(uid)
        items = referral.get_referrals_list(uid, limit=10)
        lines = []
        for it in items:
            st = _STATUS_LABELS.get(it.get("status", ""), it.get("status", ""))
            lines.append(f"• <code>{it['referred_id']}</code> — {st}")
        body = "\n".join(lines) if lines else "لا توجد إحالات بعد — شارك رابطك وابدأ! 🚀"
        await q.edit_message_text(
            "📊 <b>إحصائيات الإحالات</b>\n\n"
            f"✅ مؤهلون: <b>{stats['qualified']}</b> / {stats['target']}\n"
            f"⏳ بانتظار النشاط: <b>{stats['pending']}</b>\n"
            f"🚩 تحت المراجعة: <b>{stats['suspicious']}</b>\n\n"
            "<b>آخر الإحالات:</b>\n" + body,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="ref:home")]]),
        )
        return

    if data == "ref:faq":
        await _show_faq(q)
        return

    if data == "ref:rules":
        await _show_rules(q)
        return

    if data == "ref:activity":
        await _show_activity(q)
        return

    if data == "ref:guide":
        await _show_guide(q, 1)
        return
    if data.startswith("ref:guide:"):
        try:
            step = int(data.rsplit(":", 1)[-1])
        except ValueError:
            step = 1
        await _show_guide(q, step)
        return

    if data == "ref:refresh_status":
        await _render_status_screen(q, "🔄 <b>تحديث حالة النسخة</b>")
        return

    if data == "ref:verify_activation":
        await _render_status_screen(q, "✅ <b>التحقق من التفعيل</b>")
        return

    if data == "ref:support":
        await _show_support_home(q)
        return
    if data == "ref:support:new":
        await _show_support_new(q)
        return
    if data.startswith("ref:support:open:"):
        await _open_support_ticket(q, data.rsplit(":", 1)[-1])
        return
    if data.startswith("ref:support:view:"):
        try:
            tid = int(data.rsplit(":", 1)[-1])
        except ValueError:
            tid = 0
        await _render_support_ticket(q, tid)
        return

    if data == "ref:cancel_token":
        context.user_data.pop("awaiting_token", None)
        context.user_data.pop("token_attempts", None)
        await q.edit_message_text(
            "❌ تم إلغاء ربط التوكن. مكافأتك محفوظة — تقدر ترجع تبدأ الربط في أي وقت.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="ref:home")]]),
        )
        return

    if data == "ref:claim":
        stats = referral.get_stats(uid)
        inst = stats.get("instance")
        if inst and inst.get("status") == "active" and inst.get("bot_username"):
            await q.edit_message_text(
                f"🤖 <b>نسختك شغالة بالفعل!</b>\n\n"
                f"🔗 البوت: <a href=\"https://t.me/{inst['bot_username']}\">@{inst['bot_username']}</a>\n"
                f"👥 المستخدمون المؤهلون: {stats['qualified']} / {stats['target']}\n\n"
                "نسختك تعمل بنفس قوة البوت الأساسي، ومربوطة بشبكة الإحالات.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🤖 افتح نسختك", url=f"https://t.me/{inst['bot_username']}")],
                    [InlineKeyboardButton("🔙 رجوع", callback_data="ref:home")],
                ]),
            )
            return

        if q.message.chat.type != "private":
            await q.answer("⚠️ ربط التوكن يتم في المحادثة الخاصة فقط.", show_alert=True)
            return

        ok, status = referral.claim_reward(uid)
        if not ok:
            await q.answer(
                f"لسه محتاج {max(0, stats['target'] - stats['qualified'])} مستخدمًا مؤهلًا كمان 💪",
                show_alert=True,
            )
            return

        context.user_data["awaiting_token"] = True
        context.user_data["token_attempts"] = 0
        await q.edit_message_text(
            _BOTFATHER_GUIDE,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ إلغاء الربط", callback_data="ref:cancel_token")],
            ]),
        )
        return

    # Unknown ref: payload — degrade gracefully to home.
    await _render_home(update, context, edit=True)


async def try_handle_token_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Secure token capture. Returns True when the message was consumed.

    The user's message is deleted FIRST — before parsing, verifying or
    storing anything — so the plaintext token's lifetime in the chat is as
    short as Telegram allows. (Deletion alone isn't a secrecy guarantee, so
    storage is encrypted and the value is never logged.)
    """
    if not context.user_data.get("awaiting_token"):
        return False
    msg = update.message
    if not msg or not msg.text:
        return False
    if msg.chat.type != "private":
        return False

    try:
        await msg.delete()
    except Exception:
        pass

    uid = msg.from_user.id
    attempts = int(context.user_data.get("token_attempts", 0)) + 1
    context.user_data["token_attempts"] = attempts

    token_text = msg.text.strip()
    if not referral.looks_like_token(token_text):
        if attempts >= _MAX_TOKEN_ATTEMPTS:
            context.user_data.pop("awaiting_token", None)
            context.user_data.pop("token_attempts", None)
            await msg.reply_text(
                "⛔ تم إيقاف محاولة الربط بعد 5 محاولات غير صحيحة.\n"
                "مكافأتك محفوظة — افتح 🎁 اكسب نسختك من البوت واضغط «ربط توكن البوت» للمحاولة لاحقًا.",
            )
            return True
        await msg.reply_text(
            "🔤 ده مش شكل توكن بوت صحيح.\n"
            "التوكن شكله: <code>123456789:AAH4x... طويلة من الحروف والأرقام</code>\n"
            "أرسل التوكن الذي أعطاه لك @BotFather مباشرة هنا.",
            parse_mode="HTML",
        )
        return True

    await msg.reply_text("⏳ جارٍ التحقق من التوكن وهوية البوت…")
    result = referral.verify_and_link_token(uid, token_text)
    if not result.get("ok"):
        reason = result.get("reason", "")
        if reason == "already_linked":
            text = "⚠️ التوكن ده مربوط بالفعل بنسخة أخرى، أو البوت مستخدم قبل كده.\nأنشئ بوتًا جديدًا من @BotFather وأرسل توكنه."
        elif reason == "main_bot_token":
            text = "⚠️ ده توكن البوت الأساسي نفسه! المطلوب توكن بوت <b>جديد</b> أنشأته من @BotFather."
        elif reason == "network_error":
            text = "🌐 مشكلة اتصال مؤقتة أثناء التحقق من تيليجرام. جرّب إرسال التوكن مرة أخرى بعد دقيقة."
        elif reason == "not_claimed":
            text = "⚠️ لا يوجد طلب تفعيل نشط لحسابك. افتح 🎁 اكسب نسختك من البوت أولًا."
        else:
            text = "❌ التوكن غير صحيح أو مرفوض من تيليجرام. تأكد من نسخه كاملًا من @BotFather وأرسله مرة أخرى."
        if attempts >= _MAX_TOKEN_ATTEMPTS:
            context.user_data.pop("awaiting_token", None)
            context.user_data.pop("token_attempts", None)
            text += "\n\n⛔ تم إيقاف المحاولات بعد 5 مرات — تقدر تبدأ الربط مجددًا من صفحة 🎁."
        await msg.reply_text(text, parse_mode="HTML")
        return True

    # Verified + stored. Spawn the clone instance live inside this process.
    bot_username = result.get("bot_username", "")
    context.user_data.pop("awaiting_token", None)
    context.user_data.pop("token_attempts", None)

    from database import pg
    inst = pg.fetchone("SELECT * FROM bot_instances WHERE id = ?", (result["instance_id"],))
    started = False
    if inst:
        try:
            started = await spawn_instance(inst)
        except Exception:
            logger.exception("Live spawn failed instance=%s", inst.get("id"))

    status_line = (
        "🟢 <b>النسخة شغالة الآن!</b>" if started
        else "🟡 <b>تم الربط بنجاح.</b> النسخة ستعمل تلقائيًا خلال دقائق (أو مع أول إعادة تشغيل للنظام)."
    )
    await msg.reply_text(
        f"🎉 <b>مبروك! نسختك الخاصة اتفعلت.</b>\n\n"
        f"{status_line}\n"
        f"🤖 بوتك: <a href=\"https://t.me/{bot_username}\">@{bot_username}</a>\n\n"
        f"✅ تم التحقق من ملكية البوت وصحة التوكن.\n"
        f"🔐 التوكن مخزن مشفرًا ومحذوف من المحادثة.\n"
        f"👥 نسختك فيها زر «العودة إلى البوت الأساسي»، وكل مستخدم جديد بيبدأ منها يتسجل في شبكة الإحالات.\n\n"
        f"شكرًا إنك كبرت المجتمع — {referral.get_stats(uid)['qualified']} مستخدمًا مؤهلًا بفضلك! 💪",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🤖 افتح نسختك", url=f"https://t.me/{bot_username}")],
            [InlineKeyboardButton("🔙 العودة لصفحة المكافأة", callback_data="ref:home")],
        ]),
    )
    return True
