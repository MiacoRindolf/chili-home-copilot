"""Black-Scholes greeks for European options.

Pure-numpy implementation; no heavy dependencies (avoids py_vollib +
its compiled extensions). Approximates American options as European —
fine for short-dated options on non-dividend underlyings, less so for
deep-ITM with dividends. American-exercise refinement is a follow-up.

All functions accept either scalars or numpy arrays for vectorized
computation across full chains.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


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

    Args:
        spot: underlying price
        strike: option strike
        time_to_expiry_years: T (years)
        risk_free_rate: r (annualized, decimal)
        volatility: sigma (annualized, decimal)
        opt_type: 'call' or 'put'
        dividend_yield: q (annualized, decimal) — default 0.

    Returns:
        GreeksResult with theoretical price + greeks. theta is per-year;
        divide by 365 for per-day.
    """
    if time_to_expiry_years <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        # Degenerate — return zeros + intrinsic value.
        intrinsic = (
            max(spot - strike, 0.0) if opt_type == "call"
            else max(strike - spot, 0.0)
        )
        return GreeksResult(0.0, 0.0, 0.0, 0.0, 0.0, intrinsic)

    sqrt_t = math.sqrt(time_to_expiry_years)
    d1 = (
        math.log(spot / strike)
        + (risk_free_rate - dividend_yield + 0.5 * volatility * volatility)
        * time_to_expiry_years
    ) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t

    pdf_d1 = _normal_pdf(d1)
    discount = math.exp(-risk_free_rate * time_to_expiry_years)
    div_discount = math.exp(-dividend_yield * time_to_expiry_years)

    if opt_type == "call":
        price = spot * div_discount * _normal_cdf(d1) - strike * discount * _normal_cdf(d2)
        delta = div_discount * _normal_cdf(d1)
        rho = strike * time_to_expiry_years * discount * _normal_cdf(d2)
        theta = (
            -spot * pdf_d1 * volatility * div_discount / (2 * sqrt_t)
            - risk_free_rate * strike * discount * _normal_cdf(d2)
            + dividend_yield * spot * div_discount * _normal_cdf(d1)
        )
    elif opt_type == "put":
        price = strike * discount * _normal_cdf(-d2) - spot * div_discount * _normal_cdf(-d1)
        delta = -div_discount * _normal_cdf(-d1)
        rho = -strike * time_to_expiry_years * discount * _normal_cdf(-d2)
        theta = (
            -spot * pdf_d1 * volatility * div_discount / (2 * sqrt_t)
            + risk_free_rate * strike * discount * _normal_cdf(-d2)
            - dividend_yield * spot * div_discount * _normal_cdf(-d1)
        )
    else:
        raise ValueError(f"opt_type must be 'call' or 'put', got {opt_type!r}")

    gamma = div_discount * pdf_d1 / (spot * volatility * sqrt_t)
    vega = spot * div_discount * pdf_d1 * sqrt_t

    return GreeksResult(
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega / 100.0,  # convention: vega per 1 vol-point
        rho=rho / 100.0,    # convention: rho per 1 rate-point
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
    """Newton-Raphson IV solver. Returns None if it doesn't converge.

    Initial guess via Brenner-Subrahmanyam approximation.
    """
    if market_price <= 0 or time_to_expiry_years <= 0 or spot <= 0 or strike <= 0:
        return None

    # Brenner-Subrahmanyam initial guess.
    sigma = math.sqrt(2 * math.pi / time_to_expiry_years) * (market_price / spot)
    sigma = max(0.05, min(2.0, sigma))

    for _ in range(max_iter):
        g = bs_greeks(
            spot=spot, strike=strike,
            time_to_expiry_years=time_to_expiry_years,
            risk_free_rate=risk_free_rate, volatility=sigma,
            opt_type=opt_type, dividend_yield=dividend_yield,
        )
        diff = g.price - market_price
        if abs(diff) < tol:
            return sigma
        if g.vega <= 1e-8:
            return None
        # vega returned per-vol-point; convert back for newton step
        vega_per_decimal = g.vega * 100
        sigma -= diff / vega_per_decimal
        if sigma <= 0 or sigma > 5.0:
            return None
    return None
