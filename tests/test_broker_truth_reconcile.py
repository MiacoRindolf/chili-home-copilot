"""Broker-truth reconciliation (mig309) — additive label + accessor parity/behavior.

NON-NEGOTIABLE properties covered:

(a) PARITY        — flag OFF => authoritative_label_for_outcome returns the LEGACY
                    label byte-for-byte AND the reconcile pass is a no-op (zero rows
                    stamped). Mirrors test_fill_outcome_log.py's parity discipline.
(b) MULTI-LEG     — a clean scale-out fixture reconciles to the summed broker total,
                    with a broker-true return_bps off the entry-leg notional.
(c) PYRAMID GAP   — a pyramided session (exit qty > entry qty, pyramid_add_count>0)
                    lands UNRECONCILED and is EXCLUDED, never a leg-mismatch label.
(d) PHANTOM       — a live_trailing-recorded session with NO broker match =>
                    EXCLUDED (is_reconciled=False), NOT labeled $0 (no false LOSS).
(e) IDEMPOTENT    — re-run leaves a reconciled row untouched; a residual_open row
                    flips to reconciled once a closing fill is added.
(f) NEVER-FABRICATE for the trade-row fallback: >1 closed trading_trades sharing the
                    order id => AMBIGUOUS (excluded), never LIMIT-1 picks one.

Uses the truncating ``db`` fixture (TEST_DATABASE_URL, _test DB). Never hits a live
broker — the ledger / trading_trades rows are seeded directly.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import settings
from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import outcome_reconcile as ocr

_seq = 0


def _outcome(db, *, mode="live", execution_family="robinhood_spot", legacy_pnl=10.0,
             legacy_bps=50.0, le=None, symbol="CAST"):
    global _seq
    _seq += 1
    v = MomentumStrategyVariant(
        family="test_family", variant_key=f"recon_{_seq}", label="recon test", params_json={},
    )
    db.add(v)
    db.flush()
    sess = TradingAutomationSession(
        user_id=None, venue="test", execution_family=execution_family, mode=mode,
        symbol=symbol, variant_id=v.id, state="live_finished",
        risk_snapshot_json={"momentum_live_execution": le or {}},
        correlation_id=f"corr-recon-{_seq}",
    )
    db.add(sess)
    db.flush()
    o = MomentumAutomationOutcome(
        session_id=sess.id, user_id=None, variant_id=v.id, symbol=symbol, mode=mode,
        execution_family=execution_family, terminal_state="live_finished",
        terminal_at=__import__("datetime").datetime.utcnow(), outcome_class="success",
        realized_pnl_usd=legacy_pnl, return_bps=legacy_bps,
    )
    db.add(o)
    db.flush()
    return o, sess


def _add_leg(db, session_id, *, side, leg_seq, fill_source="broker_confirmed",
             fill_price=None, qty=None, fees=0.0, settled_pnl=None, lane_pnl=None):
    db.execute(
        text(
            "INSERT INTO momentum_fill_outcomes "
            "(session_id, leg_seq, symbol, side, mode, fill_source, broker_fill_price, "
            " qty, fees_usd, settled_pnl_usd, realized_pnl_usd) VALUES "
            "(:sid,:leg,:sym,:side,:mode,:src,:px,:qty,:fees,:spnl,:lpnl)"
        ),
        {"sid": session_id, "leg": leg_seq, "sym": "CAST", "side": side, "mode": "live",
         "src": fill_source, "px": fill_price, "qty": qty, "fees": fees,
         "spnl": settled_pnl, "lpnl": lane_pnl},
    )
    db.flush()


@pytest.fixture(autouse=True)
def _flags_off():
    # Default both flags OFF before each test; tests opt-in via monkeypatch.
    orig_w = settings.chili_momentum_broker_truth_reconciliation_enabled
    orig_r = settings.chili_momentum_broker_truth_label_enabled
    settings.chili_momentum_broker_truth_reconciliation_enabled = False
    settings.chili_momentum_broker_truth_label_enabled = False
    yield
    settings.chili_momentum_broker_truth_reconciliation_enabled = orig_w
    settings.chili_momentum_broker_truth_label_enabled = orig_r


# ── (a) PARITY ─────────────────────────────────────────────────────────────
def test_accessor_flag_off_is_byte_identical_legacy(db):
    o, _ = _outcome(db, legacy_pnl=12.5, legacy_bps=34.0)
    o.broker_recon_status = "reconciled"
    o.broker_realized_pnl_usd = 999.0
    o.broker_return_bps = 777.0
    o.broker_win = True
    pnl, bps, win, is_rec = ocr.authoritative_label_for_outcome(o)
    assert (pnl, bps, win, is_rec) == (12.5, 34.0, None, True)


def test_reconcile_pass_noop_when_flag_off(db):
    o, _ = _outcome(db)
    res = ocr.reconcile_momentum_outcomes_to_broker_truth(db)
    assert res.get("skipped") == "reconciliation_disabled"
    db.refresh(o)
    assert o.broker_recon_status is None  # nothing stamped


# ── (b) MULTI-LEG clean scale-out ────────────────────────────────────────────
def test_multileg_scaleout_reconciles_to_broker_total(db):
    settings.chili_momentum_broker_truth_reconciliation_enabled = True
    settings.chili_momentum_broker_truth_label_enabled = True
    o, sess = _outcome(db, legacy_pnl=4.0, le={"entry_order_id": "OID-1"})
    # entry 100 @ 10.00 => notional 1000; two exit legs summing pnl +6.50 (broker_confirmed)
    _add_leg(db, sess.id, side="entry", leg_seq=0, fill_price=10.00, qty=100.0, fees=1.0)
    _add_leg(db, sess.id, side="partial_exit", leg_seq=0, fill_price=10.05, qty=50.0, fees=0.5, lane_pnl=2.0)
    _add_leg(db, sess.id, side="exit", leg_seq=0, fill_price=10.09, qty=50.0, fees=0.5, lane_pnl=4.5)
    db.commit()
    ocr.reconcile_momentum_outcomes_to_broker_truth(db)
    db.refresh(o)
    assert o.broker_recon_status == ocr.STATUS_RECONCILED
    assert o.broker_realized_pnl_usd == pytest.approx(6.5)
    assert o.broker_notional_basis_usd == pytest.approx(1000.0)
    # broker-true return_bps = 6.5 / 1000 * 1e4 = 65.0 (NOT the legacy 50.0)
    assert o.broker_return_bps == pytest.approx(65.0)
    assert o.broker_win is True
    # legacy field untouched (drift baseline)
    assert o.realized_pnl_usd == pytest.approx(4.0)
    assert o.broker_divergence_usd == pytest.approx(2.5)
    pnl, bps, win, is_rec = ocr.authoritative_label_for_outcome(o)
    assert (pnl, bps, win, is_rec) == (pytest.approx(6.5), pytest.approx(65.0), True, True)


# ── (c) PYRAMID leg gap => excluded ──────────────────────────────────────────
def test_pyramided_session_excluded_not_mislabeled(db):
    settings.chili_momentum_broker_truth_reconciliation_enabled = True
    settings.chili_momentum_broker_truth_label_enabled = True
    o, sess = _outcome(db, le={"entry_order_id": "OID-PYR", "pyramid_add_count": 1})
    # starter entry 100; exit legs sum to the ENLARGED 200 (pyramid add wrote no entry leg)
    _add_leg(db, sess.id, side="entry", leg_seq=0, fill_price=5.0, qty=100.0, fees=1.0)
    _add_leg(db, sess.id, side="exit", leg_seq=0, fill_price=5.2, qty=200.0, fees=1.0, lane_pnl=40.0)
    db.commit()
    ocr.reconcile_momentum_outcomes_to_broker_truth(db)
    db.refresh(o)
    assert o.broker_recon_status == ocr.STATUS_PYRAMID_GAP
    assert o.broker_realized_pnl_usd is None
    detail = o.broker_recon_detail_json
    assert detail["ledger"]["exit_qty"] > detail["ledger"]["entry_qty"]
    pnl, bps, win, is_rec = ocr.authoritative_label_for_outcome(o)
    assert is_rec is False and pnl is None and bps is None


# ── (d) PHANTOM => excluded, not $0 ──────────────────────────────────────────
def test_phantom_session_excluded_not_zero_labeled(db):
    settings.chili_momentum_broker_truth_reconciliation_enabled = True
    settings.chili_momentum_broker_truth_label_enabled = True
    # entry_order_id present but no ledger rows and no matching trading_trades row
    o, sess = _outcome(db, le={"entry_order_id": "OID-PHANTOM"})
    db.commit()
    ocr.reconcile_momentum_outcomes_to_broker_truth(db)
    db.refresh(o)
    assert o.broker_recon_status == ocr.STATUS_PHANTOM
    assert o.broker_realized_pnl_usd is None
    pnl, bps, win, is_rec = ocr.authoritative_label_for_outcome(o)
    assert is_rec is False and pnl is None  # NOT a fabricated $0 (would be a false LOSS)


# ── (e) IDEMPOTENT + convergence ─────────────────────────────────────────────
def test_residual_open_converges_then_reconciled_is_immutable(db):
    settings.chili_momentum_broker_truth_reconciliation_enabled = True
    settings.chili_momentum_broker_truth_label_enabled = True
    o, sess = _outcome(db, le={"entry_order_id": "OID-RES"})
    _add_leg(db, sess.id, side="entry", leg_seq=0, fill_price=2.0, qty=100.0, fees=0.5)
    db.commit()
    # First pass: entry only => residual_open (re-attempted on future runs)
    ocr.reconcile_momentum_outcomes_to_broker_truth(db)
    db.refresh(o)
    assert o.broker_recon_status == ocr.STATUS_RESIDUAL_OPEN
    # Closing fill arrives; re-run converges to reconciled
    _add_leg(db, sess.id, side="exit", leg_seq=0, fill_price=2.1, qty=100.0, fees=0.5, lane_pnl=9.0)
    db.commit()
    ocr.reconcile_momentum_outcomes_to_broker_truth(db)
    db.refresh(o)
    assert o.broker_recon_status == ocr.STATUS_RECONCILED
    assert o.broker_realized_pnl_usd == pytest.approx(9.0)
    stamped_at = o.broker_reconciled_at
    # Re-run a third time: a terminally-reconciled row is NOT re-touched
    res = ocr.reconcile_momentum_outcomes_to_broker_truth(db)
    db.refresh(o)
    assert o.broker_reconciled_at == stamped_at
    assert res["skipped_terminal"] >= 1


# ── (f) trade-row fallback never LIMIT-1 picks an ambiguous row ──────────────
# In prod trading_trades.broker_order_id is nullable + NON-unique (a pyramid/re-entry
# yields multiple closed rows under one entry id); the chili_test create_all builds a
# UNIQUE index so we cannot seed the collision directly. Exercise the COUNT>1 guard via
# a stubbed db whose COUNT scalar returns 2 — proving the fallback returns AMBIGUOUS
# (UNRECONCILED) rather than LIMIT-1 picking one row.
def test_trade_row_fallback_ambiguous_count_guard():
    class _Result:
        def __init__(self, v):
            self._v = v

        def scalar(self):
            return self._v

        def fetchone(self):
            return None

    class _StubDB:
        def execute(self, *_a, **_k):
            return _Result(2)  # COUNT(*) == 2 closed rows share the order id

    out = ocr._trade_row_fallback(_StubDB(), {"entry_order_id": "OID-AMB"})
    assert out["status"] == ocr.STATUS_AMBIGUOUS_TRADE
    assert out["pnl"] is None


def test_trade_row_fallback_no_order_id_is_no_fills():
    class _StubDB:
        def execute(self, *_a, **_k):  # pragma: no cover - never reached
            raise AssertionError("must short-circuit before any query")

    out = ocr._trade_row_fallback(_StubDB(), {})
    assert out["status"] == ocr.STATUS_NO_FILLS


def test_trade_row_fallback_single_row_reconciles(db):
    settings.chili_momentum_broker_truth_reconciliation_enabled = True
    settings.chili_momentum_broker_truth_label_enabled = True
    o, sess = _outcome(db, legacy_pnl=1.0, le={"entry_order_id": "OID-ONE"})
    db.execute(
        text(
            "INSERT INTO trading_trades (ticker, direction, quantity, entry_price, "
            "broker_order_id, status, pnl, entry_date) VALUES "
            "(:s,'long',100.0,10.0,'OID-ONE','closed',8.0, now())"
        ),
        {"s": "CAST"},
    )
    db.commit()
    ocr.reconcile_momentum_outcomes_to_broker_truth(db)
    db.refresh(o)
    assert o.broker_recon_status == ocr.STATUS_RECONCILED
    assert o.broker_realized_pnl_usd == pytest.approx(8.0)
    # notional 100*10 = 1000 => bps 8/1000*1e4 = 80
    assert o.broker_return_bps == pytest.approx(80.0)
    assert o.broker_divergence_usd == pytest.approx(7.0)
