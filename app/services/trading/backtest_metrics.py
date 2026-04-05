"""Single source of truth for backtest win-rate scale: DB stores fraction [0, 1]; engine payloads use percent [0, 100]."""
from __future__ import annotations

import math
from typing import Any


def normalize_win_rate_for_db(value: float | None) -> float | None:
    """Convert engine / API ``win_rate`` into a fraction for ``trading_backtests.win_rate``.

    - Values **> 1** are treated as percent (e.g. 55.5 -> 0.555), matching migration 070.
    - Values in **[0, 1]** are stored as-is (already a fraction, or legacy rows).
    - ``None`` -> ``None`` (use 0.0 at the column when the field is non-nullable).
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if v > 1.0:
        v = v / 100.0
    return max(0.0, min(1.0, v))


def backtest_win_rate_db_to_display_pct(value: float | None) -> float | None:
    """Convert stored ``BacktestResult.win_rate`` (fraction) to percent for UI / JSON.

    If the value is already > 1 (legacy percent left in DB), return it unchanged.
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if v <= 1.0:
        return round(v * 100.0, 4)
    return round(v, 4)


def backtest_win_rate_display_pct_for_compare(value: float | None) -> float:
    """Fraction or legacy percent -> percent for thresholds (e.g. > 50)."""
    p = backtest_win_rate_db_to_display_pct(value)
    return float(p) if p is not None else 0.0


def json_win_rate_to_display_pct(value: Any) -> float | None:
    """Normalize a win_rate that may come from DB row (fraction) or fresh engine result (percent)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if v <= 1.0 and v >= 0.0:
        return round(v * 100.0, 4)
    return round(v, 4)
