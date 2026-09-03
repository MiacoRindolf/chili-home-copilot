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

WHAT THESE TESTS ARE BUILT TO CATCH, beyond "it does not crash":

* A reset that never suspends a guard at all would pass a suite whose guarded tables are
  all empty. So ``synthetic_guard`` installs a NON-EMPTY guarded table inside the cascade
  closure, raising under SQLSTATE **23514** — deliberately the other SQLSTATE the real
  guards use — which also proves nothing here matches on error shape.
* Emptiness must be proven for the tables the replay WRITES, not merely for the cascade
  closure: FK parents are structurally unreachable by CASCADE, so
  ``adaptive_risk_decision_packets`` gets its own case.
* The ``*_test`` fence must be a database identity, not a URL suffix.

These tests must run against a MIGRATED ``*_test`` database so the guards are really
present; ``test_sink_closure_actually_carries_truncate_guards`` fails loudly rather than
passing vacuously if they are not.

    pytest tests/test_replay_sink_reset_truncate_guards.py -v -p no:randomly
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import make_url

_HARNESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "replay_v3_fsm_window.py"

# The relations the replay writes — kept independent of the harness module so the
# regression test can run against a build that does not export them.
_SEED_TABLES = (
    "trading_automation_events", "trading_automation_sessions",
    "trading_automation_simulated_fills", "momentum_symbol_viability",
    "adaptive_risk_reservations", "adaptive_risk_opportunity_claims",
    "adaptive_risk_decision_packets",
    "alpaca_paper_account_settlement_heads",
    "alpaca_paper_bp_reflection_receipts",
    "alpaca_paper_fill_page_objects",
    "captured_paper_post_commit_outbox",
    "captured_paper_completed_fill_watch_events",
)

# The six-table list origin/main shipped: still the seed of the cascade, but blind to the
# FK parents above. Used to prove the coverage check rejects it.
_SEED_TABLES_BEFORE_THE_PARENT_FIX = _SEED_TABLES[:6]

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

# The FK PARENT of adaptive_risk_reservations (NOT NULL, so every reservation the replay
# writes implies one of these). TRUNCATE ... CASCADE descends to children only, so this
# table is invisible to the cascade and must be seeded explicitly.
_PARENT_TABLE = "adaptive_risk_decision_packets"
_PACKET_HASH_COLUMNS = (
    "decision_packet_sha256", "reservation_request_sha256", "account_identity_sha256",
    "account_snapshot_sha256", "policy_sha256", "input_sha256", "economic_input_sha256",
    "economic_resolution_sha256", "effective_config_sha256", "code_build_sha256",
    "feature_flags_sha256", "capture_prefix_root_sha256", "evidence_sha256",
    "reservation_ledger_sha256",
)


def _packet_params() -> dict:
    """A minimal packet row: every hash column is 64 chars (ck_..._hash_lengths)."""
    params = {name: f"{i:02d}" + "a" * 62 for i, name in enumerate(_PACKET_HASH_COLUMNS)}
    params.update(
        decision_id="sink_reset_regression_decision",
        account_scope="sink_reset_regression",
        symbol="ZZZZ",
        trading_date="2026-09-03",
        setup_family="regression",
        correlation_cluster="regression",
        client_order_id="sink_reset_regression_coid",
        execution_surface="replay",
        execution_family="robinhood_agentic_mcp",
        broker_environment="paper",
        account_snapshot_generation="1",
        resolved_quantity_shares=0,
        structural_stop=1,
        entry_limit_price=1,
        resolver_valid=False,
        admission_accepted=False,
        account_snapshot_json="{}",
        decision_packet_json="{}",
    )
    return params


def _seed_packet_sql(params: dict) -> str:
    return (
        f"INSERT INTO {_PARENT_TABLE} ({', '.join(params)}) "
        f"VALUES ({', '.join(':' + k for k in params)})"
    )


# Installed by _install_migration_354_owner_event_guards on the hash-chained reservation
# ledger. This is the guard the harness used to crash into.
_LEDGER_GUARD = "trg_adaptive_risk_reservation_events_no_truncate"

