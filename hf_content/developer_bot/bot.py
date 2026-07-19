import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

from config.settings import DEVELOPER_BOT_TOKEN, OWNER_ID
from handlers.auth import is_owner, require_owner
from handlers.dashboard import start_cmd, panel_cmd, show_main_menu
from handlers.updates import (
    show_updates_menu, prompt_upload, apply_update,
    show_update_history, handle_zip_upload,
    STATE_UPLOAD,
)
from handlers.backups import (
    show_backups_menu, create_backup, show_backups_list,
    confirm_delete_backup, do_delete_backup,
)
from handlers.services import (
    show_services_menu, prompt_restart, do_restart, show_service_status,
)
from handlers.monitor import show_monitor
from handlers.errors import (
    show_errors_menu, show_log_view, export_log_file,
    export_all_logs, confirm_clear_logs, do_clear_logs,
)
from handlers.security_h import show_security_menu, show_action_log
from handlers.settings_h import show_settings, show_paths
from handlers.ai_assistant import (
    show_ai_menu, show_ai_help, prompt_ai_input, handle_ai_command,
    STATE_AI,
)
from handlers.file_manager import (
    show_file_manager, navigate_dir, open_file, view_file,
    download_file, prompt_edit_file, handle_edit_write,
    confirm_delete_file, do_delete_file, zip_directory,
    prompt_upload_file, handle_upload_file,
    STATE_FM_UPLOAD, STATE_FM_EDIT_WRITE,
)
from handlers.health_check import show_health_check
from handlers.emergency import (
    show_emergency_menu, restore_last_backup, do_restore_backup,
    emergency_restart_all, emergency_backup,
)
from handlers.search import (
    show_search_menu, prompt_search, handle_search_query,
    STATE_SEARCH,
)
from database.db import init_db, log_action
from utils.logger import system_logger, error_logger


# ── Callback dispatcher ────────────────────────────────────────────────────────

