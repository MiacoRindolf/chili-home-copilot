"""Candlestick helpers for the Ross momentum lane — conviction break candle
(entry confirmation) + topping-tail/shooting-star (runner profit-exit)."""
from __future__ import annotations

import pandas as pd

from app.services.trading.momentum_neural.candles import (
    break_candle_ok_from_df,
    is_strong_bull_break_candle,
    is_topping_tail,
    topping_tail_from_df,
)


# ── conviction break candle ──────────────────────────────────────────────────

def test_strong_bull_break_candle_true() -> None:
    # Green, closes at the top of its range, tiny wicks -> conviction break.
    assert is_strong_bull_break_candle(o=10.0, h=10.9, l=9.95, c=10.85) is True


def test_red_break_candle_rejected() -> None:
    assert is_strong_bull_break_candle(o=10.8, h=10.9, l=10.0, c=10.1) is False


def test_topping_tail_break_candle_rejected() -> None:
    # Green body but a long upper wick (closed back down) -> not conviction.
    assert is_strong_bull_break_candle(o=10.0, h=11.0, l=9.95, c=10.2) is False


def test_weak_close_in_lower_half_rejected() -> None:
    # Closes in the lower half of the range -> weak.
    assert is_strong_bull_break_candle(o=10.0, h=10.8, l=9.9, c=10.1) is False


def test_zero_range_break_candle_false() -> None:
    assert is_strong_bull_break_candle(o=10.0, h=10.0, l=10.0, c=10.0) is False


# ── topping tail / shooting star ─────────────────────────────────────────────

def test_topping_tail_true() -> None:
    # Long upper wick dominating the range, small body -> exhaustion/rejection.
    assert is_topping_tail(o=10.0, h=11.0, l=9.95, c=10.1) is True


def test_strong_bull_is_not_topping_tail() -> None:
    assert is_topping_tail(o=10.0, h=10.9, l=9.95, c=10.85) is False


def test_hammer_long_lower_wick_is_not_topping_tail() -> None:
    # Long LOWER wick (hammer) -> not a topping tail.
    assert is_topping_tail(o=10.7, h=10.8, l=9.8, c=10.75) is False


def test_topping_tail_zero_range_false() -> None:
    assert is_topping_tail(o=10.0, h=10.0, l=10.0, c=10.0) is False


# ── frame wrappers: fail-open / fail-safe ────────────────────────────────────

def _df(rows):
    return pd.DataFrame([{"Open": o, "High": h, "Low": l, "Close": c} for (o, h, l, c) in rows])


def test_break_candle_ok_fail_open_on_empty() -> None:
    # No data -> True (never blocks an otherwise-valid entry).
    assert break_candle_ok_from_df(None) is True
    assert break_candle_ok_from_df(_df([])) is True


def test_topping_tail_fail_safe_on_empty() -> None:
    # No data -> False (never forces an exit).
    assert topping_tail_from_df(None) is False
    assert topping_tail_from_df(_df([])) is False


def test_frame_wrappers_read_last_bar() -> None:
    assert break_candle_ok_from_df(_df([(9.0, 9.5, 8.9, 9.4), (10.0, 10.9, 9.95, 10.85)])) is True
    assert topping_tail_from_df(_df([(9.0, 9.5, 8.9, 9.4), (10.0, 11.0, 9.95, 10.1)])) is True