# Synthetic guarded child (see the module docstring). Its FK column is NULLABLE so it does
# not trip the reset's NOT NULL coverage check; its guard raises 23514, not 55000.
_SYNTH_TABLE = "zz_sink_reset_synthetic_guarded_child"
_SYNTH_GUARD = "trg_zz_sink_reset_synthetic_guarded_child_no_truncate"
_SYNTH_FN = "zz_sink_reset_synthetic_guard_reject"
_SYNTH_DDL = f"""
CREATE TABLE public.{_SYNTH_TABLE} (
    id bigserial PRIMARY KEY,
    claim_id bigint NULL REFERENCES public.{_SEEDED_TABLE}(id)
);
CREATE FUNCTION public.{_SYNTH_FN}() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION '% is append-only; % is forbidden', TG_TABLE_NAME, TG_OP
        USING ERRCODE = '23514';
END
$fn$;
CREATE TRIGGER {_SYNTH_GUARD}
    BEFORE TRUNCATE ON public.{_SYNTH_TABLE}
    FOR EACH STATEMENT EXECUTE FUNCTION public.{_SYNTH_FN}();
INSERT INTO public.{_SYNTH_TABLE} (claim_id) VALUES (NULL);
"""
_SYNTH_DROP = f"""
DROP TABLE IF EXISTS public.{_SYNTH_TABLE} CASCADE;
DROP FUNCTION IF EXISTS public.{_SYNTH_FN}() CASCADE;
"""


