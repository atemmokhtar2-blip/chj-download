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
from services import redemption
from services.instance_manager import is_clone_bot, spawn_instance, running_instance_ids
from database import support

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    "pending": "⏳ بانتظار النشاط",
    "qualified": "✅ مؤهل",
    "suspicious": "🚩 تحت المراجعة",
}

_MAX_TOKEN_ATTEMPTS = 5
# Wrong-code attempts before the capture closes (typos are recoverable —
# definitive rejections like expired/revoked close it immediately instead).
_MAX_CODE_ATTEMPTS = 5

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

_FAQ_ITEMS = {
    "1": (
        "🔑 <b>التوكن مرفوض</b>\n\n"
        "<b>جرّب الخطوات دي بالترتيب:</b>\n"
        "1. افتح @BotFather وانسخ التوكن كاملًا من غير مسافات.\n"
        "2. لو التوكن مربوط قبل كده، أنشئ بوتًا جديدًا بالأمر <code>/newbot</code> واستخدم توكنه.\n"
        "3. لو فيه مشكلة اتصال، استنى دقيقة وجرّب تاني.\n\n"
        "لو المشكلة مستمرة، تواصل مع المطوّر @K_xv6 واكتب رسالة الخطأ فقط."
    ),
    "2": (
        "🤖 <b>نسختي الخاصة مش شغالة</b>\n\n"
        "<b>اعمل الفحص السريع ده:</b>\n"
        "1. ارجع لصفحة «🎁 اكسب نسختك».\n"
        "2. اضغط «🔄 تحديث حالة نسختي».\n"
        "3. اتأكد إنك ما ألغيتش التوكن بأمر <code>/revoke</code> من @BotFather.\n\n"
        "لو لسه مش شغالة، تواصل مع المطوّر @K_xv6 وابعت رسالة الخطأ اللي ظهرت لك."
    ),
    "3": (
        "👥 <b>إمتى الإحالة تتحسب مؤهلة؟</b>\n\n"
        "الشخص لازم يبدأ البوت من رابط دعوتك، وبعدها يحقق <b>شرط واحد</b> من دول:\n"
        "• يكمل أول تحميل ناجح.\n"
        "• يستخدم البوت في يومين مختلفين.\n\n"
        "الحساب الواحد بيتحسب مرة واحدة فقط، والحسابات المحظورة لا تُحتسب."
    ),
    "4": (
        "🎯 <b>ليه عدد المؤهلين ما زادش؟</b>\n\n"
        "فتح الرابط أو الضغط على Start لوحده مش كفاية. الشخص لازم يكمل أحد شرطي التأهيل في السؤال السابق.\n\n"
        "• <b>⏳ بانتظار النشاط:</b> الشخص لسه ما كملش الشرط.\n"
        "• <b>🚩 تحت المراجعة:</b> الإحالة محتاجة مراجعة قبل احتسابها."
    ),
    "5": (
        "🔗 <b>أجيب رابط الدعوة منين؟</b>\n\n"
        "1. افتح «🎁 اكسب نسختك».\n"
        "2. اضغط «📋 نسخ رابط الإحالة».\n"
        "3. انسخ الرابط وابعته للأشخاص اللي عايز تدعوهم.\n\n"
        "تابع تقدمك من نفس الصفحة."
    ),
    "6": (
        "🎁 <b>إمتى أقدر أفعّل نسختي؟</b>\n\n"
        "1. تابع عدد المستخدمين المؤهلين في صفحة «🎁 اكسب نسختك».\n"
        "2. لما توصل للهدف وتظهر إنك مؤهل، اضغط «🚀 فعّل نسختك الآن».\n"
        "3. اتبع خطوات إنشاء بوت جديد وربط التوكن.\n\n"
        "لو محتاج شرح خطوة بخطوة، افتح «📖 دليل التفعيل»."
    ),
    "7": (
        "🔐 <b>أشارك توكن البوت مع حد؟</b>\n\n"
        "<b>لا.</b> التوكن زي كلمة السر؛ ابعته في المحادثة الخاصة مع البوت أثناء الربط فقط، وما تبعتوش في قناة أو جروب أو للمطوّر.\n\n"
        "لو اتكشف، افتح @BotFather وألغيه باستخدام <code>/revoke</code>، وبعدها أنشئ توكنًا جديدًا."
    ),
    "8": (
        "🧑‍💻 <b>إزاي أتواصل مع المطوّر؟</b>\n\n"
        "1. افتح «🎁 اكسب نسختك».\n"
        "2. اضغط زر «🧑‍💻 التواصل مع المطوّر».\n\n"
        "ابعت وصف المشكلة وصورة أو رسالة الخطأ لو موجودة، ومن فضلك ما تبعتش توكن البوت."
    ),
}

_FAQ_TITLES = {
    "1": "🔑 التوكن مرفوض",
    "2": "🤖 نسختي مش شغالة",
    "3": "👥 شروط تأهيل الإحالة",
    "4": "🎯 الإحالات مش بتزيد",
    "5": "🔗 رابط الدعوة",
    "6": "🎁 تفعيل نسختي",
    "7": "🔐 أمان التوكن",
    "8": "🧑‍💻 التواصل مع المطوّر",
}

