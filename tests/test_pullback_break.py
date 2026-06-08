"""Ross-style pullback-break entry trigger (1m/5m) + RECENT refinements:
break-retest (#1), sustaining-volume gate (#3), breakout-or-bailout helper (#2)."""
from __future__ import annotations

import pandas as pd

from app.services.trading.momentum_neural.entry_gates import (
    _vol_aware_pullback_tolerances,
    breakout_failed_to_hold,
    pullback_break_confirmation,
)


def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"Open": c, "High": h, "Low": lo, "Close": c, "Volume": v} for (c, h, lo, v) in rows]
    )


def _base(close: float, vol: float = 1000.0) -> tuple[float, float, float, float]:
    return (close, close + 0.3, close - 0.3, vol)


def _retest_rows() -> list[tuple[float, float, float, float]]:
    """Long flat base (so EMA-9 lags), impulse, consolidation, then break -> retest
    of the broken level -> hold -> reclaim with a volume spike."""
    r = [(100.0, 100.2, 99.8, 1000.0) for _ in range(25)]
    r += [(105.0, 105.2, 104.8, 1000.0), (108.0, 108.2, 107.8, 1000.0)]
    r += [(109.5, 109.7, 109.3, 1000.0), (109.6, 109.8, 109.4, 1000.0), (109.4, 109.6, 109.2, 1000.0)]
    r += [(110.2, 110.4, 109.6, 3000.0)]    # break
    r += [(109.6, 109.9, 109.50, 3000.0)]   # retest dip back to the level
    r += [(109.7, 109.9, 109.55, 3000.0)]
    r += [(109.9, 110.1, 109.70, 3000.0)]
    r += [(110.3, 110.6, 110.00, 3000.0)]   # reclaim + volume
    return r


def test_pullback_break_fires_on_shallow_pullback_then_break() -> None:
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]  # impulse
    rows += [_base(109.0, 800.0), _base(108.5, 800.0)]  # shallow pullback (holds high)
    rows.append((110.6, 111.2, 109.6, 3200.0))  # current: breaks pullback high + volume spike
    ok, reason, dbg = pullback_break_confirmation(_df(rows), entry_interval="5m")
    assert ok is True, (reason, dbg)
    assert reason == "pullback_break_ok"
    assert "pullback_low" in dbg  # structural stop available


def test_deep_pullback_rejected() -> None:
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]
    rows += [_base(103.0, 800.0), _base(102.0, 800.0)]  # deep pullback (>50% retrace)
    rows.append((104.0, 110.5, 103.0, 3200.0))
    ok, reason, _ = pullback_break_confirmation(_df(rows), entry_interval="5m")
    assert ok is False
    assert reason == "pullback_too_deep"


def test_no_break_waits() -> None:
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]
    rows += [_base(109.0, 800.0), _base(108.5, 800.0)]
    rows.append((109.0, 109.5, 108.0, 3200.0))  # current high 109.5 < pullback high ~110.3
    ok, reason, _ = pullback_break_confirmation(_df(rows), entry_interval="5m")
    assert ok is False
    assert reason == "waiting_for_break"


def test_insufficient_bars() -> None:
    ok, reason, _ = pullback_break_confirmation(_df([_base(100.0) for _ in range(5)]))
    assert ok is False
    assert reason == "insufficient_bars"


# ── #1 break-retest ────────────────────────────────────────────────────────

def test_retest_fires_on_break_then_retest_then_reclaim() -> None:
    ok, reason, dbg = pullback_break_confirmation(
        _df(_retest_rows()), entry_interval="5m", require_retest=True, require_sustained_volume=False
    )
    assert ok is True, (reason, dbg)
    assert reason == "pullback_break_ok"
    assert dbg.get("mode") == "retest"
    assert "pullback_high" in dbg and "pullback_low" in dbg  # breakout level + structural stop


def test_retest_does_not_fire_on_runaway_first_break() -> None:
    # Broke then ran straight up (lows stay above the level) — never offered a retest.
    rows = _retest_rows()[:31] + [
        (110.5, 110.7, 110.30, 3000.0),
        (110.9, 111.1, 110.70, 3000.0),
        (111.3, 111.5, 111.10, 3000.0),
        (111.7, 111.9, 111.50, 3000.0),
    ]
    ok, reason, _ = pullback_break_confirmation(
        _df(rows), entry_interval="5m", require_retest=True, require_sustained_volume=False
    )
    assert ok is False
    assert reason == "waiting_for_retest"


def test_retest_rejects_failed_hold() -> None:
    # Broke, retested, but LOST the level on a close (failed breakout) — not bought.
    rows = _retest_rows()[:31] + [
        (109.5, 109.7, 109.30, 3000.0),
        (109.2, 109.4, 108.90, 3000.0),   # closes well below the level
        (109.3, 109.5, 109.00, 3000.0),
        (110.0, 110.2, 109.40, 3000.0),
    ]
    ok, reason, _ = pullback_break_confirmation(
        _df(rows), entry_interval="5m", require_retest=True, require_sustained_volume=False
    )
    assert ok is False
    assert reason == "retest_failed_hold"


def test_raw_mode_unchanged_when_retest_off() -> None:
    # The canonical raw fire still fires identically with the new params defaulted off.
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]
    rows += [_base(109.0, 800.0), _base(108.5, 800.0)]
    rows.append((110.6, 111.2, 109.6, 3200.0))
    ok, reason, _ = pullback_break_confirmation(_df(rows), entry_interval="5m", require_retest=False)
    assert ok is True
    assert reason == "pullback_break_ok"


