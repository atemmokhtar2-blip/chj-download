"""
📁 مدير الملفات — File Manager Pro
"""
import os
import zipfile
import io
import mimetypes
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from database.db import log_action
from utils.logger import action_logger, error_logger

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROTECTED_NAMES = {".env", "bot.db", "developer.db", "support.db", "settings.py"}
PROTECTED_DIRS  = {"database"}
MAX_VIEW_BYTES  = 3500
MAX_EDIT_BYTES  = 50_000

STATE_FM_UPLOAD      = "dv_fm_upload"
STATE_FM_EDIT_WRITE  = "dv_fm_edit_write"
STATE_FM_MKDIR       = "dv_fm_mkdir"
STATE_FM_SEARCH      = "dv_fm_search"


# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt_size(b: int) -> str:
    if b < 1024:        return f"{b} B"
    elif b < 1024**2:   return f"{b/1024:.1f} KB"
    elif b < 1024**3:   return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def _is_protected(path: str) -> bool:
    rel = os.path.relpath(path, _root)
    parts = rel.replace("\\", "/").split("/")
    if os.path.basename(path) in PROTECTED_NAMES:
        return True
    if any(p in PROTECTED_DIRS for p in parts):
        return True
    return False


def _is_text(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in {".py", ".txt", ".md", ".json", ".yaml", ".yml", ".env",
                   ".cfg", ".ini", ".log", ".sh", ".js", ".ts", ".html", ".css"}


def _safe_rel(path: str) -> str:
    rel = os.path.relpath(path, _root)
    return rel.replace("\\", "/")


def _resolve(rel_path: str) -> str:
    """Resolve a relative path safely within _root."""
    full = os.path.normpath(os.path.join(_root, rel_path))
    if not full.startswith(_root):
        raise ValueError("المسار خارج نطاق المشروع")
    return full


# ── navigation ───────────────────────────────────────────────────────────────

def _list_dir(path: str) -> tuple[list, list]:
    """Returns (dirs, files) sorted."""
    dirs, files = [], []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name in {"__pycache__", ".git"}:
                continue
            if entry.is_dir():
                dirs.append(entry)
            else:
                files.append(entry)
    except PermissionError:
        pass
    return dirs, files


def _dir_kb(path: str, offset: int = 0) -> InlineKeyboardMarkup:
    rel = _safe_rel(path)
    dirs, files = _list_dir(path)
    items = dirs + files
    page_size = 8
    page = items[offset:offset + page_size]
    rows = []

    for entry in page:
        name = entry.name
        icon = "📂" if entry.is_dir() else "📄"
        entry_rel = _safe_rel(entry.path)
        # Truncate callback_data safely (limit 64 bytes)
        cb = f"dv_fm_open|{entry_rel}"
        if len(cb.encode()) > 60:
            # Store in abbreviated form - use index
            cb = f"dv_fm_open|{entry_rel[:45]}"
        rows.append([InlineKeyboardButton(f"{icon} {name}", callback_data=cb)])

    # Pagination
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️ السابق", callback_data=f"dv_fm_nav|{rel}|{offset - page_size}"))
    if offset + page_size < len(items):
        nav.append(InlineKeyboardButton("التالي ▶️", callback_data=f"dv_fm_nav|{rel}|{offset + page_size}"))
    if nav:
        rows.append(nav)

    # Actions
    rows.append([
        InlineKeyboardButton("📤 رفع ملف",    callback_data=f"dv_fm_upload|{rel}"),
        InlineKeyboardButton("🔍 بحث",        callback_data="dv_search_prompt"),
    ])
    rows.append([
        InlineKeyboardButton("📦 ضغط ZIP",    callback_data=f"dv_fm_zip|{rel}"),
    ])

    # Back navigation
    parent = os.path.dirname(path)
    if path != _root and parent != path:
        parent_rel = _safe_rel(parent)
        rows.append([InlineKeyboardButton("⬆️ المجلد الأعلى", callback_data=f"dv_fm_nav|{parent_rel}|0")])
    rows.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="dv_menu")])

    return InlineKeyboardMarkup(rows)


