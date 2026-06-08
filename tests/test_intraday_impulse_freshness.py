"""Pure unit tests for ross_momentum.intraday_impulse_freshness (selection->entry M4).

The freshness measure decides whether a candidate is still in a FRESH intraday
up-impulse near its recent high (so a shallow pullback is available to enter on) or has
already FADED — the structural precondition the pullback-break entry gate needs. It must
be ADAPTIVE (per-name range, no fixed %) and reuse the gate's own retracement_threshold.
"""
from __future__ import annotations

import pandas as pd

from app.services.trading.momentum_neural.ross_momentum import (
    ImpulseFreshness,
    intraday_impulse_freshness,
)


def _frame(closes, *, hi_off=0.05, lo_off=0.05):
    """OHLCV frame from a close path; High/Low hug the close so win_high/win_low track it."""
    rows = [
        {"Open": c, "High": c + hi_off, "Low": c - lo_off, "Close": c, "Volume": 1000}
        for c in closes
    ]
    return pd.DataFrame(rows)


def test_fresh_when_price_near_recent_high():
    df = _frame([100 + i * 0.5 for i in range(20)] + [109.5])  # ramps up, sits at the top
    r = intraday_impulse_freshness(df)
    assert isinstance(r, ImpulseFreshness)
    assert r.is_fresh is True
    assert r.score >= 0.9
    assert r.reason == "fresh_impulse"


def test_faded_when_price_rolled_off_the_high():
    # Up to 110 by the middle, then fades back to ~101 now (a stale 24h mover).
    df = _frame([100 + i for i in range(11)] + [110 - i for i in range(1, 10)])
    r = intraday_impulse_freshness(df)
    assert r.is_fresh is False
    assert r.score < 0.5
    assert r.reason == "faded_below_high"


def test_new_high_gives_position_above_one_for_ranking():
    df = _frame([100 + i * 0.5 for i in range(20)] + [113.0])  # closes ABOVE the window high
    r = intraday_impulse_freshness(df)
    assert r.is_fresh is True
    assert r.score == 1.0  # clamped
    assert r.position_in_range > 1.0  # raw, so a true new high out-ranks a name at the high


def test_flat_series_is_not_a_fresh_impulse():
    df = _frame([100.0] * 21, hi_off=0.0, lo_off=0.0)  # exactly flat -> zero range
    r = intraday_impulse_freshness(df)
    assert r.is_fresh is False
    assert r.reason == "no_range"


def test_insufficient_bars_not_fresh():
    df = _frame([100.0, 101.0, 102.0])
    r = intraday_impulse_freshness(df)
    assert r.is_fresh is False
    assert r.reason == "insufficient_bars"


def test_threshold_reuse_is_adaptive_not_a_fixed_pct():
    # Same price path; a STRICTER threshold (smaller allowed retrace) demands the price
    # sit CLOSER to the high. A name retraced ~40% is fresh at 0.50 but faded at 0.30.
    df = _frame([100 + i * 0.5 for i in range(20)] + [105.7])  # ~40% up the ~9.5-wide range
    assert intraday_impulse_freshness(df, retracement_threshold=0.50).is_fresh is True
    assert intraday_impulse_freshness(df, retracement_threshold=0.30).is_fresh is False


def test_is_fresh_depends_on_relative_not_absolute_range():
    # Two names with very different absolute prices/ranges but the SAME relative position
    # (near their own recent high) are BOTH fresh — the bar floats per instrument.
    cheap = _frame([1.00 + i * 0.005 for i in range(20)] + [1.095])
    pricey = _frame([1000 + i * 5 for i in range(20)] + [1095.0])
    assert intraday_impulse_freshness(cheap).is_fresh is True
    assert intraday_impulse_freshness(pricey).is_fresh is True


def test_none_or_empty_is_safe():
    assert intraday_impulse_freshness(None).is_fresh is False
    assert intraday_impulse_freshness(pd.DataFrame()).is_fresh is False
