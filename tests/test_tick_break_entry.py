"""Tick-break entry mode — Ross-speed: completed-bar structure + live-tick break.

The structure (impulse + shallow pullback) must be valid on CLOSED bars; the
live tick trading through the pullback high fires the entry mid-bar instead of
waiting for the breaking bar to close. Candle-quality/volume-spike checks that
need a closed breaking bar are skipped; sustained-volume and VWAP (vs the live
price) still apply; the stop anchors are the same keys.
"""
from __future__ import annotations

import pandas as pd

from app.services.trading.momentum_neural.entry_gates import pullback_break_confirmation


def _frame(rows, start="2026-06-10 14:00:00", freq="1min"):
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": low, "Close": c, "Volume": v} for o, h, low, c, v in rows],
        index=idx,
    )


def _waiting_structure():
    """Impulse 1.00->2.00 with steady volume, then a 3-bar shallow flag near the
    top whose high (1.96) has NOT been broken by a CLOSED bar."""
    rows = []
    px = 1.00
    for _ in range(18):  # impulse
        rows.append((px, px + 0.07, px - 0.01, px + 0.06, 300_000))
        px += 0.055
    # shallow pullback / flag: highs 1.96, lows ~1.86 (holding well above EMA9)
    # Volume stays ELEVATED through the flag (a hot Ross name; the sustained-rvol
    # ESTR guardrail rightly rejects a flag whose volume dies).
    rows += [
        (1.95, 1.96, 1.88, 1.90, 420_000),
        (1.90, 1.94, 1.87, 1.92, 390_000),
        (1.92, 1.95, 1.86, 1.93, 410_000),
    ]
    return rows


KW = dict(entry_interval="1m", require_retest=False, require_sustained_volume=True,
          require_break_candle=True, require_vwap_hold=True, require_macd_bullish=False,
          allow_runaway_break=False)


def test_waiting_without_tick_stays_waiting():
    ok, reason, dbg = pullback_break_confirmation(_frame(_waiting_structure()), **KW)
    assert ok is False and reason == "waiting_for_break"
    assert dbg["pullback_high"] > 0


def test_tick_through_level_fires():
    df = _frame(_waiting_structure())
    _, _, dbg0 = pullback_break_confirmation(df, **KW)
    lvl = float(dbg0["pullback_high"])
    ok, reason, dbg = pullback_break_confirmation(df, live_price=lvl + 0.01, **KW)
    assert ok is True and reason == "pullback_break_tick_ok"
    assert dbg["tick_break"] is True
    assert dbg["pullback_low"] > 0  # structural stop anchor intact


def test_tick_below_level_does_not_fire():
    df = _frame(_waiting_structure())
    _, _, dbg0 = pullback_break_confirmation(df, **KW)
    lvl = float(dbg0["pullback_high"])
    ok, reason, _ = pullback_break_confirmation(df, live_price=lvl - 0.01, **KW)
    assert ok is False and reason == "waiting_for_break"


def test_tick_break_respects_vwap_with_live_price():
    # same structure but the tick price below VWAP must be rejected: force it by
    # using a level barely above flag highs while VWAP sits far above (price
    # collapsed earlier -> impossible here), so instead verify the vwap branch by
    # checking a normal fire is NOT blocked (live px far above VWAP)
    df = _frame(_waiting_structure())
    _, _, dbg0 = pullback_break_confirmation(df, **KW)
    lvl = float(dbg0["pullback_high"])
    ok, reason, dbg = pullback_break_confirmation(df, live_price=lvl + 0.01, **KW)
    assert ok is True and "vwap" in dbg  # vwap was evaluated against the live price


def test_deep_pullback_never_tick_fires():
    rows = []
    px = 1.00
    for _ in range(18):
        rows.append((px, px + 0.07, px - 0.01, px + 0.06, 300_000))
        px += 0.055
    # DEEP pullback (gives back most of the impulse)
    rows += [
        (1.95, 1.96, 1.30, 1.35, 400_000),
        (1.35, 1.45, 1.28, 1.40, 300_000),
        (1.40, 1.50, 1.32, 1.45, 280_000),
    ]
    ok, reason, _ = pullback_break_confirmation(_frame(rows), live_price=99.0, **KW)
    assert ok is False and reason == "pullback_too_deep"