def _file_kb(path: str) -> InlineKeyboardMarkup:
    rel = _safe_rel(path)
    protected = _is_protected(path)
    is_text = _is_text(path)

    rows = [
        [InlineKeyboardButton("📥 تنزيل الملف", callback_data=f"dv_fm_dl|{rel}")],
    ]
    if is_text and not protected:
        rows.append([InlineKeyboardButton("👁 عرض المحتوى",  callback_data=f"dv_fm_view|{rel}")])
        rows.append([InlineKeyboardButton("✏️ تعديل الملف",  callback_data=f"dv_fm_edit|{rel}")])
    if not protected:
        rows.append([InlineKeyboardButton("🗑 حذف الملف",    callback_data=f"dv_fm_del_confirm|{rel}")])

    parent_rel = _safe_rel(os.path.dirname(path))
    rows.append([InlineKeyboardButton("⬆️ رجوع للمجلد",  callback_data=f"dv_fm_nav|{parent_rel}|0")])
    return InlineKeyboardMarkup(rows)


# ── handlers ─────────────────────────────────────────────────────────────────

async def show_file_manager(query, context):
    context.user_data["dv_fm_path"] = _root
    dirs, files = _list_dir(_root)
    total = len(dirs) + len(files)
    await query.edit_message_text(
        f"📁 <b>مدير الملفات</b>\n\n"
        f"📂 المجلد: <code>/</code>\n"
        f"📊 {len(dirs)} مجلد، {len(files)} ملف (إجمالي: {total})",
        parse_mode="HTML",
        reply_markup=_dir_kb(_root),
    )


async def navigate_dir(query, context, rel_path: str, offset: int):
    try:
        full = _resolve(rel_path)
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return

    if os.path.isdir(full):
        context.user_data["dv_fm_path"] = full
        dirs, files = _list_dir(full)
        rel_display = rel_path if rel_path else "/"
        await query.edit_message_text(
            f"📁 <b>مدير الملفات</b>\n\n"
            f"📂 المجلد: <code>{rel_display}</code>\n"
            f"📊 {len(dirs)} مجلد، {len(files)} ملف",
            parse_mode="HTML",
            reply_markup=_dir_kb(full, offset),
        )
    elif os.path.isfile(full):
        await open_file(query, context, rel_path)
    else:
        await query.answer("المسار غير موجود", show_alert=True)


async def open_file(query, context, rel_path: str):
    try:
        full = _resolve(rel_path)
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return

    if not os.path.exists(full):
        await query.answer("الملف غير موجود", show_alert=True)
        return

    context.user_data["dv_fm_file"] = full
    size = os.path.getsize(full)
    prot = "🔐 محمي" if _is_protected(full) else "✅ قابل للتعديل"
    name = os.path.basename(full)

    await query.edit_message_text(
        f"📄 <b>معلومات الملف</b>\n\n"
        f"• الاسم: <code>{name}</code>\n"
        f"• الحجم: <b>{_fmt_size(size)}</b>\n"
        f"• الحالة: {prot}\n"
        f"• المسار: <code>{rel_path}</code>",
        parse_mode="HTML",
        reply_markup=_file_kb(full),
    )


async def view_file(query, context, rel_path: str):
    try:
        full = _resolve(rel_path)
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return

    try:
        size = os.path.getsize(full)
        if size > MAX_EDIT_BYTES:
            await query.answer("الملف كبير جداً للعرض المباشر — استخدم التنزيل.", show_alert=True)
            return
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(MAX_VIEW_BYTES)
        truncated = size > MAX_VIEW_BYTES
        name = os.path.basename(full)
        text = (
            f"📄 <b>{name}</b>\n"
            + (f"<i>(مقتطع — أول {MAX_VIEW_BYTES} حرف)</i>\n" if truncated else "")
            + f"\n<pre>{content}</pre>"
        )
        parent_rel = _safe_rel(os.path.dirname(full))
        await query.edit_message_text(
            text[:4096],
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 تنزيل الكامل",    callback_data=f"dv_fm_dl|{rel_path}")],
                [InlineKeyboardButton("✏️ تعديل",           callback_data=f"dv_fm_edit|{rel_path}")],
                [InlineKeyboardButton("🔙 رجوع",            callback_data=f"dv_fm_nav|{parent_rel}|0")],
            ]),
        )
    except Exception as e:
        await query.answer(f"خطأ في القراءة: {e}", show_alert=True)