_RULES_TEXT = (
    "📜 <b>قواعد الإحالات</b>\n\n"
    "1️⃣ كل حساب تيليجرام يُحتسب <b>مرة واحدة فقط</b> ولأول شخص دعاه.\n"
    "2️⃣ ممنوع تدعو نفسك، والإحالة بعد الربط ما بتنتقلش لمُحيل تاني أبدًا.\n"
    "3️⃣ المستخدم يبقى <b>مؤهلًا</b> لما:\n"
    "   • يبدأ البوت من رابطك ويكمل <b>أول تحميل ناجح</b>، أو\n"
    "   • يستخدم البوت في <b>يومين مختلفين</b>.\n"
    "4️⃣ أي مستخدم محظور لا يُحتسب.\n"
    "5️⃣ لو حصل ربط بعدد كبير في وقت قصير جدًا، الإحالات تتعلم «🚩 تحت المراجعة» ولا تُحتسب تلقائيًا.\n"
    "6️⃣ المكافأة: عدد المستخدمين المؤهلين = نسخة بوت خاصة بك على خطة زمنية:\n"
    "   🆓 <b>Basic</b> (الهدف الأساسي → 30 يومًا) • 👑 <b>Pro</b> (ضعف الهدف → 40 يومًا).\n"
    "   التفاصيل الكاملة في صفحة «📦 الخطط والباقات».\n\n"
    "🎟️ <b>أكواد التفعيل</b>\n"
    "• عندك كود من المالك؟ فعّله من «🤖 النسخ الخاصة» ← «🔑 استقبال كود تفعيل».\n"
    "• الكود لا يُستهلك إلا بعد نجاح إنشاء نسختك وتفعيلها كاملًا.\n\n"
    "🎁 <b>تجربة مجانية</b> — جرّب Basic أو Pro لمدة 24 ساعة (مرة واحدة لكل حساب).\n\n"
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
    elif stage == "expired":
        # An expired clone ONLY returns via an owner-approved renewal — the
        # user's path is a support ticket, never a self-service re-activation.
        rows.append([InlineKeyboardButton("⌛ طلب تجديد مدة نسختي", url="https://t.me/K_xv6")])
        rows.append([InlineKeyboardButton("🧾 سجل العمليات", callback_data="ref:activity")])
    else:
        remaining = max(0, int(stats.get("target", 0)) - int(stats.get("qualified", 0)))
        rows.append([InlineKeyboardButton(f"🎁 نسختي الخاصة ({remaining} متبقي)", callback_data="ref:claim")])

    # «🔑 تفعيل كود» — redemption-code entry for every user WITHOUT an
    # active clone (one clone per user; codes grant the same plan system).
    if stage != "active":
        rows.append([InlineKeyboardButton("🔑 تفعيل كود", callback_data="ref:code")])

    # «📜 قواعد الإحالات» appears exactly once — in this fixed bottom row.
    # «📦 الخطط والباقات» gives EVERY user full visibility into the plan
    # system (basic/pro, targets, validity, expiry, renewal) — always shown.
    rows.append([InlineKeyboardButton("📦 الخطط والباقات", callback_data="ref:plans")])
    rows.append([
        InlineKeyboardButton("🧾 سجل العمليات", callback_data="ref:activity"),
        InlineKeyboardButton("🧑‍💻 المطوّر", url="https://t.me/K_xv6"),
    ])
    rows.append([InlineKeyboardButton("📜 قواعد الإحالات — مهم قبل المشاركة", callback_data="ref:rules")])
    rows.append([InlineKeyboardButton("❓ أسئلة شائعة", callback_data="ref:faq")])

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
    """Readable Telegram HTML fallback matching the Rich Message dashboard layout."""
    bar_len = 14
    percent = max(0, min(100, int(stats.get("percent", 0))))
    filled = percent * bar_len // 100
    bar = "█" * filled + "░" * (bar_len - filled)
    link = escape(str(stats.get("link", "")))
    target = int(stats["target"])
    qualified = int(stats["qualified"])
    remaining = max(0, target - qualified)
    stage = referral.referrer_stage(stats)
    stage_line = {
        "active": "🤖 <b>نسختك الخاصة مفعّلة وشغالة.</b>",
        "expired": "⌛ <b>انتهت مدة نسختك — تم إيقافها تلقائيًا. التجديد بقرار المالك بعد التواصل مع المطوّر @K_xv6.</b>",
        "eligible": "🎉 <b>أنت مؤهل الآن لتفعيل نسختك — اضغط «فعّل الآن».</b>",
        "awaiting_token": "🔑 <b>الربط مفتوح — أرسل توكن البوت الجديد.</b>",
        "progress": f"🚀 باقي لك <b>{remaining}</b> مستخدمًا مؤهلًا لتفعيل نسختك.",
    }[stage]
    inst = stats.get("instance") or {}
    plan_line = ""
    if inst.get("plan") and inst.get("status") in ("active", "expired"):
        plan_badge = "👑 Pro" if inst.get("plan") == "pro" else "🆓 Basic"
        exp = escape(str(inst.get("expires_at") or "")[:10])
        days_left = inst.get("days_left")
        if inst.get("status") == "active" and days_left is not None:
            plan_line = f"📦 <b>{plan_badge}</b> · متبقي {int(days_left)} يوم · {exp}\n"
        elif inst.get("status") == "expired":
            plan_line = f"📦 <b>{plan_badge}</b> · انتهت {exp}\n"
    return (
        "🎁 <b>نسختك الخاصة — حالة التقدم</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎯 الهدف: <b>{target}</b> مستخدمًا مؤهلًا\n"
        f"✅ وصلت إلى: <b>{qualified}</b> مستخدمًا مؤهلًا\n"
        f"🚀 المتبقي: <b>{remaining}</b> مستخدمًا\n"
        f"📊 الإنجاز: <b>{percent}%</b>  <code>{bar}</code>\n"
        f"⏳ بانتظار النشاط: <b>{int(stats['pending'])}</b>\n"
        f"🚩 تحت المراجعة: <b>{int(stats['suspicious'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{plan_line}{stage_line}\n\n"
        "🔗 <b>رابط دعوتك</b>\n"
        f"<code>{link}</code>\n\n"
        "<b>📌 كيف يُحتسب المستخدم مؤهلًا؟</b>\n"
        "• يقوم بأول تحميل ناجح عبر البوت، أو\n"
        "• يستخدم البوت في يومين مختلفين.\n\n"
        "💡 شارك رابط دعوتك الآن — كل مستخدم مؤهل يقربك خطوة من امتلاك نسختك الخاصة."
    )


def _home_rich_html(stats: dict) -> str:
    """Polished mobile-first layout; keep referral rules and calculations unchanged."""
    link = escape(str(stats.get("link", "")))
    stage = referral.referrer_stage(stats)
    target = int(stats["target"])
    qualified = int(stats["qualified"])
    remaining = max(0, target - qualified)
    pending = int(stats["pending"])
    suspicious = int(stats["suspicious"])
    percent = max(0, min(100, int(stats.get("percent", 0))))
    bar_len = 14
    filled = min(bar_len, percent * bar_len // 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    stage_line = {
        "active": "🤖 <b>نسختك الخاصة مفعّلة وشغالة.</b>",
        "expired": "⌛ <b>انتهت مدة نسختك.</b> التجديد بعد التواصل مع المطوّر @K_xv6.",
        "eligible": "🎉 <b>أنت مؤهل لتفعيل نسختك.</b>",
        "awaiting_token": "🔑 <b>بانتظار توكن البوت الجديد.</b>",
        "progress": f"🚀 باقي لك <b>{remaining}</b> مستخدمًا مؤهلًا.",
    }[stage]
    inst = stats.get("instance") or {}
    plan_line = ""
    if inst.get("plan") and inst.get("status") in ("active", "expired"):
        plan_badge = "👑 Pro" if inst.get("plan") == "pro" else "🆓 Basic"
        exp = escape(str(inst.get("expires_at") or "")[:10])
        days_left = inst.get("days_left")
        if inst.get("status") == "active" and days_left is not None:
            plan_line = f"<p>📦 <b>{plan_badge}</b> · متبقي {int(days_left)} يوم · {exp}</p>"
        elif inst.get("status") == "expired":
            plan_line = f"<p>📦 <b>{plan_badge}</b> · انتهت {exp}</p>"
    return (
        "<h3>🎁 نسختك الخاصة — حالة التقدم</h3>"
        "<table bordered striped compact>"
        "<tr><th>المؤشر</th><th>الحالة</th></tr>"
        f"<tr><td>🎯 الهدف</td><td>{target} مستخدم مؤهل</td></tr>"
        f"<tr><td>✅ المؤهلون</td><td>{qualified}</td></tr>"
        f"<tr><td>🚀 المتبقي</td><td>{remaining}</td></tr>"
        f"<tr><td>⏳ بانتظار النشاط</td><td>{pending}</td></tr>"
        f"<tr><td>🚩 تحت المراجعة</td><td>{suspicious}</td></tr>"
        "</table>"
        f"<p><b>📊 نسبة الإنجاز: {percent}%</b></p>"
        f"<p><code>{bar}</code></p>"
        f"{plan_line}<p>{stage_line}</p><hr/>"
        "<h3>🔗 رابط دعوتك</h3>"
        f"<p><code>{link}</code></p>"
        "<details><summary>📌 كيف يُحتسب المستخدم مؤهلًا؟</summary>"
        "<p>• يقوم بأول تحميل ناجح عبر البوت، أو</p>"
        "<p>• يستخدم البوت في يومين مختلفين.</p>"
        "<p>شارك رابط دعوتك الآن — كل مستخدم مؤهل يقربك خطوة من امتلاك نسختك الخاصة.</p>"
        "</details>"
    )


async def _render_home(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    uid = update.effective_user.id
    stats = referral.get_stats(uid)
    stats["link"] = referral.referral_link(uid, context.bot.username or MAIN_BOT_USERNAME)
    markup = InlineKeyboardMarkup(_rows_for(stats, context.bot.username))
    text = _home_text(stats)
    rich_html = _home_rich_html(stats)
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
            await context.bot.do_api_request(
                endpoint="editMessageText",
                api_kwargs={
                    "chat_id": q.message.chat_id,
                    "message_id": q.message.message_id,
                    "rich_message": {"html": rich_html},
                    "reply_markup": markup,
                },
            )
            return
        except Exception:
            logger.info("Rich referral edit unavailable; falling back to HTML", exc_info=True)
        try:
            if q.message.text:
                await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            else:
                await q.edit_message_caption(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            logger.exception("Could not edit referral home")
    try:
        await context.bot.do_api_request(
            endpoint="sendRichMessage",
            api_kwargs={
                "chat_id": update.effective_chat.id,
                "rich_message": {"html": rich_html},
                "reply_markup": markup,
            },
        )
    except Exception:
        logger.info("Rich referral send unavailable; falling back to HTML", exc_info=True)
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


async def _show_faq(q, topic: str | None = None) -> None:
    """Show a compact FAQ index; each question opens its own readable page."""
    if topic in _FAQ_ITEMS:
        rows = []
        if topic == "1":
            rows.append([InlineKeyboardButton("🧑‍💻 تواصل مع المطوّر", url="https://t.me/K_xv6")])
        elif topic == "2":
            rows.append([InlineKeyboardButton("🔄 تحديث حالة نسختي", callback_data="ref:refresh_status")])
            rows.append([InlineKeyboardButton("🧑‍💻 تواصل مع المطوّر", url="https://t.me/K_xv6")])
        elif topic == "5":
            rows.append([InlineKeyboardButton("📋 نسخ رابط الإحالة", callback_data="ref:copy")])
        elif topic == "6":
            rows.append([InlineKeyboardButton("📖 دليل التفعيل", callback_data="ref:guide")])
        elif topic == "8":
            rows.append([InlineKeyboardButton("🧑‍💻 تواصل مع المطوّر", url="https://t.me/K_xv6")])
        rows.append([InlineKeyboardButton("⬅️ كل الأسئلة", callback_data="ref:faq")])
        rows.append(_back_home_row())
        await _edit_ref(q, _FAQ_ITEMS[topic], InlineKeyboardMarkup(rows))
        return

    rows = [
        [InlineKeyboardButton(_FAQ_TITLES["1"], callback_data="ref:faq:1")],
        [InlineKeyboardButton(_FAQ_TITLES["2"], callback_data="ref:faq:2")],
        [InlineKeyboardButton(_FAQ_TITLES["3"], callback_data="ref:faq:3")],
        [InlineKeyboardButton(_FAQ_TITLES["4"], callback_data="ref:faq:4")],
        [InlineKeyboardButton(_FAQ_TITLES["5"], callback_data="ref:faq:5")],
        [InlineKeyboardButton(_FAQ_TITLES["6"], callback_data="ref:faq:6")],
        [InlineKeyboardButton(_FAQ_TITLES["7"], callback_data="ref:faq:7")],
        [InlineKeyboardButton(_FAQ_TITLES["8"], callback_data="ref:faq:8")],
        [InlineKeyboardButton("🧑‍💻 المطوّر", url="https://t.me/K_xv6")],
        _back_home_row(),
    ]
    await _edit_ref(
        q,
        "❓ <b>مركز المساعدة</b>\n\n"
        "اختار السؤال اللي محتاج إجابته من الأزرار تحت.\n"
        "كل سؤال هيفتح في صفحة لوحده عشان القراءة تبقى أسهل.",
        InlineKeyboardMarkup(rows),
    )

def _rules_rich_html() -> str:
    """Mobile-first rules page using Telegram Rich Message expandable sections."""
    return (
        "<h3>📜 قواعد الإحالات</h3>"
        "<p><b>اقرأ القواعد قبل مشاركة رابطك عشان مكافأتك تتحسب بشكل صحيح.</b></p>"
        "<details><summary>👤 1–2 · ملكية الإحالة ومنع التكرار</summary>"
        "<p>كل حساب تيليجرام يُحتسب مرة واحدة فقط ولأول شخص دعاه.</p>"
        "<p>ممنوع تدعو نفسك. بعد ربط الإحالة، لا تنتقل لمُحيل آخر أبدًا.</p>"
        "</details>"
        "<details><summary>✅ 3 · إمتى المستخدم يبقى مؤهل؟</summary>"
        "<p>يتأهل لو بدأ البوت من رابطك وأكمل أول تحميل ناجح، أو استخدم البوت في يومين مختلفين.</p>"
        "</details>"
        "<details><summary>🚫 4–5 · الحظر ومراجعة النشاط</summary>"
        "<p>أي مستخدم محظور لا يُحتسب.</p>"
        "<p>لو حصلت إحالات كثيرة في وقت قصير جدًا، تتعلّم تحت المراجعة ولا تُحتسب تلقائيًا.</p>"
        "</details>"
        "<details><summary>🎁 6 · المكافآت والخطط</summary>"
        "<p>🆓 <b>Basic:</b> الهدف الأساسي يمنح نسخة خاصة لمدة 30 يومًا.</p>"
        "<p>👑 <b>Pro:</b> ضعف الهدف يمنح نسخة خاصة لمدة 40 يومًا.</p>"
        "<p>التفاصيل الكاملة في صفحة «📦 الخطط والباقات».</p>"
        "</details>"
        "<details><summary>🎟️ أكواد التفعيل والتجربة المجانية</summary>"
        "<p>فعّل كود المالك من «🤖 النسخ الخاصة» ← «🔑 استقبال كود تفعيل».</p>"
        "<p>الكود لا يُستهلك إلا بعد نجاح إنشاء نسختك وتفعيلها بالكامل.</p>"
        "<p>تجربة Basic أو Pro مجانية لمدة 24 ساعة، مرة واحدة لكل حساب.</p>"
        "</details>"
        "<p><b>⚠️ أي غش أو حسابات وهمية ممكن توقف الأهلية نهائيًا.</b></p>"
    )


async def _show_rules(q) -> None:
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 عرض الخطط والباقات", callback_data="ref:plans")],
        [InlineKeyboardButton("🧑‍💻 عندك مشكلة؟ كلم المطوّر", url="https://t.me/K_xv6")],
        _back_home_row(),
    ])
    message = getattr(q, "message", None)
    if message is not None:
        try:
            await q.get_bot().do_api_request(
                endpoint="editMessageText",
                api_kwargs={
                    "chat_id": message.chat_id,
                    "message_id": message.message_id,
                    "rich_message": {"html": _rules_rich_html()},
                    "reply_markup": markup,
                },
            )
            return
        except Exception:
            logger.info("Rich referral rules unavailable; using HTML fallback", exc_info=True)
    await _edit_ref(q, _RULES_TEXT, markup)


def _plans_text(stats: dict) -> str:
    """Full plan-system page: what exists, what it costs, how time works,
    and where THIS user currently stands — one screen, everything visible."""
    from services.referral import clone_plan_days, pro_target
    from services.plan_policy import plan_comparison_lines

    target = int(stats.get("target") or 0)
    pro_tgt = pro_target()
    basic_days = clone_plan_days("basic")
    pro_days = clone_plan_days("pro")
    qualified = int(stats.get("qualified") or 0)
    inst = stats.get("instance") or {}
    compare = "\n".join(plan_comparison_lines())

    text = (
        "📦 <b>الخطط والباقات — كل التفاصيل</b>\n\n"
        "كل نسخة بوت خاصة تعمل بنظام خطة زمنية محددة. الخطط المتاحة:\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🆓 <b>خطة Basic</b>\n"
        f"• المطلوب: <b>{target} مستخدمًا مؤهلًا</b>\n"
        f"• المدة: <b>{basic_days} يومًا</b> من يوم التفعيل\n\n"
        f"👑 <b>خطة Pro</b>\n"
        f"• المطلوب: <b>{pro_tgt} مستخدمًا مؤهلًا</b>\n"
        f"• المدة: <b>{pro_days} يومًا</b> من يوم التفعيل\n"
        "• تُمنح تلقائيًا لحظة التفعيل لو كنت وصلت للعدد المطلوب\n"
        "━━━━━━━━━━━━━━\n\n"
        "⚖️ <b>ما الموجود وما غير الموجود — 🆓 Basic مقابل 👑 Pro:</b>\n"
        f"{compare}\n"
        "• إدارة الأكواد والنسخ الأخرى: 🆓 ❌ — 👑 ❌ (للمالك الرئيسي فقط)\n"
        "━━━━━━━━━━━━━━\n\n"
        "⚙️ <b>إزاي بيتم تحديد خطتك؟</b>\n"
        f"وقت ما تفعّل نسختك، النظام يفحص عدد مؤهلكينك: لو <b>{pro_tgt}+</b> تاخد "
        f"👑 Pro ({pro_days} يومًا)، غير كده تاخد 🆓 Basic ({basic_days} يومًا).\n\n"
        "⏳ <b>نظام المدة والانتهاء:</b>\n"
        "• المدة تُحسب من <b>يوم التفعيل</b> وتظهر لك في صفحة 🎁 الرئيسية.\n"
        "• لما المدة تنتهي، النسخة <b>بتتوقف تلقائيًا</b> من النظام.\n"
        "• التجديد أو التمديد أو ترقية الخطة يتم <b>بقرار المالك</b> — من هنا:\n"
        "   «⌛ طلب تجديد مدة نسختي» والتواصل مع المطوّر.\n\n"
        "📌 <b>حالتك أنت الآن:</b>\n"
    )

    if inst and inst.get("plan"):
        plan_badge = "👑 Pro" if inst.get("plan") == "pro" else "🆓 Basic"
        exp = str(inst.get("expires_at") or "—")[:10]
        days_left = inst.get("days_left")
        if inst.get("status") == "active":
            text += (
                f"• خطتك الحالية: <b>{plan_badge}</b>\n"
                f"• تاريخ الانتهاء: <b>{exp}</b>"
                + (f" — باقي <b>{days_left}</b> يومًا\n" if days_left is not None else "\n")
            )
        else:
            text += f"• خطتك: <b>{plan_badge}</b> — <b>منتهية</b> منذ {exp}\n"
            text += "• للتجديد: اضغط «⌛ طلب تجديد مدة نسختي» وتواصل مع المطوّر.\n"
    else:
        remaining_basic = max(0, target - qualified)
        remaining_pro = max(0, pro_tgt - qualified)
        text += (
            f"• المستخدمون المؤهلون لديك: <b>{qualified}</b>\n"
            f"• للوصول إلى 🆓 Basic: باقي <b>{remaining_basic}</b> مستخدمًا مؤهلًا\n"
            f"• للوصول إلى 👑 Pro: باقي <b>{remaining_pro}</b> مستخدمًا مؤهلًا\n"
        )

    text += (
        "\n💡 <b>ملاحظة:</b> المستخدم المؤهل = بدأ من رابطك وأكمل أول تحميل ناجح، "
        "أو استخدم البوت في يومين مختلفين."
    )
    return text


def _plans_rich_html(stats: dict) -> str:
    """Compact mobile-first plan page: summary first, detailed comparison expandable."""
    from html import escape
    from services.referral import clone_plan_days, pro_target
    from services.plan_policy import PLAN_LIMITS, PLAN_BASIC, PLAN_PRO

    target = int(stats.get("target") or 0)
    pro_goal = int(pro_target())
    basic_days = int(clone_plan_days("basic"))
    pro_days = int(clone_plan_days("pro"))
    qualified = int(stats.get("qualified") or 0)
    b, p = PLAN_LIMITS[PLAN_BASIC], PLAN_LIMITS[PLAN_PRO]
    rows = [
        ("📥 التحميلات اليومية", f"{b['daily_downloads']}", f"{p['daily_downloads']}"),
        ("🔗 روابط الدفعة", f"{b['batch_urls']}", f"{p['batch_urls']}"),
        ("🎬 جودة الفيديو", f"حتى {b['max_video_height']}p", "أعلى جودة"),
        ("🎵 صوت MP3", f"حتى {b['audio_cap_kbps']} kbps", "حتى 320 kbps"),
        ("📚 سجل التحميلات", f"{b['history_days']} أيام", f"{p['history_days']} يومًا"),
        ("🛠️ لوحة الإدارة", "❌", "✅"),
        ("👥 إدارة المستخدمين", "❌", "✅"),
        ("📣 الإذاعة", "❌", "✅"),
        ("🔐 صلاحيات الإداريين", "❌", "✅"),
        ("🧩 إدارة الأكواد والنسخ", "للمالك", "للمالك"),
    ]
    table_rows = ["<tr><th>الميزة</th><th>🆓 Basic</th><th>👑 Pro</th></tr>"]
    for feature, basic, pro in rows:
        table_rows.append(
            "<tr><td>" + escape(feature) + "</td><td>" + escape(str(basic)) +
            "</td><td>" + escape(str(pro)) + "</td></tr>"
        )

    parts = [
        "<h3>📦 الخطط والباقات</h3>",
        "<p>اختر هدفك من الإحالات واعرف مميزات كل خطة قبل التفعيل.</p>",
        "<table bordered striped compact>" + "".join(table_rows) + "</table>",
        "<details><summary>🆓 خطة Basic — الهدف والمدة</summary>",
        f"<p>الهدف: <b>{target} مستخدمًا مؤهلًا</b></p>",
        f"<p>المدة: <b>{basic_days} يومًا</b> من يوم التفعيل.</p></details>",
        "<details><summary>👑 خطة Pro — الهدف والمدة</summary>",
        f"<p>الهدف: <b>{pro_goal} مستخدمًا مؤهلًا</b> (ضعف الهدف الأساسي)</p>",
        f"<p>المدة: <b>{pro_days} يومًا</b> من يوم التفعيل.</p>",
        "<p>تُمنح Pro تلقائيًا عند التفعيل إذا كان عدد المؤهلين قد وصل للهدف.</p></details>",
        "<details><summary>⚙️ كيف تُحدَّد خطتك؟</summary>",
        f"<p>إذا كان لديك {pro_goal} مؤهلًا أو أكثر وقت التفعيل، تحصل على Pro؛ وإلا تحصل على Basic.</p>",
        "</details>",
        "<details><summary>⏳ المدة والانتهاء والتجديد</summary>",
        "<p>تبدأ المدة من يوم التفعيل، وتظهر في الصفحة الرئيسية. عند انتهائها تتوقف النسخة تلقائيًا.</p>",
        "<p>التجديد أو التمديد أو الترقية يتم بقرار المالك بعد التواصل مع المطوّر عبر «⌛ طلب تجديد مدة نسختي».</p>",
        "</details>",
        "<details><summary>📌 حالتك الحالية</summary>",
    ]
    inst = stats.get("instance") or {}
    if inst and inst.get("plan"):
        plan_badge = "👑 Pro" if inst.get("plan") == "pro" else "🆓 Basic"
        expiry = escape(str(inst.get("expires_at") or "—")[:10])
        days_left = inst.get("days_left")
        parts.append(f"<p>خطتك الحالية: <b>{plan_badge}</b></p>")
        parts.append(f"<p>تاريخ الانتهاء: <b>{expiry}</b></p>")
        if days_left is not None:
            parts.append(f"<p>المتبقي: <b>{int(days_left)} يومًا</b></p>")
    else:
        parts.extend([
            f"<p>المستخدمون المؤهلون: <b>{qualified}</b></p>",
            f"<p>المتبقي لـ Basic: <b>{max(0, target - qualified)}</b> مستخدمًا مؤهلًا.</p>",
            f"<p>المتبقي لـ Pro: <b>{max(0, pro_goal - qualified)}</b> مستخدمًا مؤهلًا.</p>",
        ])
    parts.extend([
        "</details>",
        "<p><i>المستخدم المؤهل هو من بدأ عبر رابطك وأكمل أول تحميل ناجح، أو استخدم البوت في يومين مختلفين.</i></p>",
        "<p>إدارة الأكواد والنسخ الأخرى متاحة للمالك الرئيسي فقط، حتى في Pro.</p>",
    ])
    return "".join(parts)


async def _show_plans(q) -> None:
    """📦 Full plan-system overview for any user (ref:plans)."""
    stats = referral.get_stats(q.from_user.id)
    rows = [_back_home_row()]
    inst = stats.get("instance") or {}
    if inst and inst.get("status") == "expired":
        rows.insert(0, [InlineKeyboardButton("⌛ طلب تجديد مدة نسختي", url="https://t.me/K_xv6")])
    elif not inst:
        rows.insert(0, [InlineKeyboardButton("📋 نسخ رابط الإحالة", callback_data="ref:copy")])
    markup = InlineKeyboardMarkup(rows)
    message = getattr(q, "message", None)
    if message is not None:
        try:
            await q.get_bot().do_api_request(
                endpoint="editMessageText",
                api_kwargs={
                    "chat_id": message.chat_id,
                    "message_id": message.message_id,
                    "rich_message": {"html": _plans_rich_html(stats)},
                    "reply_markup": markup,
                },
            )
            return
        except Exception:
            logger.info("Rich referral plans unavailable; using HTML fallback", exc_info=True)
    await _edit_ref(q, _plans_text(stats), markup)


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
                "• لو استمرت المشكلة، تواصل مع المطوّر @K_xv6."
            )
            rows = [
                [InlineKeyboardButton("🔑 إعادة ربط التوكن", callback_data="ref:claim")],
                [InlineKeyboardButton("🧑‍💻 المطوّر", url="https://t.me/K_xv6")],
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
    """Send user-facing help requests directly to the developer."""
    await _edit_ref(
        q,
        "🧑‍💻 <b>التواصل مع المطوّر</b>\n\n"
        "لو قابلتك مشكلة، ابعت للمطوّر @K_xv6 وصف المشكلة ورسالة الخطأ أو صورة منها.\n\n"
        "🔐 ما تبعتش توكن البوت أو أي بيانات سرية.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 مراسلة المطوّر @K_xv6", url="https://t.me/K_xv6")],
            _back_home_row(),
        ]),
    )


