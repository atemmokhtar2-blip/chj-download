"""👑 Clone admin panel — Pro plan ONLY, scoped to ONE instance.

Everything is re-verified server-side on every single press:
  * the instance is resolved from the RUNNING bot (``bot_instances.bot_id``),
    NEVER from callback data — a forged callback can never reach another
    clone or the main system;
  * the plan must be ``pro`` (Basic clones get an explanatory message and
    nothing else — no panel, no actions);
  * the caller must be the instance OWNER or one of ITS ``clone_admins``,
    and each action re-checks the specific permission it needs
    (can_ban / can_broadcast / can_stats);
  * the global ADMIN_IDS / OWNER_ID grant NOTHING here, and nothing here
    grants anything on the main bot or on other clones.

Sections (spec): users • download stats • clone settings • broadcast •
admins & permissions • bot status • admin audit log (+CSV) • subscription.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from html import escape

from telegram import InputFile, Update
from telegram.ext import ContextTypes

from services import plan_policy as pp
from utils.button_text import inline_button
from utils.telegram_safe import safe_edit

logger = logging.getLogger(__name__)

# Text-capture flows (broadcast body, welcome message, add-admin id, user
# search). Keyed by (bot.id, chat.id) so every clone keeps its own state and
# two admins can never cross-capture. TTL'd so a stale prompt never swallows
# an ordinary message forever.
_pending: dict[tuple[int, int], dict] = {}
_drafts: dict[tuple[int, int], dict] = {}
_CAPTURE_TTL = 600  # 10 minutes

BROADCAST_PAUSE_EVERY = 20
BROADCAST_PAUSE_SECONDS = 1.0
USERS_PAGE = 8


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _fmt_dt(value) -> str:
    if not value:
        return "—"
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)[:16]


def _rows(buttons: list[list[tuple[str, dict]]]):
    from telegram import InlineKeyboardMarkup
    return InlineKeyboardMarkup(
        [[inline_button(text, **kw) for text, kw in row] for row in buttons])


def _label(name: str, uid: int) -> str:
    name = (name or "").strip() or f"مستخدم {uid}"
    return escape(name[:40])


async def _deny(query, text: str) -> None:
    try:
        await query.answer(text, show_alert=True)
    except Exception:
        pass


def _gate(context, user_id: int):
    """Server-side gate for ANY panel interaction.

    Returns (ctx, instance, access) or None when access must be denied.
    The instance always comes from the RUNNING bot — never from input.
    """
    ctx = pp.plan_context(context.bot)
    if not ctx.get("is_clone"):
        return None
    inst = ctx.get("instance")
    if not inst:
        return None
    acc = pp.clone_access(inst, user_id)
    if acc == "none":
        return None
    return ctx, inst, acc


def _allows_users(inst: dict, uid: int) -> bool:
    """May this admin VIEW the clone's user directory?

    Browsing serves two distinct workflows: banning (can_ban) and user
    analytics (can_stats). Either permission grants read access — the ban
    ACTION itself still re-checks can_ban separately on every press, so
    this never widens what an admin can DO, only what they can see.
    """
    return (pp.perm_allows(inst, uid, "can_stats")
            or pp.perm_allows(inst, uid, "can_ban"))


def _plan_blocked_text() -> str:
    return (
        "\U0001F193 <b>نسختك تعمل بخطة Basic</b>\n\n"
        "لوحة الإدارة وإدارة المستخدمين والإذاعة والإداريون — "
        "كلها متاحة في خطة \U0001F451 Pro.\n\n"
        "<b>المتاح لك الآن:</b>\n"
        "• كل ميزات التحميل داخل حدود خطتك "
        "(70 تحميل يومي • 3 روابط في الدفعة • حتى 720p).\n"
        "• إعدادات حسابك الشخصية من /settings.\n\n"
        "للترقية: افتح تذكرة دعم \U0001F6DF في البوت الأساسي."
    )


def _plan_blocked_keyboard():
    from config.settings import MAIN_BOT_USERNAME
    rows = []
    if MAIN_BOT_USERNAME:
        rows.append([("🏠 العودة إلى البوت الأساسي",
                      {"url": f"https://t.me/{MAIN_BOT_USERNAME}"})])
    return _rows(rows) if rows else None


# --------------------------------------------------------------------------
# entry point (/admin and every admin command inside a clone)
# --------------------------------------------------------------------------

async def clone_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Every admin command inside a clone lands here (delegated from
    handlers.admin). Basic plan -> explanation only. Pro -> gated panel."""
    user = update.effective_user
    ctx = pp.plan_context(context.bot)

    if not ctx.get("is_clone") or not ctx.get("instance"):
        if update.message:
            await update.message.reply_text("⛔ هذا الأمر غير متاح هنا.")
        return

    inst = ctx["instance"]
    if ctx["plan"] != pp.PLAN_PRO:
        if update.message:
            await update.message.reply_text(
                _plan_blocked_text(), parse_mode="HTML",
                reply_markup=_plan_blocked_keyboard())
        return

    acc = pp.clone_access(inst, user.id)
    if acc == "none":
        if update.message:
            await update.message.reply_text(
                "⛔ لوحة إدارة النسخة متاحة لصاحب النسخة وإدارييها فقط.")
        return

    text, kb = await _render_home(ctx, inst, acc)
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# --------------------------------------------------------------------------
# section renderers
# --------------------------------------------------------------------------

