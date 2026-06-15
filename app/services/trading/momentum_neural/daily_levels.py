"""Daily-chart context for the momentum lane — the broader-trend read + major daily
S&R levels Ross STARTS with, that the intraday (1m/5m/15m) lane was missing.

PURE (no DB/IO) — operates on a daily OHLCV df the caller fetched + cached, mirroring
the ``ross_momentum.py`` purity pattern. The OUTPUT is used as a SELECTION TILT (a soft
daily-structure sub-score, percentile-ranked like the other pillars — it RE-RANKS,
never blocks a fill) + log-only annotation + an opt-in INVARIANT-A stop FLOOR.

It is DELIBERATELY NOT an entry filter. A news-gap momentum SPIKE (CUPR 2.95→7.80)
often has NO daily uptrend and fires while still under an un-cleared daily level — a
hard daily filter would block exactly the winners (and a deferred entry never re-fires
once the spike runs away). So ``trend_score`` is SOFT (a downtrending gapper lands
~0.2-0.4, NEVER zeroed, never blocked) and breaking ABOVE a level is REWARDED, not
required. Fail-OPEN on thin/short/NaN daily history → no tilt, no gate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..indicator_core import compute_atr

# CAP_ATR: distances beyond this many daily-ATR units count as "all the room" — ONE
# documented scale; everything else derives from the daily ATR (equity-relative).
_CAP_ATR = 3.0


@dataclass(frozen=True)
class DailyContext:
    prior_day_high: float | None
    prior_day_low: float | None
    swing_high_nd: float | None
    swing_low_nd: float | None
    daily_atr_pct: float | None
    dist_to_resistance_atr: float | None
    dist_to_support_atr: float | None
    near_round_number: float | None
    trend_score: float                  # [0,1] SOFT broader-trend; 0.5 = neutral/unknown
    breaking_major_level: bool
    daily_structure_pct: float | None   # [0,1] selection sub-score; None = skip the pillar
    reason: str


_NULL = DailyContext(
    None, None, None, None, None, None, None, None, 0.5, False, None,
    "insufficient_daily_bars",
)


def _round_number_near(price: float, atr_pct: float) -> float | None:
    """Nearest major round number within 0.25 daily-ATR of price (a psych level).
    Clamps the decade exponent so sub-cent crypto and 5-digit names don't compute a
    misaligned grid."""
    if price is None or price <= 0 or not math.isfinite(price):
        return None
    try:
        exp = max(-4, min(6, math.floor(math.log10(price))))
        step = 10.0 ** exp
        tol = 0.25 * atr_pct * price if (atr_pct and atr_pct > 0) else 0.0
        for s in (step, step * 0.5):
            if s <= 0:
                continue
            lvl = round(price / s) * s
            if abs(price - lvl) <= tol:
                return float(lvl)
    except (ValueError, OverflowError):
        return None
    return None


def compute_daily_context(df: Any, *, lookback: int = 20, price: float | None = None) -> DailyContext:
    """Daily context from a daily OHLCV df (cols Open/High/Low/Close/Volume).

    Fail-OPEN to the neutral _NULL (trend_score=0.5, all-None, daily_structure_pct=None)
    on thin/short/NaN data so every consumer degrades to "no tilt, no gate" — never a
    crash, never a block. ``price`` overrides the last daily close (use the live price).
    """
    n = max(2, int(lookback))
    try:
        if df is None or len(df) < n + 2:
            return _NULL
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        # at least half the window must be real (non-zero-volume) bars — kills the
        # "structure from a handful of gappy bars is noise" overfit.
        if "Volume" in df.columns:
            vol = df["Volume"].astype(float)
            if int((vol.tail(n) > 0).sum()) < n // 2:
                return _NULL
        px = float(price) if (price is not None and price > 0) else float(close.iloc[-1])
        if not (math.isfinite(px) and px > 0):
            return _NULL

        atr = compute_atr(high, low, close)
        atr_last = float(atr.iloc[-1])
        last_close = float(close.iloc[-1])
        if not (math.isfinite(atr_last) and last_close > 0):
            return _NULL
        atr_pct = atr_last / last_close
        if not (math.isfinite(atr_pct) and atr_pct > 0):
            return _NULL

        pdh = float(high.iloc[-2])
        pdl = float(low.iloc[-2])
        sh = float(high.tail(n).max())
        sl = float(low.tail(n).min())
        rn = _round_number_near(px, atr_pct)

        # nearest major level above / below px (prior-day H/L + N-day swing + round#)
        above = [lv for lv in (pdh, sh, rn) if lv is not None and lv > px]
        below = [lv for lv in (pdl, sl, rn) if lv is not None and lv < px]
        d_res = (min(above) - px) / px / atr_pct if above else _CAP_ATR  # clear sky => cap
        d_sup = (px - max(below)) / px / atr_pct if below else _CAP_ATR

        breaking = bool(px > pdh or px > sh)

        # SOFT broader-trend read in [0,1] — EXCLUDES rvol/gap (those are the momentum
        # pillar; this is purely daily structure, so no double-count). A downtrending
        # gapper lands LOW but NEVER zero.
        ema9 = close.ewm(span=9, adjust=False).mean()
        above_ema = float((close.tail(n) > ema9.tail(n)).mean())
        hv = high.values
        lv2 = low.values
        start = max(1, len(hv) - n)
        hh_hl = sum(1 for i in range(start, len(hv)) if hv[i] > hv[i - 1] and lv2[i] > lv2[i - 1])
        hh_hl_frac = hh_hl / float(n)
        try:
            ret = float(close.iloc[-1] / close.iloc[-n] - 1.0)
        except (IndexError, ZeroDivisionError):
            ret = 0.0
        ret_score = max(0.0, min(1.0, 0.5 + ret * 2.5))  # +20%/N => ~1.0, -20% => ~0
        trend = max(0.0, min(1.0, (above_ema + hh_hl_frac + ret_score) / 3.0))

        # the SELECTION sub-score (the only decision-affecting output): break ABOVE a
        # major level dominates; jammed-under-resistance (small ceiling) scores low;
        # trend is a 15% MINORITY input so a downtrending news-gapper is not penalized
        # into oblivion (the CUPR guarantee).
        room_below = max(0.0, min(1.0, d_sup / _CAP_ATR))
        ceiling = max(0.0, min(1.0, d_res / _CAP_ATR))
        brk = 1.0 if breaking else 0.0
        ds = max(0.0, min(1.0, 0.40 * brk + 0.30 * ceiling + 0.15 * room_below + 0.15 * trend))

        return DailyContext(
            prior_day_high=pdh, prior_day_low=pdl, swing_high_nd=sh, swing_low_nd=sl,
            daily_atr_pct=atr_pct, dist_to_resistance_atr=d_res, dist_to_support_atr=d_sup,
            near_round_number=rn, trend_score=trend, breaking_major_level=breaking,
            daily_structure_pct=ds, reason="ok",
        )
    except Exception:
        return _NULL
