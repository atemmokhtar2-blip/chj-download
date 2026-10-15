"""User settings menu — Arabic-only, stateful, per-user.

Callback data uses a short ``settings:...`` namespace (every value well under
Telegram's 64-byte limit). Every action fetches the settings *by the pressing
user's own id*, so preferences can never leak between accounts.

Renderers are pure ``(html_text, rows)`` functions where ``rows`` is a list of
button rows as ``(label, callback_data)`` tuples — easy to unit test without a
live Telegram API.
"""

from __future__ import annotations

import logging
import math
import re

from telegram import Update, InlineKeyboardMarkup
from utils.button_text import inline_button
from telegram.ext import ContextTypes

from database.user_settings import (
    get_user_settings, save_settings, reset_settings, delete_user_data,
    get_history_count, get_user_history, get_history_entry,
    delete_history_entry, clear_history, cleanup_stale_temp_files,
    USER_SETTINGS_FIELDS,
    VIDEO_QUALITY_OPTIONS, AUDIO_QUALITY_OPTIONS, DELIVERY_MODES,
    SIZE_PRESETS_MB, HISTORY_RETENTION_OPTIONS,
    AUDIO_QUALITY_BITRATE, MAX_CUSTOM_SIZE_MB,
)
from middlewares.concurrency import active_global_slots
from middlewares.user_locks import is_user_busy
from utils.helpers import format_size
from utils.telegram_safe import safe_edit

logger = logging.getLogger(__name__)

PAGE_SIZE = 10

# ---- labels ----------------------------------------------------------------

_VQ_LABELS = {
    "auto": "ذكي تلقائيًا",
    "360p": "360p",
    "480p": "480p",
    "720p": "720p",
    "1080p": "1080p",
    "highest": "أعلى جودة متاحة",
}
_AQ_LABELS = {"low": "حجم أقل", "normal": "جودة عادية", "high": "أعلى جودة متاحة"}
_DELIVERY_LABELS = {"video": "إرسال كفيديو", "file": "إرسال كملف", "audio": "استخراج الصوت"}


def _vq(value) -> str:
    return _VQ_LABELS.get(str(value).lower(), str(value))


def _aq(value) -> str:
    return _AQ_LABELS.get(str(value).lower(), str(value))


def _delivery(value) -> str:
    return _DELIVERY_LABELS.get(str(value).lower(), str(value))


def _size(value) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 0
    return "بدون حد إضافي" if n <= 0 else f"{n} MB"


def _onoff(value) -> str:
    return "مفعّل" if bool(value) else "متوقف"


def _parse_size_mb(raw: str) -> float | None:
    """Parse a user-entered max-size value into MB.

    Accepts plain numbers, decimals, Arabic-Indic digits, and common unit
    suffixes. Returns ``None`` when the input isn't a recognizable number, so
    the caller can re-prompt instead of corrupting the setting.
    """
    if not raw:
        return None
    s = str(raw).strip()
    table = str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789",
    )
    s = s.translate(table)
    s = re.sub(
        r"(?i)\s*(?:mb|megabyte|megabytes|mega|ميجا|ميجابايت|ميجا ?بايت|م\.ب|م ب)\s*$",
        "", s,
    )
    s = s.strip().replace(",", ".")
    if not s or s in {".", "-", "+"}:
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value


# ---- pure renderers (return (html_text, rows)) -----------------------------