async def _render_home(ctx: dict, inst: dict, acc: str):
    summary = pp.clone_today_summary(inst)
    status_ar = {"active": "يعمل ✅", "expired": "منتهية ⌛"}.get(
        str(inst.get("status")), str(inst.get("status")))
    text = (
        "\U0001F451 <b>لوحة إدارة نسختك</b>\n"
        f"\U0001F4E6 الخطة: {pp.PLAN_LABELS['pro']} • حالة البوت: {escape(status_ar)}\n\n"
        f"\U0001F465 مستخدمو نسختك: <b>{pp.clone_users_count(inst)}</b>\n"
        f"⬇️ تحميلات اليوم: <b>{summary['downloads_today']}</b>\n"
        f"❌ محاولات فاشلة اليوم: <b>{summary['failures_today']}</b>\n\n"
        f"صلاحيتك: {'صاحب النسخة' if acc == 'owner' else 'إداري'}"
    )
    kb = _rows([
        [("👥 المستخدمون", {"callback_data": "ca:users:0"}),
         ("📊 الإحصائيات", {"callback_data": "ca:stats"})],
        [("⚙️ إعدادات النسخة", {"callback_data": "ca:settings"}),
         ("📢 إذاعة", {"callback_data": "ca:broadcast"})],
        [("🛡️ الإداريون", {"callback_data": "ca:admins"}),
         ("🩺 حالة البوت", {"callback_data": "ca:status"})],
        [("🧾 سجل الإدارة", {"callback_data": "ca:log"}),
         ("💳 الاشتراك", {"callback_data": "ca:subscription"})],
    ])
    return text, kb


async def _render_users(inst: dict, offset: int = 0):
    users = pp.list_clone_users(inst, USERS_PAGE, offset)
    total = pp.clone_users_count(inst)
    lines = [f"\U0001F465 <b>مستخدمو نسختك</b> ({total})\n"]
    if not users:
        lines.append("لا يوجد مستخدمون بعد — شارك رابط بوتك لبدء النمو.")
    for u in users:
        uid = int(u["telegram_id"])
        lines.append(f"• {_label(u.get('first_name'), uid)} — <code>{uid}</code>")
    buttons = []
    for u in users:
        uid = int(u["telegram_id"])
        buttons.append([(f"👤 {_label(u.get('first_name'), uid)}",
                         {"callback_data": f"ca:user:{uid}"})])
    nav = []
    if offset > 0:
        nav.append(("⬅️ السابق", {"callback_data": f"ca:users:{max(0, offset - USERS_PAGE)}"}))
    nav.append(("🔎 بحث", {"callback_data": "ca:users:search"}))
    if offset + USERS_PAGE < total:
        nav.append(("التالي ➡️", {"callback_data": f"ca:users:{offset + USERS_PAGE}"}))
    buttons.append(nav)
    buttons.append([("🔵 ↩️ رجوع", {"callback_data": "ca:home"})])
    return "\n".join(lines), _rows(buttons)


async def _render_user(inst: dict, uid: int, viewer_id: int):
    detail = pp.clone_user_detail(inst, uid)
    u = detail.get("user") or {}
    uname = (u.get("username") or "").strip()
    lines = [
        f"\U0001F464 <b>{_label(u.get('first_name'), uid)}</b>"
        + (f" (@{escape(uname[:32])})" if uname else "")
        + f"\n<code>{uid}</code>",
        f"أول استخدام: {_fmt_dt(u.get('registered_at'))}",
        f"آخر نشاط: {_fmt_dt(u.get('last_active_at'))}",
        f"تحميلات ناجحة: <b>{detail['downloads']}</b> • محاولات فاشلة: <b>{detail['failures']}</b>",
        "الحالة: " + ("🚫 محظور من النسخة" if detail["banned"] else "✅ نشط"),
    ]
    buttons = []
    if pp.perm_allows(inst, viewer_id, "can_ban"):
        if detail["banned"]:
            buttons.append([("✅ فك الحظر", {"callback_data": f"ca:user:{uid}:unban"})])
        else:
            buttons.append([("🚫 حظر من النسخة", {"callback_data": f"ca:user:{uid}:ban"})])
    buttons.append([("🔵 ↩️ رجوع", {"callback_data": "ca:users:0"})])
    return "\n".join(lines), _rows(buttons)


