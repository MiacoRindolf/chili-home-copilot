from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.portfolio_risk import (
    RiskLimits,
    compute_trade_risk_pct,
    get_risk_limits,
    size_position,
)


def _limits() -> RiskLimits:
    return RiskLimits(max_risk_per_trade_pct=1.0)


def test_size_position_rejects_stop_not_below_long_entry():
    assert size_position(10_000.0, 100.0, 100.0, limits=_limits()) == 0
    assert size_position(10_000.0, 100.0, 105.0, limits=_limits()) == 0


def test_size_position_rejects_missing_or_nonfinite_inputs():
    assert size_position(10_000.0, 100.0, 0.0, limits=_limits()) == 0
    assert size_position(float("nan"), 100.0, 95.0, limits=_limits()) == 0
    assert size_position(10_000.0, float("inf"), 95.0, limits=_limits()) == 0
    assert size_position(10_000.0, 100.0, 95.0, risk_pct=0.0, limits=_limits()) == 0


def test_size_position_valid_long_stop_preserves_fixed_fractional_sizing():
    # 1% of 10k is $100 risk. With a $5 stop distance, size is 20 shares.
    assert size_position(10_000.0, 100.0, 95.0, limits=_limits()) == 20


def test_compute_trade_risk_pct_is_direction_aware_not_absolute():
    assert compute_trade_risk_pct(100.0, 105.0, 10.0, 10_000.0) == 0.0
    assert compute_trade_risk_pct(
        100.0,
        105.0,
        10.0,
        10_000.0,
        direction="short",
    ) == 0.5


def test_compute_trade_risk_pct_rejects_nonfinite_inputs():
    assert compute_trade_risk_pct(float("inf"), 95.0, 10.0, 10_000.0) == 0.0
    assert compute_trade_risk_pct(100.0, 95.0, float("nan"), 10_000.0) == 0.0


def test_compute_trade_risk_pct_rejects_boolean_inputs():
    assert compute_trade_risk_pct(True, 0.95, 10.0, 10_000.0) == 0.0
    assert compute_trade_risk_pct(100.0, 95.0, True, 10_000.0) == 0.0


def test_get_risk_limits_defaults_malformed_settings_without_raising():
    limits = get_risk_limits(
        SimpleNamespace(
            brain_risk_max_positions="bad",
            brain_risk_max_crypto=True,
            brain_risk_max_stocks="NaN",
            brain_risk_max_heat_pct="Infinity",
            brain_risk_per_trade_pct=-1.0,
            brain_risk_max_same_ticker="",
            brain_risk_max_sector_pct=500.0,
            brain_risk_max_avg_correlation=2.0,
        )
    )

    assert limits == RiskLimits()


def test_get_risk_limits_preserves_explicit_zero_as_restrictive():
    limits = get_risk_limits(
        SimpleNamespace(
            brain_risk_max_positions=0,
            brain_risk_max_crypto=0,
            brain_risk_max_stocks=0,
            brain_risk_max_heat_pct=0.0,
            brain_risk_per_trade_pct=0.0,
            brain_risk_max_same_ticker=0,
            brain_risk_max_sector_pct=0.0,
            brain_risk_max_avg_correlation=0.0,
        )
    )

    assert limits.max_open_positions == 0
    assert limits.max_crypto_positions == 0
    assert limits.max_stock_positions == 0
    assert limits.max_portfolio_heat_pct == 0.0
    assert limits.max_risk_per_trade_pct == 0.0
    assert limits.max_same_ticker == 0
    assert limits.max_sector_pct == 0.0
    assert limits.max_avg_correlation == 0.0