def _mk(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    # Telegram inline buttons have no style/color option — the colored dots
    # (🟢/🔵/🔴/🟡) inside the labels themselves carry the visual semantics.
    return InlineKeyboardMarkup([
        [inline_button(label, callback_data=data) for label, data in row]
        for row in rows
    ])


def _edit_cb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return _mk(rows)


def build_home(settings: dict) -> tuple[str, list]:
    s = settings
    text = (
        "⚙️ <b>إعدادات حسابك</b>\n"
        "خصص تجربة التحميل بالطريقة اللي تناسبك.\n\n"
        "📋 <b>معاينة إعداداتك الحالية:</b>\n"
        f"🎯 الجودة الافتراضية: <b>{_vq(s['video_quality'])}</b>\n"
        f"📦 طريقة الاستلام: <b>{_delivery(s['delivery_mode'])}</b>\n"
        f"📏 الحد الأقصى للحجم: <b>{_size(s['max_file_size_mb'])}</b>\n"
        f"🔔 إشعارات الاكتمال: <b>{_onoff(s['notify_complete'])}</b>\n\n"
        "اختار القسم اللي عايز تعدّله:"
    )
    rows = [
        [("🟢 🎁 اكسب نسختك من البوت", "settings:referral")],
        [("🔵 🎯 الجودة والحجم", "settings:quality")],
        [("🔵 📦 طريقة الاستلام", "settings:delivery")],
        [("🔵 ⚡ مدير التحميلات", "settings:queue")],
        [("🔵 📚 سجل التحميلات", "settings:history")],
        [("🔵 🔒 الخصوصية والمساحة", "settings:privacy")],
        [("🔵 🔔 الإشعارات", "settings:notify")],
        [("🔴 ♻️ إعادة ضبط الإعدادات", "settings:reset")],
        [("🔴 ❌ إغلاق", "settings:close")],
    ]
    return text, rows


def build_quality(settings: dict) -> tuple[str, list]:
    s = settings
    text = (
        "🎯 <b>إعدادات الجودة</b>\n"
        "اختار طريقة التحميل الافتراضية:\n\n"
        f"الوضع الحالي: <b>{_vq(s['video_quality'])}</b>"
    )
    rows = [
        [("🟢 🤖 ذكي تلقائيًا", "settings:quality:set:auto"),
         ("🟡 📏 الحد الأقصى للحجم", "settings:quality:size")],
        [("🔵 🎬 جودة الفيديو", "settings:quality:video"),
         ("🔵 🎵 جودة الصوت", "settings:quality:audio")],
        [("🔵 ↩️ رجوع", "settings:home")],
    ]
    return text, rows


def build_quality_video(settings: dict) -> tuple[str, list]:
    current = str(settings["video_quality"]).lower()
    text = "🎬 <b>جودة الفيديو</b>\nاختار الجودة الافتراضية:"
    rows = []
    for opt in ("360p", "480p", "720p", "1080p", "highest"):
        label = ("🟢 ✅ " if opt == current else "⚪ ") + _vq(opt)
        rows.append([(label, f"settings:quality:video:set:{opt}")])
    rows.append([("🔵 ↩️ رجوع", "settings:quality")])
    return text, rows


def build_quality_audio(settings: dict) -> tuple[str, list]:
    current = str(settings["audio_quality"]).lower()
    text = "🎵 <b>جودة الصوت</b>\nاختار الجودة الافتراضية للـ MP3:"
    rows = []
    for opt in AUDIO_QUALITY_OPTIONS:
        bitrate = AUDIO_QUALITY_BITRATE[opt]
        label = ("🟢 ✅ " if opt == current else "⚪ ") + f"{_aq(opt)} ({bitrate}kbps)"
        rows.append([(label, f"settings:quality:audio:set:{opt}")])
    rows.append([("🔵 ↩️ رجوع", "settings:quality")])
    return text, rows


def build_quality_size(settings: dict) -> tuple[str, list]:
    current = int(settings["max_file_size_mb"] or 0)
    text = (
        "📏 <b>الحد الأقصى للحجم</b>\n"
        "اختار الحد اللي يناسبك (بدون حد إضافي = قيود البوت/تيليجرام فقط):"
    )
    rows = []
    for mb in SIZE_PRESETS_MB:
        label = ("🟢 ✅ " if mb == current else "⚪ ") + f"{mb} MB"
        rows.append([(label, f"settings:quality:size:set:{mb}")])
    rows.append([("🟡 ✍️ إدخال حجم مخصص", "settings:quality:size:custom")])
    unlimited = "🟢 ✅ " if current == 0 else "⚪ "
    rows.append([(unlimited + "🚫 بدون حد إضافي", "settings:quality:size:set:0")])
    rows.append([("🔵 ↩️ رجوع", "settings:quality")])
    return text, rows


def build_delivery(settings: dict) -> tuple[str, list]:
    current = str(settings["delivery_mode"]).lower()
    text = (
        "📦 <b>طريقة استلام الملفات</b>\n"
        "إزاي تحب البوت يبعث لك النتيجة افتراضيًا (عبر زر Smart Download):"
    )
    rows = []
    for opt in DELIVERY_MODES:
        label = ("🟢 ✅ " if opt == current else "⚪ ") + _delivery(opt)
        rows.append([(label, f"settings:delivery:set:{opt}")])
    rows.append([
        ("🔵 💬 نصيحة", "settings:delivery:info"),
        ("🔵 ↩️ رجوع", "settings:home"),
    ])
    return text, rows


def build_delivery_info(settings: dict) -> tuple[str, list]:
    text = (
        "💬 <b>عن طرق الاستلام</b>\n\n"
        "🎬 <b>كفيديو:</b> يُرسل كفيديو قابل للتشغيل داخل تيليجرام إذا كان النوع والحجم مدعومين.\n"
        "📁 <b>كملف:</b> يُرسل كمستند للحفاظ على الملف الأصلي قدر الإمكان.\n"
        "🎵 <b>استخراج الصوت:</b> يحوّل الفيديو إلى MP3 إذا كانت المعالجة متاحة.\n\n"
        "لو الصيغة اللي اخترتها غير مدعومة من المصدر، هيظهرلك السبب والبدائل بدل ما يحفظ اختيارًا مضللًا."
    )
    rows = [[("🔵 ↩️ رجوع", "settings:delivery")]]
    return text, rows


def build_queue(settings: dict, user_id: int) -> tuple[str, list]:
    in_use, max_slots = active_global_slots()
    busy = is_user_busy(user_id)
    text = (
        "⚡ <b>مدير التحميلات</b>\n\n"
        f"🔄 التحميلات الجارية على السيرفر: <b>{in_use} / {max_slots}</b>\n"
        f"👤 حالتك: <b>{'لديك تحميل قيد التنفيذ' if busy else 'خامل — جاهز للتحميل'}</b>\n\n"
        "ℹ️ لإلغاء أي تحميل جارٍ: اضغط زر 🛑 (إلغاء التحميل) الموجود أسفل رسالة التحميل نفسها — "
        "يتوقف التحميل فورًا، ويُحذف الملف الجزئي، ولا يُخصم من رصيدك.\n"
        "ومن السجل يمكنك إعادة تحميل أي عملية سابقة بضغطة واحدة."
    )
    rows = [
        [("🟢 🔄 تحديث الحالة", "settings:queue")],
        [("🔵 📋 عرض التحميلات (السجل)", "settings:history")],
        [("🔵 ↩️ رجوع", "settings:home")],
    ]
    return text, rows


def build_history(settings: dict, user_id: int, offset: int = 0) -> tuple[str, list]:
    total = get_history_count(user_id)
    entries = get_user_history(user_id, limit=PAGE_SIZE, offset=offset)
    if not entries:
        text = "📚 <b>سجل التحميلات</b>\n\nلا توجد تحميلات سابقة. أرسل رابطًا وابدأ!"
        rows = [[("🔵 ↩️ رجوع", "settings:home")]]
        return text, rows

    lines = ["📚 <b>سجل التحميلات</b>", ""]
    for i, e in enumerate(entries, start=1 + offset):
        name = (e.get("title") or e.get("url") or f"تحميل {e.get('id')}")[:34]
        lines.append(f"{i}. <b>{name}</b>")
    text = "\n".join(lines) + f"\n\nالمجموع: {total}"

    rows = []
    for e in entries:
        name = (e.get("title") or e.get("url") or f"تحميل {e.get('id')}")[:26]
        rows.append([(f"🔵 🔎 {name}", f"settings:history:detail:{e['id']}")])
    nav = []
    if total > (offset + PAGE_SIZE):
        nav.append(("🔵 ⬇️ التالية", f"settings:history:page:{offset + PAGE_SIZE}"))
    if offset > 0:
        nav.append(("🔵 ⬆️ السابقة", f"settings:history:page:{max(0, offset - PAGE_SIZE)}"))
    if nav:
        rows.append(nav)
    rows.append([("🔴 🗑 مسح السجل", "settings:history:clear:confirm"),
                 ("🔵 ↩️ رجوع", "settings:home")])
    return text, rows


def build_history_detail(settings: dict, user_id: int, entry_id: int) -> tuple[str, list]:
    e = get_history_entry(user_id, entry_id)
    if not e:
        text = "📚 هذا السجل غير موجود أو لا يخص حسابك."
        rows = [[("🔵 ↩️ رجوع للسجل", "settings:history")]]
        return text, rows
    name = (e.get("title") or e.get("url") or f"تحميل {e['id']}")[:40]
    text = (
        "📄 <b>تفاصيل التحميل</b>\n\n"
        f"الملف: <b>{name}</b>\n"
        f"الجودة: <b>{e.get('quality') or '-'}</b>\n"
        f"النوع: <b>{e.get('media_type') or '-'}</b>\n"
        f"التاريخ: <b>{e.get('created_at') or '-'}</b>\n"
        f"الحجم: <b>{format_size(e.get('file_size') or 0)}</b>\n"
    )
    rows = [
        [("🟢 🔁 تحميل مرة أخرى", f"settings:history:redownload:{entry_id}")],
        [("🔴 🗑 حذف من السجل", f"settings:history:delete:{entry_id}")],
        [("🔵 ↩️ رجوع للسجل", "settings:history")],
    ]
    return text, rows


def build_history_clear_confirm(settings: dict, user_id: int) -> tuple[str, list]:
    text = (
        "🗑 <b>مسح السجل</b>\n\n"
        "هل تريد مسح سجل التحميلات بالكامل؟\n"
        "ده مش هيحذف أي ملفات من جهازك، وبيمسح بيانات السجل من سيرفر البوت فقط."
    )
    rows = [
        [("🔴 ✅ نعم، امسح", "settings:history:clear:yes"),
         ("🟢 ❌ إلغاء", "settings:history")],
    ]
    return text, rows


def build_privacy(settings: dict) -> tuple[str, list]:
    retention = int(settings["history_retention_days"] or 0)
    ret_label = "الاحتفاظ للأبد" if retention <= 0 else f"{retention} يوم"
    text = (
        "🔒 <b>الخصوصية والمساحة</b>\n\n"
        f"⏳ مدة الاحتفاظ بالسجل الحالية: <b>{ret_label}</b>\n"
        "🧹 البوت بينضف الملفات المؤقتة تلقائيًا ولا يخزّن ملفات نهائية على الخادم."
    )
    rows = [
        [("🟢 🧹 تنظيف الملفات المؤقتة", "settings:privacy:clean")],
        [("🔵 ⏳ مدة الاحتفاظ بالسجل", "settings:privacy:retention")],
        [("🔴 🔐 حذف بيانات حسابي", "settings:privacy:delete:confirm")],
        [("🔵 ↩️ رجوع", "settings:home")],
    ]
    return text, rows


def build_retention(settings: dict) -> tuple[str, list]:
    current = int(settings["history_retention_days"] or 0)
    text = "⏳ <b>مدة الاحتفاظ بالسجل</b>\nمتى تُحذف سجلات التحميل القديمة تلقائيًا:"
    rows = []
    for days in HISTORY_RETENTION_OPTIONS:
        label = ("🟢 ✅ " if days == current else "⚪ ") + f"{days} يوم"
        rows.append([(label, f"settings:privacy:retention:set:{days}")])
    forever = "🟢 ✅ " if current == 0 else "⚪ "
    rows.append([(forever + "♾️ الاحتفاظ للأبد", "settings:privacy:retention:set:0")])
    rows.append([("🔵 ↩️ رجوع", "settings:privacy")])
    return text, rows


def build_delete_confirm(settings: dict) -> tuple[str, list]:
    text = (
        "🔐 <b>حذف بيانات حسابي</b>\n\n"
        "هيحذف: تفضيلاتك، سجل تحميلاتك، وبيانات حسابك من البوت.\n"
        "مش هيحذف: الملفات اللي وصلتك على تيليجرام.\n\n"
        "الإجراء ده غير قابل للتراجع."
    )
    rows = [
        [("🔴 ✅ نعم، احذف بياناتي", "settings:privacy:delete:yes")],
        [("🟢 ❌ إلغاء", "settings:privacy")],
    ]
    return text, rows


def build_notify(settings: dict) -> tuple[str, list]:
    s = settings
    text = (
        "🔔 <b>الإشعارات</b>\n\n"
        "اضغط على أي مفتاح لتشغيله أو إيقافه."
    )
    toggles = [
        ("notify_complete", "اكتمال التحميل"),
        ("notify_failure", "فشل التحميل"),
        ("notify_progress", "تحديثات التقدم"),
        ("notify_queue", "إشعارات قائمة الانتظار"),
        ("quiet_mode", "الوضع الهادئ"),
    ]
    rows = []
    for field, label in toggles:
        state = "🟢 مفعّل" if bool(s[field]) else "🔴 متوقف"
        rows.append([(f"{state} · {label}", f"settings:notify:toggle:{field}")])
    rows.append([("🔵 ↩️ رجوع", "settings:home")])
    return text, rows


def build_reset_confirm(settings: dict) -> tuple[str, list]:
    text = (
        "♻️ <b>إعادة ضبط الإعدادات</b>\n\n"
        "هل تريد إرجاع إعداداتك إلى القيم الافتراضية؟\n"
        "لن يتم حذف سجل التحميلات أو ملفاتك."
    )
    rows = [
        [("🔴 ✅ نعم، إعادة الضبط", "settings:reset:yes"),
         ("🟢 ❌ إلغاء", "settings:home")],
    ]
    return text, rows


def build_saved(field: str, value: str, back_cb: str = "settings:home") -> tuple[str, list]:
    text = (
        "✅ <b>تم حفظ الإعدادات</b>\n\n"
        f"{field}: <b>{value}</b>\n"
        "سيُستخدم هذا الاختيار في التحميلات الجديدة."
    )
    rows = [
        [("🟢 🎯 تعديل الجودة", "settings:quality"),
         ("🟢 ⚙️ العودة للإعدادات", back_cb)],
    ]
    return text, rows


# ---- command entry ---------------------------------------------------------

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # Opening the menu cancels any pending free-text input (e.g. custom size)
    # so the user can never get stuck answering an old prompt.
    if getattr(context, "user_data", None) is not None:
        context.user_data.pop("settings_input", None)
    settings = get_user_settings(uid)
    text, rows = build_home(settings)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_mk(rows))