async def _show_support_new(q) -> None:
    await _show_support_home(q)


async def _open_support_ticket(q, category: str) -> None:
    await _show_support_home(q)


async def _render_support_ticket(q, ticket_id: int, just_opened: bool = False) -> None:
    await _show_support_home(q)


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
    if data.startswith("ref:faq:"):
        topic = data.rsplit(":", 1)[-1]
        await _show_faq(q, topic)
        return

    if data == "ref:rules":
        await _show_rules(q)
        return

    if data == "ref:plans":
        await _show_plans(q)
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
        cr = context.user_data.pop("code_redeem", None)
        if cr:
            # A cancelled activation must NEVER consume a code use or a trial.
            try:
                if cr.get("kind") == "trial":
                    redemption.release_trial(uid, "user_cancelled")
                else:
                    redemption.release_reservation(uid, int(cr["code_id"]), "user_cancelled")
            except Exception:
                logger.exception("code reservation release failed")
        context.user_data.pop("awaiting_token", None)
        context.user_data.pop("token_attempts", None)
        kept = ""
        if cr:
            kept = (" تجربتك محفوظة ولم تُستهلك — تقدر تبدأها في أي وقت."
                    if cr.get("kind") == "trial" else
                    " الكود محفوظ ولم يُستهلك — تقدر ترجع تبدأ التفعيل في أي وقت.")
        await q.edit_message_text(
            "❌ تم إلغاء ربط التوكن." + kept,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="ref:home")]]),
        )
        return

    if data == "ref:private":
        await _render_private_hub(update, context, q)
        return

    if data == "ref:trial":
        if q.message.chat.type != "private":
            await q.answer("⚠️ التجربة المجانية تتم في المحادثة الخاصة فقط.", show_alert=True)
            return
        ok, reason = redemption.trial_eligible(uid)
        if not ok:
            text = ("⚠️ <b>لقد استخدمت تجربتك المجانية من قبل.</b>\n"
                    "التجربة مرة واحدة فقط لكل حساب — للحصول على نسخة دائمة "
                    "استخدم 🔑 كود تفعيل أو 🏆 مكافأة الإحالات."
                    if reason == "trial_used" else
                    "🤖 <b>لديك نسخة في النظام بالفعل.</b>\n"
                    "التجربة مخصصة لمن لم يملك نسخة من قبل.")
            await q.edit_message_text(
                text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([_back_home_row()]),
            )
            return
        await q.edit_message_text(
            _TRIAL_TERMS_TEXT,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🆓 تجربة Basic (24 ساعة)", callback_data="ref:trial:basic"),
                 InlineKeyboardButton("👑 تجربة Pro (24 ساعة)", callback_data="ref:trial:pro")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="ref:private")],
            ]),
        )
        return

    if data in ("ref:trial:basic", "ref:trial:pro"):
        if q.message.chat.type != "private":
            await q.answer("⚠️ التجربة المجانية تتم في المحادثة الخاصة فقط.", show_alert=True)
            return
        plan = "pro" if data.endswith("pro") else "basic"
        res = redemption.start_trial(uid, plan)
        if not res.get("ok"):
            reason = res.get("reason", "")
            text = ("⚠️ <b>لقد استخدمت تجربتك المجانية من قبل.</b>"
                    if reason == "trial_used" else
                    "🤖 <b>لديك نسخة في النظام بالفعل.</b>")
            await q.edit_message_text(
                text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([_back_home_row()]),
            )
            return
        plan_badge = "👑 Pro" if plan == "pro" else "🆓 Basic"
        # The trial rides the SAME secure token flow as codes/rewards.
        context.user_data["code_redeem"] = {
            "kind": "trial",
            "instance_id": res["instance_id"],
            "plan": plan,
            "runtime_days": 1,  # 24 hours
        }
        context.user_data["awaiting_token"] = True
        context.user_data["token_attempts"] = 0
        await q.edit_message_text(
            "✅ <b>تم فتح تجربتك المجانية!</b>\n\n"
            f"📦 نسختك: <b>{plan_badge}</b> — <b>24 ساعة</b> من لحظة ربط التوكن.\n\n"
            "🔑 <b>الخطوة الأخيرة: ربط توكن بوتك الجديد.</b>\n"
            "1️⃣ افتح @BotFather وأنشئ بوتًا بـ <code>/newbot</code>\n"
            "2️⃣ أرسل التوكن هنا مباشرة — رسالتك هتتمسح فورًا والتوكن بيتخزن مشفرًا.\n\n"
            "⏳ مدة التجربة (24 ساعة) تبدأ <b>بعد نجاح ربط التوكن</b> — "
            "ولو ألغيت الآن تجربتك تفضل محفوظة.",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ إلغاء التجربة", callback_data="ref:cancel_token")],
            ]),
        )
        return

    if data == "ref:code":
        stats = referral.get_stats(uid)
        inst = stats.get("instance")
        if inst and inst.get("status") == "active" and inst.get("bot_username"):
            await q.edit_message_text(
                "🤖 <b>لديك نسخة نشطة بالفعل!</b>\n\n"
                f"بوتك: <a href=\"https://t.me/{inst['bot_username']}\">@{inst['bot_username']}</a>\n"
                "الكود مخصص لمن لا يملك نسخة عاملة — لو نسختك منتهية المدة، "
                "التجديد يتم بقرار المالك بعد التواصل مع المطوّر.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[_back_home_row()[0]]]),
            )
            return
        if q.message.chat.type != "private":
            await q.answer("⚠️ تفعيل الأكواد يتم في المحادثة الخاصة فقط.", show_alert=True)
            return
        context.user_data["awaiting_code"] = True
        await q.edit_message_text(
            "🔑 <b>تفعيل كود</b>\n\n"
            "أرسل الآن <b>كود التفعيل</b> الذي استلمته من المالك مباشرة هنا "
            "في هذه المحادثة الخاصة.\n\n"
            "• الكود يُستخدم مرة واحدة فقط لكل مستخدم.\n"
            "• بعد التحقق من الكود ستُطلب منك ربط توكن بوتك الجديد.\n"
            "• <b>لا يُستهلك الكود إلا بعد نجاح إنشاء نسختك وتفعيلها كاملًا.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ إلغاء", callback_data="ref:code:cancel")],
            ]),
        )
        return

    if data == "ref:code:cancel":
        context.user_data.pop("awaiting_code", None)
        context.user_data.pop("code_attempts", None)
        await q.edit_message_text(
            "❌ تم إلغاء تفعيل الكود.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[_back_home_row()[0]]]),
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
            # Show a FULL progress screen instead of a fleeting toast — the
            # user must see exactly where they stand toward their own clone:
            # the dynamic target ("توصل لـ N مستخدم نشط"), what they reached
            # so far, what remains, and how a user becomes qualified.
            remaining = max(0, int(stats.get("target", 0)) - int(stats.get("qualified", 0)))
            percent = int(stats.get("percent", 0))
            bar_len = 14
            filled = int(percent / 100 * bar_len)
            bar = "█" * filled + "░" * (bar_len - filled)
            await q.answer()
            await q.edit_message_text(
                "🎁 <b>نسختك الخاصة — حالة التقدم</b>\n\n"
                f"🎯 الهدف: أن تصل إلى <b>{stats.get('target', 0)} مستخدمًا نشطًا (مؤهلًا)</b>\n"
                f"✅ أنت وصلت لـ: <b>{stats.get('qualified', 0)} مستخدمًا مؤهلًا</b>\n"
                f"🚀 باقي لك: <b>{remaining}</b> مستخدمًا\n"
                f"📊 نسبة الإنجاز: <b>{percent}%</b>  <code>{bar}</code>\n"
                f"⏳ بانتظار النشاط: <b>{stats.get('pending', 0)}</b>"
                " (سجلوا عبر رابطك ولم يكتمل تأهيلهم بعد)\n\n"
                "📌 <b>كيف يُحتسب المستخدم مؤهلًا؟</b>\n"
                "• يقوم بأول تحميل ناجح عبر البوت — أو\n"
                "• يستخدم البوت في يومين مختلفين.\n\n"
                "💡 شارك رابط دعوتك الآن — كل مستخدم مؤهل يقربك خطوة من امتلاك نسختك الخاصة!",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 نسخ رابط الإحالة", callback_data="ref:copy")],
                    [InlineKeyboardButton("📊 إحصائيات الإحالات", callback_data="ref:stats")],
                    [InlineKeyboardButton("🔙 رجوع", callback_data="ref:home")],
                ]),
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


