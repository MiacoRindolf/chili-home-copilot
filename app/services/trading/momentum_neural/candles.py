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


#: The two fractions of ``is_topping_tail`` ARE the candle's definition -- "the upper wick
#: dominates the range (>= half of it) and exceeds the body" -- not a tuned threshold. They
#: are reported on every receipt as the definition, never as a derived value.
TOPPING_TAIL_MIN_UPPER_WICK_FRAC = 0.50
TOPPING_TAIL_MIN_WICK_TO_BODY = 1.0
#: Definitional, not tuned: open and close are two prints, and an upper wick needs a THIRD
#: print above both of them. With fewer than 3 prints ``h == max(o, c)`` so the wick is 0
#: and ``is_topping_tail`` is False by construction -- the floor only makes that explicit.
TOPPING_TAIL_MIN_PRINTS = 3


def is_topping_tail(
    o: float, h: float, l: float, c: float,
    *, min_upper_wick_frac: float = TOPPING_TAIL_MIN_UPPER_WICK_FRAC,
    min_wick_to_body: float = TOPPING_TAIL_MIN_WICK_TO_BODY,
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


def topping_tail_shape(
    o: float, h: float, l: float, c: float,
    *, min_upper_wick_frac: float = TOPPING_TAIL_MIN_UPPER_WICK_FRAC,
    min_wick_to_body: float = TOPPING_TAIL_MIN_WICK_TO_BODY,
) -> dict[str, Any] | None:
    """``is_topping_tail`` WITH its receipt: the verdict (the same function, so the two can
    never disagree), the measured ``upper_wick_frac`` and ``wick_to_body``, the two
    definitional fractions, and ``binding`` -- the one condition that decided. On a refusal
    that is the first condition that failed (the order ``is_topping_tail`` checks them); on
    a fire it is the condition with the least slack (value / definition). None for an
    unreadable bar. ``wick_to_body`` is None on a zero body (a doji: the wick exceeds it at
    any size), so the receipt never carries an infinity."""
    try:
        o_f, h_f, l_f, c_f = float(o), float(h), float(l), float(c)
    except (TypeError, ValueError):
        return None
    rng, body, upper, _ = _ohlc(o_f, h_f, l_f, c_f)
    verdict = bool(is_topping_tail(
        o_f, h_f, l_f, c_f,
        min_upper_wick_frac=min_upper_wick_frac, min_wick_to_body=min_wick_to_body,
    ))
    fw = float(min_upper_wick_frac)
    wb = float(min_wick_to_body)
    uwf = (upper / rng) if rng > 0 else None
    w2b = (upper / body) if body > 0 else None
    if rng <= 0:
        binding, bval, bdef = "zero_range", 0.0, None
    elif uwf is not None and uwf < fw:
        binding, bval, bdef = "upper_wick_frac", uwf, fw
    elif upper < wb * max(body, 1e-12):
        binding, bval, bdef = "wick_to_body", w2b, wb
    elif w2b is None:
        # doji body: the wick-to-body condition holds at any wick, so the range share decides
        binding, bval, bdef = "upper_wick_frac", uwf, fw
    else:
        slack_fw = (uwf / fw) if fw > 0 else float("inf")
        slack_wb = (w2b / wb) if wb > 0 else float("inf")
        if slack_fw <= slack_wb:
            binding, bval, bdef = "upper_wick_frac", uwf, fw
        else:
            binding, bval, bdef = "wick_to_body", w2b, wb
    return {
        "is_topping_tail": verdict,
        "range": round(rng, 6),
        "body": round(body, 6),
        "upper_wick": round(upper, 6),
        "upper_wick_frac": None if uwf is None else round(uwf, 6),
        "wick_to_body": None if w2b is None else round(w2b, 6),
        "min_upper_wick_frac": fw,
        "min_wick_to_body": wb,
        "binding": binding,
        "binding_value": None if bval is None else round(float(bval), 6),
        "binding_definition": bdef,
        "definition": "candle_shape_definition_not_tuned",
    }


def leg_topping_tail(leg: dict[str, Any] | None) -> dict[str, Any] | None:
    """``topping_tail_shape`` on a LEG candle (``entry_gates.leg_print_candle``), with the
    definitional print floor: None when there is no leg or fewer than
    ``TOPPING_TAIL_MIN_PRINTS`` prints (no candle -> the caller does not arm)."""
    if not isinstance(leg, dict):
        return None
    try:
        n = int(leg.get("n") or 0)
    except (TypeError, ValueError):
        return None
    if n < TOPPING_TAIL_MIN_PRINTS:
        return None
    shape = topping_tail_shape(leg.get("o"), leg.get("h"), leg.get("l"), leg.get("c"))
    if shape is None:
        return None
    shape["n"] = n
    shape["min_prints"] = TOPPING_TAIL_MIN_PRINTS
    return shape


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


def red_candle_volume_ratio_from_df(df: Any, *, lookback_bars: int = 10) -> float | None:
    """DISTRIBUTION TELL (Ross 2026-08-17 Aral #18: "Higher volume on the red
    candle"): sa huling ``lookback_bars`` na bar, ang ratio ng kabuuang volume sa
    PULANG bar (close < open) laban sa kabuuang volume sa BERDENG bar. >1.0 =
    mas malakas ang benta kaysa bili sa kamakailang tape = distribution warning.

    None (fail-open) kapag walang volume column / kulang na bars / puro
    doji-o-berde (walang red volume ⇒ 0.0 ang isusukat kung may green volume).
    Pure, telemetry-first: sinusukat muna ang discrimination bago maging
    exit-tilt input. [[feedback_evolve_not_devolve]]"""
    try:
        if df is None or getattr(df, "empty", True) or len(df) < 2:
            return None
        cols = {x.lower(): x for x in df.columns}
        if "volume" not in cols or "open" not in cols or "close" not in cols:
            return None
        tail = df.tail(int(max(2, lookback_bars)))
        red_vol = 0.0
        green_vol = 0.0
        for o, c, v in zip(
            tail[cols["open"]].tolist(),
            tail[cols["close"]].tolist(),
            tail[cols["volume"]].tolist(),
        ):
            try:
                o_f, c_f, v_f = float(o), float(c), float(v)
            except (TypeError, ValueError):
                continue
            if not (v_f >= 0.0):
                continue
            if c_f < o_f:
                red_vol += v_f
            elif c_f > o_f:
                green_vol += v_f
        if green_vol <= 0.0 and red_vol <= 0.0:
            return None
        if green_vol <= 0.0:
            return float("inf") if red_vol > 0 else None
        return red_vol / green_vol
    except Exception:
        return None


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
