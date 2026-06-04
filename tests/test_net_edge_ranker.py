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
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import Settings, settings
from app.models.trading import AutoTraderRun, PaperTrade, ScanPattern, Trade
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


def test_cost_breakdown_total_ignores_nonfinite_or_negative_costs():
    br = ner.NetEdgeCostBreakdown(
        spread_cost=float("inf"),
        slippage_cost=-0.01,
        fees_cost=float("nan"),
        miss_prob_cost=0.001,
        partial_fill_cost=True,
    )

    assert br.total == pytest.approx(0.001, rel=1e-9)


def test_spread_cost_defaults_malformed_adaptive_spread(monkeypatch):
    from app.services.trading import execution_quality

    monkeypatch.setattr(settings, "backtest_spread", 0.0007)
    monkeypatch.setattr(
        execution_quality,
        "suggest_adaptive_spread",
        lambda _db: {"suggested_spread": float("inf")},
    )
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=97.0,
    )

    assert ner._spread_cost_fraction(SimpleNamespace(), ctx) == pytest.approx(0.0007)


def test_miss_prob_cost_defaults_malformed_backtest_spread(monkeypatch):
    monkeypatch.setattr(settings, "backtest_spread", "NaN")
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=97.0,
    )

    expected = ner._DEFAULT_MISS_PROB_EQUITY * 0.0005
    assert ner._miss_prob_cost_fraction(ctx) == pytest.approx(expected, rel=1e-9)


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


def test_options_use_options_score_bucket_and_cost_defaults():
    ctx_op = ner.NetEdgeSignalContext(
        ticker="SPY", asset_class="robinhood_options", scan_pattern_id=None,
        raw_prob=0.5, entry_price=1.25, stop_price=0.75,
    )
    ctx_eq = ner.NetEdgeSignalContext(
        ticker="SPY", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=1.25, stop_price=0.75,
    )

    assert ner._score_asset_class(ctx_op.asset_class) == "options"
    assert ner._miss_prob_cost_fraction(ctx_op) > ner._miss_prob_cost_fraction(ctx_eq)
    assert ner._partial_fill_cost_fraction(ctx_op) > ner._partial_fill_cost_fraction(ctx_eq)


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


def test_fraction_to_stop_rejects_nonfinite_or_boolean_levels():
    ctx_nan = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=float("nan"), stop_price=97.0,
    )
    ctx_bool = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=True, stop_price=97.0,
    )

    assert ner._fraction_to_stop(ctx_nan) is None
    assert ner._fraction_to_stop(ctx_bool) is None


def test_fraction_to_stop_rejects_wrong_side_long_stop():
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=105.0,
        direction="long",
    )
    assert ner._fraction_to_stop(ctx) is None


def test_fraction_to_stop_supports_short_direction():
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=105.0,
        direction="short",
    )
    assert ner._fraction_to_stop(ctx) == pytest.approx(0.05, rel=1e-9)


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


def test_explicit_target_price_is_directional_for_short():
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=105.0,
        target_price=90.0, direction="short",
    )
    loss = ner._fraction_to_stop(ctx)
    payoff = ner._payoff_fraction(ctx, loss)
    assert loss == pytest.approx(0.05, rel=1e-9)
    assert payoff == pytest.approx(0.10, rel=1e-9)


def test_explicit_target_price_rejects_wrong_side_long_target():
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=97.0,
        target_price=90.0, direction="long",
    )
    loss = ner._fraction_to_stop(ctx)
    payoff = ner._payoff_fraction(ctx, loss)
    assert loss == pytest.approx(0.03, rel=1e-9)
    assert payoff == pytest.approx(0.0, abs=1e-12)


def test_explicit_target_price_rejects_nonfinite_target():
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL", asset_class="stock", scan_pattern_id=None,
        raw_prob=0.5, entry_price=100.0, stop_price=97.0,
        target_price=float("inf"),
    )

    assert ner._payoff_fraction(ctx, 0.03) == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# Calibrator finite-math guardrails
# ---------------------------------------------------------------------------


def test_calibrator_rejects_nonfinite_raw_probability():
    cal = ner._Calibrator(
        method="cold_start",
        sample_count=0,
        version_id="test",
        regime_bucket="risk_on",
        asset_class="stock",
        fit=None,
    )

    assert cal.apply(float("nan")) == pytest.approx(0.0, abs=1e-12)


