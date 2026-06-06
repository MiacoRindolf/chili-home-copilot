"""Data-derived backtest execution cost (backtest<->live parity).

The backtest must charge the system's OWN measured realized round-trip execution
cost per asset class (incl. venue fees), derived from data — no magic numbers.
"""
from __future__ import annotations

from app.models.trading import VenueTruthLog


def _vt(ticker, cost, paper=False):
    return VenueTruthLog(
        ticker=ticker, side="buy", notional_usd=1000.0,
        realized_cost_fraction=cost, paper_bool=paper, mode="shadow",
    )


def test_backtest_costs_for_ticker_routing():
    from app.services.trading.backtest_execution_cost import backtest_costs_for_ticker
    acc = {"crypto": {"spread": 0.0, "commission": 0.008}, "equity": {"spread": 0.0, "commission": 0.001}}
    assert backtest_costs_for_ticker("ETH-USD", acc) == (0.0, 0.008)
    assert backtest_costs_for_ticker("AAPL", acc) == (0.0, 0.001)
    assert backtest_costs_for_ticker("BTC-USD", None) is None
    assert backtest_costs_for_ticker("AAPL", {"crypto": {"spread": 0, "commission": 0.008}}) is None


def test_derive_costs_from_measured_venue_truth(db):
    # >= min samples (8) measured crypto + equity realized-cost observations
    for _ in range(10):
        db.add(_vt("BTC-USD", 0.016))   # 1.6% crypto round-trip (fees dominate)
    for _ in range(10):
        db.add(_vt("AAPL", 0.002))      # 0.2% equity (cheap)
    db.add(_vt("ETH-USD", 0.5, paper=True))  # a PAPER row must be ignored
    db.commit()

    from app.services.trading.backtest_execution_cost import derive_asset_class_backtest_costs
    costs = derive_asset_class_backtest_costs(db)

    assert costs["crypto"] is not None
    assert abs(costs["crypto"]["round_trip_cost_fraction"] - 0.016) < 1e-9
    assert abs(costs["crypto"]["commission"] - 0.008) < 1e-9   # per-leg = half round-trip
    assert "venue_truth_log" in costs["crypto"]["source"]
    assert costs["equity"] is not None
    assert abs(costs["equity"]["round_trip_cost_fraction"] - 0.002) < 1e-9


def test_thin_data_yields_none_so_caller_keeps_fallback(db):
    # below the sample-size guard and no cost-estimates -> None (no fabricated cost)
    for _ in range(3):
        db.add(_vt("DOGE-USD", 0.02))
    db.commit()
    from app.services.trading.backtest_execution_cost import derive_asset_class_backtest_costs
    costs = derive_asset_class_backtest_costs(db)
    assert costs["crypto"] is None
    assert costs["equity"] is None


# ── Fix 4: realized-cost feedback bias (measured, bounded, upward-only) ──────

def _vt_gap(ticker, expected, realized, n=8):
    from app.models.trading import VenueTruthLog
    return [VenueTruthLog(ticker=ticker, side="buy", notional_usd=1000.0,
                          expected_cost_fraction=expected, realized_cost_fraction=realized,
                          paper_bool=False, mode="shadow") for _ in range(n)]


def test_feedback_bias_measured_gap(db):
    for r in _vt_gap("SOL-USD", expected=0.005, realized=0.008):
        db.add(r)
    db.commit()
    from app.services.trading.backtest_execution_cost import realized_cost_bias_fraction
    bias, snap = realized_cost_bias_fraction(db, "SOL-USD", max_fraction=0.005, min_obs=5, lookback_days=30)
    assert abs(bias - 0.003) < 1e-9   # measured gap 0.008-0.005=0.003, under the clamp
    assert snap["used"] is True


def test_feedback_bias_clamped(db):
    for r in _vt_gap("DOGE-USD", expected=0.001, realized=0.02):
        db.add(r)
    db.commit()
    from app.services.trading.backtest_execution_cost import realized_cost_bias_fraction
    bias, _ = realized_cost_bias_fraction(db, "DOGE-USD", max_fraction=0.005, min_obs=5, lookback_days=30)
    assert bias == 0.005   # gap 0.019 clamped to the 0.005 max


def test_feedback_bias_upward_only(db):
    # realized BETTER than expected (negative gap) -> no deflation
    for r in _vt_gap("BTC-USD", expected=0.01, realized=0.005):
        db.add(r)
    db.commit()
    from app.services.trading.backtest_execution_cost import realized_cost_bias_fraction
    bias, _ = realized_cost_bias_fraction(db, "BTC-USD", max_fraction=0.005, min_obs=5, lookback_days=30)
    assert bias == 0.0


def test_feedback_bias_needs_min_obs(db):
    for r in _vt_gap("ETH-USD", expected=0.005, realized=0.01, n=3):
        db.add(r)
    db.commit()
    from app.services.trading.backtest_execution_cost import realized_cost_bias_fraction
    bias, snap = realized_cost_bias_fraction(db, "ETH-USD", max_fraction=0.005, min_obs=5, lookback_days=30)
    assert bias == 0.0 and snap["used"] is False
