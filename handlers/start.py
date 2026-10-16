import logging
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from utils.button_text import strip_button_emoji, reply_button
from telegram.ext import ContextTypes

from config.settings import MAIN_BOT_USERNAME
from database.users import get_user, create_user, update_user
from middlewares.auth import is_admin, is_owner
from locales import t
from services.instance_manager import is_clone_bot

logger = logging.getLogger(__name__)


def get_main_keyboard(lang: str = "ar", user_id: int = None, bot_username: str = None) -> ReplyKeyboardMarkup:
    """Permanent Arabic shortcuts: Download + Settings.

    NOTE: reply-keyboard buttons CANNOT carry URLs — ``url`` is an
    InlineKeyboardButton-only property, so the previous KeyboardButton(url=…)
    silently raised inside try/except and the button never existed. The
    "العودة إلى البوت الأساسي" shortcut inside clones is delivered as a
    proper INLINE button with the /start message instead (see start_command).
    """
    rows = [[reply_button(t("ar", "menu_download"), "primary"), reply_button(t("ar", "menu_settings"))]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_main_keyboard(context, user_id: int) -> ReplyKeyboardMarkup:
    """The clone-aware permanent keyboard.

    Main bot / regular clone users → Download + Settings (unchanged).
    Pro clone OWNER or clone ADMIN → a third row "🛠️ لوحة التحكم": the door
    to THEIR OWN panel, which is completely isolated from the main bot's
    general admin (separate clone_admins table, Pro-plan gated, and
    re-verified server-side on every single press).
    """
    rows = [[t("ar", "menu_download"), t("ar", "menu_settings")]]
    try:
        from services import plan_policy
        ctx = plan_policy.plan_context(context.bot)
        if ctx.get("is_clone") and ctx.get("plan") == plan_policy.PLAN_PRO \
                and ctx.get("instance"):
            if plan_policy.clone_access(ctx["instance"], user_id) in ("owner", "admin"):
                rows.append([reply_button(t("ar", "menu_admin"))])
    except Exception:
        logger.debug("admin keyboard row failed", exc_info=True)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    is_new = db_user is None

    if not db_user:
        create_user(
            user_id=user.id,
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            language="ar",
        )
        db_user = get_user(user.id)

    # Force Arabic for both new and existing users, including accounts that
    # previously selected English.
    update_user(user.id, language="ar")

    if db_user and db_user.get("is_banned"):
        await update.message.reply_text(t("ar", "banned"))
        return

    # ---- referral network registration ----------------------------------
    # Every start touches the referral registry (activity-day tracking can
    # qualify pending referrals via the 2-day rule). Brand-new users may
    # carry a ref_ deep-link code and get attributed ONCE, permanently.
    ref_note = ""
    try:
        from services import referral
        bot_username = context.bot.username or ""
        source = f"clone:{context.bot.id}" if is_clone_bot(bot_username) else "main"
        referral.register_user(user, is_new=is_new, source=source)
        if is_new:
            code = None
            if context.args:
                code = str(context.args[0]).strip()
            elif update.message and update.message.text and "ref_" in update.message.text:
                # Some clients deliver the payload inside the message text.
                for part in update.message.text.split():
                    if part.startswith("ref_"):
                        code = part.strip()
                        break
            bind = referral.bind_referral(code, user.id) if code else "no_code"
            if bind in ("bound", "pending", "suspicious"):
                ref_note = "\n\n🎁 تم تسجيلك عبر رابط دعوة — أهلًا بك!"
            elif bind == "self":
                logger.info("Self-referral blocked uid=%s", user.id)
    except Exception:
        logger.exception("Referral registration failed uid=%s", user.id)

    # Panels appear ONLY for privileged accounts — regular users never see
    # any panel button or screen, anywhere in the bot.
    #   * OWNER (you)      → the Owner Control Center opens immediately on /start.
    #   * regular admin    → the admin panel opens immediately on /start.
    # FULL ISOLATION: main-bot panels NEVER open inside a clone. Inside a
    # clone even the global owner/admin is served the normal clone flow —
    # if they own/admin THIS clone, their own panel arrives via the
    # keyboard row + the auto-appointment note below.
    if not is_clone_bot(context.bot.username or ""):
        if is_owner(user.id):
            from handlers.owner_panel import owner_command  # local import: avoids a circular dependency
            await owner_command(update, context)
            return

        if is_admin(user.id):
            from handlers.admin import admin_command  # local import: avoids a circular dependency
            await admin_command(update, context)
            return

    # 📦 Clone plan enforcement touches /start too: a user banned from THIS
    # clone instance gets a clear notice instead of the normal welcome.
    try:
        if is_clone_bot(context.bot.username or ""):
            from services import plan_policy
            pctx = plan_policy.plan_context(context.bot)
            if pctx.get("is_clone") and pctx.get("instance_id") and \
                    plan_policy.is_user_banned(pctx["instance_id"], user.id):
                await update.message.reply_text(
                    "\U0001F6AB تم تعطيل حسابك في هذه النسخة. تواصل مع صاحب النسخة."
                )
                return
    except Exception:
        logger.exception("clone start gate failed uid=%s", user.id)

    await update.message.reply_text(
        t("ar", "welcome_back") + ref_note,
        parse_mode="HTML",
        reply_markup=build_main_keyboard(context, user.id),
    )

    # A Pro clone can show its OWN welcome message (set from its admin panel).
    try:
        if is_clone_bot(context.bot.username or ""):
            from services import plan_policy
            pctx = plan_policy.plan_context(context.bot)
            if pctx.get("instance_id"):
                custom = plan_policy.get_clone_setting(pctx["instance_id"], "welcome_text", "")
                if custom:
                    await update.message.reply_text(custom[:1500])
    except Exception:
        logger.debug("clone custom welcome failed", exc_info=True)

    # Inside a clone instance, new users always see the way back to the
    # main bot (sent once per process session so it never becomes spam).
    try:
        if is_clone_bot(context.bot.username or "") and MAIN_BOT_USERNAME and \
                not context.user_data.get("back_to_main_sent"):
            context.user_data["back_to_main_sent"] = True
            await update.message.reply_text(
                "🏠 هذا البوت نسخة تعمل ضمن شبكة البوت الأساسي.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "العودة إلى البوت الأساسي",
                        url=f"https://t.me/{MAIN_BOT_USERNAME}",
                    ),
                ]]),
            )
    except Exception:
        pass

    # 👑 Auto-appointment note: the Pro clone's owner/admins are told — once
    # per process session — that their OWN isolated control panel is ready
    # (they were appointed automatically at activation / plan upgrade).
    try:
        if is_clone_bot(context.bot.username or "") and \
                not context.user_data.get("clone_panel_note_sent"):
            from services import plan_policy
            pctx = plan_policy.plan_context(context.bot)
            if pctx.get("instance") and pctx.get("plan") == plan_policy.PLAN_PRO:
                acc = plan_policy.clone_access(pctx["instance"], user.id)
                if acc in ("owner", "admin"):
                    context.user_data["clone_panel_note_sent"] = True
                    role = "صاحب النسخة 👑" if acc == "owner" else "إداري 🛡️"
                    await update.message.reply_text(
                        f"👑 أنت {role} في هذه النسخة — تم تعيينك تلقائيًا.\n"
                        "لوحة التحكم الخاصة بك معزولة تمامًا عن إدارة البوت الأساسي،\n"
                        "ومتاحة دائمًا من زر «🛠️ لوحة التحكم» أو أمر /admin.",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🛠️ فتح لوحة التحكم",
                                                 callback_data="ca:home"),
                        ]]),
                    )
    except Exception:
        logger.debug("clone panel note failed", exc_info=True)


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keep old language buttons harmless; the bot now uses Arabic only."""
    query = update.callback_query
    update_user(query.from_user.id, language="ar")
    await query.answer("اللغة العربية ثابتة في البوت.")
    try:
        await query.edit_message_text("✅ تم ضبط لغة البوت على العربية.")
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=t("ar", "welcome"),
        parse_mode="HTML",
        reply_markup=build_main_keyboard(context, query.from_user.id),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    if db_user:
        update_user(user.id, language="ar")
    await update.message.reply_text(t("ar", "help_text"), parse_mode="HTML")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    if db_user:
        update_user(user.id, language="ar")
    await update.message.reply_text(
        t("ar", "settings_text", language="العربية"),
        parse_mode="HTML",
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "settings_lang":
        await query.answer("اللغة العربية ثابتة في البوت.", show_alert=True)
        try:
            await query.edit_message_text("⚙️ لغة البوت ثابتة على العربية.")
        except Exception:
            pass
        return
    await query.answer("هذا الزر لم يعد متاحًا.", show_alert=True)