_CODE_REASON_AR = {
    "shape": "🔤 ده مش شكل كود تفعيل صحيح.\nالكود شكله: <code>BASIC-7K9M-X4PQ</code> أو <code>PRO-7K9M-X4PQ</code> — انسخه كاملًا كما أرسله لك المالك.",
    "not_found": "❌ الكود غير صحيح أو غير موجود.\nتأكد من نسخه كاملًا بدون مسافات زائدة، ولو المشكلة مستمرة تواصل مع المالك.",
    "revoked": "⛔ هذا الكود <b>ملغى</b> بقرار المالك ولا يمكن استخدامه.",
    "expired": "⌛ <b>انتهت صلاحية هذا الكود</b> ولم يعد قابلًا للتفعيل.",
    "exhausted": "🈳 <b>تم استنفاد عدد استخدامات هذا الكود.</b>",
    "limit_reached": "🈳 عذرًا، تم استخدام آخر إتاحة من هذا الكود أثناء محاولتك.\nتواصل مع المالك للحصول على كود جديد.",
    "already_used": "⚠️ <b>لقد استخدمت هذا الكود من قبل.</b> كل كود يُستخدم مرة واحدة فقط لكل مستخدم.",
    "not_for_you": "👤 هذا الكود <b>مخصص لمستخدم آخر</b> ولا يمكن لحسابك استخدامه.",
    "has_active_instance": "🤖 لديك نسخة نشطة بالفعل — الكود مخصص لمن لا يملك نسخة عاملة.",
    "pending_flow": "⏳ لديك محاولة تفعيل أخرى قيد الانتظار — أكملها أو ألغها أولًا"
                    " (من «🤖 النسخ الخاصة») ثم فعّل هذا الكود.",
}