def test_probability_parser_rejects_out_of_range_values():
    assert ner._probability_or_none(True) is None
    assert ner._probability_or_none(-0.01) is None
    assert ner._probability_or_none(1.01) is None
    assert ner._probability_or_none(0.55) == pytest.approx(0.55)


def test_calibrator_rejects_out_of_range_raw_probability():
    cal = ner._Calibrator(
        method="cold_start",
        sample_count=0,
        version_id="test",
        regime_bucket="risk_on",
        asset_class="stock",
        fit=None,
    )

    assert cal.apply(1.2) == pytest.approx(0.0, abs=1e-12)
    assert cal.apply(-0.2) == pytest.approx(0.0, abs=1e-12)


def test_calibrator_ignores_nonfinite_model_output():
    class _BadFit:
        def predict(self, _values):
            return [float("nan")]

    cal = ner._Calibrator(
        method="isotonic",
        sample_count=100,
        version_id="test",
        regime_bucket="risk_on",
        asset_class="stock",
        fit=_BadFit(),
    )

    assert cal.apply(0.7) == pytest.approx(0.7, rel=1e-9)


def test_calibrator_ignores_out_of_range_model_output():
    class _BadFit:
        def predict(self, _values):
            return [1.25]

    cal = ner._Calibrator(
        method="isotonic",
        sample_count=100,
        version_id="test",
        regime_bucket="risk_on",
        asset_class="stock",
        fit=_BadFit(),
    )

    assert cal.apply(0.7) == pytest.approx(0.7, rel=1e-9)


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


class _NetEdgeQuery:
    def __init__(self, rows, *, limit_sink=None):
        self.rows = rows
        self.limit_sink = limit_sink

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *args, **_kwargs):
        if self.limit_sink is not None and args:
            self.limit_sink.append(int(args[0]))
        return self

    def all(self):
        return list(self.rows)

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class _NetEdgeDb:
    def __init__(self, *, pattern, papers, runs=None):
        self.pattern = pattern
        self.papers = papers
        self.runs = runs or []
        self.added = []
        self.autotrader_run_limits: list[int] = []

    def query(self, model):
        if model is Trade:
            return _NetEdgeQuery([])
        if model is PaperTrade:
            return _NetEdgeQuery(self.papers)
        if model is ScanPattern:
            return _NetEdgeQuery([self.pattern])
        if model is AutoTraderRun:
            return _NetEdgeQuery(self.runs, limit_sink=self.autotrader_run_limits)
        raise AssertionError(f"unexpected model query: {model!r}")

    def add(self, row):
        self.added.append(row)

    def commit(self):
        return None

    def rollback(self):
        return None


def _drag_run(
    *,
    ticker: str = "BTC-USD",
    pattern_id: int = 42,
    user_id: int = 7,
    alert_id: int = 100,
    expected_net_pct: float | None = 3.0,
    decision: str = "blocked",
    reason: str = "broker:place_no_order_id",
    asset_class: str = "crypto",
):
    snapshot = {
        "asset_class": asset_class,
        "broker_reject_missing_order_id": True,
    }
    if expected_net_pct is not None:
        snapshot["entry_edge_expected_net_pct"] = expected_net_pct
    return SimpleNamespace(
        user_id=user_id,
        scan_pattern_id=pattern_id,
        breakout_alert_id=alert_id,
        ticker=ticker,
        decision=decision,
        reason=reason,
        rule_snapshot=snapshot,
        created_at=datetime.utcnow(),
    )


def _shadow_trade(
    *,
    alert_id: int,
    user_id: int = 7,
    pnl_pct: float | None = None,
    entry_price: float = 100.0,
    exit_price: float | None = None,
    shadow_decision: str = "blocked_no_order_id",
):
    if exit_price is None and pnl_pct is not None:
        exit_price = entry_price * (1.0 + pnl_pct / 100.0)
    return SimpleNamespace(
        user_id=user_id,
        paper_shadow_of_alert_id=alert_id,
        scan_pattern_id=42,
        ticker="BTC-USD",
        direction="long",
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=1.0,
        pnl_pct=pnl_pct,
        pnl=None,
        status="closed",
        entry_date=datetime.utcnow(),
        signal_json={
            "paper_shadow": True,
            "shadow_decision": shadow_decision,
            "asset_class": "crypto",
        },
    )


