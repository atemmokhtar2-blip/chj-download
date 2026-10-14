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
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config.settings import MAIN_BOT_USERNAME, REFERRAL_TARGET
from services import referral
from services.instance_manager import is_clone_bot, spawn_instance

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    "pending": "⏳ بانتظار النشاط",
    "qualified": "✅ مؤهل",
    "suspicious": "🚩 تحت المراجعة",
}

_MAX_TOKEN_ATTEMPTS = 5


def _main_bot_url() -> str:
    return f"https://t.me/{MAIN_BOT_USERNAME}" if MAIN_BOT_USERNAME else ""


def _rows_for(stats: dict, bot_username: str | None) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("📋 نسخ رابط الإحالة", callback_data="ref:copy"),
            InlineKeyboardButton("📊 إحصائيات الإحالات", callback_data="ref:stats"),
        ],
    ]
    inst = stats.get("instance")
    status = stats.get("reward_status", "progress")
    if inst and inst.get("status") == "active" and inst.get("bot_username"):
        rows.append([InlineKeyboardButton(
            f"🤖 نسختك جاهزة: @{inst['bot_username']}",
            url=f"https://t.me/{inst['bot_username']}",
        )])
    elif status == "eligible":
        rows.append([InlineKeyboardButton("🚀 فعّل نسختك الآن", callback_data="ref:claim")])
    elif status in ("awaiting_token", "claimed"):
        rows.append([InlineKeyboardButton("🔑 ربط توكن البوت", callback_data="ref:claim")])
    else:
        rows.append([InlineKeyboardButton("🎁 طلب نسختي الخاصة", callback_data="ref:claim")])

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
    return (
        "🎁 <b>اكسب نسختك من البوت</b>\n\n"
        f"🎯 هدفك: <b>{stats['target']} مستخدمًا مؤهلًا</b>\n"
        f"👥 المستخدمون المؤهلون: <b>{stats['qualified']} / {stats['target']}</b>\n"
        f"📊 التقدم: <b>{stats['percent']}%</b>  <code>{bar}</code>\n"
        f"⏳ بانتظار النشاط: {stats['pending']}\n\n"
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
