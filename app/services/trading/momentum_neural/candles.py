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


def is_bounce_curl_candle(
    o: float, h: float, l: float, c: float,
    *, min_close_pos: float = 0.55,
) -> bool:
    """The CURL-BACK-UP bar after a micro-pullback dip: a GREEN candle that closes in
    the upper part of its range (the bounce is reasserting). Distinct from the BREAK
    candle (``is_strong_bull_break_candle``) — this is the re-load trigger Ross buys
    on the dip-and-curl during a run, so the geometry is the same conviction shape but
    the SEMANTICS are the bounce off a higher-low, not the initial break.

    Green (close >= open) AND close in the upper ``min_close_pos`` of the range. The
    higher-low / shelf-hold / shallow-dip checks live in the caller (they need the
    multi-bar pullback context); this is the per-bar conviction confirm. Fail-safe: a
    zero-range bar yields False (NO fire — an extra discretionary BUY needs proof,
    opposite of the break candle's fail-open). Range-relative, no fixed cents."""
    rng, _body, _upper, _lower = _ohlc(o, h, l, c)
    if rng <= 0:
        return False
    if float(c) < float(o):                                  # red curl -> no reassert
        return False
    if (float(c) - float(l)) / rng < float(min_close_pos):   # closed weak (low in range)
        return False
    return True


def bounce_curl_from_df(df: Any, **kw: Any) -> bool:
    """``is_bounce_curl_candle`` on the last bar of ``df``; False (fail-SAFE, NO fire)
    when the bar is unreadable — the OPPOSITE of ``break_candle_ok_from_df``'s fail-
    open, because a re-load is an extra discretionary BUY that needs proof, not the
    benefit of the doubt. Thin/unreadable micro-bars therefore never fire a re-load."""
    ohlc = _last_ohlc_from_df(df)
    return False if ohlc is None else is_bounce_curl_candle(*ohlc, **kw)


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


def _ema(values: list[float], span: int) -> list[float]:
    """Recursive EWM (``adjust=False``, seeded with the first value) — matches
    pandas ``Series.ewm(span=span, adjust=False).mean()`` so replay/live agree.
    Pure, no pandas dependency (keeps these helpers testable on plain lists)."""
    if not values or span <= 0:
        return []
    alpha = 2.0 / (float(span) + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1.0 - alpha) * out[-1])
    return out


def _closes_from_df(df: Any) -> list[float] | None:
    """Close column of an OHLCV frame as a float list, or None if unavailable."""
    try:
        if df is None or getattr(df, "empty", True) or len(df) < 1:
            return None
        cols = {x.lower(): x for x in df.columns}
        return [float(x) for x in df[cols["close"]].tolist()]
    except Exception:
        return None


def macd_hist_rollover_from_df(
    df: Any, *, fast: int = 12, slow: int = 26, signal: int = 9,
) -> bool:
    """1m MACD-histogram ROLLOVER = up-momentum decelerating: a POSITIVE histogram
    that has peaked and is now declining (``hist[-1] < hist[-2] >= hist[-3]`` with
    ``hist[-2] > 0``) OR a fresh cross below zero (``hist[-1] < 0 <= hist[-2]``).

    The lagging-but-spoof-proof complement to the topping-tail wick: it catches the
    real top that gave NO dominant upper wick (LNAI 5204, 2026-06-16 — the lock fire
    the wick missed but MACD caught). An exhaustion CONFIRMER for the runner exit,
    OR'd with the topping-tail. Standard MACD params (12/26/9), tunable. Fail-safe
    False on short/unreadable data so missing data never forces an exit. Pure."""
    closes = _closes_from_df(df)
    need = int(slow) + int(signal) + 3
    if closes is None or len(closes) < need:
        return False
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd = [f - s for f, s in zip(ema_fast, ema_slow)]
    sig = _ema(macd, signal)
    hist = [m - s for m, s in zip(macd, sig)]
    if len(hist) < 3:
        return False
    h0, h1, h2 = hist[-1], hist[-2], hist[-3]
    peak_roll = (h0 < h1) and (h1 >= h2) and (h1 > 0.0)
    zero_cross = (h0 < 0.0) and (h1 >= 0.0)
    return bool(peak_roll or zero_cross)
