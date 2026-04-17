"""Phase E: NetEdgeRanker shadow-rollout tests.

Scope (frozen by .cursor/plans/phase_e_net_edge_ranker_v1.plan.md):
* Cost math composition and sign
* Mode gating: off -> no work, no log, no DB write
* Cold-start path: identity calibration when sample_count < min_samples
* Score composition math (expected_net_pnl = p * payoff - (1-p) * loss - costs)
* Ops log shape parity with chili_prediction_ops
* Diagnostics endpoint shape on empty DB

No triple-barrier / venue-truth assumptions. These are Phase D/F territory and
are explicitly out of scope here.
"""

from __future__ import annotations

import logging

import pytest

from app.config import settings
from app.services.trading import net_edge_ranker as ner
from app.trading_brain.infrastructure.net_edge_ops_log import (
    CHILI_NET_EDGE_OPS_PREFIX,
    MODE_AUTHORITATIVE,
    MODE_COMPARE,
    MODE_OFF,
    MODE_SHADOW,
    format_net_edge_ops_line,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test gets a fresh calibrator cache so mode changes take effect."""
    ner._CACHE.clear()
    yield
    ner._CACHE.clear()


@pytest.fixture
def shadow_mode(monkeypatch):
    monkeypatch.setattr(settings, "brain_net_edge_ranker_mode", MODE_SHADOW)
    monkeypatch.setattr(settings, "brain_net_edge_min_samples", 50)
    monkeypatch.setattr(settings, "brain_net_edge_ops_log_enabled", True)
    yield


@pytest.fixture
def off_mode(monkeypatch):
    monkeypatch.setattr(settings, "brain_net_edge_ranker_mode", MODE_OFF)
    yield


# ---------------------------------------------------------------------------
# Ops log shape
# ---------------------------------------------------------------------------


def test_ops_log_line_shape_matches_prediction_ops_contract():
    line = format_net_edge_ops_line(
        mode=MODE_SHADOW,
        read="shadow",
        decision_id="abc123",
        pattern_id=42,
        asset_class="stock",
        regime="risk_on",
        net_edge=0.00123,
        heuristic_score=0.002,
        disagree=False,
        sample_pct=1.0,
    )
    assert line.startswith(CHILI_NET_EDGE_OPS_PREFIX)
    # Fixed field order and enums - any accidental widening must break this test.
    assert " mode=shadow " in line
    assert " read=shadow " in line
    assert " decision_id=abc123 " in line
    assert " pattern_id=42 " in line
    assert " asset_class=stock " in line
    assert " regime=risk_on " in line
    assert " disagree=false " in line
    assert "sample_pct=1.000" in line


def test_ops_log_line_handles_none_fields():
    line = format_net_edge_ops_line(
        mode=MODE_AUTHORITATIVE,
        read="authoritative",
        decision_id="",
        pattern_id=None,
        asset_class=None,
        regime=None,
        net_edge=None,
        heuristic_score=None,
        disagree=True,
        sample_pct=0.1,
    )
    assert " pattern_id=none " in line
    assert " asset_class=none " in line
    assert " regime=none " in line
    assert " net_edge=none " in line
    assert " heuristic_score=none " in line
    assert " disagree=true " in line
    assert "sample_pct=0.100" in line


def test_ops_log_line_truncates_decision_id():
    long_id = "x" * 64
    line = format_net_edge_ops_line(
        mode=MODE_SHADOW,
        read="shadow",
        decision_id=long_id,
        pattern_id=None,
        asset_class=None,
        regime=None,
        net_edge=None,
        heuristic_score=None,
        disagree=False,
        sample_pct=1.0,
    )
    # decision_id is bounded at 24 chars per format_net_edge_ops_line.
    assert "decision_id=" + ("x" * 24) + " " in line


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


def test_mode_is_active_false_when_off(off_mode):
    assert ner.current_mode() == MODE_OFF
    assert ner.mode_is_active() is False
    assert ner.mode_is_authoritative() is False


def test_mode_is_active_true_when_shadow(shadow_mode):
    assert ner.current_mode() == MODE_SHADOW
    assert ner.mode_is_active() is True
    assert ner.mode_is_authoritative() is False


def test_invalid_mode_falls_back_to_off(monkeypatch):
    monkeypatch.setattr(settings, "brain_net_edge_ranker_mode", "banana")
    assert ner.current_mode() == MODE_OFF
    assert ner.mode_is_active() is False


def test_score_returns_none_when_off(db, off_mode):
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL",
        asset_class="stock",
        scan_pattern_id=1,
        raw_prob=0.6,
        entry_price=100.0,
        stop_price=97.0,
    )
    result = ner.score(db, ctx)
    assert result is None


# ---------------------------------------------------------------------------
# Cost math
# ---------------------------------------------------------------------------


def test_cost_breakdown_total_is_sum_of_parts():
    br = ner.NetEdgeCostBreakdown(
        spread_cost=0.001,
        slippage_cost=0.0005,
        fees_cost=0.003,
        miss_prob_cost=0.0001,
        partial_fill_cost=0.00005,
    )
    expected = 0.001 + 0.0005 + 0.003 + 0.0001 + 0.00005
    assert br.total == pytest.approx(expected, rel=1e-9)


def test_fees_cost_is_higher_for_crypto_than_equity():
    ctx_eq = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=97.0,
    )
    ctx_cr = ner.NetEdgeSignalContext(
        ticker="BTC-USD", asset_class="crypto", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=97.0,
    )
    assert ner._fees_cost_fraction(ctx_cr) > ner._fees_cost_fraction(ctx_eq)


def test_miss_prob_and_partial_fill_are_nonnegative():
    ctx = ner.NetEdgeSignalContext(
        ticker="BTC-USD", asset_class="crypto", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=97.0,
    )
    assert ner._miss_prob_cost_fraction(ctx) >= 0.0
    assert ner._partial_fill_cost_fraction(ctx) >= 0.0


# ---------------------------------------------------------------------------
# Payoff / loss geometry
# ---------------------------------------------------------------------------


def test_fraction_to_stop_is_relative_to_entry():
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=97.0,
    )
    assert ner._fraction_to_stop(ctx) == pytest.approx(0.03, rel=1e-9)


def test_fraction_to_stop_zero_or_negative_entry_returns_none():
    ctx = ner.NetEdgeSignalContext(
        ticker="X", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=0.0, stop_price=10.0,
    )
    assert ner._fraction_to_stop(ctx) is None


def test_payoff_defaults_to_2R_when_no_target():
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=97.0,
    )
    loss = ner._fraction_to_stop(ctx)
    payoff = ner._payoff_fraction(ctx, loss)
    assert payoff == pytest.approx(2.0 * loss, rel=1e-9)


def test_explicit_target_price_overrides_2R_default():
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=97.0, target_price=110.0,
    )
    loss = ner._fraction_to_stop(ctx)
    payoff = ner._payoff_fraction(ctx, loss)
    assert payoff == pytest.approx(0.10, rel=1e-9)  # (110-100)/100


# ---------------------------------------------------------------------------
# End-to-end score composition (cold-start path, no DB outcomes needed)
# ---------------------------------------------------------------------------


def test_score_cold_start_produces_identity_calibration(db, shadow_mode):
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL",
        asset_class="stock",
        scan_pattern_id=None,
        raw_prob=0.65,
        entry_price=100.0,
        stop_price=97.0,
        target_price=103.0,
        regime="risk_on",
    )
    result = ner.score(db, ctx)
    assert result is not None
    assert result.provenance.cold_start is True
    assert result.provenance.calibrator_method == "cold_start"
    # Identity calibration -> calibrated prob matches raw prob.
    assert result.calibrated_prob == pytest.approx(0.65, rel=1e-9)


def test_score_composition_matches_formula(db, shadow_mode):
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL",
        asset_class="stock",
        scan_pattern_id=None,
        raw_prob=0.6,
        entry_price=100.0,
        stop_price=97.0,
        target_price=106.0,
        regime="risk_on",
    )
    result = ner.score(db, ctx)
    assert result is not None
    loss = 0.03
    payoff = 0.06
    p = result.calibrated_prob
    total_costs = result.costs.total
    expected = p * payoff - (1 - p) * loss - total_costs
    assert result.expected_net_pnl == pytest.approx(expected, rel=1e-9, abs=1e-9)


def test_score_zero_or_bad_stop_returns_none_and_does_not_log_score(db, shadow_mode, caplog):
    caplog.set_level(logging.INFO, logger="app.services.trading.net_edge_ranker")
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL",
        asset_class="stock",
        scan_pattern_id=None,
        raw_prob=0.6,
        entry_price=100.0,
        stop_price=100.0,  # no risk -> invalid
    )
    result = ner.score(db, ctx)
    assert result is None
    # Error ops-log line is emitted but no DB row written.
    assert any("read=error" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# DB side effects
# ---------------------------------------------------------------------------


def test_score_writes_log_row_in_shadow_mode(db, shadow_mode):
    from app.models.trading import NetEdgeScoreLog

    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL",
        asset_class="stock",
        scan_pattern_id=None,
        raw_prob=0.55,
        entry_price=100.0,
        stop_price=97.0,
        target_price=104.0,
        regime="risk_on",
        heuristic_score=0.01,
    )
    before = db.query(NetEdgeScoreLog).count()
    result = ner.score(db, ctx)
    assert result is not None
    after = db.query(NetEdgeScoreLog).count()
    assert after == before + 1
    row = db.query(NetEdgeScoreLog).order_by(NetEdgeScoreLog.id.desc()).first()
    assert row.ticker == "AAPL"
    assert row.mode == MODE_SHADOW
    assert row.asset_class == "stock"
    assert row.regime == "risk_on"


def test_score_is_disagreement_flag_trips_on_sign_flip(db, shadow_mode):
    from app.models.trading import NetEdgeScoreLog

    # Heuristic positive, stop tight and target weak -> likely negative net edge.
    ctx = ner.NetEdgeSignalContext(
        ticker="BTC-USD",
        asset_class="crypto",
        scan_pattern_id=None,
        raw_prob=0.35,  # low win prob
        entry_price=100.0,
        stop_price=97.0,
        target_price=100.5,  # tiny target
        regime="risk_off",
        heuristic_score=0.01,  # heuristic said positive
    )
    result = ner.score(db, ctx)
    assert result is not None
    assert result.expected_net_pnl < 0.0
    assert result.disagree_vs_heuristic is True
    row = db.query(NetEdgeScoreLog).order_by(NetEdgeScoreLog.id.desc()).first()
    assert bool(row.disagree_flag) is True


# ---------------------------------------------------------------------------
# Diagnostics endpoint + function
# ---------------------------------------------------------------------------


def test_diagnostics_shape_on_empty_db(db):
    payload = ner.diagnostics(db, lookback_hours=24)
    assert payload["ok"] is True
    assert payload["sample_count"] == 0
    assert payload["disagreement_rate"] is None
    assert payload["per_regime"] == []
    assert payload["last_calibration"] is None
    assert "mode" in payload and "lookback_hours" in payload


def test_diagnostics_aggregates_disagreement_rate(db, shadow_mode):
    # Two decisions: one disagreeing, one not. Disagreement rate should be 0.5.
    agree_ctx = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.7, entry_price=100.0, stop_price=97.0, target_price=110.0,
        regime="risk_on", heuristic_score=0.05,
    )
    disagree_ctx = ner.NetEdgeSignalContext(
        ticker="BTC-USD", asset_class="crypto", scan_pattern_id=None,
        raw_prob=0.30, entry_price=100.0, stop_price=97.0, target_price=100.5,
        regime="risk_off", heuristic_score=0.02,
    )
    r1 = ner.score(db, agree_ctx)
    r2 = ner.score(db, disagree_ctx)
    assert r1 is not None and r2 is not None
    payload = ner.diagnostics(db, lookback_hours=24)
    assert payload["sample_count"] == 2
    assert payload["disagreement_rate"] == pytest.approx(0.5, rel=1e-9)
    regimes = {item["regime"]: item for item in payload["per_regime"]}
    assert set(regimes.keys()) == {"risk_on", "risk_off"}


def test_diagnostics_endpoint_returns_frozen_shape(client, db):
    resp = client.get("/api/trading/brain/net-edge/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "net_edge_ranker" in body
    ne = body["net_edge_ranker"]
    # Frozen keys from diagnostics()
    for key in (
        "ok",
        "mode",
        "lookback_hours",
        "sample_count",
        "disagreement_rate",
        "per_regime",
        "last_calibration",
    ):
        assert key in ne, f"missing frozen key: {key}"
