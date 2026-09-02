"""#1285 — WHERE-bound na Alpaca reservation-ledger scan (sinukat 2026-09-02).

Ang `_reserve_alpaca_entry_risk` at `_certify_alpaca_owned_entry_posture` ay
humihila ng 7,895 live alpaca row / 53 MB jsonb (2,119 / 2,591 / 2,806 ms sa
tahimik na DB) sa BAWAT placement habang hawak ang account advisory lock.
7,509 ay terminal, 0 ang may posisyon. Tatlong bantay dito:

(a) AST/source guard — parehong seam ay gumagamit ng IISANG helper, at ang
    helper SQL ay may state filter + bawat exposure marker na binabasa ng loop.
(b) Superset oracle — sintetikong hilera; bawat hilerang ginagalaw ng loop ay
    PASOK sa Python mirror AT sa tunay na SQL sa Postgres (parehong id set).
(c) Migration 374 — dalawang beses tumakbo, umiiral ang index, walang error.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.migrations import (
    MIGRATIONS,
    _assert_migration_ids_unique,
    _migration_364_active_sessions_partial_index,
    _migration_374_alpaca_ledger_scan_bound_indexes,
)
from app.services.trading.momentum_neural import alpaca_orphan_claims as claims
from app.services.trading.momentum_neural.live_fsm import (
    LIVE_RUNNER_TERMINAL_STATES,
    STATE_LIVE_PENDING_ENTRY,
)


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _function_node(name: str) -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(claims))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in alpaca_orphan_claims")


def _called_names(fn: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _string_constants(fn: ast.FunctionDef) -> list[str]:
    return [
        node.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _live_keys_read(fn: ast.FunctionDef) -> set[str]:
    """Bawat literal key na binabasa mula sa `live` dict sa loob ng function."""
    keys: set[str] = set()
    for node in ast.walk(fn):
        # live.get("key") / live.get("key", default)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "live"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
        # "key" in live
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Name)
            and node.comparators[0].id == "live"
            and any(isinstance(op, ast.In) for op in node.ops)
        ):
            keys.add(node.left.value)
    return keys


# ── (a) source / AST guard ──────────────────────────────────────────────────


def test_both_seams_scan_sessions_only_through_the_shared_helper():
    for name in ("_reserve_alpaca_entry_risk", "_certify_alpaca_owned_entry_posture"):
        fn = _function_node(name)
        called = _called_names(fn)
        assert "alpaca_ledger_session_scan_sql" in called, name
        assert "alpaca_ledger_session_scan_params" in called, name
        # Walang inline na session scan na natitira sa alinmang seam.
        inline = [
            s for s in _string_constants(fn)
            if "trading_automation_sessions" in s
        ]
        assert inline == [], (name, inline)


def test_helper_sql_has_state_filter_owner_claim_arms_and_every_marker():
    sql = claims.alpaca_ledger_session_scan_sql()
    flat = _norm(sql)
    assert "FROM trading_automation_sessions" in flat
    assert "mode = 'live'" in flat
    assert "execution_family IN ('alpaca_spot', 'alpaca_short')" in flat
    assert "id = :owner_session_id" in flat
    assert "id = ANY(CAST(:claim_owner_session_ids AS INTEGER[]))" in flat
    terminal = ", ".join(f"'{s}'" for s in claims.ALPACA_LEDGER_TERMINAL_STATES)
    assert f"state NOT IN ({terminal})" in flat
    expr = claims.alpaca_ledger_exposure_sql_expr()
    assert f"OR {expr} IS NOT NULL" in flat
    for key in claims.ALPACA_LEDGER_EXPOSURE_MARKERS:
        assert (
            f"risk_snapshot_json->'momentum_live_execution'->>'{key}'" in expr
        ), key
    # Ang bawat marker ay lumalabas nang EKSAKTONG isang beses sa coalesce.
    assert expr.count("->>'") == len(claims.ALPACA_LEDGER_EXPOSURE_MARKERS)


def test_terminal_list_is_a_superset_of_fsm_terminal_and_matches_mig_364():
    fsm_terminal = set(LIVE_RUNNER_TERMINAL_STATES)
    assert fsm_terminal <= set(claims.ALPACA_LEDGER_TERMINAL_STATES)
    assert "live_arm_expired" in claims.ALPACA_LEDGER_TERMINAL_STATES
    assert STATE_LIVE_PENDING_ENTRY not in claims.ALPACA_LEDGER_TERMINAL_STATES
    # Byte-identical sa partial predicate ng ix_tas_live_active (mig 364) para
    # manatiling implied ang `state NOT IN` arm at magamit ang index sa BitmapOr.
    mig364 = _norm(inspect.getsource(_migration_364_active_sessions_partial_index))
    for state in claims.ALPACA_LEDGER_TERMINAL_STATES:
        assert f"'{state}'" in mig364, state
    assert len(re.findall(r"'[a-z_]+'", mig364.split("WHERE state NOT IN", 1)[1])) == len(
        claims.ALPACA_LEDGER_TERMINAL_STATES
    )


def test_every_live_key_the_loops_read_is_an_exposure_marker():
    markers = set(claims.ALPACA_LEDGER_EXPOSURE_MARKERS)
    read: set[str] = set()
    for name in (
        "_reserve_alpaca_entry_risk",
        "_certify_alpaca_owned_entry_posture",
        "_adaptive_atomic_ledger_from_rows",
    ):
        read |= _live_keys_read(_function_node(name))
    # Positibo sa magkabilang panig: nabasa nga ang mga susi na inaasahan…
    assert {"position", "entry_submitted", "deadman_stop", "entry_client_order_id"} <= read
    # …at WALA ni isang nabasang susi na wala sa marker list.
    assert read <= markers, read - markers
    assert set(claims.ALPACA_LEDGER_ACTIVE_ORDER_KEYS) <= markers
    certify = _function_node("_certify_alpaca_owned_entry_posture")
    assert "ALPACA_LEDGER_ACTIVE_ORDER_KEYS" in {
        n.id for n in ast.walk(certify) if isinstance(n, ast.Name)
    }


def test_python_mirror_reads_the_same_constants_as_the_sql_builder():
    mirror = _function_node("alpaca_ledger_row_may_carry_exposure")
    builder = _function_node("alpaca_ledger_session_scan_sql")
    expr_fn = _function_node("alpaca_ledger_exposure_sql_expr")
    names = lambda fn: {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}  # noqa: E731
    assert {"ALPACA_LEDGER_TERMINAL_STATES", "ALPACA_LEDGER_EXPOSURE_MARKERS"} <= names(mirror)
    assert "ALPACA_LEDGER_TERMINAL_STATES" in names(builder)
    assert "ALPACA_LEDGER_EXPOSURE_MARKERS" in names(expr_fn)


def test_migration_374_index_expression_is_identical_to_the_query_expression():
    source = _norm(inspect.getsource(_migration_374_alpaca_ledger_scan_bound_indexes))
    assert claims.alpaca_ledger_exposure_sql_expr() in source
    assert "ix_tas_alpaca_ledger_exposure" in source
    assert "ON trading_automation_sessions ( mode, execution_family, (coalesce(" in source
    assert "ix_tas_live_family_state" in source
    assert "(mode, execution_family, state)" in source
    # Non-partial ang expression index: ang partial-index stats ay hindi
    # ginagamit ng planner (sinukat: 3,970 ms partial vs 1.24 ms non-partial).
    assert "WHERE mode" not in source.split("ix_tas_alpaca_ledger_exposure", 1)[1]


# ── (b) superset oracle ─────────────────────────────────────────────────────

OWNER = 985001
CLAIM_OWNER = 985002
_BASE = 985000


def _snap(live: dict | None, **extra) -> dict:
    snap = {"alpaca_account_scope": "alpaca:paper", "alpaca_account_id": "acct-1"}
    if live is not None:
        snap["momentum_live_execution"] = live
    snap.update(extra)
    return snap


def _synthetic_rows() -> list[tuple[int, str, str, str, dict, bool]]:
    """(id, symbol, family, state, snapshot, loop_acts_on_it)."""
    pos = {"quantity": 3, "side": "long"}
    return [
        # terminal + position: reserve raises/blocks on it regardless of state
        (_BASE + 10, "AAA", "alpaca_spot", "live_cancelled", _snap({"position": pos}), True),
        # terminal + entry_submitted: certify's generation-mismatch check reads it
        (_BASE + 11, "BBB", "alpaca_spot", "live_error", _snap({"entry_submitted": True}), True),
        # terminal + entry_order_id: certify allowed_order_ids
        (_BASE + 12, "CCC", "alpaca_spot", "live_arm_expired", _snap({"entry_order_id": "o-12"}), True),
        # terminal + deadman stop ids
        (_BASE + 13, "DDD", "alpaca_spot", "live_finished",
         _snap({"deadman_stop": {"order_id": "o-13", "client_order_id": "c-13"}}), True),
        # terminal + scale-out order id
        (_BASE + 14, "EEE", "alpaca_spot", "live_cancelled", _snap({"scale_out_order_id": "o-14"}), True),
        # terminal + pyramid add order id
        (_BASE + 15, "FFF", "alpaca_short", "cancelled", _snap({"pyramid_order_id": "o-15"}), True),
        # terminal + entry_submitted=false (falsy) — SQL admits (superset), loop skips
        (_BASE + 16, "GGG", "alpaca_spot", "live_cancelled", _snap({"entry_submitted": False}), False),
        # terminal clean (empty live dict) — the 7,509-row bulk; must be EXCLUDED
        (_BASE + 17, "HHH", "alpaca_spot", "live_cancelled", _snap({}), False),
        # terminal clean with JSON nulls only — `->>` is SQL NULL; must be EXCLUDED
        (_BASE + 18, "III", "alpaca_spot", "live_error",
         _snap({"position": None, "entry_order_id": None}), False),
        # terminal, momentum_live_execution missing entirely
        (_BASE + 19, "JJJ", "alpaca_spot", "live_arm_expired", _snap(None), False),
        # terminal, momentum_live_execution is a list (unreadable shape) — loop treats as {}
        (_BASE + 20, "KKK", "alpaca_spot", "live_cancelled", _snap([1, 2]), False),
        # non-terminal clean: pending entry without submit
        (_BASE + 21, "LLL", "alpaca_spot", "live_pending_entry", _snap({}), True),
        # non-terminal with entry_submitted: legacy pending path
        (_BASE + 22, "MMM", "alpaca_spot", "live_pending_entry",
         _snap({"entry_submitted": True, "entry_client_order_id": "c-22",
                "entry_inflight_risk_usd": 12.5}), True),
        # non-terminal watching
        (_BASE + 23, "NNN", "alpaca_spot", "watching_live", _snap({}), True),
        # owner row: terminal + clean — reserve needs exactly one owner row
        (OWNER, "OOO", "alpaca_spot", "live_cancelled", _snap({}), True),
        # claim owner row: terminal + clean — rows_by_id lookup else claim_owner_missing
        (CLAIM_OWNER, "PPP", "alpaca_spot", "live_error", _snap({}), True),
        # position on a non-dict value: loop raises unreadable ⇒ must be admitted
        (_BASE + 24, "QQQ", "alpaca_spot", "live_cancelled", _snap({"position": "bogus"}), True),
        # position quarantined by mig 362 — no longer a marker; clean terminal
        (_BASE + 25, "RRR", "alpaca_spot", "live_cancelled",
         _snap({"position_quarantined_uncertified": pos}), False),
    ]


def test_python_mirror_admits_every_row_the_loops_act_on_and_excludes_clean_terminal():
    rows = _synthetic_rows()
    admitted = {
        sid
        for sid, sym, fam, state, snap, _acts in rows
        if claims.alpaca_ledger_row_may_carry_exposure(
            (sid, sym, fam, state, snap),
            owner_session_id=OWNER,
            claim_owner_session_ids=[CLAIM_OWNER, None],
        )
    }
    must = {sid for sid, *_rest, acts in rows if acts}
    assert must <= admitted, must - admitted
    # Ang bulk (terminal at walang marker) ay talagang nalalaktawan.
    for sid in (_BASE + 17, _BASE + 18, _BASE + 19, _BASE + 20, _BASE + 25):
        assert sid not in admitted, sid
    # Superset: ang falsy marker ay pumapasok (ang loop ang nagpapasya).
    assert _BASE + 16 in admitted
    # Non-alpaca o non-live ay hindi kailanman pumapasok.
    assert not claims.alpaca_ledger_row_may_carry_exposure(
        (_BASE + 10, "AAA", "coinbase_spot", "live_entered", _snap({"position": {}})),
        owner_session_id=None, claim_owner_session_ids=[],
    )
    assert not claims.alpaca_ledger_row_may_carry_exposure(
        {"mode": "paper", "id": _BASE + 10, "execution_family": "alpaca_spot",
         "state": "live_entered", "risk_snapshot_json": _snap({"position": {}})},
        owner_session_id=None, claim_owner_session_ids=[],
    )


def test_scan_params_normalise_owner_and_claim_ids():
    params = claims.alpaca_ledger_session_scan_params(
        owner_session_id="7", claim_owner_session_ids=[None, 3, "3", 9, 1],
    )
    assert params == {"owner_session_id": 7, "claim_owner_session_ids": [1, 3, 9]}
    params = claims.alpaca_ledger_session_scan_params(
        owner_session_id=None, claim_owner_session_ids=None,
    )
    assert params == {"owner_session_id": None, "claim_owner_session_ids": []}


def _seed_fk_rows(db) -> None:
    db.execute(text(
        "INSERT INTO users (id, name) VALUES (:id, 'scan-bound-1285') "
        "ON CONFLICT (id) DO NOTHING"
    ), {"id": _BASE})
    db.execute(text(
        "INSERT INTO momentum_strategy_variants "
        "(id, family, variant_key, version, label, params_json, is_active, "
        " execution_family, created_at, updated_at) "
        "VALUES (:id, 'momentum', 'scan-bound-1285', 1, 'scan-bound', '{}', false, "
        "        'alpaca_spot', now(), now()) "
        "ON CONFLICT (id) DO NOTHING"
    ), {"id": _BASE})


def _insert_rows(db, rows) -> None:
    db.execute(text(
        "DELETE FROM trading_automation_sessions WHERE id BETWEEN :lo AND :hi"
    ), {"lo": _BASE, "hi": _BASE + 999})
    started = datetime.now(timezone.utc) - timedelta(days=1)
    for sid, sym, fam, state, snap, _acts in rows:
        db.execute(text(
            "INSERT INTO trading_automation_sessions "
            "(id, user_id, venue, symbol, mode, execution_family, state, "
            " variant_id, started_at, created_at, updated_at, risk_snapshot_json) "
            "VALUES (:id, :uid, 'alpaca', :sym, 'live', :fam, :state, "
            "        :vid, :started, :started, :started, :snap)"
        ), {
            "id": sid, "uid": _BASE, "sym": sym, "fam": fam, "state": state,
            "vid": _BASE, "started": started, "snap": json.dumps(snap),
        })


def test_real_sql_and_python_mirror_agree_on_postgres(db):
    rows = _synthetic_rows()
    # Isang paper-mode alpaca row na may posisyon: hindi dapat lumabas sa live scan.
    _seed_fk_rows(db)
    _insert_rows(db, rows)
    db.execute(text(
        "INSERT INTO trading_automation_sessions "
        "(id, user_id, venue, symbol, mode, execution_family, state, "
        " variant_id, started_at, created_at, updated_at, risk_snapshot_json) "
        "VALUES (:id, :uid, 'alpaca', 'ZZZ', 'paper', 'alpaca_spot', 'live_entered', "
        "        :vid, now(), now(), now(), :snap)"
    ), {"id": _BASE + 900, "uid": _BASE, "vid": _BASE,
        "snap": json.dumps(_snap({"position": {"quantity": 1}}))})

    for owner, claim_ids in ((OWNER, [CLAIM_OWNER]), (None, []), (None, [CLAIM_OWNER, OWNER])):
        got = db.execute(
            text(claims.alpaca_ledger_session_scan_sql()),
            claims.alpaca_ledger_session_scan_params(
                owner_session_id=owner, claim_owner_session_ids=claim_ids,
            ),
        ).fetchall()
        got_ids = {int(r[0]) for r in got if _BASE <= int(r[0]) <= _BASE + 999}
        expected = {
            sid for sid, sym, fam, state, snap, _acts in rows
            if claims.alpaca_ledger_row_may_carry_exposure(
                (sid, sym, fam, state, snap),
                owner_session_id=owner, claim_owner_session_ids=claim_ids,
            )
        }
        assert got_ids == expected, (owner, claim_ids, got_ids ^ expected)
        must = {sid for sid, *_rest, acts in rows if acts}
        if owner is not None:
            assert must <= got_ids
        assert _BASE + 900 not in got_ids
        # Ang hilera ay umuuwi sa hugis na inaasahan ng loop (5 column, dict snapshot).
        sample = next(r for r in got if int(r[0]) == _BASE + 10)
        assert len(sample) == 5
        assert sample[1] == "AAA"
        assert isinstance(sample[4], dict)
        assert sample[4]["momentum_live_execution"]["position"]["quantity"] == 3
    db.rollback()


# ── (c) migration 374 ───────────────────────────────────────────────────────


def test_migration_374_registered_exactly_once_and_ids_unique():
    ids = [m[0] for m in MIGRATIONS]
    assert ids.count("374_alpaca_ledger_scan_bound_indexes") == 1
    assert ids.index("374_alpaca_ledger_scan_bound_indexes") > ids.index(
        "373_db_paper_account_identity"
    )
    assert dict(MIGRATIONS)["374_alpaca_ledger_scan_bound_indexes"] is (
        _migration_374_alpaca_ledger_scan_bound_indexes
    )
    _assert_migration_ids_unique()


def test_migration_374_is_idempotent_and_creates_both_indexes(db):
    conn = db.connection()
    _migration_374_alpaca_ledger_scan_bound_indexes(conn)
    _migration_374_alpaca_ledger_scan_bound_indexes(conn)
    defs = dict(db.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE tablename = 'trading_automation_sessions' "
        "AND indexname IN ('ix_tas_live_family_state', 'ix_tas_alpaca_ledger_exposure')"
    )).fetchall())
    assert set(defs) == {"ix_tas_live_family_state", "ix_tas_alpaca_ledger_exposure"}
    assert "(mode, execution_family, state)" in defs["ix_tas_live_family_state"]
    exposure = defs["ix_tas_alpaca_ledger_exposure"]
    assert "COALESCE" in exposure
    assert "WHERE" not in exposure
    for key in claims.ALPACA_LEDGER_EXPOSURE_MARKERS:
        assert f"'{key}'" in exposure, key
    # Ang query mismo ay tumatakbo laban sa na-index na schema (walang SQL error).
    db.execute(
        text(claims.alpaca_ledger_session_scan_sql()),
        claims.alpaca_ledger_session_scan_params(
            owner_session_id=1, claim_owner_session_ids=[],
        ),
    ).fetchall()
    db.rollback()
