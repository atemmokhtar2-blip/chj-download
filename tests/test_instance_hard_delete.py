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
