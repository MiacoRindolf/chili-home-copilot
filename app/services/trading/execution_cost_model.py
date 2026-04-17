"""Execution-cost model (pure math, no I/O, no DB) — Phase F.

Given a rolling cost estimate for a (ticker, side, window) pair, return
the **fraction of notional** expected to be paid in round-trip costs and
the maximum notional the venue can absorb without us exceeding a
configurable share of average daily volume.

Design goals:
  * Pure: takes a plain dict or dataclass-like object, not an ORM row.
  * Deterministic: same inputs → same outputs.
  * Conservative by default: uses p90 spread/slippage so NetEdgeRanker
    decisions assume the worse-but-still-plausible case.

Used by:
  * NetEdgeRanker (Phase E) — to price `costs` in
    ``expected_net_pnl = p * payoff - costs``.
  * Backtests — to replace the flat `backtest_spread` with a per-ticker
    estimate when `brain_execution_cost_mode` is not ``off``.
  * Position sizer (Phase H, later) — to cap size at
    ``estimate_capacity_usd`` so we don't try to trade 30% of daily volume.

This module is shadow-safe: the cost model is never invoked until an
explicit caller opts in. No side effects, no logging.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


# ── Public dataclasses ─────────────────────────────────────────────────

@dataclass(frozen=True)
class CostFractionBreakdown:
    """Round-trip execution cost expressed as a fraction of notional.

    All fields are non-negative fractions of the trade's notional (e.g.
    ``0.001`` = 10 bps). ``total`` is the sum of the other four
    components.
    """
    spread: float
    slippage: float
    fees: float
    impact: float
    total: float


# ── Helpers ────────────────────────────────────────────────────────────

def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        if math.isfinite(f):
            return f
    except (TypeError, ValueError):
        pass
    return default


def _get(row: Mapping[str, Any] | Any, key: str, default: float = 0.0) -> float:
    """Read a numeric field from either a dict or an ORM row."""
    if row is None:
        return default
    if isinstance(row, Mapping):
        return _as_float(row.get(key), default)
    return _as_float(getattr(row, key, default), default)


# ── Core API ───────────────────────────────────────────────────────────

def estimate_cost_fraction(
    ticker: str,
    side: str,
    notional_usd: float,
    estimate_row: Mapping[str, Any] | Any,
    *,
    fee_bps: float = 1.0,
    impact_cap_bps: float = 50.0,
    use_p90: bool = True,
) -> CostFractionBreakdown:
    """Return the expected round-trip cost as a fraction of ``notional_usd``.

    Parameters
    ----------
    ticker, side:
        Logged only; not used for the math (the estimate_row is already
        per-ticker, per-side).
    notional_usd:
        Absolute dollar notional of the planned trade (``abs`` is applied
        defensively).
    estimate_row:
        ``ExecutionCostEstimate`` ORM row or an equivalent dict with at
        least ``median_spread_bps``, ``p90_spread_bps``,
        ``median_slippage_bps``, ``p90_slippage_bps``,
        ``avg_daily_volume_usd``, ``sample_trades``.
    fee_bps:
        Flat fee assumption applied round-trip (bps of notional).
    impact_cap_bps:
        Hard cap on the impact component so pathological ADVs don't blow
        up the estimate.
    use_p90:
        When True (default) we price the conservative case. Setting to
        False uses median (less conservative; typically for diagnostics
        or backtest-expected-cost displays).

    Returns
    -------
    CostFractionBreakdown with all four components and the sum.

    Notes
    -----
    * If the estimate has ``sample_trades <= 0`` we return a zero
      breakdown (caller can decide to fall back to a global default).
    * Impact model: ``impact_bps = sqrt(notional / adv) * 10``, capped
      at ``impact_cap_bps``. This is the classic Almgren-Chriss
      proportional-to-sqrt(participation) heuristic, calibrated loosely
      to 10bps at 1% of ADV.
    """
    notional = abs(_as_float(notional_usd))
    sample = int(_get(estimate_row, "sample_trades", 0))

    if notional <= 0 or sample <= 0:
        return CostFractionBreakdown(0.0, 0.0, 0.0, 0.0, 0.0)

    spread_bps_key = "p90_spread_bps" if use_p90 else "median_spread_bps"
    slip_bps_key = "p90_slippage_bps" if use_p90 else "median_slippage_bps"

    spread_bps = max(0.0, _get(estimate_row, spread_bps_key, 0.0))
    slippage_bps = max(0.0, _get(estimate_row, slip_bps_key, 0.0))
    adv_usd = max(0.0, _get(estimate_row, "avg_daily_volume_usd", 0.0))
    fee_bps_f = max(0.0, _as_float(fee_bps))
    cap_bps = max(0.0, _as_float(impact_cap_bps))

    if adv_usd > 0:
        participation = notional / adv_usd
        impact_bps = min(cap_bps, math.sqrt(max(participation, 0.0)) * 10.0)
    else:
        impact_bps = cap_bps

    spread_frac = spread_bps / 10_000.0
    slippage_frac = slippage_bps / 10_000.0
    fees_frac = fee_bps_f / 10_000.0
    impact_frac = impact_bps / 10_000.0
    total = spread_frac + slippage_frac + fees_frac + impact_frac

    return CostFractionBreakdown(
        spread=spread_frac,
        slippage=slippage_frac,
        fees=fees_frac,
        impact=impact_frac,
        total=total,
    )


def estimate_capacity_usd(
    estimate_row: Mapping[str, Any] | Any,
    *,
    max_adv_frac: float = 0.05,
) -> float:
    """Max notional we can deploy without exceeding ``max_adv_frac`` of ADV.

    Returns 0.0 if the estimate row is missing / stale (no ADV info).
    Callers may choose to fall back to a global default when 0.
    """
    adv_usd = max(0.0, _get(estimate_row, "avg_daily_volume_usd", 0.0))
    frac = max(0.0, min(1.0, _as_float(max_adv_frac)))
    return adv_usd * frac


__all__ = [
    "CostFractionBreakdown",
    "estimate_cost_fraction",
    "estimate_capacity_usd",
]
