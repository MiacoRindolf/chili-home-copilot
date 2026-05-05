"""Canonical timeframe parsing.

Single source of truth for converting between timeframe strings (``"1m"``,
``"5m"``, ``"1h"``, ``"1d"``, ``"1w"``, etc.) and integer seconds. Used by
exit-engine adapters, time-decay computations, and any code that needs
to convert between bar counts and wall-clock durations.

Existing parallel maps in the repo serve different purposes and stay
distinct:
  * ``coinbase_ohlcv._GRANULARITY_MAP`` -- Coinbase API granularity (a
    subset; provider-specific).
  * ``market_data._VALID_INTERVALS`` -- yfinance interval validation
    (includes ``"60m"``, ``"1wk"``, ``"5d"``, ``"1mo"``, ``"3mo"`` which
    don't map cleanly to "bars" duration).
  * ``paper_trading._expiry_days_for_timeframe`` -- maps timeframe to a
    paper-trade expiry policy, not a duration.

This module is the right home for "how many seconds is one bar at this
timeframe" -- the question every time-decay / bars-held computation
asks. The CHECK constraint added in migration 227 enforces that
``ScanPattern.timeframe`` only stores values present here.
"""
from __future__ import annotations


# Frozen mapping. Add new entries at the bottom of the existing list;
# downstream callers should NEVER hardcode timeframe strings, they should
# go through this module. The CHECK constraint in mig 227 enforces that
# the DB only stores values present here.
_TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}


def timeframe_to_seconds(tf: str) -> int:
    """Convert a timeframe string to seconds. Raises ValueError on unknown."""
    if tf in _TIMEFRAME_SECONDS:
        return _TIMEFRAME_SECONDS[tf]
    raise ValueError(
        f"Unknown timeframe: {tf!r}. Allowed: {list(_TIMEFRAME_SECONDS)}"
    )


def known_timeframes() -> list[str]:
    """Return the list of allowed timeframe strings, in ascending duration order."""
    return list(_TIMEFRAME_SECONDS.keys())
