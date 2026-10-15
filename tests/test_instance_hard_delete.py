"""Hard-delete contract tests — owner panel "حذف النسخة تمامًا".

Deleting a clone permanently must:
  1. erase EVERY row scoped to that instance (downloads, failures,
     broadcasts, admin roles/bans/logs, settings, encrypted token,
     activation + redemption logs, the instance row itself);
  2. leave a DIFFERENT instance's data completely untouched;
  3. keep entitlement rows (clone_trials / referral_rewards) alive with a
     NULL instance pointer — the consumption record survives (no free
     second trial, no re-claimable reward) without a dangling reference;
  4. be idempotent — a second call is a safe no-op.
"""

from __future__ import annotations


def test_hard_delete_erases_target_instance_only(isolated_db):
    from database import pg
    from services import referral

    # isolated_db ran init_db + ensure_plan_schema; the clone core tables
    # (bot_instances / bot_secrets / clone_settings / activation_log /
    # clone_trials / referral_rewards) come from init_referral_schema —
    # idempotent CREATE IF NOT EXISTS, same boot order as bot.py.
    pg.init_referral_schema()

    # ---- fixture data: two clones + one trial user + one reward owner ----
    pg.execute(
        "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id) DO NOTHING", (6101, "victim_user", "V"))
    pg.execute(
        "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id) DO NOTHING", (6102, "other_user", "O"))

    inst_a = pg.insert_returning_id(
        "INSERT INTO bot_instances (owner_id, bot_id, bot_username, status, plan, "
        "activated_at, expires_at) VALUES (?, ?, 'doomed_clone_bot', 'active', 'pro', "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '30 days')", (6001, 880011))
    inst_b = pg.insert_returning_id(
        "INSERT INTO bot_instances (owner_id, bot_id, bot_username, status, plan, "
        "activated_at, expires_at) VALUES (?, ?, 'survivor_clone_bot', 'active', 'basic', "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '30 days')", (6002, 880022))

    for iid, tok in ((inst_a, "enc:tok_a_hash_a"), (inst_b, "enc:tok_b_hash_b")):
        pg.execute(
            "INSERT INTO bot_secrets (instance_id, token_encrypted, token_hash) "
            "VALUES (?, ?, ?)", (iid, tok, f"hash_{iid}"))
        pg.execute("INSERT INTO clone_settings (instance_id, key, value) VALUES (?, 'k', 'v')",
                   (iid,))
        pg.execute("INSERT INTO activation_log (owner_id, instance_id, action) VALUES (?, ?, 'x')",
                   (6001, iid))
        pg.execute("INSERT INTO clone_admins (instance_id, user_id) VALUES (?, ?)", (iid, 6201))
        pg.execute("INSERT INTO clone_bans (instance_id, user_id) VALUES (?, ?)", (iid, 6301))
        pg.execute("INSERT INTO clone_admin_log (instance_id, actor_id, action) VALUES (?, ?, 'a')",
                   (iid, 6201))
        pg.execute("INSERT INTO clone_broadcasts (instance_id, actor_id) VALUES (?, ?)",
                   (iid, 6201))
        pg.execute("INSERT INTO download_failures (instance_id, user_id, platform) VALUES (?, ?, 'yt')",
                   (iid, 6101))
        pg.execute(
            "INSERT INTO downloads (user_id, url, title, platform, quality, media_type, "
            "file_size, instance_id) VALUES (?, ?, 't', 'youtube', '720p', 'video', 10, ?)",
            (6101, f"https://example.com/{iid}", iid))
        pg.execute("INSERT INTO redemption_log (code_id, action, instance_id) VALUES (1, 'used', ?)",
                   (iid,))

    # Entitlement rows: a consumed trial + a claimed reward, both pointing
    # at the doomed instance.
    pg.execute("INSERT INTO clone_trials (user_id, instance_id, plan) VALUES (?, ?, 'basic')",
               (6101, inst_a))
    pg.execute(
        "INSERT INTO referral_rewards (user_id, status, instance_id) VALUES (?, 'claimed', ?)",
        (6101, inst_a))

    # ---- act ----
    counts = referral.delete_instance_hard(inst_a)

    # ---- 1) target instance fully erased ----
    assert counts["bot_instances"] == 1
    assert referral.get_instance(inst_a) is None
    for table in ("bot_secrets", "clone_settings", "activation_log", "clone_admins",
                  "clone_bans", "clone_admin_log", "clone_broadcasts",
                  "download_failures", "downloads", "redemption_log"):
        row = pg.fetchone(f"SELECT COUNT(*) AS c FROM {table} WHERE instance_id = ?",
                          (inst_a,))
        assert int(row["c"]) == 0, f"{table} still holds rows of the deleted instance"

    # ---- 2) the other instance is untouched, row for row ----
    assert referral.get_instance(inst_b) is not None
    for table, expected in (("bot_secrets", 1), ("clone_settings", 1), ("activation_log", 1),
                            ("clone_admins", 1), ("clone_bans", 1), ("clone_admin_log", 1),
                            ("clone_broadcasts", 1), ("download_failures", 1),
                            ("downloads", 1), ("redemption_log", 1)):
        row = pg.fetchone(f"SELECT COUNT(*) AS c FROM {table} WHERE instance_id = ?",
                          (inst_b,))
        assert int(row["c"]) == expected, f"{table} lost rows of the surviving instance"

    # ---- 3) entitlement rows survive with NULL pointers ----
    trial = pg.fetchone("SELECT instance_id, plan FROM clone_trials WHERE user_id = ?", (6101,))
    assert trial is not None and trial["plan"] == "basic"
    assert trial["instance_id"] is None
    reward = pg.fetchone("SELECT status, instance_id FROM referral_rewards WHERE user_id = ?",
                         (6101,))
    assert reward is not None and reward["status"] == "claimed"
    assert reward["instance_id"] is None

    # ---- 4) idempotent: second call is a safe no-op ----
    again = referral.delete_instance_hard(inst_a)
    assert again["bot_instances"] == 0
    assert referral.get_instance(inst_b) is not None


