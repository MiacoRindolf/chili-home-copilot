"""Candlestick-pattern helpers for the Ross momentum lane.

The structural pullback-break gate captures the FLAG shape (shallow retrace that
holds the 9-EMA, then breaks) but not the candle SHAPES Ross reads bar-by-bar:
the conviction of the break candle (entry confirmation) and the topping-tail /
shooting-star rejection he sells into (profit protection). These are the missing
pieces vs his actual method.

Pure functions on a single bar's OHLC (or the last bar of a frame) — range-
relative so they are adaptive across price/volatility, no fixed cents. Documented
fractions are the single place to tune; defaults are lenient (filter the clearly
weak / clearly exhausted, not the normal bar). Fail-safe: a zero-range bar yields
False from every predicate.
"""

from __future__ import annotations

from typing import Any


def _ohlc(o: float, h: float, l: float, c: float) -> tuple[float, float, float, float]:
    """(range, body, upper_wick, lower_wick) for one bar."""
    rng = float(h) - float(l)
    body = abs(float(c) - float(o))
    upper = float(h) - max(float(o), float(c))
    lower = min(float(o), float(c)) - float(l)
    return rng, body, upper, lower


def is_strong_bull_break_candle(
    o: float, h: float, l: float, c: float,
    *, min_close_pos: float = 0.50, max_upper_wick_frac: float = 0.50,
) -> bool:
    """The break/reclaim bar is a CONVICTION bull candle — green, closing in the
    upper part of its range, without a dominant upper wick. Rejects the weak / doji
    / topping-tail "break" that wicks out and reverses (a false break). Lenient by
    default (close in the upper half, upper wick not over half the range)."""
    rng, body, upper, _ = _ohlc(o, h, l, c)
    if rng <= 0:
        return False
    if float(c) < float(o):                       # red break bar -> no conviction
        return False
    if (float(c) - float(l)) / rng < float(min_close_pos):   # closed weak (low in range)
        return False
    if upper / rng > float(max_upper_wick_frac):  # dominant upper wick = rejection
        return False
    return True


def is_topping_tail(
    o: float, h: float, l: float, c: float,
    *, min_upper_wick_frac: float = 0.50, min_wick_to_body: float = 1.0,
) -> bool:
    """Topping-tail / shooting-star / gravestone-doji: a long UPPER wick that
    dominates the bar's range AND exceeds the body — momentum exhaustion /
    rejection at the highs. Ross's cue to sell into strength. (Independent of
    the bar's color: a green bar that gives back most of its high still rejects.)"""
    rng, body, upper, _ = _ohlc(o, h, l, c)
    if rng <= 0:
        return False
    if upper / rng < float(min_upper_wick_frac):  # upper wick must dominate the range
        return False
    if upper < float(min_wick_to_body) * max(body, 1e-12):  # and exceed the body
        return False
    return True


def _ema(values: list[float], span: int) -> list[float]:
    """Simple EMA series helper used by micro pullback detectors."""
    try:
        vals = [float(v) for v in values if v is not None]
        n = max(1, int(span))
        if not vals:
            return []
        alpha = 2.0 / (n + 1.0)
        out: list[float] = []
        cur = vals[0]
        for v in vals:
            cur = alpha * v + (1.0 - alpha) * cur
            out.append(cur)
        return out
    except Exception:
        return []


def is_bounce_curl_candle(
    o: float, h: float, l: float, c: float,
    *, min_close_pos: float = 0.55, max_upper_wick_frac: float = 0.55,
) -> bool:
    """Green curl candle that reclaims into the upper part of its range."""
    rng, body, upper, _ = _ohlc(o, h, l, c)
    if rng <= 0 or body <= 0:
        return False
    if float(c) <= float(o):
        return False
    if (float(c) - float(l)) / rng < float(min_close_pos):
        return False
    if upper / rng > float(max_upper_wick_frac):
        return False
    return True


def _last_ohlc_from_df(df: Any) -> tuple[float, float, float, float] | None:
    """(o,h,l,c) of the last bar of an OHLCV frame, or None if unavailable."""
    try:
        if df is None or getattr(df, "empty", True) or len(df) < 1:
            return None
        cols = {x.lower(): x for x in df.columns}
        return (
            float(df[cols["open"]].iloc[-1]),
            float(df[cols["high"]].iloc[-1]),
            float(df[cols["low"]].iloc[-1]),
            float(df[cols["close"]].iloc[-1]),
        )
    except Exception:
        return None


def break_candle_ok_from_df(df: Any, **kw: Any) -> bool:
    """``is_strong_bull_break_candle`` on the last bar of ``df``; True (fail-open)
    when the bar is unreadable so thin data never blocks an otherwise-valid entry."""
    ohlc = _last_ohlc_from_df(df)
    return True if ohlc is None else is_strong_bull_break_candle(*ohlc, **kw)


def topping_tail_from_df(df: Any, **kw: Any) -> bool:
    """``is_topping_tail`` on the last bar of ``df``; False (fail-safe) when the bar
    is unreadable so missing data never forces an exit."""
    ohlc = _last_ohlc_from_df(df)
    return False if ohlc is None else is_topping_tail(*ohlc, **kw)


def bounce_curl_from_df(df: Any, **kw: Any) -> bool:
    """``is_bounce_curl_candle`` on the last bar; unreadable data fails safe."""
    ohlc = _last_ohlc_from_df(df)
    return False if ohlc is None else is_bounce_curl_candle(*ohlc, **kw)


def macd_hist_rollover_from_df(df: Any) -> bool:
    """MACD histogram rollover on the last completed bars; unreadable data is False."""
    try:
        if df is None or getattr(df, "empty", True) or len(df) < 4:
            return False
        cols = {x.lower(): x for x in df.columns}
        closes = [float(x) for x in df[cols["close"]].tolist()]
        if len(closes) < 4:
            return False
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        macd = [a - b for a, b in zip(ema12, ema26)]
        signal = _ema(macd, 9)
        hist = [m - s for m, s in zip(macd, signal)]
        if len(hist) < 3:
            return False
        return hist[-3] < hist[-2] and hist[-1] < hist[-2]
    except Exception:
        return False
