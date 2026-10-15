"""Regression tests for wiring and schema upgrades of redemption codes."""

import ast
from contextlib import contextmanager
from pathlib import Path

from database import pg


ROOT = Path(__file__).resolve().parents[1]


def test_bot_imports_code_handler_used_by_message_router():
    tree = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "handlers.referral"
        and any(alias.name == "try_handle_code_message" for alias in node.names)
        for node in ast.walk(tree)
    )
    router_calls_handler = any(
        isinstance(node, ast.AsyncFunctionDef)
        and node.name == "message_router"
        and any(
            isinstance(child, ast.Name)
            and child.id == "try_handle_code_message"
            and isinstance(child.ctx, ast.Load)
            for child in ast.walk(node)
        )
        for node in ast.walk(tree)
    )

    assert imported, "bot.py must import try_handle_code_message"
    assert router_calls_handler, "message_router must route awaiting-code messages"


def test_referral_schema_migrates_legacy_redemption_codes(monkeypatch):
    executed = []

    class FakeCursor:
        def execute(self, sql, params=()):
            executed.append(sql)
            return self

    @contextmanager
    def fake_ref_cursor(commit=True):
        yield FakeCursor()

    monkeypatch.setattr(pg, "ref_cursor", fake_ref_cursor)
    monkeypatch.setattr(pg, "execute", lambda sql, params=(): executed.append(sql) or 1)
    monkeypatch.setattr(pg, "fetchone", lambda sql, params=(): None)

    pg.init_referral_schema()

    assert any(
        "CREATE TABLE IF NOT EXISTS redemption_codes" in sql
        and "targeted_user_id" in sql
        for sql in executed
    )
    assert any(
        sql == "ALTER TABLE redemption_codes ADD COLUMN IF NOT EXISTS targeted_user_id BIGINT"
        for sql in executed
    )
