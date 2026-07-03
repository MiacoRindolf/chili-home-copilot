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


def test_sma200_computed_with_enough_bars():
    """≥200 daily bars => the 200-SMA macro benchmark populates (above/below + signed
    ATR distance). An uptrend's last price sits ABOVE its 200-SMA."""
    df = _daily(np.linspace(3.0, 9.0, 210))   # 210 bars, uptrend
    ctx = compute_daily_context(df, lookback=20, price=9.2)
    assert ctx.sma_200 is not None and ctx.sma_200 > 0
    assert ctx.above_sma_200 is True
    assert ctx.dist_to_sma_200_atr is not None and ctx.dist_to_sma_200_atr > 0


def test_sma200_none_when_too_few_bars():
    """< 200 daily bars => 200-SMA is None (fail-open, neutral macro — a fresh listing
    is never penalized)."""
    df = _daily(np.linspace(3.0, 6.0, 24))
    ctx = compute_daily_context(df, lookback=20, price=6.5)
    assert ctx.sma_200 is None and ctx.above_sma_200 is None


def test_cupr_guarantee_holds_below_sma200():
    """THE CUPR GUARANTEE under the macro layer: a name DEEP BELOW its 200-SMA (bearish
    macro) that breaks a recent level still scores HIGH in daily_structure_pct — the
    200-SMA is a SOFT minority input, never a hard macro filter that blocks a spike."""
    df = _daily(np.linspace(15.0, 4.0, 210))   # long downtrend, far below the 200-SMA
    px = 5.0                                    # breaks the recent (declining) highs, still below 200-SMA
    ctx = compute_daily_context(df, lookback=20, price=px)
    assert ctx.above_sma_200 is False           # bearish macro
    assert ctx.breaking_major_level is True
    assert ctx.daily_structure_pct is not None and ctx.daily_structure_pct > 0.5  # still preferred


def test_above_sma200_lifts_trend_vs_below():
    """Above the 200-SMA reads more bullish than below (the macro is folded into trend)."""
    up = compute_daily_context(_daily(np.linspace(3.0, 9.0, 210)), lookback=20, price=9.2)
    down = compute_daily_context(_daily(np.linspace(9.0, 3.0, 210)), lookback=20, price=3.1)
    assert up.trend_score > down.trend_score


def test_levels_are_populated_on_ok():
    df = _daily(np.linspace(3.0, 6.0, 24))
    ctx = compute_daily_context(df, lookback=20, price=6.5)
    assert isinstance(ctx, DailyContext)
    assert ctx.prior_day_high is not None and ctx.swing_high_nd is not None
    assert ctx.daily_atr_pct is not None and ctx.daily_atr_pct > 0
    assert ctx.dist_to_resistance_atr is not None and ctx.dist_to_support_atr is not None


# ─────────────────────────── A5: SYMBOL-FRESHNESS TWO-SIGNED TILT ───────────────────────────
# Ross prefers a FRESH name ("recent reverse split, no big days of volume recently" — CLRO) and
# passes a STALE one ("made a huge move and then sold off" — DSY). TILT only, never a veto;
# fail-open-to-neutral on thin history. Reuses the lane's OWN explosive change-pct floor (10%).

def _daily_vol(closes, vols, *, hi_pad=0.02, lo_pad=0.02):
    """Daily OHLCV df with a PER-BAR volume series (for the trailing-$vol percentile)."""
    closes = [float(c) for c in closes]
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * (1 + hi_pad) for o, c in zip(opens, closes)]
    lows = [min(o, c) * (1 - lo_pad) for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows, "Close": closes,
        "Volume": [float(v) for v in vols],
    })