# ---- panel wiring: the confirm → execute flow (button-level contract) -----------------

from pathlib import Path
from types import SimpleNamespace

_OWNER_PANEL = Path(__file__).resolve().parents[1] / "handlers" / "owner_panel.py"


def _panel_src() -> str:
    return _OWNER_PANEL.read_text(encoding="utf-8")


def test_dispatcher_routes_delete_before_generic_detail():
    """`own_inst_del_*` and `own_inst_deldo_*` MUST be dispatched to their own
    handlers BEFORE the generic `own_inst_` catch-all — otherwise the press is
    swallowed and the detail merely re-renders (the old broken behavior)."""
    src = _panel_src()
    i_deldo = src.index('elif data.startswith("own_inst_deldo_")')
    i_del = src.index('elif data.startswith("own_inst_del_")')
    i_generic = src.index('elif data.startswith("own_inst_"):')
    assert i_deldo < i_generic and i_del < i_generic
    assert "_inst_del_do(context, query, inst_id)" in src[i_deldo:i_del]
    assert "_inst_del_confirm(query, inst_id)" in src[i_del:i_generic]


def test_delete_execution_is_permanent_and_audited():
    """_inst_del_do: process detach → DB wipe (delete_instance_hard is the ONLY
    erase path) → PERMANENT admin-audit entry → clone-owner notification."""
    src = _panel_src()
    start = src.index("async def _inst_del_do(")
    end = src.index("# ---- downloads section", start)
    seg = src[start:end]
    assert "await stop_instance(inst_id)" in seg
    assert "referral.delete_instance_hard(inst_id)" in seg
    assert 'log_admin_action(query.from_user.id, "owner_delete_instance", inst_id' in seg
    assert "context.bot.send_message" in seg


class _FakeQuery:
    def __init__(self, uid: int = 9001):
        self.from_user = SimpleNamespace(id=uid)
        self.edits: list = []

    async def edit_message_text(self, text=None, **kwargs):
        self.edits.append((text or "", kwargs))
        return SimpleNamespace(message_id=1, chat_id=1)


def _cbs(markup) -> list[str]:
    out = []
    for row in getattr(markup, "inline_keyboard", []) or []:
        for btn in row:
            if getattr(btn, "callback_data", None):
                out.append(btn.callback_data)
    return out


_INST = {"id": 5, "owner_id": 6001, "bot_id": 880011, "bot_username": "doomed_clone_bot",
         "status": "active", "plan": "pro", "last_error": None,
         "activated_at": "2026-01-01 00:00:00", "expires_at": "2026-02-01 00:00:00"}