def _non_drag_run(*, ticker: str = "BTC-USD", pattern_id: int = 42, user_id: int = 7):
    return SimpleNamespace(
        user_id=user_id,
        scan_pattern_id=pattern_id,
        breakout_alert_id=101,
        ticker=ticker,
        decision="placed",
        reason="",
        rule_snapshot={"asset_class": "crypto"},
        created_at=datetime.utcnow(),
    )


def _enable_execution_drag_cost(monkeypatch, *, min_attempts=3, min_positive=2, cap=0.05):
    monkeypatch.setattr(settings, "brain_net_edge_execution_drag_cost_enabled", True, raising=False)
    monkeypatch.setattr(settings, "brain_net_edge_execution_drag_lookback_days", 7, raising=False)
    monkeypatch.setattr(settings, "brain_net_edge_execution_drag_min_attempts", min_attempts, raising=False)
    monkeypatch.setattr(settings, "brain_net_edge_execution_drag_min_positive_events", min_positive, raising=False)
    monkeypatch.setattr(settings, "brain_net_edge_execution_drag_cost_cap_fraction", cap, raising=False)


def test_execution_drag_cost_prices_positive_edge_missed_fills(monkeypatch):
    _enable_execution_drag_cost(monkeypatch)
    db = _NetEdgeDb(
        pattern=SimpleNamespace(id=42, asset_class="crypto", win_rate=0.6, oos_win_rate=None),
        papers=[],
        runs=[
            _drag_run(expected_net_pct=2.0),
            _drag_run(expected_net_pct=4.0),
            _non_drag_run(),
            _drag_run(pattern_id=99, expected_net_pct=20.0),
        ],
    )
    ctx = ner.NetEdgeSignalContext(
        ticker="BTC-USD",
        asset_class="crypto",
        scan_pattern_id=42,
        raw_prob=0.6,
        entry_price=100.0,
        stop_price=97.0,
        extra={"user_id": 7},
    )

    details = ner._execution_drag_cost_details(db, ctx)

    assert details["reason"] == "measured_execution_drag"
    assert details["attempts"] == 3
    assert details["positive_drag_events"] == 2
    assert details["penalized_positive_drag_events"] == 2
    assert details["unobserved_positive_drag_events"] == 2
    assert details["avg_positive_expected_net_pct"] == pytest.approx(3.0)
    assert details["drag_rate"] == pytest.approx(2 / 3)
    assert details["cost_fraction"] == pytest.approx((2 / 3) * 0.03)


def test_execution_drag_max_rows_setting_bounds_autotrader_scan(monkeypatch):
    monkeypatch.setenv("BRAIN_NET_EDGE_EXECUTION_DRAG_MAX_ROWS", "9")
    settings_obj = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr(ner, "settings", settings_obj)
    db = _NetEdgeDb(
        pattern=SimpleNamespace(id=42, asset_class="crypto", win_rate=0.6),
        papers=[],
        runs=[],
    )
    ctx = ner.NetEdgeSignalContext(
        ticker="BTC-USD",
        asset_class="crypto",
        scan_pattern_id=42,
        raw_prob=0.6,
        entry_price=100.0,
        stop_price=97.0,
        extra={"user_id": 7},
    )

    details = ner._execution_drag_cost_details(db, ctx)

    assert settings_obj.brain_net_edge_execution_drag_max_rows == 9
    assert db.autotrader_run_limits == [9]
    assert details["reason"] == "insufficient_attempts"


def test_execution_drag_cost_requires_positive_sample_floor(monkeypatch):
    _enable_execution_drag_cost(monkeypatch, min_attempts=3, min_positive=2)
    db = _NetEdgeDb(
        pattern=SimpleNamespace(id=42, asset_class="crypto", win_rate=0.6, oos_win_rate=None),
        papers=[],
        runs=[
            _drag_run(expected_net_pct=2.0),
            _non_drag_run(),
            _non_drag_run(),
        ],
    )
    ctx = ner.NetEdgeSignalContext(
        ticker="BTC-USD",
        asset_class="crypto",
        scan_pattern_id=42,
        raw_prob=0.6,
        entry_price=100.0,
        stop_price=97.0,
    )

    details = ner._execution_drag_cost_details(db, ctx)

    assert details["reason"] == "insufficient_positive_drag_events"
    assert details["cost_fraction"] == pytest.approx(0.0)


