"""Simulated paper fills: spread, slippage, fee estimates (Phase 7)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ....config import settings


@dataclass
class SyntheticQuote:
    mid: float
    bid: float
    ask: float
    source: str


def regime_atr_pct(regime_json: dict[str, Any]) -> float:
    """Resolve ATR as fraction of price from regime snapshot (top-level or nested meta)."""
    raw = regime_json.get("atr_pct")
    if raw is None and isinstance(regime_json.get("meta"), dict):
        raw = regime_json["meta"].get("atr_pct")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.015
    return max(0.004, min(v, 0.12))


def default_reference_mid(
    *,
    viability_score: float,
    symbol: str,
    quote_mid: Optional[float],
) -> float:
    if quote_mid is not None and quote_mid > 0:
        return float(quote_mid)
    # Deterministic stub for tests / offline (not a price oracle).
    h = abs(hash(symbol)) % 1000
    return 50.0 + float(h) / 10.0 + float(viability_score)


def build_synthetic_quote(
    mid: float,
    spread_bps: float,
    *,
    source: str = "synthetic",
) -> SyntheticQuote:
    half = mid * (float(spread_bps) / 2.0) / 10_000.0
    return SyntheticQuote(mid=mid, bid=mid - half, ask=mid + half, source=source)


def long_entry_fill_price(ask: float, mid: float, slippage_bps: float) -> float:
    slip = mid * float(slippage_bps) / 10_000.0
    return ask + slip


def long_exit_fill_price(bid: float, mid: float, slippage_bps: float) -> float:
    slip = mid * float(slippage_bps) / 10_000.0
    return max(1e-12, bid - slip)


def roundtrip_fee_usd(
    notional: float,
    fee_to_target_ratio: float,
    *,
    entry: float = 0.0,
    target: float = 0.0,
) -> float:
    """Estimate round-trip fees for a paper trade.

    ``fee_to_target_ratio`` is the fraction of *expected target profit*
    consumed by fees (e.g. 0.08 = 8 % of target PnL).  When ``entry``
    and ``target`` are supplied we compute fees from the target P&L;
    otherwise fall back to a conservative 0.5 % per-side exchange rate.
    """
    r = float(fee_to_target_ratio)
    if entry > 0 and target > 0 and entry != target:
        qty = abs(notional) / entry if entry else 0.0
        expected_target_pnl = abs(target - entry) * qty
        return max(0.0, expected_target_pnl * r)
    # Conservative per-side estimate when target unknown (tiered venues ~0.04–0.6%).
    return abs(notional) * 0.0025 * 2.0


def stop_target_prices(
    entry: float,
    *,
    atr_pct: float,
    side_long: bool = True,
    stop_atr_mult: float = 0.60,
    target_atr_mult: float = 0.90,  # legacy; superseded by reward_risk below
    reward_risk: float | None = None,
) -> tuple[float, float]:
    """ATR-scaled STOP + a reward:risk-anchored TARGET (Ross-style, >= 2:1).

    The TARGET is derived from the ACTUAL stop distance x a reward:risk multiple
    (not an independent ATR mult), so R:R is explicit and at least the documented
    floor — fixing the old ~1.3-1.5:1 (target_atr 0.90 vs stop_atr 0.60) that sat
    below Ross's strict 2:1. ``reward_risk`` defaults to
    chili_momentum_risk_reward_risk_ratio (2.0) — the single documented, learnable
    R:R knob (Ross = floor, the learner can raise it). docs/DESIGN/MOMENTUM_LANE.md
    """
    try:
        rr = float(reward_risk) if reward_risk is not None else float(
            getattr(settings, "chili_momentum_risk_reward_risk_ratio", 2.0) or 2.0
        )
    except (TypeError, ValueError):
        rr = 2.0
    if not math.isfinite(rr) or rr <= 0:
        rr = 2.0
    if side_long:
        stop = entry * (1.0 - max(0.003, atr_pct * float(stop_atr_mult)))
        target = entry + rr * (entry - stop)  # reward = rr x risk(stop distance)
    else:
        stop = entry * (1.0 + max(0.003, atr_pct * float(stop_atr_mult)))
        target = entry - rr * (stop - entry)
    return stop, target


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