async def _render_stats(inst: dict, viewer_id: int):
    if not pp.perm_allows(inst, viewer_id, "can_stats"):
        return ("⛔ صلاحية الإحصائيات (can_stats) مطلوبة لهذا القسم.",
                _rows([[("🔵 ↩️ رجوع", {"callback_data": "ca:home"})]]))
    s = pp.clone_download_stats(inst)
    from utils.helpers import format_size
    plats = " • ".join(f"{escape(str(p.get('platform') or '?'))} ({p.get('cnt')})"
                       for p in s["top_platforms"][:5]) or "—"
    reasons = " • ".join(f"{escape(str(r.get('reason') or '?')[:40])} ({r.get('cnt')})"
                         for r in s["top_failure_reasons"][:5]) or "—"
    text = (
        "\U0001F4CA <b>إحصائيات التحميل — نسختك</b>\n\n"
        f"اليوم: <b>{s['today']}</b> • 7 أيام: <b>{s['week']}</b> • 30 يومًا: <b>{s['month']}</b>\n"
        f"محاولات فاشلة (30 يومًا): <b>{s['failures_30d']}</b>\n"
        f"أكثر المنصات: {plats}\n"
        f"أكثر أسباب الفشل: {reasons}\n"
        f"مستخدمون نشطون (7 أيام): <b>{s['active_users']}</b>\n"
        f"حجم البيانات المعالجة: <b>{escape(format_size(s['total_bytes']))}</b>"
    )
    return text, _rows([[("🔵 ↩️ رجوع", {"callback_data": "ca:home"})]])


async def _render_settings(inst: dict, viewer_id: int):
    inst_id = int(inst["id"])
    welcome = pp.get_clone_setting(inst_id, "welcome_text", "")
    dq = pp.get_clone_setting(inst_id, "default_quality", "")
    notify = pp.get_clone_setting(inst_id, "notify_users", "1")
    lim = pp.limits(inst.get("plan"))
    text = (
        "⚙️ <b>إعدادات النسخة</b>\n\n"
        f"رسالة الترحيب: {'✏️ محددة' if welcome else 'غير محددة'}\n"
        f"الجودة الافتراضية للنسخة: <b>{escape(dq or 'ذكي تلقائيًا')}</b>\n"
        f"إشعارات المستخدمين: {'🔔 مفعلة' if notify == '1' else '🔕 معطلة'}\n\n"
        "<b>حدود خطتك (للقراءة فقط):</b>\n"
        f"• {lim['daily_downloads']} تحميل يومي لكل مستخدم\n"
        f"• {lim['batch_urls']} روابط في الدفعة الواحدة\n"
        f"• جودة: {'أعلى جودة متاحة' if not lim['max_video_height'] else str(lim['max_video_height']) + 'p'}\n"
        f"• سجل التحميلات: {lim['history_days']} يومًا"
    )
    q_buttons = []
    row = []
    for opt in ("360p", "480p", "720p", "1080p", "highest"):
        if not pp.quality_option_allowed(opt, inst.get("plan")):
            continue  # never even OFFER what the plan forbids
        mark = "✅ " if dq == opt else ""
        row.append((f"{mark}{opt}", {"callback_data": f"ca:settings:quality:{opt}"}))
        if len(row) == 3:
            q_buttons.append(row)
            row = []
    if row:
        q_buttons.append(row)
    q_buttons.append([("🤖 ذكي تلقائيًا", {"callback_data": "ca:settings:quality:auto"})])
    buttons = [
        [("✏️ تعديل رسالة الترحيب", {"callback_data": "ca:settings:welcome"})],
    ] + q_buttons + [
        [("🔔 إشعارات: معطلة" if notify == "1" else "🔕 إشعارات: مفعلة",
          {"callback_data": "ca:settings:notify"})],
        [("🔵 ↩️ رجوع", {"callback_data": "ca:home"})],
    ]
    return text, _rows(buttons)


async def _render_broadcast(inst: dict, viewer_id: int):
    if not pp.perm_allows(inst, viewer_id, "can_broadcast"):
        return ("⛔ صلاحية الإذاعة (can_broadcast) مطلوبة لهذا القسم.",
                _rows([[("🔵 ↩️ رجوع", {"callback_data": "ca:home"})]]))
    count = pp.clone_users_count(inst)
    lines = [
        "\U0001F4E2 <b>إذاعة لمستخدمي نسختك</b>\n",
        f"عدد المستلمين المتوقع: <b>{count}</b>",
        "يُطبَّق معدل إرسال آمن احترامًا لحدود Telegram.",
    ]
    last = pp.last_broadcasts(inst and int(inst["id"]), 3)
    if last:
        lines.append("\n<b>آخر الإذاعات:</b>")
        for b in last:
            lines.append(
                f"• {_fmt_dt(b.get('created_at'))} — أُرسلت {b.get('sent')} • فشلت {b.get('failed')}")
    buttons = [
        [("✉️ رسالة جديدة", {"callback_data": "ca:broadcast:new"})],
        [("🔵 ↩️ رجوع", {"callback_data": "ca:home"})],
    ]
    return "\n".join(lines), _rows(buttons)


