"""Black-Scholes greeks for European options.

Pure-Python implementation; no heavy dependencies. Approximates American
options as European, which is acceptable for this guardrail layer. The
functions are intentionally fail-closed on malformed market inputs so NaN,
Inf, or boolean values cannot leak into option risk budgets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


def _finite_float_or_none(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _positive_float_or_none(value) -> float | None:
    out = _finite_float_or_none(value)
    if out is None or out <= 0.0:
        return None
    return out


def _normalize_option_type(value) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {"c", "call"}:
        return "call"
    if raw in {"p", "put"}:
        return "put"
    return None


def _normal_cdf(x: float) -> float:
    """Standard-normal CDF via erfc (no scipy dependency)."""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _normal_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass
class GreeksResult:
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    price: float


def bs_greeks(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    risk_free_rate: float,
    volatility: float,
    opt_type: str,
    dividend_yield: float = 0.0,
) -> GreeksResult:
    """Black-Scholes-Merton greeks.

    Theta is per-year. Vega is returned per one volatility point and rho per
    one rate point.
    """
    opt = _normalize_option_type(opt_type)
    if opt is None:
        raise ValueError(f"opt_type must be 'call' or 'put', got {opt_type!r}")

    spot_f = _positive_float_or_none(spot)
    strike_f = _positive_float_or_none(strike)
    t_f = _positive_float_or_none(time_to_expiry_years)
    vol_f = _positive_float_or_none(volatility)
    rate_f = _finite_float_or_none(risk_free_rate)
    div_f = _finite_float_or_none(dividend_yield)

    if spot_f is None or strike_f is None:
        return GreeksResult(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    if t_f is None or vol_f is None:
        intrinsic = (
            max(spot_f - strike_f, 0.0)
            if opt == "call"
            else max(strike_f - spot_f, 0.0)
        )
        return GreeksResult(0.0, 0.0, 0.0, 0.0, 0.0, intrinsic)
    if rate_f is None:
        rate_f = 0.0
    if div_f is None:
        div_f = 0.0

    sqrt_t = math.sqrt(t_f)
    d1 = (
        math.log(spot_f / strike_f)
        + (rate_f - div_f + 0.5 * vol_f * vol_f) * t_f
    ) / (vol_f * sqrt_t)
    d2 = d1 - vol_f * sqrt_t

    pdf_d1 = _normal_pdf(d1)
    discount = math.exp(-rate_f * t_f)
    div_discount = math.exp(-div_f * t_f)

    if opt == "call":
        price = spot_f * div_discount * _normal_cdf(d1) - strike_f * discount * _normal_cdf(d2)
        delta = div_discount * _normal_cdf(d1)
        rho = strike_f * t_f * discount * _normal_cdf(d2)
        theta = (
            -spot_f * pdf_d1 * vol_f * div_discount / (2 * sqrt_t)
            - rate_f * strike_f * discount * _normal_cdf(d2)
            + div_f * spot_f * div_discount * _normal_cdf(d1)
        )
    else:
        price = strike_f * discount * _normal_cdf(-d2) - spot_f * div_discount * _normal_cdf(-d1)
        delta = -div_discount * _normal_cdf(-d1)
        rho = -strike_f * t_f * discount * _normal_cdf(-d2)
        theta = (
            -spot_f * pdf_d1 * vol_f * div_discount / (2 * sqrt_t)
            + rate_f * strike_f * discount * _normal_cdf(-d2)
            - div_f * spot_f * div_discount * _normal_cdf(-d1)
        )

    gamma = div_discount * pdf_d1 / (spot_f * vol_f * sqrt_t)
    vega = spot_f * div_discount * pdf_d1 * sqrt_t

    return GreeksResult(
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega / 100.0,
        rho=rho / 100.0,
        price=price,
    )


def implied_vol(
    *,
    market_price: float,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    risk_free_rate: float,
    opt_type: str,
    dividend_yield: float = 0.0,
    tol: float = 1e-5,
    max_iter: int = 100,
) -> Optional[float]:
    """Newton-Raphson IV solver. Returns None if it cannot converge."""
    opt = _normalize_option_type(opt_type)
    market_price_f = _positive_float_or_none(market_price)
    spot_f = _positive_float_or_none(spot)
    strike_f = _positive_float_or_none(strike)
    t_f = _positive_float_or_none(time_to_expiry_years)
    rate_f = _finite_float_or_none(risk_free_rate)
    div_f = _finite_float_or_none(dividend_yield)
    tol_f = _positive_float_or_none(tol) or 1e-5
    if (
        opt is None
        or market_price_f is None
        or spot_f is None
        or strike_f is None
        or t_f is None
        or rate_f is None
        or div_f is None
    ):
        return None
    if max_iter is None or isinstance(max_iter, bool):
        return None
    try:
        max_iter_i = int(max_iter)
    except (TypeError, ValueError):
        return None
    if max_iter_i <= 0:
        return None

    sigma = math.sqrt(2 * math.pi / t_f) * (market_price_f / spot_f)
    sigma = max(0.05, min(2.0, sigma))

    for _ in range(max_iter_i):
        g = bs_greeks(
            spot=spot_f,
            strike=strike_f,
            time_to_expiry_years=t_f,
            risk_free_rate=rate_f,
            volatility=sigma,
            opt_type=opt,
            dividend_yield=div_f,
        )
        diff = g.price - market_price_f
        if abs(diff) < tol_f:
            return sigma
        if g.vega <= 1e-8:
            return None
        vega_per_decimal = g.vega * 100.0
        sigma -= diff / vega_per_decimal
        if sigma <= 0 or sigma > 5.0 or not math.isfinite(sigma):
            return None
    return None
