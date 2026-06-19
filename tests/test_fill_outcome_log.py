"""FILL_OUTCOME_LOG Stage-1 (mig308) — WRITE-ONLY logger safety + behavior.

Covers the four NON-NEGOTIABLE properties of ``_record_fill_outcome_safe`` in the
momentum live runner:

1. PARITY     — flag OFF (default) => zero rows AND zero new SQL (returns before any
                DB work or broker read).
2. FAIL-OPEN  — a write error inside the SAVEPOINT rolls back ONLY the insert; the
                call returns normally, writes no row, and the shared session stays
                usable (a subsequent commit succeeds).
3. IDEMPOTENT — two calls that collide on the (session_id, side, leg_seq) unique key
                yield exactly ONE row (ON CONFLICT DO NOTHING).
4. WRITE      — flag ON + live mode => one row with the expected fields, incl.
                spread_bps_at_decision and fill_source.

Uses the truncating ``db`` fixture (TEST_DATABASE_URL, _test DB).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.trading import (
    MomentumFillOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import live_runner as lr


# ── seeding ────────────────────────────────────────────────────────────────
_variant_seq = 0


def _variant(db):
    global _variant_seq
    _variant_seq += 1
    v = MomentumStrategyVariant(
        family="test_family",
        variant_key=f"fillog_{_variant_seq}",
        label="fill-log test variant",
        params_json={},
    )
    db.add(v)
    db.flush()
    return v


def _session(db, *, mode="live", symbol="CAST", execution_family="robinhood_spot"):
    v = _variant(db)
    sess = TradingAutomationSession(
        user_id=None,
        venue="test",
        execution_family=execution_family,
        mode=mode,
        symbol=symbol,
        variant_id=v.id,
        state="live_entered",
        risk_snapshot_json={"momentum_live_execution": {}},
        correlation_id="corr-fillog",
    )
    db.add(sess)
    db.flush()
    return sess


def _count_rows(db, session_id) -> int:
    return int(
        db.execute(
            text("SELECT COUNT(*) FROM momentum_fill_outcomes WHERE session_id = :s"),
            {"s": session_id},
        ).scalar()
        or 0
    )


# ── 1. PARITY — flag OFF => zero rows, zero new SQL ──────────────────────────
def test_flag_off_writes_nothing_and_emits_no_sql(db, monkeypatch):
    sess = _session(db, mode="live")
    monkeypatch.setattr(lr.settings, "chili_momentum_fill_log_enabled", False, raising=False)

    # Spy: if the writer touches the session at all while OFF, fail.
    calls = {"execute": 0, "begin_nested": 0}
    real_execute = db.execute
    real_begin_nested = db.begin_nested

    def _exec(*a, **k):
        calls["execute"] += 1
        return real_execute(*a, **k)

    def _bn(*a, **k):
        calls["begin_nested"] += 1
        return real_begin_nested(*a, **k)

    monkeypatch.setattr(db, "execute", _exec)
    monkeypatch.setattr(db, "begin_nested", _bn)

    lr._record_fill_outcome_safe(
        db, sess,
        side="entry", fill_source="broker_confirmed", broker_order_id="O1",
        fill_price=1.23, qty=10.0, fees_usd=0.0, order_status="filled",
        intended_price=1.22, spread_bps_at_decision=42.0,
    )

    assert calls["execute"] == 0, "flag OFF must issue no SQL"
    assert calls["begin_nested"] == 0, "flag OFF must open no savepoint"
    assert _count_rows(db, sess.id) == 0


def test_paper_mode_writes_nothing(db, monkeypatch):
    """Even with the flag ON, a non-live (paper) session writes zero rows."""
    sess = _session(db, mode="paper")
    monkeypatch.setattr(lr.settings, "chili_momentum_fill_log_enabled", True, raising=False)

    lr._record_fill_outcome_safe(
        db, sess,
        side="entry", fill_source="broker_confirmed", broker_order_id="O1",
        fill_price=1.23, qty=10.0, fees_usd=0.0, order_status="filled",
        intended_price=1.22, spread_bps_at_decision=42.0,
    )
    assert _count_rows(db, sess.id) == 0


# ── 2. FAIL-OPEN — insert error rolls back only the savepoint; session usable ─
def test_fail_open_insert_error_leaves_session_usable(db, monkeypatch):
    sess = _session(db, mode="live")
    monkeypatch.setattr(lr.settings, "chili_momentum_fill_log_enabled", True, raising=False)

    # Make the INSERT raise INSIDE the savepoint. The leg_seq SELECT runs first, so
    # raise only on the INSERT statement (keeps the begin_nested path exercised).
    real_execute = db.execute

    def _boom(stmt, *a, **k):
        sql = str(getattr(stmt, "text", stmt))
        if "INSERT INTO momentum_fill_outcomes" in sql:
            raise RuntimeError("simulated insert failure")
        return real_execute(stmt, *a, **k)

    monkeypatch.setattr(db, "execute", _boom)

    # Must NOT raise.
    lr._record_fill_outcome_safe(
        db, sess,
        side="exit", fill_source="broker_confirmed", broker_order_id="O2",
        fill_price=1.10, qty=10.0, fees_usd=0.0, order_status="filled",
        intended_price=1.10, spread_bps_at_decision=10.0,
        entry_price=1.20, exit_reason="stop", realized_pnl_usd=-1.0,
    )

    # No row written…
    monkeypatch.setattr(db, "execute", real_execute)
    assert _count_rows(db, sess.id) == 0

    # …and the OUTER session is still healthy: a subsequent write + commit succeeds
    # (a poisoned transaction would raise PendingRollbackError here).
    sess2 = _session(db, mode="live", symbol="PBK")
    monkeypatch.setattr(lr.settings, "chili_momentum_fill_log_enabled", True, raising=False)
    lr._record_fill_outcome_safe(
        db, sess2,
        side="entry", fill_source="broker_confirmed", broker_order_id="O3",
        fill_price=2.00, qty=5.0, fees_usd=0.0, order_status="filled",
        intended_price=2.00, spread_bps_at_decision=8.0,
    )
    db.commit()
    assert _count_rows(db, sess2.id) == 1


# ── 3. IDEMPOTENT — colliding (session_id, side, leg_seq) => exactly one row ──
def test_idempotent_same_session_side_collision_one_row(db, monkeypatch):
    sess = _session(db, mode="live")
    monkeypatch.setattr(lr.settings, "chili_momentum_fill_log_enabled", True, raising=False)

    # Pin leg_seq to a constant so two calls for the same (session, side) collide on
    # the unique key — proving ON CONFLICT DO NOTHING (a real retried/repegged poll).
    real_execute = db.execute

    def _pin_leg_seq(stmt, *a, **k):
        sql = str(getattr(stmt, "text", stmt))
        if "COALESCE(MAX(leg_seq)" in sql:
            class _R:
                def scalar(self_inner):
                    return 0
            return _R()
        return real_execute(stmt, *a, **k)

    monkeypatch.setattr(db, "execute", _pin_leg_seq)

    common = dict(
        side="exit", fill_source="broker_confirmed", broker_order_id="O-retry",
        fill_price=1.10, qty=10.0, fees_usd=0.0, order_status="filled",
        intended_price=1.10, spread_bps_at_decision=10.0,
        entry_price=1.20, exit_reason="stop", realized_pnl_usd=-1.0,
    )
    lr._record_fill_outcome_safe(db, sess, **common)
    lr._record_fill_outcome_safe(db, sess, **common)

    monkeypatch.setattr(db, "execute", real_execute)
    db.commit()
    assert _count_rows(db, sess.id) == 1


# ── 4. WRITE — flag ON + live => one row with the expected fields ────────────
def test_write_live_row_has_expected_fields(db, monkeypatch):
    sess = _session(db, mode="live", symbol="CAST", execution_family="robinhood_spot")
    monkeypatch.setattr(lr.settings, "chili_momentum_fill_log_enabled", True, raising=False)

    lr._record_fill_outcome_safe(
        db, sess,
        side="exit", fill_source="broker_confirmed", broker_order_id="ORD-9",
        fill_price=1.05, qty=100.0, fees_usd=0.12, order_status="filled",
        intended_price=1.06, spread_bps_at_decision=37.5,
        entry_price=1.20, exit_reason="max_loss_circuit",
        realized_pnl_usd=-15.12, pnl_gross_usd=-15.0,
        entry_l2_snapshot={"bid": 1.04, "ask": 1.06},
        raw={"slip_bps": 9.0},
    )
    db.commit()

    row = (
        db.query(MomentumFillOutcome)
        .filter(MomentumFillOutcome.session_id == sess.id)
        .one()
    )
    assert row.side == "exit"
    assert row.mode == "live"
    assert row.asset_class == "equity"
    assert row.execution_family == "robinhood_spot"
    assert row.fill_source == "broker_confirmed"
    assert row.broker_order_id == "ORD-9"
    assert row.symbol == "CAST"
    assert row.leg_seq == 0
    assert abs(row.broker_fill_price - 1.05) < 1e-9
    assert abs(row.qty - 100.0) < 1e-9
    assert abs(row.fees_usd - 0.12) < 1e-9
    assert abs(row.spread_bps_at_decision - 37.5) < 1e-9
    assert abs(row.entry_price - 1.20) < 1e-9
    assert row.exit_reason == "max_loss_circuit"
    assert abs(row.realized_pnl_usd - (-15.12)) < 1e-9
    assert abs(row.pnl_gross_usd - (-15.0)) < 1e-9
    assert row.entry_l2_snapshot_json == {"bid": 1.04, "ask": 1.06}
    assert row.raw_json == {"slip_bps": 9.0}
    assert row.fill_ts is not None
    assert row.settled_fill_price is None  # Stage-1 write-only: no reconcile yet


def test_write_crypto_asset_class(db, monkeypatch):
    sess = _session(db, mode="live", symbol="TAO-USD", execution_family="coinbase_spot")
    monkeypatch.setattr(lr.settings, "chili_momentum_fill_log_enabled", True, raising=False)
    lr._record_fill_outcome_safe(
        db, sess,
        side="entry", fill_source="broker_confirmed", broker_order_id="CB-1",
        fill_price=300.0, qty=0.5, fees_usd=1.50, order_status="filled",
        intended_price=300.0, spread_bps_at_decision=12.0,
    )
    db.commit()
    row = (
        db.query(MomentumFillOutcome)
        .filter(MomentumFillOutcome.session_id == sess.id)
        .one()
    )
    assert row.asset_class == "crypto"
    assert row.side == "entry"
    assert row.realized_pnl_usd is None