def test_detail_offers_hard_delete_for_every_status(monkeypatch):
    """The 🗑️ button lives on the clone detail for EVERY status — an expired
    or disabled clone is exactly the one an owner wants gone for good."""
    import asyncio
    import handlers.owner_panel as op
    monkeypatch.setattr(op.referral, "instance_days_left", lambda inst: 10)
    monkeypatch.setattr(op.referral, "clone_plan_days", lambda plan: 30)
    for status in ("active", "expired", "disabled", "error"):
        monkeypatch.setattr(op.referral, "get_instance",
                            lambda iid, s=status: dict(_INST, status=s))
        q = _FakeQuery()
        asyncio.run(op._inst_detail(q, 5))
        _text, kwargs = q.edits[-1]
        markup = kwargs.get("reply_markup")
        assert "own_inst_del_5" in _cbs(markup), status
        labels = [btn.text or "" for row in getattr(markup, "inline_keyboard", [])
                  for btn in row]
        assert any("حذف النسخة تمامًا" in label for label in labels), status


def test_del_confirm_shows_blast_radius_and_wires_do(monkeypatch):
    import asyncio
    import handlers.owner_panel as op
    monkeypatch.setattr(op.referral, "get_instance", lambda iid: dict(_INST))
    q = _FakeQuery()
    asyncio.run(op._inst_del_confirm(q, 5))
    text, kwargs = q.edits[-1]
    assert "doomed_clone_bot" in text          # identity shown before execution
    assert "لا يمكن التراجع" in text            # permanence warning
    cbs = _cbs(kwargs.get("reply_markup"))
    assert "own_inst_deldo_5" in cbs           # confirm → execute
    assert "own_inst_5" in cbs                 # تراجع → back to detail


def test_del_do_wipes_audits_and_notifies(monkeypatch):
    import asyncio
    import handlers.owner_panel as op

    wiped: list = []
    stopped: list = []
    audits: list = []
    notified: list = []

    async def _fake_stop(inst_id):
        stopped.append(inst_id)

    async def _fake_send(chat_id, text, **kwargs):
        notified.append((chat_id, text))

    monkeypatch.setattr(op.referral, "get_instance", lambda iid: dict(_INST))
    monkeypatch.setattr(op.referral, "delete_instance_hard",
                        lambda iid: wiped.append(iid) or {
                            "bot_instances": 1, "downloads": 3, "download_failures": 0,
                            "clone_admins": 2, "clone_bans": 1, "clone_admin_log": 4,
                            "clone_broadcasts": 0, "clone_settings": 1, "bot_secrets": 1,
                            "activation_log": 2, "redemption_log": 0})
    monkeypatch.setattr(op, "stop_instance", _fake_stop)
    monkeypatch.setattr(op, "log_admin_action", lambda *a, **k: audits.append(a))

    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=_fake_send))
    q = _FakeQuery(uid=555000)
    asyncio.run(op._inst_del_do(ctx, q, 5))

    assert wiped == [5]                        # the ONLY erase path, right id
    assert stopped == [5]                      # process detached first
    assert any(a[1] == "owner_delete_instance" and a[2] == 5 for a in audits)
    assert notified and notified[0][0] == 6001 and "حذف نسختك" in notified[0][1]
    text, _kw = q.edits[-1]
    assert "تم حذف النسخة #5 نهائيًا" in text
    assert "الحذف اكتمل" in text               # bot_instances == 1 → success copy


def test_del_confirm_and_do_reject_missing_instance(monkeypatch):
    """A stale callback for an already-deleted clone must NOT wipe anything."""
    import asyncio
    import handlers.owner_panel as op
    wiped: list = []
    monkeypatch.setattr(op.referral, "get_instance", lambda iid: None)
    monkeypatch.setattr(op.referral, "delete_instance_hard", lambda iid: wiped.append(iid) or {})

    q = _FakeQuery()
    asyncio.run(op._inst_del_confirm(q, 999))
    assert "غير موجودة" in q.edits[-1][0]

    q2 = _FakeQuery()
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=None))
    asyncio.run(op._inst_del_do(ctx, q2, 999))
    assert "غير موجودة" in q2.edits[-1][0]
    assert wiped == []