def test_execution_drag_cost_ignores_shadow_spared_losses(monkeypatch):
    _enable_execution_drag_cost(monkeypatch, min_attempts=3, min_positive=2)
    db = _NetEdgeDb(
        pattern=SimpleNamespace(id=42, asset_class="crypto", win_rate=0.6, oos_win_rate=None),
        papers=[
            _shadow_trade(alert_id=201, pnl_pct=-2.0),
            _shadow_trade(alert_id=202, pnl_pct=-1.0),
        ],
        runs=[
            _drag_run(alert_id=201, expected_net_pct=3.0),
            _drag_run(alert_id=202, expected_net_pct=4.0),
            _non_drag_run(),
        ],
    )
    ctx = ner.NetEdgeSignalContext(
        ticker="BTC-USD",
        asset_class="crypto",
        scan_pattern_id=42,
        raw_prob=0.6,
        entry_price=100.0,
        stop_price=97.0,
    )

    details = ner._execution_drag_cost_details(db, ctx)

    assert details["reason"] == "insufficient_shadow_adjusted_positive_drag_events"
    assert details["positive_drag_events"] == 2
    assert details["penalized_positive_drag_events"] == 0
    assert details["paper_shadow_sample_n"] == 2
    assert details["paper_shadow_spared_loss_events"] == 2
    assert details["cost_fraction"] == pytest.approx(0.0)


def test_execution_drag_cost_keeps_shadow_winners_and_unobserved_misses(monkeypatch):
    _enable_execution_drag_cost(monkeypatch, min_attempts=4, min_positive=2)
    db = _NetEdgeDb(
        pattern=SimpleNamespace(id=42, asset_class="crypto", win_rate=0.6, oos_win_rate=None),
        papers=[
            _shadow_trade(alert_id=301, pnl_pct=5.0),
            _shadow_trade(alert_id=302, pnl_pct=-2.0),
        ],
        runs=[
            _drag_run(alert_id=301, expected_net_pct=2.0),
            _drag_run(alert_id=302, expected_net_pct=4.0),
            _drag_run(alert_id=303, expected_net_pct=6.0),
            _non_drag_run(),
        ],
    )
    ctx = ner.NetEdgeSignalContext(
        ticker="BTC-USD",
        asset_class="crypto",
        scan_pattern_id=42,
        raw_prob=0.6,
        entry_price=100.0,
        stop_price=97.0,
    )

    details = ner._execution_drag_cost_details(db, ctx)

    assert details["reason"] == "measured_execution_drag"
    assert details["positive_drag_events"] == 3
    assert details["penalized_positive_drag_events"] == 2
    assert details["paper_shadow_confirmed_missed_alpha_events"] == 1
    assert details["paper_shadow_spared_loss_events"] == 1
    assert details["unobserved_positive_drag_events"] == 1
    assert details["avg_positive_expected_net_pct"] == pytest.approx(4.0)
    assert details["drag_rate"] == pytest.approx(0.5)
    assert details["raw_positive_drag_rate"] == pytest.approx(0.75)
    assert details["cost_fraction"] == pytest.approx(0.5 * 0.04)


def test_score_includes_execution_drag_cost_in_miss_cost_and_provenance(monkeypatch, shadow_mode):
    _enable_execution_drag_cost(monkeypatch)
    monkeypatch.setattr(settings, "backtest_spread", 0.0)
    monkeypatch.setattr(settings, "brain_net_edge_ops_log_enabled", False)
    db = _NetEdgeDb(
        pattern=SimpleNamespace(id=42, asset_class="crypto", win_rate=0.6, oos_win_rate=None),
        papers=[],
        runs=[
            _drag_run(expected_net_pct=2.0),
            _drag_run(expected_net_pct=4.0),
            _non_drag_run(),
        ],
    )
    ctx = ner.NetEdgeSignalContext(
        ticker="BTC-USD",
        asset_class="crypto",
        scan_pattern_id=42,
        raw_prob=0.6,
        entry_price=100.0,
        stop_price=97.0,
        target_price=106.0,
    )

    result = ner.score(db, ctx)

    assert result is not None
    drag_cost = (2 / 3) * 0.03
    assert result.costs.miss_prob_cost == pytest.approx(
        ner._miss_prob_cost_fraction(ctx) + drag_cost
    )
    assert result.provenance.execution_drag_cost_fraction == pytest.approx(drag_cost)
    assert result.provenance.execution_drag_sample["reason"] == "measured_execution_drag"
    assert db.added[-1].provenance_json["execution_drag_cost_fraction"] == pytest.approx(drag_cost)


