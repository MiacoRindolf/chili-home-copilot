"""Regression: the replay harness clean-sink reset must survive append-only TRUNCATE guards.

Replay is the only merge gate this project has for behavioural change, and the
2026-08-29 sink-contamination incident is why ``_reset_sim_sink`` truncates before every
run. On origin/main that reset issues ``TRUNCATE ... RESTART IDENTITY CASCADE`` over
``adaptive_risk_reservations``; the CASCADE reaches ``adaptive_risk_reservation_events``,
whose BEFORE TRUNCATE statement trigger (installed by migration 354, app/migrations.py)
raises ERRCODE 55000 — so against a migrated database the harness aborted before loading
a single tick, pushing operators toward ``REPLAY_KEEP_SINK=1``, which is the
contamination shortcut itself.

The headline test below touches only ``_reset_sim_sink`` — the catalog SQL it needs is
duplicated here on purpose — so on origin/main it fails with the real 55000 rather than
with an AttributeError about symbols the fix introduced.

These tests must run against a MIGRATED ``*_test`` database so the guards are really
present; ``test_sink_closure_actually_carries_truncate_guards`` fails loudly rather than
passing vacuously if they are not.

    pytest tests/test_replay_sink_reset_truncate_guards.py -v -p no:randomly
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

_HARNESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "replay_v3_fsm_window.py"

# The six relations the replay writes — kept independent of the harness module so the
# regression test can run against a build that does not export them.
_SEED_TABLES = (
    "trading_automation_events", "trading_automation_sessions",
    "trading_automation_simulated_fills", "momentum_symbol_viability",
    "adaptive_risk_reservations", "adaptive_risk_opportunity_claims",
)

_CLOSURE_SQL = """
WITH RECURSIVE reached(oid) AS (
        SELECT c.oid
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
          AND c.relname = ANY(:seeds)
    UNION
        SELECT con.conrelid
        FROM pg_constraint con
        JOIN reached r ON con.confrelid = r.oid
        WHERE con.contype = 'f'
)
SELECT c.oid, n.nspname, c.relname
FROM reached r
JOIN pg_class c ON c.oid = r.oid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
ORDER BY n.nspname, c.relname
"""

_GUARD_SQL = """
SELECT t.tgrelid, t.tgname, t.tgenabled
FROM pg_trigger t
WHERE NOT t.tgisinternal AND (t.tgtype & 32) <> 0 AND t.tgrelid = ANY(:oids)
ORDER BY t.tgrelid, t.tgname
"""

# A sink-seed row with no FK parents and no triggers of its own: ``status='available'``
# is the branch of ck_adaptive_risk_opportunity_owner that needs neither reservation id.
_SEEDED_TABLE = "adaptive_risk_opportunity_claims"
_SEED_CLAIM_SQL = (
    "INSERT INTO adaptive_risk_opportunity_claims "
    "(account_scope, symbol, trading_date, setup_family, status, event_sequence, version) "
    "VALUES ('sink_reset_regression', 'ZZZZ', DATE '2026-09-03', 'regression', "
    "'available', 0, 1)"
)
# Installed by _install_migration_354_owner_event_guards on the hash-chained reservation
# ledger. This is the guard the harness used to crash into.
_LEDGER_GUARD = "trg_adaptive_risk_reservation_events_no_truncate"


@pytest.fixture(scope="module")
def harness():
    """Import scripts/replay_v3_fsm_window.py (not a package) by path, once."""
    spec = importlib.util.spec_from_file_location(
        "_replay_v3_fsm_window_under_test", _HARNESS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _closure(db):
    """The relations TRUNCATE ... CASCADE reaches from the sink seeds."""
    rows = db.execute(text(_CLOSURE_SQL), {"seeds": list(_SEED_TABLES)}).all()
    return [(int(oid), ns, rel) for (oid, ns, rel) in rows]


def _truncate_guards(db, closure):
    rows = db.execute(text(_GUARD_SQL), {"oids": [oid for (oid, _n, _r) in closure]}).all()
    return [(int(oid), tg, en) for (oid, tg, en) in rows]


def _seed_and_release(db):
    """Seed one sink row and COMMIT, so the reset's connection can take ACCESS EXCLUSIVE."""
    db.execute(text(_SEED_CLAIM_SQL))
    db.commit()