async def download_file(query, context, rel_path: str):
    uid = query.from_user.id
    try:
        full = _resolve(rel_path)
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return

    if not os.path.exists(full):
        await query.answer("الملف غير موجود", show_alert=True)
        return

    try:
        with open(full, "rb") as f:
            data = f.read()
        name = os.path.basename(full)
        await query.message.reply_document(
            document=InputFile(io.BytesIO(data), filename=name),
            caption=f"📥 <code>{rel_path}</code>",
            parse_mode="HTML",
        )
        log_action(uid, "fm_download", rel_path, "ok")
    except Exception as e:
        error_logger.error("FM download error: %s", e)
        await query.answer(f"فشل التنزيل: {e}", show_alert=True)


async def prompt_edit_file(query, context, rel_path: str):
    try:
        full = _resolve(rel_path)
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return

    if _is_protected(full):
        await query.answer("🔐 هذا الملف محمي — لا يمكن تعديله مباشرة.", show_alert=True)
        return

    size = os.path.getsize(full)
    if size > MAX_EDIT_BYTES:
        await query.answer("الملف كبير جداً للتعديل المباشر.", show_alert=True)
        return

    context.user_data["dv_state"] = STATE_FM_EDIT_WRITE
    context.user_data["dv_fm_edit_path"] = full

    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            current = f.read(1500)
    except Exception:
        current = "(تعذّر قراءة المحتوى الحالي)"

    await query.edit_message_text(
        f"✏️ <b>تعديل الملف</b>\n\n"
        f"📄 <code>{rel_path}</code>\n\n"
        f"<b>المحتوى الحالي (مقتطع):</b>\n<pre>{current[:800]}</pre>\n\n"
        "أرسل المحتوى الجديد الكامل للملف.\n"
        "<i>أرسل /cancel للإلغاء</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data=f"dv_fm_open|{rel_path}")]
        ]),
    )


async def handle_edit_write(update, context):
    uid = update.effective_user.id
    full = context.user_data.get("dv_fm_edit_path")
    context.user_data.pop("dv_state", None)
    context.user_data.pop("dv_fm_edit_path", None)

    if not full or not os.path.exists(full):
        await update.message.reply_text("❌ الملف المراد تعديله غير محدد.")
        return

    new_content = update.message.text
    rel = _safe_rel(full)

    # Auto-backup the file before edit
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            old_content = f.read()
        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()
        added   = len([l for l in new_lines if l not in old_lines])
        removed = len([l for l in old_lines if l not in new_lines])

        # Write new content
        with open(full, "w", encoding="utf-8") as f:
            f.write(new_content)

        log_action(uid, "fm_edit_file", rel, f"added={added},removed={removed}")
        action_logger.info("File edited: %s (+%d/-%d lines)", rel, added, removed)

        parent_rel = _safe_rel(os.path.dirname(full))
        await update.message.reply_text(
            f"✅ <b>تم حفظ الملف بنجاح</b>\n\n"
            f"📄 <code>{rel}</code>\n"
            f"➕ أسطر مضافة: <b>{added}</b>\n"
            f"➖ أسطر محذوفة: <b>{removed}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📄 عرض الملف",    callback_data=f"dv_fm_view|{rel}")],
                [InlineKeyboardButton("🔙 رجوع للمجلد",  callback_data=f"dv_fm_nav|{parent_rel}|0")],
            ]),
        )
    except Exception as e:
        error_logger.error("FM edit error: %s", e)
        log_action(uid, "fm_edit_file", rel, f"error: {e}")
        await update.message.reply_text(f"❌ فشل حفظ الملف: <code>{e}</code>", parse_mode="HTML")


async def confirm_delete_file(query, context, rel_path: str):
    try:
        full = _resolve(rel_path)
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return

    if _is_protected(full):
        await query.answer("🔐 هذا الملف محمي — لا يمكن حذفه.", show_alert=True)
        return

    name = os.path.basename(full)
    await query.edit_message_text(
        f"⚠️ <b>تأكيد الحذف</b>\n\n"
        f"هل تريد حذف: <code>{name}</code>؟\n"
        f"📍 المسار: <code>{rel_path}</code>\n\n"
        "⛔ لا يمكن التراجع عن هذا الإجراء.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ تأكيد الحذف", callback_data=f"dv_fm_del_do|{rel_path}"),
                InlineKeyboardButton("❌ إلغاء",       callback_data=f"dv_fm_open|{rel_path}"),
            ]
        ]),
    )