async def _render_private_hub(update: Update, context: ContextTypes.DEFAULT_TYPE, q) -> None:
    """🤖 النسخ الخاصة — choose HOW to get a private clone:

      * 🎁 تجربة مجانية — 24h basic/pro trial (once per user, ever)
      * 🔑 استقبال كود تفعيل — owner-issued redemption code
      * 🏆 مكافأة الإحالات — the qualified-referrals reward
      * 📊 نسختي الخاصة — plan / status / expiry / renewal card
    """
    uid = q.from_user.id
    stats = referral.get_stats(uid)
    inst = stats.get("instance") or {}
    stage = referral.referrer_stage(stats)

    # Active clone → the "نسختي الخاصة" card directly.
    if stage == "active" and inst.get("bot_username"):
        plan_badge = "👑 Pro" if inst.get("plan") == "pro" else "🆓 Basic"
        exp = str(inst.get("expires_at") or "")[:16]
        days_left = inst.get("days_left")
        text = (
            "🤖 <b>نسختي الخاصة</b>\n\n"
            f"📦 نوع النسخة: <b>{plan_badge}</b>\n"
            "🟢 الحالة: <b>نشطة</b>\n"
            + (f"⌛ تنتهي خلال <b>{days_left}</b> يومًا ({exp})\n" if days_left is not None
               else (f"⌛ تنتهي في {exp}\n" if exp else ""))
            + f"🔗 البوت: <a href=\"https://t.me/{inst['bot_username']}\">@{inst['bot_username']}</a>\n\n"
            "التجديد يتم بقرار المالك — لو نسختك اقتربت من الانتهاء تواصل مع المطوّر @K_xv6."
        )
        rows = [
            [InlineKeyboardButton("🤖 افتح نسختك", url=f"https://t.me/{inst['bot_username']}")],
            [InlineKeyboardButton("🔄 تحديث حالة نسختي", callback_data="ref:refresh_status"),
             InlineKeyboardButton("✅ تحقق من التفعيل", callback_data="ref:verify_activation")],
            [InlineKeyboardButton("🧑‍💻 المطوّر", url="https://t.me/K_xv6")],
            _back_home_row(),
        ]
        await _edit_ref(q, text, InlineKeyboardMarkup(rows))
        return

    trial = redemption.trial_status(uid)
    trial_ok, trial_reason = redemption.trial_eligible(uid)
    text = (
        "🤖 <b>النسخ الخاصة</b>\n\n"
        "اختر الطريقة المناسبة للحصول على نسختك الخاصة:\n\n"
        "🎁 <b>تجربة مجانية</b> — جرّب النسخة الأساسية أو البرو لمدة <b>24 ساعة</b>"
        " (مرة واحدة لكل مستخدم).\n"
        "🔑 <b>استقبال كود تفعيل</b> — أدخل الكود الذي حصلت عليه من المالك.\n"
        "🏆 <b>مكافأة الإحالات</b> — اجمع المستخدمين المؤهلين وافتح نسختك مجانًا.\n\n"
        + (f"📊 تجربتك: <b>مستخدمة</b> ({str(trial.get('created_at') or '')[:10]})\n"
           if trial.get("used") else
           ("🎁 التجربة المجانية: <b>متاحة لك</b>\n" if trial_ok
            else ("⏳ التجربة المجانية: متوقفة مؤقتًا — عندك خطوة تفعيل قيد الانتظار،"
                  " أكملها أو ألغها أولًا.\n" if trial_reason == "pending_flow"
                  else "🎁 التجربة المجانية: غير متاحة (لديك نسخة في النظام)\n")))
    )
    rows = []
    if trial_ok:
        rows.append([InlineKeyboardButton("🎁 تجربة مجانية (24 ساعة)", callback_data="ref:trial")])
    rows.append([InlineKeyboardButton("🔑 استقبال كود تفعيل", callback_data="ref:code")])
    if stage in ("eligible", "awaiting_token"):
        label = "🚀 فعّل نسختك الآن" if stage == "eligible" else "🔑 ربط توكن البوت"
        rows.append([InlineKeyboardButton(f"🏆 مكافأة الإحالات — {label}", callback_data="ref:claim")])
    elif stage == "expired":
        rows.append([InlineKeyboardButton("⌛ طلب تجديد مدة نسختي", url="https://t.me/K_xv6")])
    else:
        remaining = max(0, int(stats.get("target", 0)) - int(stats.get("qualified", 0)))
        rows.append([InlineKeyboardButton(
            f"🏆 مكافأة الإحالات ({remaining} متبقي)", callback_data="ref:claim")])
    if inst:
        rows.append([InlineKeyboardButton("📊 حالة نسختي", callback_data="ref:refresh_status")])
    rows.append(_back_home_row())
    await _edit_ref(q, text, InlineKeyboardMarkup(rows))