async def _render_admins(inst: dict, viewer_id: int):
    lines = ["\U0001F6E1️ <b>إداريو نسختك</b>\n"]
    admins = pp.list_clone_admins(int(inst["id"]))
    if not admins:
        lines.append("لا يوجد إداريون — أنت المدير الوحيد للنسخة.")
    buttons = []
    for a in admins:
        uid = int(a["user_id"])
        badges = "".join(
            ("✅" if a.get(p) else "✖") for p in
            ("can_ban", "can_broadcast", "can_stats"))
        lines.append(f"• <code>{uid}</code> — [حظر/إذاعة/إحصائيات: {badges}]")
        buttons.append([
            (f"🔄 صلاحيات {uid}", {"callback_data": f"ca:admin:{uid}:perms"}),
            (f"🗑️ إزالة {uid}", {"callback_data": f"ca:admin:{uid}:remove"}),
        ])
    buttons.append([("➕ إضافة إداري", {"callback_data": "ca:admins:add"})])
    buttons.append([("🔵 ↩️ رجوع", {"callback_data": "ca:home"})])
    return "\n".join(lines), _rows(buttons)


async def _render_admin_perms(inst: dict, uid: int):
    a = pp.get_clone_admin(int(inst["id"]), uid)
    if not a:
        return ("هذا الإداري غير موجود.",
                _rows([[("🔵 ↩️ رجوع", {"callback_data": "ca:admins"})]]))
    lines = [f"\U0001F6E1️ صلاحيات الإداري <code>{uid}</code>\n"]
    buttons = []
    labels = {"can_ban": "🚫 حظر المستخدمين", "can_broadcast": "📢 الإذاعة",
              "can_stats": "📊 الإحصائيات"}
    for p in pp.ADMIN_PERMS:
        on = bool(a.get(p))
        lines.append(f"• {labels[p]}: {'✅ مفعلة' if on else '✖ معطلة'}")
        buttons.append([(
            f"{'🔕 إيقاف' if on else '🔔 تفعيل'} {labels[p]}",
            {"callback_data": f"ca:admin:{uid}:toggle:{p}"})])
    buttons.append([("🔵 ↩️ رجوع", {"callback_data": "ca:admins"})])
    return "\n".join(lines), _rows(buttons)


async def _render_status(context, inst: dict):
    lines = ["\U0001FA7A <b>حالة البوت</b>\n"]
    try:
        me = await context.bot.get_me()
        lines.append(f"اتصال Telegram: ✅ (@{escape(me.username or '')})")
    except Exception:
        lines.append("اتصال Telegram: ❌ تعذر الوصول للواجهة")
    try:
        from services.engines import registered_engines
        engine_count = len(registered_engines())
    except Exception:
        engine_count = 0
    lines.append(f"محركات التحميل المسجلة: <b>{engine_count}</b>")
    lines.append(f"وقت التفعيل: {_fmt_dt(inst.get('activated_at'))}")
    lines.append(f"تنتهي الصلاحية: {_fmt_dt(inst.get('expires_at'))}")
    last_error = str(inst.get("last_error") or "").strip()
    lines.append(f"آخر خطأ مخزن: {escape(last_error[:80]) if last_error else 'لا شيء ✅'}")
    fails = pp.recent_failures(int(inst["id"]), 3)
    if fails:
        lines.append("\n<b>آخر أخطاء التحميل (مختصرة وآمنة):</b>")
        for f in fails:
            lines.append(
                f"• {escape(str(f.get('platform') or '?'))}: "
                f"{escape(str(f.get('reason') or '')[:60])}")
    buttons = [
        [("🔄 إعادة الفحص", {"callback_data": "ca:status"})],
        [("🔵 ↩️ رجوع", {"callback_data": "ca:home"})],
    ]
    return "\n".join(lines), _rows(buttons)


async def _render_log(inst: dict):
    rows = pp.list_admin_log(int(inst["id"]), 15)
    lines = ["\U0001F9FE <b>سجل الإدارة — آخر 15 إجراءً</b>\n"]
    if not rows:
        lines.append("لا توجد إجراءات مسجلة بعد.")
    for r in rows:
        detail = str(r.get("detail") or "")[:60]
        lines.append(
            f"• {_fmt_dt(r.get('created_at'))} — <code>{r.get('actor_id')}</code> — "
            f"{escape(str(r.get('action') or ''))}"
            + (f" — {escape(detail)}" if detail else ""))
    buttons = [
        [("📥 تصدير CSV", {"callback_data": "ca:log:csv"})],
        [("🔵 ↩️ رجوع", {"callback_data": "ca:home"})],
    ]
    return "\n".join(lines), _rows(buttons)