def test_a5_fresh_profile_boosts_vs_neutral():
    """FRESH = no recent explosive daily move AND quiet trailing $vol vs the symbol's own year
    => a POSITIVE tilt folded into daily_structure_pct (boost vs the flag-OFF baseline)."""
    n = 260
    # flat, calm price (no >=10% daily move anywhere) with a LATER quiet-volume window: high $vol
    # for most of the year, then a much quieter trailing-20d window (low own-year percentile).
    closes = [5.0 + 0.01 * np.sin(i / 7.0) for i in range(n)]
    vols = [5_000_000] * (n - 20) + [400_000] * 20  # quiet recent 20d vs a busy year
    df = _daily_vol(closes, vols)
    px = float(closes[-1])
    on = compute_daily_context(df, lookback=20, price=px, symbol_freshness_tilt=True)
    off = compute_daily_context(df, lookback=20, price=px, symbol_freshness_tilt=False)
    assert on.freshness_tilt_sign == 1  # FRESH
    assert on.days_since_last_explosive_move is None  # no explosive move in the window
    assert on.trailing_dollar_vol_pctl is not None and on.trailing_dollar_vol_pctl <= 0.40
    assert on.daily_structure_pct is not None and off.daily_structure_pct is not None
    assert on.daily_structure_pct > off.daily_structure_pct  # boost applied


def test_a5_spike_and_fade_derates():
    """STALE = a recent explosive daily move (>=10% up) that FADED back below its pre-move base
    => a NEGATIVE tilt folded into daily_structure_pct (derate vs the flag-OFF baseline)."""
    n = 30
    closes = [5.0] * (n - 4)
    # a +30% explosive up bar, then a fade all the way back below the 5.0 pre-move base.
    closes += [6.5, 6.0, 5.2, 4.8]  # explosive spike then fade under the base (STALE)
    df = _daily(closes)
    px = float(closes[-1])
    on = compute_daily_context(df, lookback=20, price=px, symbol_freshness_tilt=True)
    off = compute_daily_context(df, lookback=20, price=px, symbol_freshness_tilt=False)
    assert on.freshness_tilt_sign == -1  # STALE
    assert on.days_since_last_explosive_move is not None  # a recent explosive move exists
    assert on.daily_structure_pct is not None and off.daily_structure_pct is not None
    assert on.daily_structure_pct < off.daily_structure_pct  # derate applied


def test_a5_thin_history_is_neutral():
    """Thin / short history => fail-open-to-neutral: no freshness sign, ds byte-identical."""
    df = _daily([5.0, 5.1])  # too few bars => _NULL (fail-open)
    ctx = compute_daily_context(df, lookback=20, price=5.1, symbol_freshness_tilt=True)
    assert ctx.daily_structure_pct is None  # neutral _NULL
    assert ctx.freshness_tilt_sign is None


# ─────────────────────────── A7: CONSECUTIVE-RED-DAILIES SOFT DE-RANK ───────────────────────────
# Ross passes "four or five red candles in a row … price clearly coming down." SOFT selection
# de-rate scaled by run length; NEVER an entry veto. Folds under the existing red_rejection flag.

def test_a7_red_run_derates_vs_alternating():
    """4-5 consecutive down-closes ending yesterday => a SOFT derate vs the flag-OFF baseline;
    an alternating series => neutral (no red-run derate)."""
    # a clean run of 5 consecutive DOWN closes ending at yesterday (bar -2), then a flat today.
    red = [6.0, 5.9, 5.8, 5.7, 5.6, 5.5, 5.4, 5.3, 5.2, 5.1, 5.0, 4.9, 4.8, 4.7, 4.6, 4.5,
           4.4, 4.3, 4.2, 4.1, 4.0, 3.9, 3.8, 3.7, 3.7]  # last bar (today) flat; -2..-6 all down
    df_red = _daily(red)
    px = float(red[-1])
    on = compute_daily_context(df_red, lookback=20, price=px, red_rejection_derate=True)
    off = compute_daily_context(df_red, lookback=20, price=px, red_rejection_derate=False)
    assert on.red_run_count is not None and on.red_run_count >= 4
    assert on.daily_structure_pct is not None and off.daily_structure_pct is not None
    assert on.daily_structure_pct < off.daily_structure_pct  # red-run derate applied

    # an ALTERNATING series ends yesterday NOT on a down-close-run => neutral (red_run_count 0/1).
    alt = [5.0 + (0.1 if i % 2 == 0 else -0.1) for i in range(25)]
    df_alt = _daily(alt)
    ctx_alt = compute_daily_context(df_alt, lookback=20, price=float(alt[-1]), red_rejection_derate=True)
    assert ctx_alt.red_run_count is not None and ctx_alt.red_run_count <= 1  # no multi-bar run