def test_training_pairs_match_stock_asset_aliases():
    pat = SimpleNamespace(
        id=1,
        asset_class="stocks",
        win_rate=0.62,
        oos_win_rate=None,
    )
    db = _NetEdgeDb(
        pattern=pat,
        papers=[
            SimpleNamespace(
                scan_pattern_id=pat.id,
                ticker="AAPL",
                entry_price=100.0,
                exit_price=103.0,
                quantity=1.0,
                status="closed",
                entry_date=datetime.utcnow() - timedelta(minutes=5),
                exit_date=datetime.utcnow(),
            )
        ],
    )

    pairs = ner._load_training_pairs(
        db,
        asset_class="stock",
        regime_bucket="risk_on",
        lookback_days=1,
    )

    assert any(raw == pytest.approx(0.62) and win == 1 for raw, win in pairs)


def test_training_pairs_match_option_asset_aliases():
    pat = SimpleNamespace(
        id=1,
        asset_class="option",
        win_rate=0.62,
        oos_win_rate=None,
    )
    db = _NetEdgeDb(
        pattern=pat,
        papers=[
            SimpleNamespace(
                scan_pattern_id=pat.id,
                ticker="SPY",
                entry_price=1.25,
                exit_price=1.45,
                quantity=1.0,
                status="closed",
                entry_date=datetime.utcnow() - timedelta(minutes=5),
                exit_date=datetime.utcnow(),
            )
        ],
    )

    pairs = ner._load_training_pairs(
        db,
        asset_class="options",
        regime_bucket="risk_on",
        lookback_days=1,
    )

    assert any(raw == pytest.approx(0.62) and win == 1 for raw, win in pairs)


def test_pattern_raw_probability_rejects_boolean_before_percent_fallback():
    pat = SimpleNamespace(
        id=1,
        asset_class="stocks",
        oos_win_rate=True,
        win_rate=55.0,
    )

    assert ner._pattern_raw_prob(pat) == pytest.approx(0.55)


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


def test_score_wrong_side_long_stop_returns_none_without_db_read(shadow_mode, caplog):
    caplog.set_level(logging.INFO, logger="app.services.trading.net_edge_ranker")
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL",
        asset_class="stock",
        scan_pattern_id=None,
        raw_prob=0.6,
        entry_price=100.0,
        stop_price=105.0,
        direction="long",
    )

    result = ner.score(SimpleNamespace(), ctx)

    assert result is None
    assert any("read=error" in rec.getMessage() for rec in caplog.records)


def test_score_nonfinite_raw_probability_returns_none_without_db_read(shadow_mode, caplog):
    caplog.set_level(logging.INFO, logger="app.services.trading.net_edge_ranker")
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL",
        asset_class="stock",
        scan_pattern_id=None,
        raw_prob=float("nan"),
        entry_price=100.0,
        stop_price=97.0,
    )

    result = ner.score(SimpleNamespace(), ctx)

    assert result is None
    assert any("read=error" in rec.getMessage() for rec in caplog.records)


def test_score_out_of_range_raw_probability_returns_none_without_db_read(shadow_mode, caplog):
    caplog.set_level(logging.INFO, logger="app.services.trading.net_edge_ranker")
    ctx = ner.NetEdgeSignalContext(
        ticker="AAPL",
        asset_class="stock",
        scan_pattern_id=None,
        raw_prob=1.2,
        entry_price=100.0,
        stop_price=97.0,
    )

    result = ner.score(SimpleNamespace(), ctx)

    assert result is None
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
