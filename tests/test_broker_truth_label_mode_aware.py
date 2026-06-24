"""Mode-aware broker-truth routing of the evolution + param-refinement learners (mig309).

Second companion to test_broker_truth_reconcile.py / test_broker_truth_label_consumers.py.
These consumers aggregate PAPER + LIVE outcomes together, so they route through
`outcome_reconcile.mode_aware_label_for_outcome` (NOT the plain accessor): paper keeps
its self-report, only the LIVE arm goes through the broker-truth switch. A naive
skip-unreconciled would have nuked the paper arm.

Covered:
  * mode_aware_label_for_outcome — paper kept, live reconciled = broker-true, live
    unreconciled = excluded; flag-OFF byte-identical for every mode.
  * evolution._aggregate_rows — n = USED rows; broker-true live + paper self-report.
  * evolution.maybe_kill_underperforming_variant — flag-ON KILLS off broker truth where
    legacy would have spared the variant.
  * evolution.maybe_pause_symbol_variant_after_losses — flag-ON conservative: an
    unreconciled live row in the last 3 blocks the pause.
  * evolution.apply_outcome_feedback_to_viability — the viability nudge tally uses the
    broker-true bps for a reconciled live outcome.
  * strategy_params.refine_strategy_params — refines off broker-true bps; unreconciled
    live rows drop out of the sample.

flag-OFF is asserted byte-identical everywhere. DB-free tests build transient ORM rows;
DB tests use the truncating `db` fixture (_test DB), seeding parent sessions for the FK.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app.config import settings
from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import evolution
from app.services.trading.momentum_neural import outcome_reconcile as ocr
from app.services.trading.momentum_neural import strategy_params

_seq = 0


@pytest.fixture(autouse=True)
def _label_flag_off():
    orig = settings.chili_momentum_broker_truth_label_enabled
    settings.chili_momentum_broker_truth_label_enabled = False
    yield
    settings.chili_momentum_broker_truth_label_enabled = orig


# ── transient row factory (DB-free) ───────────────────────────────────────────
def _row(*, mode="live", bps=50.0, pnl=5.0, broker_status=None, broker_bps=None,
         broker_pnl=None, broker_win=None, hold=60.0, oc="success", weight=1.0):
    return MomentumAutomationOutcome(
        mode=mode, return_bps=bps, realized_pnl_usd=pnl,
        broker_recon_status=broker_status, broker_return_bps=broker_bps,
        broker_realized_pnl_usd=broker_pnl, broker_win=broker_win,
        hold_seconds=hold, outcome_class=oc, evidence_weight=weight,
    )


# ── mode_aware_label_for_outcome ──────────────────────────────────────────────
def test_mode_aware_flag_off_legacy_all_modes():
    live = _row(mode="live", bps=50.0, pnl=5.0, broker_status="reconciled", broker_bps=80.0, broker_pnl=8.0)
    paper = _row(mode="paper", bps=30.0, pnl=3.0)
    assert ocr.mode_aware_label_for_outcome(live) == (50.0, 5.0, True)
    assert ocr.mode_aware_label_for_outcome(paper) == (30.0, 3.0, True)


def test_mode_aware_flag_on_routes_live_keeps_paper():
    settings.chili_momentum_broker_truth_label_enabled = True
    paper = _row(mode="paper", bps=30.0, pnl=3.0)
    live_rec = _row(mode="live", bps=50.0, pnl=5.0, broker_status="reconciled", broker_bps=80.0, broker_pnl=8.0, broker_win=True)
    live_unrec = _row(mode="live", bps=50.0, pnl=5.0, broker_status=None)
    assert ocr.mode_aware_label_for_outcome(paper) == (30.0, 3.0, True)       # paper self-report kept
    assert ocr.mode_aware_label_for_outcome(live_rec) == (80.0, 8.0, True)    # broker-true
    assert ocr.mode_aware_label_for_outcome(live_unrec) == (None, None, False)  # excluded


# ── evolution._aggregate_rows ─────────────────────────────────────────────────
def test_aggregate_rows_flag_off_byte_identical():
    rows = [
        _row(mode="live", bps=50.0, pnl=5.0, broker_status="reconciled", broker_bps=999.0, broker_pnl=99.0),
        _row(mode="paper", bps=-10.0, pnl=-1.0),
    ]
    agg = evolution._aggregate_rows(rows)
    assert agg["n"] == 2
    assert agg["mean_return_bps"] == pytest.approx((50.0 - 10.0) / 2)  # legacy
    assert agg["weighted_pnl_sum"] == pytest.approx(5.0 - 1.0)


def test_aggregate_rows_flag_on_routes_and_counts_used():
    settings.chili_momentum_broker_truth_label_enabled = True
    rows = [
        _row(mode="live", bps=50.0, pnl=5.0, broker_status="reconciled", broker_bps=80.0, broker_pnl=8.0),
        _row(mode="paper", bps=-10.0, pnl=-1.0),               # paper kept
        _row(mode="live", bps=50.0, pnl=5.0, broker_status=None),  # unreconciled -> dropped
    ]
    agg = evolution._aggregate_rows(rows)
    assert agg["n"] == 2  # the unreconciled live row excluded
    assert agg["mean_return_bps"] == pytest.approx((80.0 - 10.0) / 2)  # broker-true live + paper self-report
    assert agg["weighted_pnl_sum"] == pytest.approx(8.0 - 1.0)


# ── strategy_params.refine_strategy_params ────────────────────────────────────
def test_refine_params_flag_off_vs_on_uses_broker_bps():
    rows = [_row(mode="live", bps=60.0, pnl=6.0, broker_status="reconciled", broker_bps=-60.0, broker_pnl=-6.0, broker_win=False) for _ in range(6)]
    _, meta_off = strategy_params.refine_strategy_params({}, list(rows))
    assert meta_off["sample_size"] == 6
    assert meta_off["mean_return_bps"] == pytest.approx(60.0)
    settings.chili_momentum_broker_truth_label_enabled = True
    _, meta_on = strategy_params.refine_strategy_params({}, list(rows))
    assert meta_on["sample_size"] == 6
    assert meta_on["mean_return_bps"] == pytest.approx(-60.0)  # broker-true, not legacy +60


def test_refine_params_flag_on_excludes_unreconciled_live():
    rows = [_row(mode="live", bps=60.0, pnl=6.0, broker_status=None) for _ in range(6)]
    _, meta_off = strategy_params.refine_strategy_params({}, list(rows))
    assert meta_off["sample_size"] == 6
    settings.chili_momentum_broker_truth_label_enabled = True
    _, meta_on = strategy_params.refine_strategy_params({}, list(rows))
    assert meta_on["eligible"] is False
    assert meta_on["reason"] == "insufficient_outcomes"
    assert meta_on["sample_size"] == 0


# ── DB-backed: seed a real outcome (+ parent session for the FK) ──────────────
def _seed_outcome(db, *, variant_id=None, symbol="CAST", mode="live", legacy_bps=50.0,
                  legacy_pnl=5.0, broker_status=None, broker_bps=None, broker_pnl=None,
                  broker_win=None, weight=1.0, contributes=True, family="reclaim",
                  is_active=True):
    global _seq
    _seq += 1
    if variant_id is None:
        v = MomentumStrategyVariant(
            family=family, variant_key=f"ma_{_seq}", label="mode-aware test",
            params_json={}, is_active=is_active,
        )
        db.add(v)
        db.flush()
        variant_id = v.id
    sess = TradingAutomationSession(
        user_id=None, venue="test", execution_family="robinhood_spot", mode=mode,
        symbol=symbol, variant_id=variant_id, state="live_finished",
        risk_snapshot_json={}, correlation_id=f"corr-ma-{_seq}",
    )
    db.add(sess)
    db.flush()
    o = MomentumAutomationOutcome(
        session_id=sess.id, user_id=None, variant_id=variant_id, symbol=symbol, mode=mode,
        execution_family="robinhood_spot", terminal_state="live_finished",
        terminal_at=_dt.datetime.utcnow(), outcome_class="success",
        realized_pnl_usd=legacy_pnl, return_bps=legacy_bps, evidence_weight=weight,
        contributes_to_evolution=contributes,
        broker_recon_status=broker_status, broker_return_bps=broker_bps,
        broker_realized_pnl_usd=broker_pnl, broker_win=broker_win,
    )
    db.add(o)
    db.flush()
    return o, variant_id


# ── evolution.maybe_kill_underperforming_variant ──────────────────────────────
def test_maybe_kill_flag_on_kills_on_broker_truth(db):
    # Legacy looks GREAT (+50, wr=1.0) -> flag-OFF spares the variant. Broker truth is
    # BAD (-60, wr=0) -> flag-ON kills.
    vid = None
    for _ in range(6):
        _o, vid = _seed_outcome(db, variant_id=vid, legacy_bps=50.0, broker_status="reconciled",
                                broker_bps=-60.0, broker_pnl=-6.0, broker_win=False)
    db.commit()

    res_off = evolution.maybe_kill_underperforming_variant(db, variant_id=vid)
    assert res_off.get("killed") is not True  # spared on the legacy self-report

    settings.chili_momentum_broker_truth_label_enabled = True
    res_on = evolution.maybe_kill_underperforming_variant(db, variant_id=vid)
    assert res_on.get("killed") is True
    assert res_on["mean_return_bps"] == pytest.approx(-60.0)  # decided on broker truth


# ── evolution.maybe_pause_symbol_variant_after_losses ─────────────────────────
def test_maybe_pause_flag_on_conservative_when_unreconciled(db):
    settings.chili_momentum_broker_truth_label_enabled = True
    sym = "PAUS"
    vid = None
    # 2 reconciled live losses + 1 UNRECONCILED live (most recent 3) -> cannot confirm a
    # 3-loss streak on broker truth -> NO pause.
    _o1, vid = _seed_outcome(db, variant_id=vid, symbol=sym, legacy_bps=-20.0, broker_status="reconciled", broker_bps=-20.0, broker_pnl=-2.0)
    _o2, vid = _seed_outcome(db, variant_id=vid, symbol=sym, legacy_bps=-20.0, broker_status="reconciled", broker_bps=-20.0, broker_pnl=-2.0)
    o3, vid = _seed_outcome(db, variant_id=vid, symbol=sym, legacy_bps=-20.0, broker_status=None)
    db.add(MomentumSymbolViability(symbol=sym, variant_id=vid, viability_score=0.5))
    db.commit()

    evolution.maybe_pause_symbol_variant_after_losses(db, outcome_row=o3)
    db.commit()
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == sym, MomentumSymbolViability.variant_id == vid)
        .one()
    )
    assert "variant_symbol_pause_until_utc" not in (via.explain_json or {})  # conservative: not paused


def test_maybe_pause_flag_off_pauses_on_three_losses(db):
    sym = "PAUF"
    vid = None
    _o1, vid = _seed_outcome(db, variant_id=vid, symbol=sym, legacy_bps=-20.0)
    _o2, vid = _seed_outcome(db, variant_id=vid, symbol=sym, legacy_bps=-15.0)
    o3, vid = _seed_outcome(db, variant_id=vid, symbol=sym, legacy_bps=-30.0)
    db.add(MomentumSymbolViability(symbol=sym, variant_id=vid, viability_score=0.5))
    db.commit()

    evolution.maybe_pause_symbol_variant_after_losses(db, outcome_row=o3)
    db.commit()
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == sym, MomentumSymbolViability.variant_id == vid)
        .one()
    )
    assert "variant_symbol_pause_until_utc" in (via.explain_json or {})  # legacy 3-loss streak -> paused


# ── evolution.apply_outcome_feedback_to_viability ─────────────────────────────
def test_apply_feedback_uses_broker_bps_flag_on(db):
    settings.chili_momentum_broker_truth_label_enabled = True
    sym = "FEED"
    o, vid = _seed_outcome(db, symbol=sym, legacy_bps=50.0, broker_status="reconciled",
                           broker_bps=80.0, broker_pnl=8.0, broker_win=True, weight=1.0)
    db.add(MomentumSymbolViability(symbol=sym, variant_id=vid, viability_score=0.5))
    db.commit()

    evolution.apply_outcome_feedback_to_viability(db, o)
    db.commit()
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == sym, MomentumSymbolViability.variant_id == vid)
        .one()
    )
    fb = via.evidence_window_json["neural_feedback_v1"]["live"]
    assert fb["weighted_return_bps_sum"] == pytest.approx(80.0)  # broker-true, not legacy 50


def test_apply_feedback_flag_off_uses_legacy_bps(db):
    sym = "FEEG"
    o, vid = _seed_outcome(db, symbol=sym, legacy_bps=50.0, broker_status="reconciled",
                           broker_bps=80.0, broker_pnl=8.0, broker_win=True, weight=1.0)
    db.add(MomentumSymbolViability(symbol=sym, variant_id=vid, viability_score=0.5))
    db.commit()

    evolution.apply_outcome_feedback_to_viability(db, o)
    db.commit()
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == sym, MomentumSymbolViability.variant_id == vid)
        .one()
    )
    fb = via.evidence_window_json["neural_feedback_v1"]["live"]
    assert fb["weighted_return_bps_sum"] == pytest.approx(50.0)  # legacy self-report (byte-identical)