# ── #3 sustaining-volume gate ──────────────────────────────────────────────

def _faded_rows() -> list[tuple[float, float, float, float]]:
    """Setup whose break bar spikes vs the recent (dead) average, but whose recent
    rel-vol has FADED — a 24h mover gone quiet by entry time."""
    r = [(100.0, 100.2, 99.8, 1000.0) for _ in range(25)]
    r += [(c, c + 0.3, c - 0.3, 500.0) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]
    r += [(109.0, 109.3, 108.7, 500.0), (108.5, 108.8, 108.2, 500.0)]
    r += [(110.6, 111.2, 109.6, 1300.0)]
    return r


def test_sustaining_volume_blocks_faded_mover() -> None:
    ok, reason, dbg = pullback_break_confirmation(
        _df(_faded_rows()), entry_interval="5m",
        require_sustained_volume=True, sustained_rvol_floor=1.0,
    )
    assert ok is False
    assert reason == "faded_volume_no_sustain"
    assert dbg.get("sustained_rvol") is not None and dbg["sustained_rvol"] < 1.0


def test_sustaining_volume_off_lets_faded_through() -> None:
    ok, reason, _ = pullback_break_confirmation(
        _df(_faded_rows()), entry_interval="5m", require_sustained_volume=False
    )
    assert ok is True
    assert reason == "pullback_break_ok"


# ── #2 breakout-or-bailout fast-exit decision ──────────────────────────────

def test_breakout_failed_to_hold_true_inside_window() -> None:
    assert breakout_failed_to_hold(
        breakout_level=110.0, bid=109.8, held_seconds=120, window_seconds=600
    ) is True


def test_breakout_failed_to_hold_false_outside_window() -> None:
    assert breakout_failed_to_hold(
        breakout_level=110.0, bid=109.8, held_seconds=900, window_seconds=600
    ) is False


def test_breakout_failed_to_hold_false_when_level_holds() -> None:
    assert breakout_failed_to_hold(
        breakout_level=110.0, bid=110.5, held_seconds=120, window_seconds=600
    ) is False


def test_breakout_failed_to_hold_guards() -> None:
    # No level / bad inputs / non-positive window -> never fires (won't fight the stop).
    assert breakout_failed_to_hold(breakout_level=None, bid=110.0, held_seconds=10, window_seconds=600) is False
    assert breakout_failed_to_hold(breakout_level=110.0, bid=None, held_seconds=10, window_seconds=600) is False
    assert breakout_failed_to_hold(breakout_level=110.0, bid=109.0, held_seconds=10, window_seconds=0) is False
    assert breakout_failed_to_hold(breakout_level="x", bid=109.0, held_seconds=10, window_seconds=600) is False


def test_breakout_buffer_suppresses_wick_noise() -> None:
    # A bid a hair below the level (inside the buffer) does NOT bail; clearly below does.
    assert breakout_failed_to_hold(
        breakout_level=100.0, bid=99.95, held_seconds=10, window_seconds=600, buffer_pct=0.001
    ) is False  # 99.95 >= 100*(1-0.001)=99.90
    assert breakout_failed_to_hold(
        breakout_level=100.0, bid=99.80, held_seconds=10, window_seconds=600, buffer_pct=0.001
    ) is True


# ── volatility-aware pullback tolerances (selection<->entry alignment) ───────

def test_vol_aware_calm_name_is_ross_floor() -> None:
    # No / zero ATR -> exactly the original Ross floors (backward-compatible: calm
    # large-caps behave as before; only volatile small-caps get extra room).
    for atr in (None, 0.0):
        shallow, ema_wick, retest = _vol_aware_pullback_tolerances(atr, 0.50)
        assert shallow == 0.50
        assert ema_wick == 0.001
        assert retest == 0.0


def test_vol_aware_volatile_smallcap_gets_room() -> None:
    # A 10%-ATR small-cap is allowed a deeper flag, a bigger EMA-9 wick, and retest room.
    shallow, ema_wick, retest = _vol_aware_pullback_tolerances(0.10, 0.50)
    assert shallow > 0.50
    assert ema_wick > 0.001
    assert retest > 0.0
    assert shallow <= 0.75  # never beyond the reversal ceiling


def test_vol_aware_shallow_cap_respects_ceiling() -> None:
    # Even at absurd volatility the shallow cap is hard-capped (still a pullback, not a reversal).
    shallow, _, _ = _vol_aware_pullback_tolerances(5.0, 0.50)
    assert shallow == 0.75


def test_vol_aware_scales_monotonically_with_atr() -> None:
    s_lo, w_lo, r_lo = _vol_aware_pullback_tolerances(0.02, 0.50)
    s_hi, w_hi, r_hi = _vol_aware_pullback_tolerances(0.15, 0.50)
    assert s_hi >= s_lo and w_hi > w_lo and r_hi > r_lo


def test_vol_aware_respects_passed_base_threshold() -> None:
    # The base retrace knob is honored (vol scaling is additive on top of it).
    shallow_a, _, _ = _vol_aware_pullback_tolerances(0.05, 0.40)
    shallow_b, _, _ = _vol_aware_pullback_tolerances(0.05, 0.50)
    assert shallow_b > shallow_a