async def _render_subscription(inst: dict):
    from config.settings import MAIN_BOT_USERNAME
    plan = pp.normalize_plan(inst.get("plan"))
    status_ar = {"active": "نشطة ✅", "expired": "منتهية ⌛"}.get(
        str(inst.get("status")), str(inst.get("status")))
    remaining = "—"
    exp = inst.get("expires_at")
    if exp:
        try:
            from datetime import datetime
            delta = exp - datetime.now()
            remaining = f"{max(0, delta.days)} يومًا"
        except Exception:
            remaining = "—"
    lines = [
        "\U0001F4B3 <b>اشتراك النسخة</b>\n",
        f"الخطة: <b>{pp.PLAN_LABELS[plan]}</b>",
        f"تاريخ التفعيل: {_fmt_dt(inst.get('activated_at'))}",
        f"تاريخ الانتهاء: {_fmt_dt(exp)}",
        f"الوقت المتبقي: <b>{escape(str(remaining))}</b>",
        f"حالة النسخة: {escape(status_ar)}",
    ]
    buttons = [[("📨 طلب تجديد", {"callback_data": "ca:renew"})]]
    if MAIN_BOT_USERNAME:
        buttons.append([("🏠 العودة إلى البوت الأساسي",
                         {"url": f"https://t.me/{MAIN_BOT_USERNAME}"})])
    buttons.append([("🔵 ↩️ رجوع", {"callback_data": "ca:home"})])
    return "\n".join(lines), _rows(buttons)


# --------------------------------------------------------------------------
# callback dispatcher — every press re-verifies plan + access + permission
# --------------------------------------------------------------------------