async def callback_handler(update: Update, context):
    query = update.callback_query
    if not query:
        return

    user = query.from_user
    if not is_owner(user.id):
        await query.answer("🚫 وصول مرفوض — هذا البوت للمطوّر فقط.", show_alert=True)
        return

    await query.answer()
    data = query.data

    try:
        # ── Main menu ──────────────────────────────────────────────────────────
        if data == "dv_menu":
            await show_main_menu(query, context)

        # ── AI Assistant ──────────────────────────────────────────────────────
        elif data == "dv_ai":
            await show_ai_menu(query, context)
        elif data == "dv_ai_help":
            await show_ai_help(query, context)
        elif data == "dv_ai_prompt":
            await prompt_ai_input(query, context)

        # ── File Manager ──────────────────────────────────────────────────────
        elif data == "dv_files":
            await show_file_manager(query, context)
        elif data.startswith("dv_fm_nav|"):
            parts = data.split("|")
            rel = parts[1] if len(parts) > 1 else "."
            offset = int(parts[2]) if len(parts) > 2 else 0
            await navigate_dir(query, context, rel, offset)
        elif data.startswith("dv_fm_open|"):
            rel = data[len("dv_fm_open|"):]
            await open_file(query, context, rel)
        elif data.startswith("dv_fm_view|"):
            rel = data[len("dv_fm_view|"):]
            await view_file(query, context, rel)
        elif data.startswith("dv_fm_dl|"):
            rel = data[len("dv_fm_dl|"):]
            await download_file(query, context, rel)
        elif data.startswith("dv_fm_edit|"):
            rel = data[len("dv_fm_edit|"):]
            await prompt_edit_file(query, context, rel)
        elif data.startswith("dv_fm_del_confirm|"):
            rel = data[len("dv_fm_del_confirm|"):]
            await confirm_delete_file(query, context, rel)
        elif data.startswith("dv_fm_del_do|"):
            rel = data[len("dv_fm_del_do|"):]
            await do_delete_file(query, context, rel)
        elif data.startswith("dv_fm_zip|"):
            rel = data[len("dv_fm_zip|"):]
            await zip_directory(query, context, rel)
        elif data.startswith("dv_fm_upload|"):
            rel = data[len("dv_fm_upload|"):]
            await prompt_upload_file(query, context, rel)

        # ── Search ────────────────────────────────────────────────────────────
        elif data == "dv_search":
            await show_search_menu(query, context)
        elif data == "dv_search_prompt":
            context.user_data["dv_state"] = STATE_SEARCH
            context.user_data["dv_search_type"] = "all"
            await query.edit_message_text(
                "🔍 <b>البحث داخل المشروع</b>\n\nأرسل الكلمة أو النص:",
                parse_mode="HTML",
            )
        elif data.startswith("dv_srch_type_"):
            stype = data[len("dv_srch_type_"):]
            await prompt_search(query, context, stype)

        # ── Updates ───────────────────────────────────────────────────────────
        elif data == "dv_updates":
            await show_updates_menu(query, context)
        elif data == "dv_upd_upload":
            await prompt_upload(query, context)
        elif data == "dv_upd_apply":
            await apply_update(query, context)
        elif data == "dv_upd_history":
            await show_update_history(query, context)

        # ── Backups ───────────────────────────────────────────────────────────
        elif data == "dv_backups":
            await show_backups_menu(query, context)
        elif data == "dv_bkp_create":
            await create_backup(query, context)
        elif data == "dv_bkp_list":
            await show_backups_list(query, context)
        elif data.startswith("dv_bkp_del_confirm_"):
            bid = int(data.split("_")[-1])
            await confirm_delete_backup(query, context, bid)
        elif data.startswith("dv_bkp_del_do_"):
            bid = int(data.split("_")[-1])
            await do_delete_backup(query, context, bid)

        # ── Services ──────────────────────────────────────────────────────────
        elif data == "dv_services":
            await show_services_menu(query, context)
        elif data == "dv_svc_status":
            await show_service_status(query, context)
        elif data.startswith("dv_svc_restart_") and not data.startswith("dv_svc_confirm_"):
            target = data[len("dv_svc_restart_"):]
            await prompt_restart(query, context, target)
        elif data.startswith("dv_svc_confirm_"):
            target = data[len("dv_svc_confirm_"):]
            await do_restart(query, context, target)

        # ── Monitor ───────────────────────────────────────────────────────────
        elif data == "dv_monitor":
            await show_monitor(query, context)

        # ── Errors ────────────────────────────────────────────────────────────
        elif data == "dv_errors":
            await show_errors_menu(query, context)
        elif data.startswith("dv_err_view_"):
            log_key = data[len("dv_err_view_"):]
            await show_log_view(query, context, log_key)
        elif data.startswith("dv_err_dl_"):
            log_key = data[len("dv_err_dl_"):]
            await export_log_file(query, context, log_key)
        elif data == "dv_err_export":
            await export_all_logs(query, context)
        elif data == "dv_err_clear_confirm":
            await confirm_clear_logs(query, context)
        elif data == "dv_err_clear_do":
            await do_clear_logs(query, context)

        # ── Health Check ──────────────────────────────────────────────────────
        elif data == "dv_health":
            await show_health_check(query, context)

        # ── Emergency ─────────────────────────────────────────────────────────
        elif data == "dv_emergency":
            await show_emergency_menu(query, context)
        elif data == "dv_em_restore_last":
            await restore_last_backup(query, context)
        elif data.startswith("dv_em_restore_do_"):
            bid = int(data.split("_")[-1])
            await do_restore_backup(query, context, bid)
        elif data == "dv_em_restart_all":
            await emergency_restart_all(query, context)
        elif data == "dv_em_backup_now":
            await emergency_backup(query, context)

        # ── Security ──────────────────────────────────────────────────────────
        elif data == "dv_security":
            await show_security_menu(query, context)
        elif data == "dv_sec_log":
            await show_action_log(query, context)

        # ── Settings ──────────────────────────────────────────────────────────
        elif data == "dv_settings":
            await show_settings(query, context)
        elif data == "dv_cfg_paths":
            await show_paths(query, context)

    except Exception as exc:
        error_logger.error("Callback error [%s]: %s", data, exc, exc_info=True)
        log_action(user.id, "callback_error", data, str(exc))
        try:
            await query.edit_message_text(
                f"❌ حدث خطأ: <code>{exc}</code>\n\nاستخدم /panel للمتابعة.",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ── Message & document router ─────────────────────────────────────────────────

@require_owner
async def message_router(update: Update, context):
    state = context.user_data.get("dv_state", "")

    if state == STATE_UPLOAD:
        await update.message.reply_text("📤 يرجى إرسال ملف ZIP، أو /cancel للإلغاء.")
        return
    if state == STATE_AI:
        await handle_ai_command(update, context)
        return
    if state == STATE_FM_EDIT_WRITE:
        await handle_edit_write(update, context)
        return
    if state == STATE_SEARCH:
        await handle_search_query(update, context)
        return

    # No state — treat as AI command attempt
    if update.message.text and not update.message.text.startswith("/"):
        context.user_data["dv_state"] = STATE_AI
        await handle_ai_command(update, context)
    else:
        await update.message.reply_text("🛠 استخدم /panel لفتح لوحة المطوّر.")


@require_owner
async def document_router(update: Update, context):
    state = context.user_data.get("dv_state", "")
    if state == STATE_UPLOAD:
        await handle_zip_upload(update, context)
    elif state == STATE_FM_UPLOAD:
        await handle_upload_file(update, context)
    else:
        doc = update.message.document
        if doc and doc.file_name and doc.file_name.endswith(".zip"):
            # Auto-route ZIP uploads to update handler
            context.user_data["dv_state"] = STATE_UPLOAD
            await handle_zip_upload(update, context)
        else:
            await update.message.reply_text(
                "📁 لرفع ملف، استخدم <b>مدير الملفات</b> → رفع ملف.\n"
                "أو لتطبيق تحديث ZIP، استخدم <b>إدارة التحديثات</b>.",
                parse_mode="HTML",
            )


# ── Cancel ─────────────────────────────────────────────────────────────────────

@require_owner
async def cancel_cmd(update: Update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ تم الإلغاء. استخدم /panel لمتابعة.")


# ── Post-init ──────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    init_db()
    system_logger.info("Developer Bot starting up… Owner ID: %s", OWNER_ID)


# ── Build app ──────────────────────────────────────────────────────────────────

def build_application() -> Application:
    if not DEVELOPER_BOT_TOKEN:
        raise ValueError("DEVELOPER_BOT_TOKEN غير محدد. أضفه كـ Secret في الإعدادات.")

    app = Application.builder().token(DEVELOPER_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",  start_cmd))
    app.add_handler(CommandHandler("panel",  panel_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    app.add_handler(CallbackQueryHandler(callback_handler, pattern="^dv_"))

    app.add_handler(MessageHandler(filters.Document.ALL, document_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    return app


def main():
    system_logger.info("=== DEVELOPER BOT STARTUP === PID:%s", os.getpid())
    app = build_application()
    system_logger.info("Developer Bot polling started.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
