"""Shared alert-direction helpers for fast-path signals."""
from __future__ import annotations

DIRECTION_LONG = "long"
DIRECTION_SHORT = "short"
DIRECTION_NEUTRAL = "neutral"

SIDE_BUY = "buy"
SIDE_SELL = "sell"


def direction_for_alert_type(alert_type: str) -> str:
    """Return long/short/neutral from the fast-path alert naming convention."""
    a = (alert_type or "").strip().lower()
    if a.endswith("_short"):
        return DIRECTION_SHORT
    if a.endswith("_long"):
        return DIRECTION_LONG
    return DIRECTION_NEUTRAL


def spot_entry_side_for_alert_type(alert_type: str) -> str:
    """Entry side for Coinbase spot.

    Spot can open longs only. Explicit short signals are not entry
    candidates and map to sell so the executor can reject them with the
    existing spot-short reason. Neutral signals, such as spread squeeze,
    are long-entry candidates in today's scanner and map to buy.
    """
    if direction_for_alert_type(alert_type) == DIRECTION_SHORT:
        return SIDE_SELL
    return SIDE_BUY


__all__ = [
    "DIRECTION_LONG",
    "DIRECTION_SHORT",
    "DIRECTION_NEUTRAL",
    "SIDE_BUY",
    "SIDE_SELL",
    "direction_for_alert_type",
    "spot_entry_side_for_alert_type",
]
