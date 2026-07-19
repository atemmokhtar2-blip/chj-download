"""
🤖 مساعد التحديث الذكي
Arabic natural-language command router for the Developer Bot.
Parses Arabic text and routes to the correct action.
"""
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import log_action
from utils.logger import action_logger

STATE_AI = "dv_ai_chat"

HELP_TEXT = (
    "🤖 <b>مساعد التحديث الذكي</b>\n\n"
    "أرسل أمرًا بالعربية وسأنفذه فورًا.\n\n"
    "<b>أمثلة على الأوامر:</b>\n"
    "• <code>أنشئ نسخة احتياطية</code>\n"
    "• <code>اعرض عدد المستخدمين</code>\n"
    "• <code>افحص المشروع</code>\n"
    "• <code>أعد تشغيل البوت الأساسي</code>\n"
    "• <code>أعد تشغيل جميع البوتات</code>\n"
    "• <code>اعرض آخر الأخطاء</code>\n"
    "• <code>اعرض حالة الخدمات</code>\n"
    "• <code>ابحث عن كلمة في المشروع</code>\n"
    "• <code>اعرض الملفات</code>\n"
    "• <code>وضع الطوارئ</code>\n\n"
    "💡 <i>أرسل أمرك الآن…</i>"
)

# Intent patterns: (compiled_regex, action_key, description)
INTENTS = [
    # Backup
    (re.compile(r"(نسخة احتياطية|نسخ احتياطي|backup|احتياط)", re.I),
     "dv_bkp_create", "إنشاء نسخة احتياطية"),

    # Health check
    (re.compile(r"(افحص|فحص|health|سلامة|صحة المشروع|فحص المشروع)", re.I),
     "dv_health", "فحص المشروع"),

    # Monitor / stats
    (re.compile(r"(مستخدم|إحصاء|إحصائيات|مراقبة|monitor|تقرير النظام|عدد)", re.I),
     "dv_monitor", "مراقبة النظام"),

    # Restart all
    (re.compile(r"(جميع البوتات|كل البوتات|restart all|أعد تشغيل الكل)", re.I),
     "dv_svc_restart_all_confirm", "إعادة تشغيل جميع البوتات"),

    # Restart main
    (re.compile(r"(البوت الأساسي|الرئيسي|main bot)", re.I),
     "dv_svc_restart_main_confirm", "إعادة تشغيل البوت الأساسي"),

    # Restart support
    (re.compile(r"(بوت الدعم|support bot)", re.I),
     "dv_svc_restart_support_confirm", "إعادة تشغيل بوت الدعم"),

    # Restart admin
    (re.compile(r"(بوت الأدمن|admin bot)", re.I),
     "dv_svc_restart_admin_confirm", "إعادة تشغيل بوت الأدمن"),

    # Service status
    (re.compile(r"(حالة الخدمات|حالة البوتات|service status|هل يعمل)", re.I),
     "dv_svc_status", "حالة الخدمات"),

    # Errors / logs
    (re.compile(r"(أخطاء|خطأ|logs|سجل|error)", re.I),
     "dv_errors", "عرض سجل الأخطاء"),

    # Emergency
    (re.compile(r"(طوارئ|emergency|استعادة طارئة|أزمة)", re.I),
     "dv_emergency", "وضع الطوارئ"),

    # File manager
    (re.compile(r"(ملفات|مدير الملفات|files|استعرض|تصفح)", re.I),
     "dv_files", "مدير الملفات"),

    # Search
    (re.compile(r"(ابحث|بحث|search|ابحث عن)", re.I),
     "dv_search_prompt", "البحث في المشروع"),

    # Updates
    (re.compile(r"(تحديث|updates|upload|رفع)", re.I),
     "dv_updates", "إدارة التحديثات"),

    # Backups list
    (re.compile(r"(عرض النسخ|قائمة النسخ|list backups)", re.I),
     "dv_bkp_list", "عرض النسخ الاحتياطية"),

    # Security log
    (re.compile(r"(سجل الإجراءات|أمان|security|نشاط)", re.I),
     "dv_sec_log", "سجل الأمان"),

    # Main menu
    (re.compile(r"(القائمة الرئيسية|رجوع|menu|رئيسية)", re.I),
     "dv_menu", "القائمة الرئيسية"),
]


def ai_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 إرسال أمر", callback_data="dv_ai_prompt")],
        [InlineKeyboardButton("📋 قائمة الأوامر", callback_data="dv_ai_help")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="dv_menu")],
    ])


async def show_ai_menu(query, context):
    await query.edit_message_text(
        "🤖 <b>مساعد التحديث الذكي</b>\n\n"
        "أرسل أمرك بالعربية وسأوجهك إلى الإجراء المناسب.\n\n"
        "اضغط <b>إرسال أمر</b> لبدء الحوار، أو <b>قائمة الأوامر</b> لعرض الأمثلة.",
        parse_mode="HTML",
        reply_markup=ai_menu_kb(),
    )


async def show_ai_help(query, context):
    await query.edit_message_text(
        HELP_TEXT,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 إرسال أمر", callback_data="dv_ai_prompt")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="dv_ai")],
        ]),
    )


async def prompt_ai_input(query, context):
    context.user_data["dv_state"] = STATE_AI
    await query.edit_message_text(
        "💬 <b>أرسل أمرك بالعربية الآن…</b>\n\n"
        "مثال: <code>أنشئ نسخة احتياطية</code>\n"
        "أو: <code>افحص المشروع</code>\n\n"
        "<i>أرسل /cancel للإلغاء</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data="dv_ai")]
        ]),
    )


async def handle_ai_command(update, context):
    """Process Arabic text commands and route to correct action."""
    text = (update.message.text or "").strip()
    uid = update.effective_user.id

    context.user_data.pop("dv_state", None)
    log_action(uid, "ai_command", text[:200], "processing")

    matched_action = None
    matched_label = None
    for pattern, action, label in INTENTS:
        if pattern.search(text):
            matched_action = action
            matched_label = label
            break

    if not matched_action:
        await update.message.reply_text(
            f"🤖 <b>لم أفهم الأمر</b>\n\n"
            f"الأمر المُرسل: <code>{text[:100]}</code>\n\n"
            "جرّب أمرًا من القائمة:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 قائمة الأوامر", callback_data="dv_ai_help")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="dv_ai")],
            ]),
        )
        log_action(uid, "ai_command", text[:200], "no_match")
        return

    action_logger.info("AI command matched: '%s' → %s", text, matched_action)
    log_action(uid, "ai_command", text[:200], f"→ {matched_action}")

    # Handle special confirm-style actions
    if matched_action.endswith("_confirm"):
        real_action = matched_action[:-8]  # strip _confirm
        await update.message.reply_text(
            f"🤖 <b>تم فهم الأمر:</b> <code>{matched_label}</code>\n\n"
            "⚠️ هذا الإجراء يتطلب تأكيدًا. اضغط زر التأكيد أدناه:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ تأكيد: {matched_label}", callback_data=real_action)],
                [InlineKeyboardButton("❌ إلغاء", callback_data="dv_menu")],
            ]),
        )
        return

    await update.message.reply_text(
        f"🤖 <b>تم فهم الأمر:</b> <code>{matched_label}</code>\n\n"
        "⏳ جارٍ التنفيذ…",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"▶️ فتح: {matched_label}", callback_data=matched_action)],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="dv_menu")],
        ]),
    )
