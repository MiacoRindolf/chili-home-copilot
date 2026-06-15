"""Daily-chart context layer — pure-fn units + the CUPR guarantee.

The load-bearing property: a NEWS-GAP SPIKE breaking a major daily level from a flat/
DOWN daily base (CUPR 2.95→7.80) must score HIGH in daily_structure_pct (so selection
PREFERS it), NOT be zeroed/blocked — even though its trend_score is low. The layer is a
SELECTION TILT, never an entry gate.
"""
import numpy as np
import pandas as pd

from app.services.trading.momentum_neural.daily_levels import (
    DailyContext,
    compute_daily_context,
)


def _daily(closes, *, vol=1_000_000, hi_pad=0.02, lo_pad=0.02):
    """Build a daily OHLCV df from a close series (cols Open/High/Low/Close/Volume)."""
    closes = [float(c) for c in closes]
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * (1 + hi_pad) for o, c in zip(opens, closes)]
    lows = [min(o, c) * (1 - lo_pad) for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows, "Close": closes,
        "Volume": [vol] * len(closes),
    })


def test_uptrend_breaking_scores_high():
    """Clean daily uptrend + price breaking above => high trend AND high structure."""
    df = _daily(np.linspace(3.0, 6.0, 24))  # steady uptrend
    px = 6.5  # breaking above the prior-day high + 20d swing high
    ctx = compute_daily_context(df, lookback=20, price=px)
    assert ctx.reason == "ok"
    assert ctx.breaking_major_level is True
    assert ctx.trend_score > 0.6
    assert ctx.daily_structure_pct is not None and ctx.daily_structure_pct > 0.6


def test_cupr_guarantee_downtrend_break_not_blocked():
    """THE CUPR GUARANTEE: a DOWNTRENDING daily base whose price breaks above the
    prior-day high today => trend_score is LOW (downtrend) but daily_structure_pct is
    HIGH (breaking a major level dominates) => selection PREFERS it, never blocks it."""
    df = _daily(np.linspace(9.0, 3.0, 24))  # steady DOWNtrend (news-gapper's base)
    px = 4.5  # today's spike breaks above the recent (declining) highs
    ctx = compute_daily_context(df, lookback=20, price=px)
    assert ctx.breaking_major_level is True
    assert ctx.trend_score < 0.5              # downtrend => low (can be 0 on a steep decline)
    # THE GUARANTEE: even when trend is at its FLOOR, the structure score stays HIGH
    # (break-above + clear-sky dominate; trend is only a 15% minority input) => selection
    # PREFERS the breaking spike, never blocks it. This is what protects CUPR-class names.
    assert ctx.daily_structure_pct is not None and ctx.daily_structure_pct > 0.5


def test_jammed_under_resistance_scores_lower_than_break():
    """A price pinned just UNDER a major resistance (no room, not breaking) scores
    LOWER than a clean break above — the level-awareness value, at the SELECTION slot."""
    df = _daily(np.linspace(3.0, 5.0, 24))
    pdh = float(df["High"].iloc[-2])
    jammed = compute_daily_context(df, lookback=20, price=pdh - 0.01)   # just under PDH
    breaking = compute_daily_context(df, lookback=20, price=pdh + 0.20)  # clean break
    assert jammed.breaking_major_level is False
    assert breaking.breaking_major_level is True
    assert (jammed.daily_structure_pct or 0) < (breaking.daily_structure_pct or 0)


def test_failopen_too_few_bars():
    """< lookback+2 daily bars => neutral _NULL (trend 0.5, structure None) — skip."""
    df = _daily([3.0, 3.1, 3.2])  # 3 bars, lookback 20
    ctx = compute_daily_context(df, lookback=20, price=3.3)
    assert ctx.reason == "insufficient_daily_bars"
    assert ctx.trend_score == 0.5
    assert ctx.daily_structure_pct is None
    assert ctx.prior_day_high is None


def test_failopen_thin_zero_volume_window():
    """Mostly zero-volume daily bars (illiquid/gappy) => fail-open _NULL."""
    df = _daily(np.linspace(3.0, 5.0, 24), vol=0)  # all zero volume
    ctx = compute_daily_context(df, lookback=20, price=5.5)
    assert ctx.reason == "insufficient_daily_bars"
    assert ctx.daily_structure_pct is None


def test_failopen_none_and_empty():
    assert compute_daily_context(None, lookback=20, price=5.0).daily_structure_pct is None
    assert compute_daily_context(pd.DataFrame(), lookback=20, price=5.0).reason == "insufficient_daily_bars"


def test_structure_always_in_unit_range():
    """daily_structure_pct and trend_score stay in [0,1] across regimes."""
    for closes in (np.linspace(3, 9, 30), np.linspace(9, 3, 30), [5.0] * 30):
        df = _daily(closes)
        for px in (2.0, 5.0, 12.0):
            c = compute_daily_context(df, lookback=20, price=px)
            assert 0.0 <= c.trend_score <= 1.0
            if c.daily_structure_pct is not None:
                assert 0.0 <= c.daily_structure_pct <= 1.0


def test_selection_pillar_parity_and_tilt():
    """The 5th selection pillar: INERT under the deployed liquidity-biased weights
    (daily_structure ignored → byte-identical selection), ACTIVE under the daily-
    context weights (a high-daily-structure name ranks above a low one)."""
    from app.services.trading.momentum_neural.ross_momentum import (
        ROSS_PILLAR_WEIGHTS_DAILY_CONTEXT,
        ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
        score_universe,
    )
    sigs = {
        "A": {"vol_ratio": 5, "gap_pct": 30, "dollar_volume": 5e6, "daily_structure_pct": 0.9},
        "B": {"vol_ratio": 5, "gap_pct": 30, "dollar_volume": 5e6, "daily_structure_pct": 0.1},
    }
    # PARITY: with the liquidity-biased weights (no daily_structure key), A and B are
    # identical on every other pillar → identical score (daily_structure ignored).
    lb = score_universe(sigs, weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED)
    assert lb["A"].score == lb["B"].score
    # TILT: with the daily-context weights, the high-daily-structure name ranks above.
    dc = score_universe(sigs, weights=ROSS_PILLAR_WEIGHTS_DAILY_CONTEXT)
    assert dc["A"].score > dc["B"].score


def test_levels_are_populated_on_ok():
    df = _daily(np.linspace(3.0, 6.0, 24))
    ctx = compute_daily_context(df, lookback=20, price=6.5)
    assert isinstance(ctx, DailyContext)
    assert ctx.prior_day_high is not None and ctx.swing_high_nd is not None
    assert ctx.daily_atr_pct is not None and ctx.daily_atr_pct > 0
    assert ctx.dist_to_resistance_atr is not None and ctx.dist_to_support_atr is not None