async def clone_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data or ""

    gated = _gate(context, user.id if user else 0)
    if not gated:
        await _deny(query, "⛔ لوحة إدارة النسخة متاحة لصاحب النسخة وإدارييها فقط.")
        return
    ctx, inst, acc = gated
    inst_id = int(inst["id"])

    if ctx["plan"] != pp.PLAN_PRO:
        await query.answer()
        await safe_edit(query.edit_message_text, _plan_blocked_text(),
                        parse_mode="HTML", reply_markup=_plan_blocked_keyboard())
        return

    await query.answer()
    parts = data.split(":")

    try:
        if data == "ca:home":
            text, kb = await _render_home(ctx, inst, acc)
        elif data.startswith("ca:users:search"):
            # Server-side perm check on EVERY press — a forwarded panel
            # message or a crafted callback can't browse users without
            # can_stats/can_ban, no matter which buttons were rendered.
            if not _allows_users(inst, user.id):
                await _deny(query, "⛔ صلاحية الإحصائيات أو الحظر مطلوبة لعرض المستخدمين.")
                return
            key = (context.bot.id, query.message.chat_id)
            _pending[key] = {"kind": "user_search", "ts": time.monotonic(), "uid": int(user.id)}
            text, kb = ("🔎 أرسل Telegram ID (أرقام) أو اسم مستخدم للبحث داخل نسختك.",
                        _rows([[("🔵 إلغاء", {"callback_data": "ca:users:0"})]]))
        elif data.startswith("ca:users:"):
            if not _allows_users(inst, user.id):
                await _deny(query, "⛔ صلاحية الإحصائيات أو الحظر مطلوبة لعرض المستخدمين.")
                return
            try:
                offset = int(parts[2])
            except (ValueError, IndexError):
                offset = 0
            text, kb = await _render_users(inst, max(0, offset))
        elif data.startswith("ca:user:"):
            if not _allows_users(inst, user.id):
                await _deny(query, "⛔ صلاحية الإحصائيات أو الحظر مطلوبة لعرض المستخدمين.")
                return
            uid = int(parts[2])
            action = parts[3] if len(parts) > 3 else ""
            if action in ("ban", "unban"):
                if not pp.perm_allows(inst, user.id, "can_ban"):
                    await _deny(query, "⛔ صلاحية الحظر (can_ban) مطلوبة.")
                    return
                if action == "ban":
                    pp.ban_user(inst_id, uid, user.id)
                    pp.log_admin_action(inst_id, user.id, "ban_user", str(uid))
                else:
                    pp.unban_user(inst_id, uid)
                    pp.log_admin_action(inst_id, user.id, "unban_user", str(uid))
            text, kb = await _render_user(inst, uid, user.id)
        elif data == "ca:stats":
            text, kb = await _render_stats(inst, user.id)
        elif data == "ca:settings":
            text, kb = await _render_settings(inst, user.id)
        elif data == "ca:settings:welcome":
            key = (context.bot.id, query.message.chat_id)
            _pending[key] = {"kind": "welcome", "ts": time.monotonic(), "uid": int(user.id)}
            text, kb = ("✏️ أرسل رسالة الترحيب الجديدة (أو /cancel للإلغاء).\n"
                        "تُعرض للمستخدمين الجدد عند /start داخل نسختك.",
                        _rows([[("🔵 إلغاء", {"callback_data": "ca:settings"})]]))
        elif data.startswith("ca:settings:quality:"):
            value = parts[3]
            if value != "auto" and not pp.quality_option_allowed(value, inst.get("plan")):
                await _deny(query, "🔒 خطتك لا تسمح بهذه الجودة.")
                return
            pp.set_clone_setting(inst_id, "default_quality", value)
            pp.log_admin_action(inst_id, user.id, "set_default_quality", value)
            text, kb = await _render_settings(inst, user.id)
        elif data == "ca:settings:notify":
            current = pp.get_clone_setting(inst_id, "notify_users", "1")
            new_val = "0" if current == "1" else "1"
            pp.set_clone_setting(inst_id, "notify_users", new_val)
            pp.log_admin_action(inst_id, user.id, "toggle_notify", new_val)
            text, kb = await _render_settings(inst, user.id)
        elif data == "ca:broadcast":
            text, kb = await _render_broadcast(inst, user.id)
        elif data == "ca:broadcast:new":
            if not pp.perm_allows(inst, user.id, "can_broadcast"):
                await _deny(query, "⛔ صلاحية الإذاعة مطلوبة.")
                return
            key = (context.bot.id, query.message.chat_id)
            _pending[key] = {"kind": "broadcast", "ts": time.monotonic(), "uid": int(user.id)}
            text, kb = ("✉️ أرسل نص الرسالة التي تريد إذاعتها لمستخدمي نسختك.\n"
                        "ستظهر لك معاينة وعدد المستلمين قبل الإرسال.",
                        _rows([[("🔵 إلغاء", {"callback_data": "ca:broadcast"})]]))
        elif data == "ca:broadcast:confirm":
            if not pp.perm_allows(inst, user.id, "can_broadcast"):
                await _deny(query, "⛔ صلاحية الإذاعة مطلوبة.")
                return
            key = (context.bot.id, query.message.chat_id)
            draft = _drafts.pop(key, None)
            if not draft or time.monotonic() - draft.get("ts", 0) > _CAPTURE_TTL:
                await _deny(query, "انتهت صلاحية المسودة — ابدأ رسالة جديدة.")
                return
            # Identity binding: a draft may only be confirmed by the admin
            # who authored it — another admin (even with can_broadcast)
            # cannot send someone else's draft text.
            if int(draft.get("uid") or 0) != int(user.id):
                await _deny(query, "🔒 المسودة تخص إداريًا آخر — أنشئ رسالة جديدة بنفسك.")
                return
            await _do_broadcast(context, inst, user.id, str(draft["text"]), query)
            return
        elif data == "ca:broadcast:cancel":
            _drafts.pop((context.bot.id, query.message.chat_id), None)
            text, kb = await _render_broadcast(inst, user.id)
        elif data == "ca:admins":
            if acc != "owner":
                await _deny(query, "🔒 إدارة الإداريين لصاحب النسخة فقط.")
                return
            text, kb = await _render_admins(inst, user.id)
        elif data == "ca:admins:add":
            if acc != "owner":
                await _deny(query, "🔒 إدارة الإداريين لصاحب النسخة فقط.")
                return
            key = (context.bot.id, query.message.chat_id)
            _pending[key] = {"kind": "add_admin", "ts": time.monotonic(), "uid": int(user.id)}
            text, kb = ("➕ أرسل Telegram ID (أرقام) للشخص الذي تريد منحه صلاحية إداري.\n"
                        "الافتراضي: مشاهدة الإحصائيات فقط — بقية الصلاحيات تُفعَّل يدويًا.",
                        _rows([[("🔵 إلغاء", {"callback_data": "ca:admins"})]]))
        elif data.startswith("ca:admin:"):
            if acc != "owner":
                await _deny(query, "🔒 إدارة الإداريين لصاحب النسخة فقط.")
                return
            uid = int(parts[2])
            action = parts[3] if len(parts) > 3 else ""
            if action == "remove":
                pp.remove_clone_admin(inst_id, uid)
                pp.log_admin_action(inst_id, user.id, "remove_admin", str(uid))
                text, kb = await _render_admins(inst, user.id)
            elif action == "perms":
                text, kb = await _render_admin_perms(inst, uid)
            elif action == "toggle" and len(parts) > 4 and parts[4] in pp.ADMIN_PERMS:
                perm = parts[4]
                row = pp.get_clone_admin(inst_id, uid)
                new_val = not bool((row or {}).get(perm))
                pp.set_admin_perm(inst_id, uid, perm, new_val)
                pp.log_admin_action(inst_id, user.id, f"admin_perm:{perm}",
                                    f"{uid}={'1' if new_val else '0'}")
                text, kb = await _render_admin_perms(inst, uid)
            else:
                await _deny(query, "انتهت صلاحية الجلسة.")
                return
        elif data == "ca:status":
            text, kb = await _render_status(context, inst)
        elif data == "ca:log":
            # The audit log contains actor ids + every admin action —
            # gated by can_stats (owner passes perm_allows automatically).
            if not pp.perm_allows(inst, user.id, "can_stats"):
                await _deny(query, "⛔ صلاحية الإحصائيات (can_stats) مطلوبة لعرض السجل.")
                return
            text, kb = await _render_log(inst)
        elif data == "ca:log:csv":
            if not pp.perm_allows(inst, user.id, "can_stats"):
                await _deny(query, "⛔ صلاحية الإحصائيات (can_stats) مطلوبة لتصدير السجل.")
                return
            csv_text = pp.admin_log_csv(inst_id, 500)
            pp.log_admin_action(inst_id, user.id, "export_log_csv")
            await query.message.reply_document(
                document=InputFile(io.BytesIO(csv_text.encode("utf-8")),
                                   filename=f"clone_{inst_id}_admin_log.csv"),
                caption="🧾 سجل إدارة النسخة (CSV)")
            return
        elif data == "ca:subscription":
            text, kb = await _render_subscription(inst)
        elif data == "ca:renew":
            # DB-backed cooldown — one renewal request per instance per 6h.
            # Runs server-side on every press (hiding the button afterwards
            # is cosmetics, not protection) and survives restarts because
            # it reads the permanent clone_admin_log.
            if pp.recent_renew_request(inst_id, hours=6):
                await _deny(query, "⏳ طلب التجديد مُرسل مسبقًا — بانتظار رد مالك النظام.")
                return
            from config.settings import OWNER_ID
            from services import referral
            referral.notify_user(
                OWNER_ID,
                "\U0001F4E8 <b>طلب تجديد نسخة</b>\n"
                f"النسخة: @{escape(str(inst.get('bot_username') or ''))} (id={inst_id})\n"
                f"الخطة: {pp.PLAN_LABELS[pp.normalize_plan(inst.get('plan'))]}\n"
                f"تنتهي: {_fmt_dt(inst.get('expires_at'))}\n"
                f"الطلب من: <code>{user.id}</code>",
            )
            pp.log_admin_action(inst_id, user.id, "renew_requested")
            await _deny(query, "📨 تم إرسال طلب التجديد إلى مالك النظام.")
            return
        else:
            await _deny(query, "انتهت صلاحية الجلسة.")
            return

        await safe_edit(query.edit_message_text, text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("clone admin callback failed data=%s", data[:40])
        try:
            await safe_edit(query.edit_message_text,
                            "⚠️ تعذر تنفيذ الطلب — حاول مرة أخرى.",
                            parse_mode="HTML",
                            reply_markup=_rows([[("🔵 رجوع", {"callback_data": "ca:home"})]]))
        except Exception:
            pass


# --------------------------------------------------------------------------
# broadcast execution (safe rate — Telegram-friendly)
# --------------------------------------------------------------------------

async def _do_broadcast(context, inst: dict, actor_id: int, text: str, query) -> None:
    inst_id = int(inst["id"])
    from database import pg
    recipients = pg.fetchall(
        "SELECT telegram_id FROM ref_users WHERE source = ?",
        (f"clone:{int(inst.get('bot_id') or 0)}",))
    total = len(recipients)
    body = f"{text}\n\n— إدارة @{inst.get('bot_username') or 'النسخة'}"
    sent = failed = 0
    await safe_edit(query.edit_message_text,
                    f"📢 جارٍ الإرسال إلى {total} مستخدمًا…", parse_mode="HTML")
    for i, row in enumerate(recipients):
        try:
            await context.bot.send_message(int(row["telegram_id"]), body)
            sent += 1
        except Exception:
            failed += 1
        if (i + 1) % BROADCAST_PAUSE_EVERY == 0:
            await asyncio.sleep(BROADCAST_PAUSE_SECONDS)
    pp.log_broadcast(inst_id, actor_id, total, sent, failed)
    pp.log_admin_action(inst_id, actor_id, "broadcast", f"total={total} sent={sent} failed={failed}")
    await safe_edit(query.edit_message_text,
                    f"📢 <b>انتهت الإذاعة</b>\nالمستلمون: {total}\n"
                    f"أُرسلت بنجاح: ✅ {sent}\nفشلت: ❌ {failed}",
                    parse_mode="HTML",
                    reply_markup=_rows([[("🔵 رجوع", {"callback_data": "ca:home"})]]))


# --------------------------------------------------------------------------
# text capture (broadcast body / welcome / add-admin / user search)
# --------------------------------------------------------------------------

def has_pending(bot_id: int, chat_id: int) -> bool:
    pend = _pending.get((int(bot_id), int(chat_id)))
    return bool(pend)


async def try_handle_capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume a text message when a clone-admin capture prompt is pending.
    Called from bot.message_router BEFORE normal routing. Returns True only
    when the message was consumed."""
    msg = update.message
    if not msg or not msg.text:
        return False
    key = (context.bot.id, msg.chat_id)
    pend = _pending.get(key)
    if not pend:
        return False
    if time.monotonic() - pend.get("ts", 0) > _CAPTURE_TTL:
        _pending.pop(key, None)
        return False
    # Identity binding — server-side, per action: ONLY the exact user who
    # pressed the button may complete this capture. In a group chat nobody
    # else can hijack it, and a stranger's chatter can no longer wipe the
    # session; a mismatched sender is simply not handled here (returns
    # False so the message routes on normally, pending stays intact).
    sender_id = int(msg.from_user.id) if msg.from_user else 0
    if not sender_id or sender_id != int(pend.get("uid") or 0):
        return False
    gated = _gate(context, msg.from_user.id if msg.from_user else 0)
    if not gated:
        _pending.pop(key, None)
        return False
    ctx, inst, acc = gated
    if ctx["plan"] != pp.PLAN_PRO:
        _pending.pop(key, None)
        return False

    text = msg.text.strip()
    if text in ("/cancel", "الغاء", "إلغاء"):
        _pending.pop(key, None)
        await msg.reply_text("تم الإلغاء.", reply_markup=None)
        return True

    kind = pend.get("kind")
    _pending.pop(key, None)
    inst_id = int(inst["id"])

    if kind == "broadcast":
        if not pp.perm_allows(inst, msg.from_user.id, "can_broadcast"):
            await msg.reply_text("⛔ صلاحية الإذاعة مطلوبة.")
            return True
        _drafts[key] = {"text": text[:3500], "ts": time.monotonic(), "uid": int(msg.from_user.id)}
        count = pp.clone_users_count(inst)
        preview = escape(text[:300]) + ("…" if len(text) > 300 else "")
        await msg.reply_text(
            f"👀 <b>معاينة الرسالة</b>\n\n{preview}\n\n"
            f"عدد المستلمين المتوقع: <b>{count}</b>\n"
            "⚠️ الإرسال نهائي ولا يمكن التراجع عنه.",
            parse_mode="HTML",
            reply_markup=_rows([
                [("✅ إرسال نهائي", {"callback_data": "ca:broadcast:confirm"}),
                 ("❌ إلغاء", {"callback_data": "ca:broadcast:cancel"})],
            ]))
        return True

    if kind == "welcome":
        pp.set_clone_setting(inst_id, "welcome_text", text[:1500])
        pp.log_admin_action(inst_id, msg.from_user.id, "set_welcome", f"len={len(text)}")
        await msg.reply_text("✅ تم حفظ رسالة الترحيب.",
                             reply_markup=_rows([[("🔵 رجوع للإعدادات",
                                                   {"callback_data": "ca:settings"})]]))
        return True

    if kind == "add_admin":
        if acc != "owner":
            await msg.reply_text("🔒 إدارة الإداريين لصاحب النسخة فقط.")
            return True
        if not text.isdigit():
            await msg.reply_text("⚠️ أرسل Telegram ID رقميًا صحيحًا.")
            return True
        uid = int(text)
        if uid == inst.get("owner_id"):
            await msg.reply_text("أنت صاحب النسخة أصلًا — لست بحاجة لصلاحية إداري.")
            return True
        pp.add_clone_admin(inst_id, uid, msg.from_user.id)
        pp.log_admin_action(inst_id, msg.from_user.id, "add_admin", str(uid))
        await msg.reply_text(
            f"✅ تم إضافة <code>{uid}</code> كإداري (إحصائيات فقط افتراضيًا).\n"
            "فعّل بقية الصلاحيات من زر 🔄 بجانب اسمه.",
            parse_mode="HTML",
            reply_markup=_rows([[("🔵 رجوع", {"callback_data": "ca:admins"})]]))
        return True

    if kind == "user_search":
        # Re-check on capture too — the pending flag alone is not authority:
        # identity/instance/plan were re-gated above, and the directory perm
        # is verified here again at the moment the data would be revealed.
        if not _allows_users(inst, msg.from_user.id):
            await msg.reply_text("⛔ صلاحية الإحصائيات أو الحظر مطلوبة.")
            return True
        results = pp.search_clone_user(inst, text)
        if not results:
            await msg.reply_text("🔎 لا نتائج داخل نسختك.",
                                 reply_markup=_rows([[("🔵 رجوع",
                                                       {"callback_data": "ca:users:0"})]]))
            return True
        lines = ["\U0001F50E <b>نتائج البحث</b>\n"]
        buttons = []
        for u in results:
            uid = int(u["telegram_id"])
            lines.append(f"• {_label(u.get('first_name'), uid)} — <code>{uid}</code>")
            buttons.append([(f"👤 {_label(u.get('first_name'), uid)}",
                             {"callback_data": f"ca:user:{uid}"})])
        buttons.append([("🔵 رجوع", {"callback_data": "ca:users:0"})])
        await msg.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=_rows(buttons))
        return True

    return False