def test_sink_closure_actually_carries_truncate_guards(db):
    """Guard the guard: if the bed is unmigrated, every other test here is vacuous."""
    closure = _closure(db)
    assert closure, "sink closure is empty — TEST_DATABASE_URL is not a migrated database"
    names = {rel for (_oid, _ns, rel) in closure}
    assert set(_SEED_TABLES) <= names
    # CASCADE reaches well past the six seeds — that reach is the whole defect.
    assert len(closure) > len(_SEED_TABLES)

    guarded = {tg for (_oid, tg, _en) in _truncate_guards(db, closure)}
    assert _LEDGER_GUARD in guarded, (
        f"{_LEDGER_GUARD} is absent — migration 354 has not run against this database, "
        "so this file cannot prove anything. Point TEST_DATABASE_URL at a migrated DB."
    )


def test_reset_sim_sink_truncates_through_append_only_guards(db, harness):
    """The regression itself: on origin/main this raises ERRCODE 55000 before any tape loads."""
    _seed_and_release(db)
    assert db.execute(text(f"SELECT count(*) FROM {_SEEDED_TABLE}")).scalar_one() == 1
    db.commit()

    harness._reset_sim_sink()

    # CONTRACT: every relation the reset covers is genuinely empty, verified by count.
    closure = _closure(db)
    residue = {
        rel: db.execute(text(f'SELECT count(*) FROM "{ns}"."{rel}"')).scalar_one()
        for (_oid, ns, rel) in closure
    }
    assert not {k: v for k, v in residue.items() if v}, f"sink not empty after reset: {residue}"

    # CONTRACT: the append-only guards are back on — the suspension is not permanent.
    still_off = [tg for (_oid, tg, en) in _truncate_guards(db, closure) if en == "D"]
    assert not still_off, f"TRUNCATE guards left disabled after reset: {still_off}"


def test_reset_sim_sink_fails_loudly_on_a_partial_clean(db, harness, monkeypatch):
    """A silent partial clean is worse than a hard failure: contaminated results look real."""
    _seed_and_release(db)
    closure = _closure(db)
    # Simulate a reset that misses one table. Truncating a child never cascades UP to its
    # parent, so holding the seeded parent back really does leave its row behind.
    kept = [t for t in closure if t[2] != _SEEDED_TABLE]
    assert len(kept) == len(closure) - 1
    monkeypatch.setattr(harness, "_sink_truncate_targets", lambda _c: kept)

    with pytest.raises(SystemExit) as excinfo:
        harness._reset_sim_sink()

    message = str(excinfo.value)
    assert "NOT EMPTY after reset" in message
    assert f"{_SEEDED_TABLE}=1" in message

    # It aborted, but it still restored the guards it suspended.
    still_off = [tg for (_oid, tg, en) in _truncate_guards(db, closure) if en == "D"]
    assert not still_off, f"TRUNCATE guards left disabled after a loud abort: {still_off}"


def test_guards_are_restored_when_the_reset_transaction_fails(db, harness, monkeypatch):
    """Crash safety: DDL is transactional, so a mid-reset failure must roll the suspension back."""
    closure = _closure(db)
    monkeypatch.setattr(
        harness,
        "_sink_truncate_targets",
        lambda c: list(c) + [(0, "public", "chili_no_such_sink_relation")],
    )

    with pytest.raises(Exception):
        harness._reset_sim_sink()

    still_off = [tg for (_oid, tg, en) in _truncate_guards(db, closure) if en == "D"]
    assert not still_off, (
        f"TRUNCATE guards left DISABLED after a failed reset: {still_off} — the append-only "
        "guard must never survive the transaction in a disabled state."
    )
