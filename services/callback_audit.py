from __future__ import annotations

import ast
from pathlib import Path

KNOWN_LITERAL_CALLBACKS = {
    "noop",
    "lang_en", "lang_ar",
    "settings_lang",
    "dl_unavailable", "dl_smart", "dl_video_best", "dl_audio", "dl_image", "dl_album",
    "admin_panel", "admin_users", "admin_broadcast", "admin_announce_update",
    "admin_search", "admin_ban", "admin_engine_status", "admin_update_ytdlp",
    "admin_maintenance", "admin_send_broadcast", "admin_maintenance_on", "admin_maintenance_off",
}

KNOWN_DYNAMIC_PREFIXES = (
    "admin_user_detail_",
    "admin_users_page_",
    "admin_unban_",
    "admin_ban_confirm_",
    "dl_video_",
)

ROUTED_PREFIXES = ("noop", "admin_", "lang_", "settings_", "dl_")


def _literal_from_node(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dynamic_prefix_from_joined_str(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            break
    return "".join(parts)


def audit_callbacks(root: str | Path = ".") -> dict:
    """Static audit for inline callback_data values used by Telegram keyboards."""
    base = Path(root)
    callback_entries: list[dict] = []
    unknown_literals: list[dict] = []
    unknown_dynamic: list[dict] = []
    unrouted_literals: list[dict] = []

    for source in sorted((base / "handlers").glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "callback_data":
                    continue
                literal = _literal_from_node(kw.value)
                if literal is not None:
                    entry = {"file": str(source), "line": node.lineno, "value": literal, "kind": "literal"}
                    callback_entries.append(entry)
                    if literal not in KNOWN_LITERAL_CALLBACKS:
                        unknown_literals.append(entry)
                    if not any(literal == p or literal.startswith(p) for p in ROUTED_PREFIXES):
                        unrouted_literals.append(entry)
                    continue
                if isinstance(kw.value, ast.JoinedStr):
                    prefix = _dynamic_prefix_from_joined_str(kw.value)
                    entry = {"file": str(source), "line": node.lineno, "value": prefix + "*", "kind": "dynamic"}
                    callback_entries.append(entry)
                    if not any(prefix.startswith(known) for known in KNOWN_DYNAMIC_PREFIXES):
                        unknown_dynamic.append(entry)
                    continue
                entry = {"file": str(source), "line": node.lineno, "value": ast.unparse(kw.value), "kind": "unknown_expr"}
                callback_entries.append(entry)
                unknown_dynamic.append(entry)

    return {
        "ok": not unknown_literals and not unknown_dynamic and not unrouted_literals,
        "total": len(callback_entries),
        "literal_count": sum(1 for e in callback_entries if e["kind"] == "literal"),
        "dynamic_count": sum(1 for e in callback_entries if e["kind"] == "dynamic"),
        "unknown_literals": unknown_literals,
        "unknown_dynamic": unknown_dynamic,
        "unrouted_literals": unrouted_literals,
    }


def render_callback_audit_html(root: str | Path = ".") -> str:
    report = audit_callbacks(root)
    status = "✅ OK" if report["ok"] else "⚠️ NEEDS REVIEW"
    lines = [
        "🧭 <b>Callback Routing Audit</b>",
        "",
        f"Status: <b>{status}</b>",
        f"Total buttons: <b>{report['total']}</b>",
        f"Literal callbacks: <b>{report['literal_count']}</b>",
        f"Dynamic callbacks: <b>{report['dynamic_count']}</b>",
        "",
    ]
    problems = report["unknown_literals"] + report["unknown_dynamic"] + report["unrouted_literals"]
    if not problems:
        lines.append("كل callback_data الموجودة في الكود لها route معروف أو prefix محمي.")
    else:
        lines.append("<b>Problems:</b>")
        for item in problems[:20]:
            lines.append(f"• <code>{item['value']}</code> — {item['file']}:{item['line']}")
        if len(problems) > 20:
            lines.append(f"… and {len(problems) - 20} more")
    return "\n".join(lines)
