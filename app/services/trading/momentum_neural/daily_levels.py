"""Daily-chart context for Ross momentum entries.

Pure helpers used by the live runner and entry gates.  The module intentionally
does no fetching; callers provide already-loaded daily bars.  Distances are
expressed in daily ATR units so the same math works across $1 and $20 names.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DailyContext:
    price: float | None
    atr: float | None
    sma_200: float | None
    dist_to_sma_200_atr: float | None
    dist_to_resistance_atr: float | None
    swing_high_nd: float | None
    nearest_unfilled_gap_bottom: float | None
    rejection_count: int
    is_blue_sky: bool
    room_to_gap_top_atr: float | None = None


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _col(df: Any, name: str) -> list[float]:
    try:
        values = df[name].tolist()
    except Exception:
        try:
            values = [row.get(name) for row in df]
        except Exception:
            return []
    out: list[float] = []
    for value in values:
        f = _finite_float(value)
        if f is not None:
            out.append(f)
    return out


def _mean(values: list[float]) -> float | None:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    return (sum(vals) / len(vals)) if vals else None


def _true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    n = min(len(highs), len(lows), len(closes))
    out: list[float] = []
    prev_close: float | None = None
    for idx in range(n):
        hi = highs[idx]
        lo = lows[idx]
        if not (math.isfinite(hi) and math.isfinite(lo) and hi >= lo):
            prev_close = closes[idx] if idx < len(closes) else prev_close
            continue
        if prev_close is None or not math.isfinite(prev_close):
            tr = hi - lo
        else:
            tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        if math.isfinite(tr) and tr > 0:
            out.append(float(tr))
        prev_close = closes[idx] if idx < len(closes) else prev_close
    return out


def _nearest_above(levels: list[float], entry: float) -> float | None:
    above = [float(level) for level in levels if math.isfinite(float(level)) and float(level) > entry]
    return min(above) if above else None


def _round_number_near(price: float, atr_pct: float | None = None) -> float | None:
    """Nearest psychological half/whole-dollar level at or just above ``price``.

    Ross-style small-cap names cluster liquidity at whole and half dollars. For
    sub-dollar names the same decimal-place idea maps to nickels. ``atr_pct`` is
    accepted for call-site parity with ATR-normalized gates; the grid itself is a
    price-scale convention, not a volatility threshold.
    """
    px = _finite_float(price)
    if px is None or px <= 0:
        return None
    step = 0.5 if px >= 1.0 else 0.05
    level = math.ceil((px - 1e-12) / step) * step
    if level <= 0:
        return None
    return round(float(level), 6)


def compute_daily_context(
    df: Any,
    *,
    lookback: int = 20,
    price: float | None = None,
    entry_context: bool = False,
) -> DailyContext | None:
    """Build daily ATR/overhead context from daily OHLCV bars.

    ``dist_to_sma_200_atr`` is signed ``(price - sma200) / ATR``.  Negative means
    price is below the 200SMA and the average is overhead; positive means price is
    already above it and the 200SMA should not shrink a long momentum entry.

    When ``entry_context`` is true, the last daily bar is treated as the live day
    and excluded from prior-resistance discovery so today's breakout high does not
    become its own overhead wall.
    """
    highs = _col(df, "High")
    lows = _col(df, "Low")
    closes = _col(df, "Close")
    if not highs or not lows or not closes:
        return None

    px = _finite_float(price)
    if px is None:
        px = closes[-1] if closes else None
    if px is None or px <= 0:
        return None

    n = min(len(highs), len(lows), len(closes))
    highs = highs[:n]
    lows = lows[:n]
    closes = closes[:n]

    lb = max(1, int(lookback or 20))
    trs = _true_ranges(highs, lows, closes)
    atr = _mean(trs[-lb:]) if trs else None
    if atr is not None and atr <= 0:
        atr = None

    sma_200 = _mean(closes[-200:]) if len(closes) >= 200 else None
    dist_200 = ((px - sma_200) / atr) if (sma_200 is not None and atr and atr > 0) else None

    prior_end = max(0, n - 1) if entry_context and n > 1 else n
    prior_start = max(0, prior_end - lb)
    prior_highs = highs[prior_start:prior_end]
    swing_high = _nearest_above(prior_highs, px)

    gap_bottoms: list[float] = []
    gap_tops: list[float] = []
    for idx in range(max(1, prior_start), prior_end):
        prev_high = highs[idx - 1]
        cur_low = lows[idx]
        if cur_low > prev_high:
            gap_bottoms.append(cur_low)
            gap_tops.append(prev_high)
    gap_bottom = _nearest_above(gap_bottoms, px)

    overhead_levels = [x for x in (swing_high, gap_bottom) if x is not None]
    nearest = min(overhead_levels) if overhead_levels else None
    dist_res = ((nearest - px) / atr) if (nearest is not None and atr and atr > 0) else None

    max_prior_high = max(prior_highs) if prior_highs else None
    is_blue_sky = bool(max_prior_high is None or px >= max_prior_high)
    room_to_gap_top = None
    gap_top = _nearest_above(gap_tops, px)
    if gap_top is not None and atr and atr > 0:
        room_to_gap_top = (gap_top - px) / atr

    rejection_count = 0
    if nearest is not None and atr and atr > 0:
        band = atr
        for hi, close in zip(prior_highs, closes[prior_start:prior_end]):
            if abs(hi - nearest) <= band and close < hi:
                rejection_count += 1

    return DailyContext(
        price=float(px),
        atr=float(atr) if atr is not None else None,
        sma_200=float(sma_200) if sma_200 is not None else None,
        dist_to_sma_200_atr=float(dist_200) if dist_200 is not None else None,
        dist_to_resistance_atr=float(dist_res) if dist_res is not None else None,
        swing_high_nd=float(swing_high) if swing_high is not None else None,
        nearest_unfilled_gap_bottom=float(gap_bottom) if gap_bottom is not None else None,
        rejection_count=int(rejection_count),
        is_blue_sky=is_blue_sky,
        room_to_gap_top_atr=float(room_to_gap_top) if room_to_gap_top is not None else None,
    )


def overhead_supply_atr(daily_ctx: Any, *, entry: float) -> float | None:
    """Nearest overhead supply distance in daily ATR units, or ``None`` for clear room."""
    atr = _finite_float(getattr(daily_ctx, "atr", None))
    px = _finite_float(entry)
    if atr is None or atr <= 0 or px is None or px <= 0:
        return None
    levels: list[float] = []
    for attr in ("swing_high_nd", "nearest_unfilled_gap_bottom"):
        level = _finite_float(getattr(daily_ctx, attr, None))
        if level is not None and level > px:
            levels.append(level)
    sma_200 = _finite_float(getattr(daily_ctx, "sma_200", None))
    if sma_200 is not None and sma_200 > px:
        levels.append(sma_200)
    if not levels:
        return None
    return (min(levels) - px) / atr


def entry_is_clear_sky(daily_ctx: Any, *, entry: float, min_room_atr: float) -> bool:
    """True when entry is a prior-high breakout with enough ATR-normalized room."""
    px = _finite_float(entry)
    if daily_ctx is None or px is None or px <= 0:
        return False
    swing_high = _finite_float(getattr(daily_ctx, "swing_high_nd", None))
    if swing_high is not None and px < swing_high:
        return False
    if not bool(getattr(daily_ctx, "is_blue_sky", False)):
        return False
    room = overhead_supply_atr(daily_ctx, entry=px)
    if room is None:
        return True
    try:
        floor = max(0.0, float(min_room_atr))
    except (TypeError, ValueError):
        floor = 0.0
    return float(room) >= floor