# ---- text input (custom max size) -----------------------------------------

async def try_handle_settings_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle free-text replies for settings prompts. Returns True if consumed."""
    pending = context.user_data.get("settings_input")
    if not pending:
        return False
    raw = (update.message.text or "").strip()
    uid = update.effective_user.id

    if pending == "size":
        context.user_data.pop("settings_input", None)
        if not raw:
            await update.message.reply_text(
                "📭 <b>مكتبتش أي قيمة.</b>\n\n"
                f"اكتب رقم بالميجابايت بين 1 و {MAX_CUSTOM_SIZE_MB}.\n"
                "مثال: <code>75</code> — أو /settings للرجوع.",
                parse_mode="HTML",
            )
            context.user_data["settings_input"] = "size"
            return True

        parsed = _parse_size_mb(raw)
        if parsed is None:
            await update.message.reply_text(
                "🔤 <b>دي مش رقم.</b> اللي كتبته حروف أو اسم.\n\n"
                f"اكتب رقم صحيح بالميجابايت بين 1 و {MAX_CUSTOM_SIZE_MB}.\n"
                "مثال: <code>120</code> — أو /settings للرجوع.",
                parse_mode="HTML",
            )
            context.user_data["settings_input"] = "size"
            return True
        if parsed < 1:
            await update.message.reply_text(
                "⬇️ <b>الرقم أقل من 1.</b> مينفعش صفر أو رقم سالب.\n\n"
                f"اكتب قيمة بين 1 و {MAX_CUSTOM_SIZE_MB} MB.",
                parse_mode="HTML",
            )
            context.user_data["settings_input"] = "size"
            return True
        if parsed > MAX_CUSTOM_SIZE_MB:
            await update.message.reply_text(
                f"⬆️ <b>الرقم كبير جدًا.</b> الحد الأقصى المسموح {MAX_CUSTOM_SIZE_MB} MB.\n\n"
                "جرّب قيمة أصغر.",
                parse_mode="HTML",
            )
            context.user_data["settings_input"] = "size"
            return True
        mb = int(parsed + 0.5)  # half-up rounding: 50.5 -> 51
        mb = max(1, min(mb, MAX_CUSTOM_SIZE_MB))
        save_settings(uid, max_file_size_mb=mb)
        note = "" if abs(parsed - mb) < 1e-9 else f" (تم تقريب {parsed:g} إلى {mb})"
        await update.message.reply_text(
            f"🟢 تم حفظ الحد الأقصى: <b>{mb} MB</b>{note}\nسيُستخدم في التحميلات الجديدة.",
            parse_mode="HTML",
        )
        return True

    context.user_data.pop("settings_input", None)
    return False


# ---- callback dispatcher ---------------------------------------------------

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    data = query.data or ""
    await query.answer()
    # Any inline-button press exits free-text entry mode (e.g. the custom-size
    # prompt), so a later ordinary message isn't swallowed by an old prompt.
    if getattr(context, "user_data", None) is not None:
        context.user_data.pop("settings_input", None)

    if data == "settings:close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    # 🎁 Referral center — its own async module (pages, reward claim, and the
    # secure token-linking flow live in handlers/referral.py).
    if data == "settings:referral":
        from handlers.referral import referral_page
        await referral_page(update, context, edit=True)
        return

    # Re-download from history must actually START a download, so it lives in
    # this async path (process_url) rather than the pure render dispatcher.
    if data.startswith("settings:history:redownload:"):
        try:
            eid = int(data.rsplit(":", 1)[-1])
        except ValueError:
            eid = -1
        entry = get_history_entry(uid, eid) if eid > 0 else None
        url = entry.get("url") if entry else None
        if not url:
            try:
                await safe_edit(
                    query.edit_message_text,
                    "📚 هذا السجل غير موجود أو لا يخص حسابك.",
                    parse_mode="HTML",
                    reply_markup=_mk([[("🔵 ↩️ رجوع للسجل", "settings:history")]]),
                )
            except Exception:
                pass
            return
        await query.answer("🔁 بدأ إعادة التحميل…")
        try:
            from handlers.download import process_url
            await process_url(update, context, query.from_user, url, query.message)
        except Exception:
            logger.exception("Redownload failed uid=%s url=%s", uid, url[:120])
            try:
                await query.message.reply_text(
                    "⚠️ فشل بدء إعادة التحميل. تأكد أن الرابط ما زال صالحًا أو أرسل رابطًا جديدًا.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    try:
        text, rows = _dispatch(uid, data, context)
    except Exception as exc:
        logger.exception("Settings callback failed uid=%s data=%s", uid, data)
        text = "⚠️ حصل خطأ غير متوقع. ارجع للقائمة وحاول مرة أخرى."
        rows = [[("🔵 ↩️ رجوع", "settings:home")]]
    try:
        await safe_edit(query.edit_message_text, text, parse_mode="HTML", reply_markup=_mk(rows))
    except Exception:
        # If the message can't be edited (deleted/stale), fall back to a new one.
        try:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=_mk(rows))
        except Exception:
            pass


def _dispatch(uid: int, data: str, context) -> tuple[str, list]:
    settings = get_user_settings(uid)

    if data == "settings:home":
        return build_home(settings)
    if data == "settings:close":
        return "✅ تم إغلاق الإعدادات.", []
    if data == "settings:quality":
        return build_quality(settings)
    if data == "settings:quality:video":
        return build_quality_video(settings)
    if data == "settings:quality:audio":
        return build_quality_audio(settings)
    if data == "settings:quality:size":
        return build_quality_size(settings)

    if data == "settings:quality:set:auto":
        save_settings(uid, video_quality="auto")
        return build_saved("الجودة الافتراضية", "ذكي تلقائيًا", "settings:quality")

    if data.startswith("settings:quality:video:set:"):
        value = data.rsplit(":", 1)[-1]
        if value not in VIDEO_QUALITY_OPTIONS:
            return build_quality_video(settings)
        save_settings(uid, video_quality=value)
        return build_saved("جودة الفيديو الافتراضية", _vq(value), "settings:quality")

    if data.startswith("settings:quality:audio:set:"):
        value = data.rsplit(":", 1)[-1]
        if value not in AUDIO_QUALITY_OPTIONS:
            return build_quality_audio(settings)
        save_settings(uid, audio_quality=value)
        return build_saved("جودة الصوت الافتراضية", _aq(value), "settings:quality")

    if data.startswith("settings:quality:size:set:"):
        try:
            mb = int(data.rsplit(":", 1)[-1])
        except ValueError:
            mb = -1
        if mb < 0 or mb > MAX_CUSTOM_SIZE_MB:
            return build_quality_size(settings)
        save_settings(uid, max_file_size_mb=mb)
        return build_saved("الحد الأقصى للحجم", _size(mb), "settings:quality")

    if data == "settings:quality:size:custom":
        context.user_data["settings_input"] = "size"
        return (
            "📏 <b>إدخال حجم مخصص</b>\n\n"
            f"اكتب رقم بين 1 و {MAX_CUSTOM_SIZE_MB} (بالميجابايت).\n"
            "مثال: <code>75</code>\n\n"
            "⚠️ هيتقبل الأرقام بس — الحروف والقيم السالبة هترفض.",
            [[("🔴 ↩️ إلغاء", "settings:quality:size")]],
        )

    if data == "settings:delivery":
        return build_delivery(settings)
    if data == "settings:delivery:info":
        return build_delivery_info(settings)
    if data.startswith("settings:delivery:set:"):
        value = data.rsplit(":", 1)[-1]
        if value not in DELIVERY_MODES:
            return build_delivery(settings)
        save_settings(uid, delivery_mode=value)
        return build_saved("طريقة الاستلام", _delivery(value), "settings:delivery")

    if data == "settings:queue":
        return build_queue(settings, uid)

    if data == "settings:history":
        return build_history(settings, uid, 0)
    if data.startswith("settings:history:page:"):
        try:
            offset = int(data.rsplit(":", 1)[-1])
        except ValueError:
            offset = 0
        return build_history(settings, uid, max(0, offset))
    if data.startswith("settings:history:detail:"):
        try:
            eid = int(data.rsplit(":", 1)[-1])
        except ValueError:
            eid = -1
        return build_history_detail(settings, uid, eid)
    if data.startswith("settings:history:delete:"):
        try:
            eid = int(data.rsplit(":", 1)[-1])
        except ValueError:
            eid = -1
        if delete_history_entry(uid, eid):
            return ("✅ تم حذف العملية من السجل فقط (الملف نفسه لم يُمس).\n\n"
                    "<b>سجل التحميلات</b>", [[("🔵 ↩️ رجوع للسجل", "settings:history")]])
        return build_history(settings, uid, 0)
    if data == "settings:history:clear:confirm":
        return build_history_clear_confirm(settings, uid)
    if data == "settings:history:clear:yes":
        removed = clear_history(uid)
        return (
            f"✅ تم مسح {removed} عملية من سجل التحميلات.\n"
            "ملفاتك على الجهاز لم تُمس.",
            [[("🔵 ↩️ رجوع", "settings:home")]],
        )

    if data == "settings:privacy":
        return build_privacy(settings)
    if data == "settings:privacy:clean":
        removed = cleanup_stale_temp_files()
        return (
            f"🧹 تم تنظيف الملفات المؤقتة (حُذف {removed} ملف قديم).\n"
            "أي تحميل جارٍ لم يتأثر.",
            [[("🔵 ↩️ رجوع", "settings:privacy")]],
        )
    if data == "settings:privacy:retention":
        return build_retention(settings)
    if data.startswith("settings:privacy:retention:set:"):
        try:
            days = int(data.rsplit(":", 1)[-1])
        except ValueError:
            days = -1
        if days < 0:
            return build_retention(settings)
        save_settings(uid, history_retention_days=days)
        label = "الاحتفاظ للأبد" if days <= 0 else f"{days} يوم"
        return build_saved("مدة الاحتفاظ بالسجل", label, "settings:privacy")
    if data == "settings:privacy:delete:confirm":
        return build_delete_confirm(settings)
    if data == "settings:privacy:delete:yes":
        delete_user_data(uid)
        return (
            "🔐 تم حذف بيانات حسابك بالكامل (التفضيلات + السجل + الحساب).\n\n"
            "أرسل /start لبدء استخدام البوت من جديد.",
            [],
        )

    if data == "settings:notify":
        return build_notify(settings)
    if data.startswith("settings:notify:toggle:"):
        field = data.rsplit(":", 1)[-1]
        if field not in USER_SETTINGS_FIELDS:
            return build_notify(settings)
        new_value = 0 if bool(settings.get(field)) else 1
        save_settings(uid, **{field: new_value})
        settings = get_user_settings(uid)
        return build_notify(settings)

    if data == "settings:reset":
        return build_reset_confirm(settings)
    if data == "settings:reset:yes":
        reset_settings(uid)
        return (
            "♻️ <b>تمت إعادة الضبط.</b>\n\n"
            "تم إرجاع كل التفضيلات للقيم الافتراضية — سجل التحميلات وملفاتك لم تُمس.",
            [[("🟢 ⚙️ فتح الإعدادات", "settings:home")]],
        )

    return build_home(settings)