_TRIAL_TERMS_TEXT = (
    "🎁 <b>التجربة المجانية — 24 ساعة</b>\n\n"
    "جرّب نسخة خاصة كاملة القوة لمدة <b>24 ساعة</b> قبل أن تقرر.\n\n"
    "📌 <b>شروط التجربة:</b>\n"
    "• التجربة <b>مرة واحدة فقط</b> لكل حساب تيليجرام — نهائيًا.\n"
    "• تحتاج بوتًا جديدًا من @BotFather وربط توكنه هنا (مشفَّر ومحذوف من المحادثة).\n"
    "• بعد 24 ساعة تتوقف النسخة تلقائيًا بدون أي إجراء منك.\n"
    "• بعدها يمكنك الحصول على نسخة دائمة عبر 🔑 كود تفعيل أو 🏆 مكافأة الإحالات.\n\n"
    "اختر نوع النسخة التي تريد تجربتها:"
)


async def try_handle_code_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Secure redemption-code capture (mirrors the token capture pattern).

    Returns True when the message was consumed. The message is deleted
    first; the code text never appears in any log row. A successful check
    atomically RESERVES one use — it is only confirmed as consumed after
    the clone is fully activated (see try_handle_token_message).
    """
    if not context.user_data.get("awaiting_code"):
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
    code_text = msg.text.strip()

    await msg.reply_text("⏳ جارٍ التحقق من الكود…")
    result = redemption.redeem_code(uid, code_text)
    if not result.get("ok"):
        reason = result.get("reason", "")
        text = _CODE_REASON_AR.get(reason, "❌ تعذر تفعيل هذا الكود. تواصل مع المالك للتأكد من صلاحيته.")
        # Typos happen — RECOVERABLE reasons keep the capture open (up to
        # 5 attempts, same policy as the token flow). Definitive reasons
        # (expired / revoked / targeted / duplicate flow…) close it: only
        # the owner or the user's own state can resolve those.
        attempts = int(context.user_data.get("code_attempts", 0)) + 1
        context.user_data["code_attempts"] = attempts
        if reason in ("shape", "not_found") and attempts < _MAX_CODE_ATTEMPTS:
            context.user_data["awaiting_code"] = True  # re-arm the capture
            text += f"\n\n🔁 أرسل الكود الصحيح الآن — المحاولة {attempts} من {_MAX_CODE_ATTEMPTS}."
        else:
            context.user_data.pop("awaiting_code", None)
            context.user_data.pop("code_attempts", None)
            if reason in ("shape", "not_found"):
                text += ("\n\n⛔ تم إيقاف المحاولات بعد 5 مرات — "
                         "تقدر تبدأ التفعيل مجددًا من «🤖 النسخ الخاصة».")
        await msg.reply_text(text, parse_mode="HTML")
        return True

    context.user_data.pop("awaiting_code", None)
    context.user_data.pop("code_attempts", None)

    # Slot reserved + pending instance created → straight into the SAME
    # secure token-linking flow used by referral rewards.
    plan = result.get("plan") or "basic"
    plan_badge = "👑 Pro" if plan == "pro" else "🆓 Basic"
    context.user_data["code_redeem"] = {
        "code_id": result["code_id"],
        "plan": plan,
        "runtime_days": int(result.get("runtime_days") or 30),
        "instance_id": result["instance_id"],
    }
    context.user_data["awaiting_token"] = True
    context.user_data["token_attempts"] = 0
    await msg.reply_text(
        "✅ <b>الكود صحيح وتم قبوله!</b>\n\n"
        f"📦 خطتك: <b>{plan_badge}</b> — مدة التشغيل <b>{int(result.get('runtime_days') or 30)} يومًا</b> من لحظة التفعيل.\n\n"
        "🔑 <b>الخطوة الأخيرة: ربط توكن بوتك الجديد.</b>\n"
        "1️⃣ افتح @BotFather وأنشئ بوتًا بـ <code>/newbot</code>\n"
        "2️⃣ أرسل التوكن هنا مباشرة — رسالتك هتتمسح فورًا والتوكن بيتخزن مشفرًا.\n\n"
        "⚠️ <b>لا يُستهلك الكود إلا بعد نجاح إنشاء نسختك وتفعيلها كاملًا.</b>",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء التفعيل", callback_data="ref:cancel_token")],
        ]),
    )
    return True


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
    cr = context.user_data.get("code_redeem")
    if cr:
        # Code activation: pre-created instance + the code's plan/window.
        result = referral.verify_and_link_token(
            uid, token_text,
            instance_id=int(cr["instance_id"]),
            plan=cr.get("plan"),
            days=int(cr.get("runtime_days") or 0) or None,
        )
    else:
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
        if reason == "has_other_instance":
            # Another of this user's instances is active/mid-activation —
            # retrying can't help. Close this flow and RETURN the reserved
            # code use / trial chance instead of burning 5 pointless tries.
            failed_cr = context.user_data.pop("code_redeem", None)
            context.user_data.pop("awaiting_token", None)
            context.user_data.pop("token_attempts", None)
            if failed_cr:
                try:
                    if failed_cr.get("kind") == "trial":
                        redemption.release_trial(uid, "blocked_other_instance")
                    else:
                        redemption.release_reservation(uid, int(failed_cr["code_id"]),
                                                       "blocked_other_instance")
                except Exception:
                    logger.exception("code/trial release failed")
            text = ("⚠️ <b>لديك نسخة أخرى نشطة أو قيد التفعيل</b> — لا يمكن ربط نسختين "
                    "لنفس الحساب.\nأنهِ أو ألغِ المحاولة الأخرى أولًا، أو تواصل مع المطوّر @K_xv6."
                    + ("\n\n✅ الكود محفوظ ولم يُستهلك." if failed_cr else ""))
        elif attempts >= _MAX_TOKEN_ATTEMPTS:
            context.user_data.pop("awaiting_token", None)
            context.user_data.pop("token_attempts", None)
            failed_cr = context.user_data.pop("code_redeem", None)
            if failed_cr:
                # A FAILED activation must never consume the code's use —
                # and never burns the user's one-ever trial either.
                try:
                    if failed_cr.get("kind") == "trial":
                        redemption.release_trial(uid, "token_attempts_exhausted")
                    else:
                        redemption.release_reservation(uid, int(failed_cr["code_id"]),
                                                       "token_attempts_exhausted")
                except Exception:
                    logger.exception("code/trial release failed")
            text += "\n\n⛔ تم إيقاف المحاولات بعد 5 مرات — تقدر تبدأ الربط مجددًا من صفحة 🎁."
        await msg.reply_text(text, parse_mode="HTML")
        return True

    # Verified + stored. Spawn the clone instance live inside this process.
    bot_username = result.get("bot_username", "")
    cr = context.user_data.pop("code_redeem", None)
    context.user_data.pop("awaiting_token", None)
    context.user_data.pop("token_attempts", None)
    if cr:
        if cr.get("kind") == "trial":
            # The registry row already marks the trial consumed forever;
            # record the activation moment. The 24h window starts NOW.
            try:
                redemption.confirm_trial(uid, int(result["instance_id"]), bot_username)
            except Exception:
                logger.exception("trial confirm failed")
        else:
            # NOW — and only now — the code's use is confirmed as consumed.
            try:
                redemption.confirm_use(uid, int(cr["code_id"]), int(result["instance_id"]), bot_username)
            except Exception:
                logger.exception("code confirm_use failed")

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
    plan_line = ""
    closing = f"شكرًا إنك كبرت المجتمع — {referral.get_stats(uid)['qualified']} مستخدمًا مؤهلًا بفضلك! 💪"
    # Plan features — rendered FROM the enforced limits table itself, so what
    # the owner reads is EXACTLY what the server applies inside their clone
    # ("ياخد مميزات الخطة بالملي"): Basic = 70/3/720p/7 أيام بلا لوحة،
    # Pro = 150/10/أعلى جودة/90 يومًا + لوحة إدارة معزولة + تعيين تلقائي.
    from services import plan_policy as _pp
    _plan = str(result.get("plan") or "basic")
    lim = _pp.limits(_plan)
    plan_badge = "👑 Pro" if _plan == "pro" else "🆓 Basic"
    q_line = "أعلى جودة متاحة" if not lim["max_video_height"] else f"{lim['max_video_height']}p كحد أقصى"
    a_line = "320kbps كامل" if not lim["audio_cap_kbps"] else f"{lim['audio_cap_kbps']}kbps كحد أقصى"
    panel_line = (
        "• لوحة إدارة خاصة: ✅ <b>تم تعيينك مديرًا لنسختك تلقائيًا</b> — «🛠️ لوحة التحكم» داخل بوتك\n"
        if _plan == "pro" else
        "• لوحة إدارة خاصة: ❌ غير متوفرة (تتوفر مع خطة 👑 Pro)\n"
    )
    feats = (
        f"📋 <b>مميزات خطة {plan_badge} — مطبقة تلقائيًا بالكامل داخل نسختك:</b>\n"
        f"• تحميلات يومية لكل مستخدم: <b>{lim['daily_downloads']}</b>\n"
        f"• روابط في الدفعة الواحدة: <b>{lim['batch_urls']}</b>\n"
        f"• جودة الفيديو: <b>{q_line}</b>\n"
        f"• نقاء الصوت MP3: <b>{a_line}</b>\n"
        f"• سجل التحميلات: <b>{lim['history_days']} يومًا</b>\n"
        f"{panel_line}\n"
    )
    if cr:
        exp = str(result.get("expires_at") or "")[:16]
        if cr.get("kind") == "trial":
            plan_line = (
                f"📦 تجربتك: <b>{plan_badge}</b> — مدة <b>24 ساعة</b> بدأت الآن"
                + (f" وتنتهي في <b>{exp}</b>.\n" if exp else ".\n")
            )
            closing = ("⏳ بعد 24 ساعة ستتوقف النسخة تلقائيًا.\n"
                       "حابب نسخة دائمة؟ 🔑 فعّل كود من المالك أو 🏆 أكمل هدف الإحالات.")
        else:
            plan_line = (
                f"📦 خطتك: <b>{plan_badge}</b> — مدة التشغيل تبدأ الآن"
                + (f" وتنتهي في <b>{exp}</b>.\n" if exp else ".\n")
            )
            closing = "✅ تم احتساب استخدام واحد من الكود بعد نجاح إنشاء نسختك وتفعيلها كاملًا. 🚀"
    else:
        plan_line = f"📦 خطتك: <b>{plan_badge}</b>\n"
    await msg.reply_text(
        f"🎉 <b>مبروك! نسختك الخاصة اتفعلت.</b>\n\n"
        f"{status_line}\n"
        f"🤖 بوتك: <a href=\"https://t.me/{bot_username}\">@{bot_username}</a>\n\n"
        f"{plan_line}"
        f"{feats}"
        f"✅ تم التحقق من ملكية البوت وصحة التوكن.\n"
        f"🔐 التوكن مخزن مشفرًا ومحذوف من المحادثة.\n"
        f"👥 نسختك فيها زر «العودة إلى البوت الأساسي»، وكل مستخدم جديد بيبدأ منها يتسجل في شبكة الإحالات.\n\n"
        f"{closing}",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🤖 افتح نسختك", url=f"https://t.me/{bot_username}")],
            [InlineKeyboardButton("🔙 العودة لصفحة المكافأة", callback_data="ref:home")],
        ]),
    )
    return True