@pytest.fixture(scope="module")
def harness():
    """Import scripts/replay_v3_fsm_window.py (not a package) by path, once."""
    spec = importlib.util.spec_from_file_location(
        "_replay_v3_fsm_window_under_test", _HARNESS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def synthetic_guard(db, harness):
    """A NON-EMPTY guarded table inside the cascade closure, raising under 23514.

    Without this, every guarded table in the closure is empty for the whole run and the
    emptiness assertions cannot tell a real truncate-through-guards from a DELETE
    workaround that never suspends anything.
    """
    engine = sa.create_engine(harness.SIM)
    try:
        with engine.begin() as conn:
            conn.execute(text(_SYNTH_DROP))
            conn.execute(text(_SYNTH_DDL))
        yield _SYNTH_TABLE
    finally:
        # Must not outlive this test: the next test's conftest TRUNCATE would cascade
        # into it and hit this guard. Release the session's locks first — DROP TABLE
        # needs ACCESS EXCLUSIVE, and a failed assertion can leave the test's last read
        # open.
        db.rollback()
        with engine.begin() as conn:
            conn.execute(text(_SYNTH_DROP))
        engine.dispose()


def _closure(db, seeds=_SEED_TABLES):
    """The relations TRUNCATE ... CASCADE reaches from the sink seeds."""
    rows = db.execute(text(_CLOSURE_SQL), {"seeds": list(seeds)}).all()
    return [(int(oid), ns, rel) for (oid, ns, rel) in rows]


def _truncate_guards(db, closure):
    rows = db.execute(text(_GUARD_SQL), {"oids": [oid for (oid, _n, _r) in closure]}).all()
    return [(int(oid), tg, en) for (oid, tg, en) in rows]


def _count(db, ns, rel):
    return db.execute(text(f'SELECT count(*) FROM "{ns}"."{rel}"')).scalar_one()


def _release(db):
    """End the session's transaction before calling the reset.

    ``_reset_sim_sink`` takes ACCESS EXCLUSIVE on the whole closure and deliberately sets
    no ``lock_timeout`` (a timeout would turn a correct wait into a spurious failure), so
    a test session left idle-in-transaction on a sink table blocks the TRUNCATE forever.
    Every read of a sink relation here is followed by a commit for that reason.
    """
    db.commit()


def _seed_and_release(db):
    """Seed one sink row and COMMIT, so the reset's connection can take ACCESS EXCLUSIVE."""
    db.execute(text(_SEED_CLAIM_SQL))
    _release(db)


def test_sink_closure_actually_carries_truncate_guards(db):
    """Guard the guard: if the bed is unmigrated, every other test here is vacuous."""
    closure = _closure(db)
    assert closure, "sink closure is empty — TEST_DATABASE_URL is not a migrated database"
    names = {rel for (_oid, _ns, rel) in closure}
    assert set(_SEED_TABLES) <= names
    # CASCADE reaches well past the seeds — that reach is the whole defect.
    assert len(closure) > len(_SEED_TABLES)

    guarded = {tg for (_oid, tg, _en) in _truncate_guards(db, closure)}
    assert _LEDGER_GUARD in guarded, (
        f"{_LEDGER_GUARD} is absent — migration 354 has not run against this database, "
        "so this file cannot prove anything. Point TEST_DATABASE_URL at a migrated DB."
    )


def test_reset_sim_sink_truncates_through_append_only_guards(db, harness, synthetic_guard):
    """The regression itself: on origin/main this raises ERRCODE 55000 before any tape loads.

    The synthetic guard makes the assertions load-bearing: a reset that quietly skipped
    guarded tables (or DELETEd instead of truncating) leaves its row behind and fails here.
    """
    _seed_and_release(db)
    assert db.execute(text(f"SELECT count(*) FROM {_SEEDED_TABLE}")).scalar_one() == 1
    assert db.execute(text(f"SELECT count(*) FROM {synthetic_guard}")).scalar_one() == 1
    _release(db)

    summary = harness._reset_sim_sink()

    # CONTRACT: a guard was really suspended — "nothing was ever disabled" must not pass.
    suspended = {tg for (_rel, tg) in summary["suspended"]}
    assert _LEDGER_GUARD in suspended, summary["suspended"]
    assert _SYNTH_GUARD in suspended, (
        "the reset did not discover a BEFORE TRUNCATE guard it had never seen — discovery "
        f"is not catalog-driven. suspended={summary['suspended']}"
    )

    # CONTRACT: every relation the reset covers is genuinely empty, verified by count.
    closure = _closure(db)
    residue = {rel: _count(db, ns, rel) for (_oid, ns, rel) in closure}
    assert not {k: v for k, v in residue.items() if v}, f"sink not empty after reset: {residue}"
    # Including the guarded table that actually held a row.
    assert db.execute(text(f"SELECT count(*) FROM {synthetic_guard}")).scalar_one() == 0

    # CONTRACT: the append-only guards are back on — the suspension is not permanent.
    still_off = [tg for (_oid, tg, en) in _truncate_guards(db, closure) if en == "D"]
    assert not still_off, f"TRUNCATE guards left disabled after reset: {still_off}"
    _release(db)


def test_reset_sim_sink_cleans_fk_parents_the_cascade_cannot_reach(db, harness):
    """CASCADE descends to CHILDREN only, so parents need seeding, not discovery.

    ``adaptive_risk_reservations.decision_packet_sha256`` is a NOT NULL FK to
    ``adaptive_risk_decision_packets``. A surviving packet makes the next run take the
    idempotent-retry branch in adaptive_risk_reservation.py, or raise
    AdaptiveReservationIdempotencyConflict — a wrong verdict from a clean-looking sink.
    """
    params = _packet_params()
    db.execute(text(_seed_packet_sql(params)), params)
    assert db.execute(text(f"SELECT count(*) FROM {_PARENT_TABLE}")).scalar_one() == 1
    _release(db)

    harness._reset_sim_sink()

    assert db.execute(text(f"SELECT count(*) FROM {_PARENT_TABLE}")).scalar_one() == 0
    _release(db)


def test_reset_sim_sink_rejects_a_seed_list_that_leaves_a_parent_behind(db, harness, monkeypatch):
    """Self-policing coverage: the six-table list origin/main shipped is now refused.

    A NOT NULL FK out of the covered set is a provable split clean, so the next migration
    that adds one cannot silently reopen this gap.
    """
    monkeypatch.setattr(
        harness, "_SINK_SEED_TABLES", _SEED_TABLES_BEFORE_THE_PARENT_FIX
    )
    monkeypatch.setattr(
        harness, "_sink_truncate_targets",
        lambda _c: pytest.fail("reset reached TRUNCATE despite an uncovered NOT NULL parent"),
    )
    _release(db)

    with pytest.raises(SystemExit) as excinfo:
        harness._reset_sim_sink()

    message = str(excinfo.value)
    assert "SPLIT CLEAN" in message
    assert _PARENT_TABLE in message


def test_reset_sim_sink_fails_loudly_on_a_partial_clean(db, harness, monkeypatch):
    """A silent partial clean is worse than a hard failure: contaminated results look real."""
    _seed_and_release(db)
    closure = _closure(db)
    # Simulate a reset that misses one table. Truncating a child never cascades UP to its
    # parent, so holding the seeded parent back really does leave its row behind.
    kept = [t for t in closure if t[2] != _SEEDED_TABLE]
    assert len(kept) == len(closure) - 1
    monkeypatch.setattr(harness, "_sink_truncate_targets", lambda _c: kept)
    _release(db)

    with pytest.raises(SystemExit) as excinfo:
        harness._reset_sim_sink()

    message = str(excinfo.value)
    assert "NOT EMPTY after reset" in message
    assert f"{_SEEDED_TABLE}=1" in message

    # It aborted, but it still restored the guards it suspended.
    still_off = [tg for (_oid, tg, en) in _truncate_guards(db, closure) if en == "D"]
    assert not still_off, f"TRUNCATE guards left disabled after a loud abort: {still_off}"
    _release(db)


def test_guards_are_restored_when_the_reset_transaction_fails(db, harness, monkeypatch):
    """Crash safety: DDL is transactional, so a mid-reset failure must roll the suspension back."""
    closure = _closure(db)
    monkeypatch.setattr(
        harness,
        "_sink_truncate_targets",
        lambda c: list(c) + [(0, "public", "chili_no_such_sink_relation")],
    )
    _release(db)

    with pytest.raises(Exception):
        harness._reset_sim_sink()

    still_off = [tg for (_oid, tg, en) in _truncate_guards(db, closure) if en == "D"]
    assert not still_off, (
        f"TRUNCATE guards left DISABLED after a failed reset: {still_off} — the append-only "
        "guard must never survive the transaction in a disabled state."
    )
    _release(db)


# --------------------------------------------------------------------------------------
# The *_test fence. ``_reset_sim_sink`` issues ALTER TABLE ... DISABLE TRIGGER against a
# real-money hash chain, so what authorizes it must be the DATABASE, not a URL string.
# --------------------------------------------------------------------------------------

def test_sim_db_name_parses_the_database_not_the_url_suffix(harness):
    """A raw endswith('_test') on the URL passes for prod and fails for the replay lane."""
    prod_url_that_fools_a_suffix_match = (
        "postgresql://chili:chili@localhost:5433/chili?application_name=chili_test"
    )
    assert prod_url_that_fools_a_suffix_match.rstrip("/").endswith("_test")
    assert harness._sim_db_name(prod_url_that_fools_a_suffix_match) == "chili"

    # docker-compose.replay-zero-egress.yml — a real *_test database a suffix match rejects.
    lane_url = "postgresql://chili:chili@/chili_test?host=%2Fvar%2Frun%2Fpostgresql"
    assert not lane_url.rstrip("/").endswith("_test")
    assert harness._sim_db_name(lane_url) == "chili_test"


@pytest.fixture()
def scratch_non_test_database(harness):
    """A disposable database whose name does NOT end in _test. Never points at chili."""
    name = "chili_sink_fence_scratch"
    admin = sa.create_engine(
        make_url(harness.SIM).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield name
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_reset_sim_sink_refuses_a_database_that_is_not_a_test_database(
    db, harness, monkeypatch, scratch_non_test_database
):
    """DO NOT WIDEN THIS FENCE.

    The URL below passes a raw ``endswith('_test')`` check while resolving somewhere else
    entirely — the shape that, against ``chili``, would DISABLE the append-only guards and
    TRUNCATE the reservation-event hash chain, then print a clean-sink banner over it. The
    abort must come from ``current_database()`` on the reset's own connection, before any
    DDL: ``_sink_truncate_targets`` is monkeypatched to fail, and it runs before the first
    ALTER TABLE, so reaching it at all is the failure.
    """
    misleading_url = make_url(harness.SIM).set(
        database=scratch_non_test_database
    ).render_as_string(hide_password=False) + "?application_name=chili_test"
    assert misleading_url.rstrip("/").endswith("_test")
    assert harness._sim_db_name(misleading_url) == scratch_non_test_database

    monkeypatch.setattr(harness, "SIM", misleading_url)
    monkeypatch.setattr(
        harness, "_sink_truncate_targets",
        lambda _c: pytest.fail("reset reached the TRUNCATE path on a non-_test database"),
    )

    with pytest.raises(SystemExit) as excinfo:
        harness._reset_sim_sink()

    message = str(excinfo.value)
    assert scratch_non_test_database in message
    assert "refusing to suspend append-only TRUNCATE guards" in message