async def do_delete_file(query, context, rel_path: str):
    uid = query.from_user.id
    try:
        full = _resolve(rel_path)
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return

    if _is_protected(full):
        await query.answer("🔐 هذا الملف محمي.", show_alert=True)
        return

    try:
        os.remove(full)
        log_action(uid, "fm_delete_file", rel_path, "ok")
        parent_rel = _safe_rel(os.path.dirname(full))
        await query.edit_message_text(
            f"🗑 <b>تم حذف الملف</b>\n\n<code>{rel_path}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع للمجلد", callback_data=f"dv_fm_nav|{parent_rel}|0")]
            ]),
        )
    except Exception as e:
        error_logger.error("FM delete error: %s", e)
        await query.answer(f"فشل الحذف: {e}", show_alert=True)


async def zip_directory(query, context, rel_path: str):
    uid = query.from_user.id
    try:
        full = _resolve(rel_path)
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return

    await query.edit_message_text("📦 <b>جارٍ ضغط المجلد…</b>", parse_mode="HTML")
    try:
        buf = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if os.path.isdir(full):
                for dp, _, files in os.walk(full):
                    for fname in files:
                        fp = os.path.join(dp, fname)
                        zf.write(fp, os.path.relpath(fp, full))
                        count += 1
            else:
                zf.write(full, os.path.basename(full))
                count = 1
        buf.seek(0)
        name = os.path.basename(full) or "project"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_name = f"{name}_{ts}.zip"
        await query.message.reply_document(
            document=InputFile(buf, filename=zip_name),
            caption=f"📦 <code>{rel_path}</code> — {count} ملف",
            parse_mode="HTML",
        )
        log_action(uid, "fm_zip", rel_path, f"files={count}")
        parent_rel = _safe_rel(os.path.dirname(full) if os.path.isfile(full) else full)
        await query.edit_message_text(
            f"✅ <b>تم إنشاء ZIP</b>\n<code>{zip_name}</code> — {count} ملف",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data=f"dv_fm_nav|{parent_rel}|0")]
            ]),
        )
    except Exception as e:
        error_logger.error("FM zip error: %s", e)
        await query.edit_message_text(
            f"❌ فشل الضغط: <code>{e}</code>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="dv_files")]
            ]),
        )


async def prompt_upload_file(query, context, rel_path: str):
    context.user_data["dv_state"] = STATE_FM_UPLOAD
    context.user_data["dv_fm_upload_dir"] = rel_path
    await query.edit_message_text(
        f"📤 <b>رفع ملف</b>\n\n"
        f"📂 إلى: <code>{rel_path}</code>\n\n"
        "أرسل الملف الآن.\n"
        "<i>أرسل /cancel للإلغاء</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data=f"dv_fm_nav|{rel_path}|0")]
        ]),
    )


async def handle_upload_file(update, context):
    uid = update.effective_user.id
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ يرجى إرسال ملف.")
        return

    rel_dir = context.user_data.get("dv_fm_upload_dir", ".")
    context.user_data.pop("dv_state", None)
    context.user_data.pop("dv_fm_upload_dir", None)

    try:
        dest_dir = _resolve(rel_dir)
    except ValueError:
        dest_dir = _root

    dest_path = os.path.join(dest_dir, doc.file_name)
    if _is_protected(dest_path):
        await update.message.reply_text("🔐 لا يمكن رفع ملف إلى مسار محمي.")
        return

    try:
        file_obj = await doc.get_file()
        await file_obj.download_to_drive(dest_path)
        rel = _safe_rel(dest_path)
        log_action(uid, "fm_upload", rel, f"size={doc.file_size}")
        await update.message.reply_text(
            f"✅ <b>تم رفع الملف بنجاح</b>\n\n"
            f"📄 <code>{rel}</code>\n"
            f"💾 {_fmt_size(doc.file_size or 0)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📄 فتح الملف",    callback_data=f"dv_fm_open|{rel}")],
                [InlineKeyboardButton("🔙 رجوع للمجلد",  callback_data=f"dv_fm_nav|{rel_dir}|0")],
            ]),
        )
    except Exception as e:
        error_logger.error("FM upload error: %s", e)
        await update.message.reply_text(f"❌ فشل رفع الملف: <code>{e}</code>", parse_mode="HTML")
