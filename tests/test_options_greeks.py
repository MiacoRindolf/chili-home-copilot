from __future__ import annotations

import math

import pytest

from app.services.trading.options.greeks import bs_greeks, implied_vol


def test_bs_greeks_returns_finite_values_for_valid_call() -> None:
    result = bs_greeks(
        spot=100.0,
        strike=105.0,
        time_to_expiry_years=30.0 / 365.0,
        risk_free_rate=0.04,
        volatility=0.25,
        opt_type="call",
    )

    assert result.price > 0.0
    assert 0.0 < result.delta < 1.0
    assert all(
        math.isfinite(value)
        for value in (
            result.delta,
            result.gamma,
            result.theta,
            result.vega,
            result.rho,
            result.price,
        )
    )


@pytest.mark.parametrize(
    "bad_input",
    [
        {"spot": float("nan")},
        {"spot": True},
        {"strike": float("inf")},
        {"time_to_expiry_years": float("nan")},
        {"volatility": True},
        {"risk_free_rate": float("inf")},
        {"dividend_yield": float("nan")},
    ],
)
def test_bs_greeks_rejects_bad_numeric_inputs_without_nan_output(bad_input) -> None:
    kwargs = dict(
        spot=100.0,
        strike=105.0,
        time_to_expiry_years=30.0 / 365.0,
        risk_free_rate=0.04,
        volatility=0.25,
        opt_type="call",
        dividend_yield=0.0,
    )
    kwargs.update(bad_input)

    result = bs_greeks(**kwargs)

    assert all(
        math.isfinite(value)
        for value in (
            result.delta,
            result.gamma,
            result.theta,
            result.vega,
            result.rho,
            result.price,
        )
    )


def test_bs_greeks_returns_intrinsic_for_degenerate_valid_contract() -> None:
    call = bs_greeks(
        spot=110.0,
        strike=105.0,
        time_to_expiry_years=0.0,
        risk_free_rate=0.04,
        volatility=0.25,
        opt_type="call",
    )
    put = bs_greeks(
        spot=100.0,
        strike=105.0,
        time_to_expiry_years=30.0 / 365.0,
        risk_free_rate=0.04,
        volatility=0.0,
        opt_type="put",
    )

    assert call.price == pytest.approx(5.0)
    assert put.price == pytest.approx(5.0)
    assert call.delta == 0.0
    assert put.vega == 0.0


def test_bs_greeks_rejects_invalid_option_type_before_degenerate_fallback() -> None:
    with pytest.raises(ValueError, match="opt_type"):
        bs_greeks(
            spot=100.0,
            strike=105.0,
            time_to_expiry_years=0.0,
            risk_free_rate=0.04,
            volatility=0.25,
            opt_type="straddle",
        )


def test_implied_vol_recovers_known_volatility() -> None:
    price = bs_greeks(
        spot=100.0,
        strike=105.0,
        time_to_expiry_years=45.0 / 365.0,
        risk_free_rate=0.04,
        volatility=0.30,
        opt_type="call",
    ).price

    iv = implied_vol(
        market_price=price,
        spot=100.0,
        strike=105.0,
        time_to_expiry_years=45.0 / 365.0,
        risk_free_rate=0.04,
        opt_type="call",
    )

    assert iv == pytest.approx(0.30, abs=1e-4)


@pytest.mark.parametrize(
    "bad_input",
    [
        {"market_price": True},
        {"market_price": float("nan")},
        {"spot": float("inf")},
        {"strike": True},
        {"time_to_expiry_years": float("nan")},
        {"risk_free_rate": float("inf")},
        {"opt_type": "bad"},
        {"max_iter": True},
        {"max_iter": 0},
    ],
)
def test_implied_vol_rejects_bad_inputs(bad_input) -> None:
    kwargs = dict(
        market_price=2.0,
        spot=100.0,
        strike=105.0,
        time_to_expiry_years=45.0 / 365.0,
        risk_free_rate=0.04,
        opt_type="call",
    )
    kwargs.update(bad_input)

    assert implied_vol(**kwargs) is None
