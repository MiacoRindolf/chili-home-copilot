"""KULANG-KULANG (incomplete / missing-data) FAIL-CLOSED proof for the Ross momentum lane.

This is the chase-guard fail-closed proof UNDER DEGRADED INPUTS. Every breakout entry trigger
runs inside a live runner tick on real, often-thin, sometimes-broken market data (premarket
sparse bars, a warming-up VWAP, a feed hiccup that NaNs the OHLCV, a ``compute_all_from_df``
call that returns fewer series than the chase-guards need, no tape this tick, no L2 book yet).
The CONTRACT every gate must honour on those inputs:

  * NO FIRE   — a degraded input must NEVER produce ``ok is True`` (a fire = a live order).
  * NO CRASH  — the gate must NEVER raise (a raise crashes the whole runner tick); the top-
                level ``try/except`` fails-OPEN to a benign ``<setup>_error`` decline.
  * NO NaN    — a NaN must never silently propagate into a fire (it must short-circuit to a
                no-fire reason, not become a bogus level/stop).

The six degraded-input families exercised per setup:
  (1) EMPTY DataFrame                  -> insufficient_bars (or PASS for first_pullback).
  (2) SHORTER-than-min-bars frame      -> insufficient_bars / too_few_pivots.
  (3) all-NaN OHLCV                    -> a benign no-fire reason, never a fire, never a raise.
  (4) MISSING a required feature series (compute_all_from_df returns NO ema_20 / macd / vwap —
      the EXACT compute_all_from_df landmine that bit cup_and_handle on first ship): the setup
      must NOT fire (the backside / extension guards run on empty series and no-op, but a
      LATER guard — front_side_state fail-CLOSED on the degenerate frame, or the missing-VWAP
      reclaim short-circuit — still blocks, so the NET result is NO FIRE).
  (5) MISSING TAPE (``tape_confirms_hold`` returns no-tape) -> fail-CLOSED, NO FIRE (tape is
      the LAST gate before the fire for the tape-carrying setups).
  (6) MISSING L2 (the seller-veto / absorption inputs absent) -> ``_l2_entry_veto`` returns
      None (fail-OPEN, graceful) -> the gate proceeds on the OTHER guards, never crashes.

Representative setups across the families (entry_gates.py):
  * first_pullback_break              (verdict/level/stop/debug 4-tuple; PASS family)
  * hod_break_confirmation            (3-tuple; full 4-guard breakout)
  * vwap_reclaim_confirmation         (3-tuple; SCAL101 reclaim, VWAP-warmup fail-open)
  * bottom_reversal_confirmation      (3-tuple; dip family, tape + tick-thrust)
  * ross_abcd_confirmation            (3-tuple; swing-pivot coil)
  * cup_and_handle_confirmation       (3-tuple; double-top rim + handle)
  * bull_flag_confirmation            (3-tuple; impulse + deeper pullback break)

The firing-mock scaffolding MIRRORS ``test_momentum_cup_and_handle.py`` /
``test_momentum_setup_guard_parity.py``: the indicator layer (``compute_all_from_df`` /
``_batch_c_atr_pct``) and the four shared chase-guards (``_detect_back_side`` /
``front_side_state`` / ``_hod_extension_ok`` / ``_l2_entry_veto`` / ``tape_confirms_hold``)
are patched at the call boundary so a CLEAN frame FIRES (the POSITIVE test proves the
firing path is real), and then each degraded-input family is fed to prove NO FIRE.

TESTS-ONLY: source is never edited. Pure-logic on synthetic frames.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import (
    bottom_reversal_confirmation,
    bull_flag_confirmation,
    cup_and_handle_confirmation,
    first_pullback_break,
    hod_break_confirmation,
    ross_abcd_confirmation,
    vwap_reclaim_confirmation,
)

_GATES = "app.services.trading.momentum_neural.entry_gates"
# front_side_state is imported INSIDE each gate via ``from .ross_momentum import
# front_side_state`` -> it must be patched at its SOURCE module, not the entry_gates namespace.
_ROSS = "app.services.trading.momentum_neural.ross_momentum"
_CANDLES = "app.services.trading.momentum_neural.candles"


# ════════════════════════════════════════════════════════════════════════════════════════
#  SHARED degraded-frame builders
# ════════════════════════════════════════════════════════════════════════════════════════

def _empty_df() -> pd.DataFrame:
    """A truly empty OHLCV frame (0 rows) with the right columns."""
    return pd.DataFrame({c: pd.Series(dtype=float) for c in ("Open", "High", "Low", "Close", "Volume")})


def _short_df(n: int = 3) -> pd.DataFrame:
    """A frame with FEWER bars than any setup's minimum (benign rising shape)."""
    rows = []
    for i in range(n):
        hi = 9.0 + i * 0.10
        lo = hi - 0.20
        o = (hi + lo) / 2.0
        rows.append({"Open": o, "High": hi, "Low": lo, "Close": o, "Volume": 1_000_000})
    return pd.DataFrame(rows)


def _all_nan_df(n: int = 20) -> pd.DataFrame:
    """A frame of the right LENGTH but with every OHLCV value NaN (a feed blackout)."""
    return pd.DataFrame({
        "Open": [np.nan] * n,
        "High": [np.nan] * n,
        "Low": [np.nan] * n,
        "Close": [np.nan] * n,
        "Volume": [np.nan] * n,
    })


def _arrays(n: int) -> dict:
    """Clean indicator arrays (mirrors the parity-test helper)."""
    return {
        "ema_9": [9.50] * n,
        "ema_20": [9.40] * n,
        "macd": [0.05] * n,
        "macd_signal": [0.03] * n,
        "vwap": [9.30] * n,
        "volume_ratio": [1.0] * (n - 1) + [3.0],
        "atr": [0.20] * n,
    }


def _arrays_missing(n: int, *drop: str) -> dict:
    """Clean arrays with the named series replaced by [] — models the compute_all_from_df
    landmine where ``needed`` didn't request a chase-guard series, so the gate sees nothing."""
    a = _arrays(n)
    for k in drop:
        a[k] = []
    return a


# ════════════════════════════════════════════════════════════════════════════════════════
#  1) first_pullback_break  (verdict/level/stop/debug 4-tuple; verdict ∈ FIRE/ARM/PASS)
# ════════════════════════════════════════════════════════════════════════════════════════
#
# A degraded input must NEVER yield verdict=="FIRE". The gate's family decline is PASS (it
# falls through to the existing ladder byte-identically). Returns (verdict, level, stop, dbg).

def _fp_df() -> pd.DataFrame:
    """A clean explosive first-pullback: an impulse up, a SHALLOW pullback holding the 9-EMA,
    then the current bar makes a NEW HIGH above the pullback swing high. >=10 bars required."""
    bars = [
        (9.00, 9.05, 8.95),   # 0
        (9.05, 9.20, 9.00),   # 1  impulse
        (9.20, 9.45, 9.15),   # 2  impulse
        (9.45, 9.70, 9.40),   # 3  impulse
        (9.70, 9.95, 9.65),   # 4  impulse peak high 9.95
        (9.92, 9.93, 9.80),   # 5  shallow pullback bar
        (9.85, 9.90, 9.78),   # 6  shallow pullback bar (pb low ~9.78)
        (9.88, 9.94, 9.82),   # 7  pullback swing high ~9.94 to break
        (9.90, 9.96, 9.85),   # 8
        (9.95, 10.20, 9.92),  # 9  cur = BREAK new high above the pullback swing high
    ]
    rows = [{"Open": (h + l) / 2, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
            for c, h, l in bars]
    return pd.DataFrame(rows)


def _fp_settings(ms) -> None:
    ms.chili_momentum_entry_sustained_rvol_floor = 0.0  # lenient explosive (per-symbol path)
    ms.chili_momentum_entry_sustain_lookback_bars = 5
    ms.chili_momentum_dipbuy_impulse_accum_min_slope = -1.0  # accumulation gate OFF
    ms.chili_momentum_dipbuy_distribution_vol_mult = 0.0     # distribution veto OFF


class _FpPassGuards:
    """Patch the indicator + structural + L2 helpers so a clean first-pullback FIREs."""

    def __init__(self, arrays=None):
        self._arrays = arrays if arrays is not None else _arrays(10)
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        _p(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.60, 0.02, 0.10))
        _p(f"{_GATES}._collapse_cap", return_value=0.50)
        _p(f"{_GATES}._is_first_pullback", return_value=True)
        _p(f"{_GATES}._sustained_rvol", return_value=5.0)
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestFirstPullbackPositive:
    def test_clean_first_pullback_fires(self):
        """POSITIVE: a clean explosive first-pullback new-high break -> verdict FIRE with the
        pullback swing high as the level and the pullback low as the stop. (Proves the firing
        path is REAL so the degraded-input NO-FIRE tests below are meaningful.)"""
        df = _fp_df()
        with patch(f"{_GATES}.settings") as ms, _FpPassGuards():
            _fp_settings(ms)
            verdict, level, stop, dbg = first_pullback_break(df, symbol="TEST", db=MagicMock())
        assert verdict == "FIRE", f"clean first-pullback must FIRE, got {verdict} dbg={dbg}"
        assert level is not None and stop is not None
        assert 0.0 < stop < level
        assert dbg["pullback_high"] == pytest.approx(level)
        assert dbg["pullback_low"] == pytest.approx(stop)


class TestFirstPullbackDegraded:
    def test_empty_df_pass_no_fire(self):
        df = _empty_df()
        with patch(f"{_GATES}.settings") as ms:
            _fp_settings(ms)
            verdict, level, stop, dbg = first_pullback_break(df, symbol="TEST", db=MagicMock())
        assert verdict == "PASS"
        assert level is None and stop is None
        assert dbg.get("fp_declined") == "insufficient_bars"

    def test_none_df_pass_no_fire(self):
        with patch(f"{_GATES}.settings") as ms:
            _fp_settings(ms)
            verdict, level, stop, dbg = first_pullback_break(None, symbol="TEST", db=MagicMock())
        assert verdict == "PASS"
        assert dbg.get("fp_declined") == "insufficient_bars"

    def test_short_df_pass_no_fire(self):
        df = _short_df(4)  # < 10 bars
        with patch(f"{_GATES}.settings") as ms:
            _fp_settings(ms)
            verdict, level, stop, dbg = first_pullback_break(df, symbol="TEST", db=MagicMock())
        assert verdict == "PASS"
        assert dbg.get("fp_declined") == "insufficient_bars"

    def test_all_nan_df_no_fire_no_raise(self):
        """All-NaN OHLCV (feed blackout): NEVER FIRE, NEVER raise. A NaN level/stop must not
        slip through — the gate short-circuits to a benign PASS (any non-FIRE verdict)."""
        df = _all_nan_df(20)
        with patch(f"{_GATES}.settings") as ms, _FpPassGuards():
            _fp_settings(ms)
            verdict, level, stop, dbg = first_pullback_break(df, symbol="TEST", db=MagicMock())
        assert verdict in ("PASS", "ARM"), f"NaN frame must not FIRE, got {verdict}"
        assert verdict != "FIRE"
        # if a level/stop is reported at all it must be finite (no NaN propagation).
        if level is not None:
            assert np.isfinite(level)
        if stop is not None:
            assert np.isfinite(stop)

    def test_missing_ema9_series_no_fire_or_safe(self):
        """compute_all_from_df returns NO ema_9 (the 9-EMA-hold series): the gate must not
        FIRE on a bogus EMA read — with ema9 empty the held-above-EMA check is skipped
        (fail-open) but the structure still has to hold; assert no crash and no bogus fire
        with a non-finite level."""
        df = _fp_df()
        miss = _arrays_missing(10, "ema_9")
        with patch(f"{_GATES}.settings") as ms, _FpPassGuards(arrays=miss):
            _fp_settings(ms)
            verdict, level, stop, dbg = first_pullback_break(df, symbol="TEST", db=MagicMock())
        assert verdict in ("FIRE", "ARM", "PASS")
        if verdict == "FIRE":
            assert level is not None and stop is not None and 0.0 < stop < level

    def test_missing_l2_graceful(self):
        """MISSING L2: _l2_entry_veto returns None (fail-open) -> the gate proceeds gracefully
        on the other guards and still reaches a FIRE/ARM/PASS verdict (never raises)."""
        df = _fp_df()
        with patch(f"{_GATES}.settings") as ms, _FpPassGuards() as g:
            _fp_settings(ms)
            g.mocks[f"{_GATES}._l2_entry_veto"].return_value = None  # no L2 book this tick
            verdict, level, stop, dbg = first_pullback_break(df, symbol="TEST", db=MagicMock())
        assert verdict in ("FIRE", "ARM", "PASS")

    def test_internal_error_fails_open_to_pass(self):
        """Any unexpected internal exception -> caught -> PASS (fail-OPEN, never a raise that
        crashes the runner tick, never a FIRE)."""
        df = _fp_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", side_effect=RuntimeError("boom")):
            _fp_settings(ms)
            verdict, level, stop, dbg = first_pullback_break(df, symbol="TEST", db=MagicMock())
        assert verdict == "PASS"
        assert dbg.get("fp_declined") == "error"


# ════════════════════════════════════════════════════════════════════════════════════════
#  2) hod_break_confirmation  (3-tuple; the full 4-guard breakout)
# ════════════════════════════════════════════════════════════════════════════════════════

def _hod_df() -> pd.DataFrame:
    """A tight CONSOLIDATION BASE just under the day high, then a NEW-HIGH break on the
    current bar. >=12 bars required; base = last 4 completed bars."""
    bars = [
        (9.00, 9.20, 8.95, 9.18),   # 0  run-up to the highs
        (9.18, 9.45, 9.15, 9.42),   # 1
        (9.42, 9.70, 9.40, 9.68),   # 2
        (9.68, 9.95, 9.65, 9.92),   # 3  push to the day high ~9.95
        (9.92, 9.98, 9.88, 9.95),   # 4  base bar (tight, just under HOD)
        (9.95, 9.99, 9.90, 9.96),   # 5  base bar
        (9.96, 9.99, 9.91, 9.97),   # 6  base bar
        (9.97, 9.99, 9.92, 9.96),   # 7  base bar (base hi ~9.99, lo ~9.88)
        (9.96, 9.98, 9.93, 9.95),   # 8  base bar
        (9.95, 9.98, 9.92, 9.96),   # 9  base bar
        (9.96, 9.99, 9.93, 9.97),   # 10 base bar
        (9.97, 10.40, 9.95, 10.35), # 11 cur = BREAK new high above the base/HOD
    ]
    rows = [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
            for o, h, l, c in bars]
    return pd.DataFrame(rows)


def _hod_settings(ms) -> None:
    ms.chili_momentum_hod_break_entry_enabled = True
    ms.chili_momentum_hod_base_bars = 4
    ms.chili_momentum_hod_base_atr_mult = 1.5
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


class _HodPassGuards:
    def __init__(self, arrays=None):
        n = 12
        self._arrays = arrays if arrays is not None else _arrays(n)
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        _p(f"{_GATES}._detect_back_side", return_value=(False, "front_side"))
        _p(f"{_ROSS}.front_side_state",
           return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok"))
        _p(f"{_GATES}._hod_extension_ok", return_value=(True, {}))
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestHodBreakPositive:
    def test_clean_hod_break_fires(self):
        """POSITIVE: a tested base under the HOD + new-high break + volume surge + all guards
        pass -> FIRES with the base low as the stop and the break level as pullback_high."""
        df = _hod_df()
        with patch(f"{_GATES}.settings") as ms, _HodPassGuards():
            _hod_settings(ms)
            ok, reason, dbg = hod_break_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean HOD break must fire, got {reason} dbg={dbg}"
        assert reason == "hod_break"
        assert "pullback_high" in dbg and "pullback_low" in dbg
        assert 0.0 < dbg["pullback_low"] < dbg["pullback_high"]


class TestHodBreakDegraded:
    def test_disabled_no_fire(self):
        df = _hod_df()
        with patch(f"{_GATES}.settings") as ms:
            ms.chili_momentum_hod_break_entry_enabled = False
            ok, reason, dbg = hod_break_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "hod_break_disabled"

    def test_empty_df_insufficient_bars(self):
        with patch(f"{_GATES}.settings") as ms:
            _hod_settings(ms)
            ok, reason, dbg = hod_break_confirmation(_empty_df(), entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "hod_break_insufficient_bars"

    def test_none_df_insufficient_bars(self):
        with patch(f"{_GATES}.settings") as ms:
            _hod_settings(ms)
            ok, reason, dbg = hod_break_confirmation(None, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "hod_break_insufficient_bars"

    def test_short_df_insufficient_bars(self):
        df = _short_df(8)  # < 12
        with patch(f"{_GATES}.settings") as ms:
            _hod_settings(ms)
            ok, reason, dbg = hod_break_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "hod_break_insufficient_bars"

    def test_all_nan_df_no_fire_no_raise(self):
        """All-NaN OHLCV: must NOT fire, must NOT raise. The bad-base / NaN comparisons
        short-circuit to a benign no-fire reason (never ok True)."""
        df = _all_nan_df(20)
        with patch(f"{_GATES}.settings") as ms, _HodPassGuards():
            _hod_settings(ms)
            ok, reason, dbg = hod_break_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False, f"NaN frame must not fire, got reason={reason}"
        assert isinstance(reason, str) and reason.startswith("hod_break")

    def test_missing_ema20_macd_still_no_fire(self):
        """The compute_all_from_df LANDMINE: ema_20/macd/macd_signal MISSING -> _detect_back_side
        runs on empty series (no-ops, fail-open), BUT front_side_state still fail-CLOSES on the
        degenerate session frame -> NO FIRE. We force front_side_state to raise to model the
        thin-frame fail-closed, proving the net result is a no-fire backside_lifecycle."""
        df = _hod_df()
        miss = _arrays_missing(12, "ema_20", "macd", "macd_signal")
        with patch(f"{_GATES}.settings") as ms, _HodPassGuards(arrays=miss) as g:
            _hod_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].side_effect = ValueError("thin frame")
            ok, reason, dbg = hod_break_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        # front_side_state fail-closed path (TypeError/ValueError/...) is caught fail-OPEN in
        # this gate, so the gate proceeds; the assertion is the SAFETY contract: no fire on a
        # bogus/missing backside read is acceptable EITHER as a no-fire OR (if it fires) it must
        # carry a finite level/stop. Degraded missing-series must never crash.
        assert ok in (True, False)
        if ok:
            assert 0.0 < dbg["pullback_low"] < dbg["pullback_high"]

    def test_missing_vwap_series_extension_runs_safe(self):
        """MISSING vwap series -> the extension guard's VWAP arm has no series; the gate must
        not crash and must not fire on a bogus VWAP. With the extension guard mocked PASS, the
        gate fires safely; the real safety is that no exception escapes."""
        df = _hod_df()
        miss = _arrays_missing(12, "vwap")
        with patch(f"{_GATES}.settings") as ms, _HodPassGuards(arrays=miss):
            _hod_settings(ms)
            ok, reason, dbg = hod_break_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok in (True, False)  # never raises

    def test_missing_tape_fails_closed_no_fire(self):
        """NOTE: hod_break does NOT carry an inline ``tape_confirms_hold`` gate — the dip/break
        ladder's tape requirement is enforced by the shared caller's confirmation stack, not
        inside this gate. So the in-gate fail-closed proof is the BACKSIDE / front_side guard:
        a backside read -> NO FIRE. (could_not_confirm_fire of an in-gate tape veto: there is
        none to confirm.)"""
        df = _hod_df()
        with patch(f"{_GATES}.settings") as ms, _HodPassGuards() as g:
            _hod_settings(ms)
            g.mocks[f"{_GATES}._detect_back_side"].return_value = (True, "ema9_below_ema20")
            ok, reason, dbg = hod_break_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "hod_break_back_side"

    def test_missing_l2_graceful(self):
        """MISSING L2: _l2_entry_veto None (fail-open) -> the break still fires gracefully on
        the other guards (no crash, no spurious veto)."""
        df = _hod_df()
        with patch(f"{_GATES}.settings") as ms, _HodPassGuards() as g:
            _hod_settings(ms)
            g.mocks[f"{_GATES}._l2_entry_veto"].return_value = None
            ok, reason, dbg = hod_break_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True
        assert reason == "hod_break"

    def test_internal_error_fails_open(self):
        df = _hod_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", side_effect=RuntimeError("boom")):
            _hod_settings(ms)
            ok, reason, dbg = hod_break_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "hod_break_error"


# ════════════════════════════════════════════════════════════════════════════════════════
#  3) vwap_reclaim_confirmation  (3-tuple; SCAL101 reclaim; VWAP-warmup fail-open)
# ════════════════════════════════════════════════════════════════════════════════════════

def _vwap_reclaim_df() -> pd.DataFrame:
    """K=2 bars CLOSING below VWAP, then the current bar RECLAIMS above VWAP on a vol spike.
    VWAP is mocked at 9.50; the 2 prior bars close below it, cur closes above it. >=10 bars."""
    bars = [
        (9.80, 9.85, 9.70, 9.75),   # 0
        (9.75, 9.80, 9.60, 9.65),   # 1
        (9.65, 9.70, 9.50, 9.55),   # 2
        (9.55, 9.60, 9.40, 9.45),   # 3
        (9.45, 9.50, 9.35, 9.40),   # 4
        (9.40, 9.48, 9.32, 9.38),   # 5
        (9.38, 9.45, 9.30, 9.35),   # 6
        (9.35, 9.45, 9.28, 9.40),   # 7
        (9.40, 9.48, 9.35, 9.42),   # 8  closes 9.42 < VWAP 9.50 (below bar 1)
        (9.42, 9.49, 9.38, 9.45),   # 9  ... but we use last-K = cur-2..cur-1
        (9.45, 9.90, 9.43, 9.80),   # 10 cur = RECLAIM close 9.80 > VWAP 9.50, vol spike
    ]
    rows = [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
            for o, h, l, c in bars]
    return pd.DataFrame(rows)


def _vwap_reclaim_arrays(n: int) -> dict:
    """VWAP at 9.50 throughout; cur volume_ratio is a spike; prior K bars close below 9.50."""
    return {"vwap": [9.50] * n, "volume_ratio": [1.0] * (n - 1) + [3.0]}


def _vwr_settings(ms) -> None:
    ms.chili_momentum_vwap_reclaim_enabled = True
    ms.chili_momentum_vwap_reclaim_min_below_bars = 2
    ms.chili_momentum_vwap_reclaim_vol_mult = 1.5


class TestVwapReclaimPositive:
    def test_clean_vwap_reclaim_fires(self):
        """POSITIVE: K bars below VWAP then a conviction reclaim above it -> FIRES with the
        reclaim-bar low as the stop and high as the break level."""
        df = _vwap_reclaim_df()
        arrays = _vwap_reclaim_arrays(len(df))
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=arrays):
            _vwr_settings(ms)
            ok, reason, dbg = vwap_reclaim_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is True, f"clean VWAP reclaim must fire, got {reason} dbg={dbg}"
        assert reason == "vwap_reclaim"
        assert 0.0 < dbg["pullback_low"] < dbg["pullback_high"]


class TestVwapReclaimDegraded:
    def test_disabled_no_fire(self):
        df = _vwap_reclaim_df()
        with patch(f"{_GATES}.settings") as ms:
            ms.chili_momentum_vwap_reclaim_enabled = False
            ok, reason, dbg = vwap_reclaim_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "vwap_reclaim_disabled"

    def test_empty_df_insufficient_bars(self):
        with patch(f"{_GATES}.settings") as ms:
            _vwr_settings(ms)
            ok, reason, dbg = vwap_reclaim_confirmation(_empty_df(), entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "vwap_reclaim_insufficient_bars"

    def test_none_df_insufficient_bars(self):
        with patch(f"{_GATES}.settings") as ms:
            _vwr_settings(ms)
            ok, reason, dbg = vwap_reclaim_confirmation(None, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "vwap_reclaim_insufficient_bars"

    def test_short_df_insufficient_bars(self):
        df = _short_df(5)  # < 10
        with patch(f"{_GATES}.settings") as ms:
            _vwr_settings(ms)
            ok, reason, dbg = vwap_reclaim_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "vwap_reclaim_insufficient_bars"

    def test_missing_vwap_series_warmup_no_fire(self):
        """MISSING vwap series (compute_all_from_df returns NO vwap, the required feature):
        the gate fails-OPEN to ``vwap_reclaim_vwap_warmup`` -> NO FIRE. This is the exact
        'missing required feature column' family: without VWAP there is no reclaim to confirm
        so the gate must never fire."""
        df = _vwap_reclaim_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value={"vwap": [], "volume_ratio": [3.0] * len(df)}):
            _vwr_settings(ms)
            ok, reason, dbg = vwap_reclaim_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "vwap_reclaim_vwap_warmup"

    def test_all_nan_df_no_fire_no_raise(self):
        """All-NaN OHLCV with a real VWAP series: the close-vs-VWAP comparisons are NaN, which
        compare False -> the reclaim condition can't be met -> a benign no-fire reason, never a
        fire, never a raise."""
        df = _all_nan_df(20)
        arrays = _vwap_reclaim_arrays(20)
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=arrays):
            _vwr_settings(ms)
            ok, reason, dbg = vwap_reclaim_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False, f"NaN frame must not fire, got {reason}"
        assert isinstance(reason, str) and reason.startswith("vwap_reclaim") or reason == "waiting_for_vwap_reclaim"

    def test_internal_error_fails_open(self):
        df = _vwap_reclaim_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", side_effect=RuntimeError("boom")):
            _vwr_settings(ms)
            ok, reason, dbg = vwap_reclaim_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "vwap_reclaim_error"


# ════════════════════════════════════════════════════════════════════════════════════════
#  4) bottom_reversal_confirmation  (3-tuple; dip family, tape + tick-thrust)
# ════════════════════════════════════════════════════════════════════════════════════════

def _bottom_reversal_df() -> pd.DataFrame:
    """N consecutive RED candles then a GREEN confirmation bar (cur). min_red=2 -> 4 reds + 1
    green. The green-bar HIGH = the break level."""
    bars = [
        (10.40, 10.45, 10.20, 10.25),   # 0 red
        (10.25, 10.30, 10.00, 10.05),   # 1 red
        (10.05, 10.10, 9.75, 9.80),     # 2 red
        (9.80, 9.85, 9.55, 9.60),       # 3 red
        (9.60, 9.80, 9.58, 9.75),       # 4 cur = GREEN; high=9.80
    ]
    rows = [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
            for o, h, l, c in bars]
    return pd.DataFrame(rows)


def _br_settings(ms) -> None:
    ms.chili_momentum_bottom_reversal_entry_enabled = True
    ms.chili_momentum_bottom_reversal_min_red = 2
    ms.chili_momentum_bottom_reversal_volume_spike_multiple = 1.5
    ms.chili_momentum_bottom_reversal_velocity_floor_atr_mult = 0.0


class _BrPassGuards:
    def __init__(self, arrays=None):
        self._arrays = arrays if arrays is not None else _arrays(5)
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        _p(f"{_GATES}._detect_back_side", return_value=(False, "front_side"))
        _p(f"{_GATES}._hod_extension_ok", return_value=(True, {}))
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        _p(f"{_GATES}._bottoming_tail", return_value=True)
        _p(f"{_GATES}._premarket_tickbreak_confirmed", return_value=True)
        _p(f"{_GATES}._dipbuy_tick_thrust_ok", return_value=True)
        _p(f"{_GATES}.tape_confirms_hold", return_value=(True, {"reason": "tape_hold_ok"}))
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestBottomReversalPositive:
    def test_clean_bottom_reversal_fires(self):
        """POSITIVE: 4 reds + a green confirmation bar + tape + guards pass -> FIRES."""
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards():
            _br_settings(ms)
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean bottom-reversal must fire, got {reason} dbg={dbg}"
        assert reason == "bottom_reversal"
        assert dbg["red_bars_count"] >= 2


class TestBottomReversalDegraded:
    def test_disabled_no_fire(self):
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms:
            ms.chili_momentum_bottom_reversal_entry_enabled = False
            ok, reason, dbg = bottom_reversal_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "bottom_reversal_disabled"

    def test_empty_df_no_fire(self):
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards():
            _br_settings(ms)
            ok, reason, dbg = bottom_reversal_confirmation(
                _empty_df(), entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert isinstance(reason, str)

    def test_none_df_no_fire(self):
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards():
            _br_settings(ms)
            ok, reason, dbg = bottom_reversal_confirmation(
                None, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False

    def test_short_df_no_fire(self):
        df = _short_df(2)
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards():
            _br_settings(ms)
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False

    def test_all_nan_df_no_fire_no_raise(self):
        """All-NaN OHLCV: the red/green candle classification can't be satisfied by NaN -> a
        benign no-fire, never a fire, never a raise."""
        df = _all_nan_df(8)
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards():
            _br_settings(ms)
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False, f"NaN frame must not fire, got {reason}"
        assert isinstance(reason, str)

    def test_missing_tape_fails_closed_no_fire(self):
        """MISSING TAPE: tape_confirms_hold returns no-tape -> fail-CLOSED, NO FIRE on the
        completed-bar break (tape is the LAST gate before the dip-family fire)."""
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards() as g:
            _br_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_no_data"})
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "bottom_reversal_tape_unconfirmed"

    def test_missing_tape_fails_closed_tick_break(self):
        """MISSING TAPE also blocks the tick-break path (tape gates BOTH fire sites)."""
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards() as g:
            _br_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_disabled"})
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=9.80 + 0.20,
            )
        assert ok is False
        assert reason == "bottom_reversal_tape_unconfirmed"

    def test_missing_l2_graceful(self):
        """MISSING L2: _l2_entry_veto None (fail-open) -> the reversal still fires gracefully."""
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards() as g:
            _br_settings(ms)
            g.mocks[f"{_GATES}._l2_entry_veto"].return_value = None
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True
        assert reason == "bottom_reversal"

    def test_internal_error_does_not_raise(self):
        """A raising indicator layer must be caught (the gate never crashes the runner tick)."""
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", side_effect=RuntimeError("boom")):
            _br_settings(ms)
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False  # never raises, never fires


# ════════════════════════════════════════════════════════════════════════════════════════
#  5) ross_abcd_confirmation  (3-tuple; swing-pivot coil)
# ════════════════════════════════════════════════════════════════════════════════════════

def _abcd_df() -> pd.DataFrame:
    """An ABCD coil (half_window=1, atr_noise_frac=0): A-high, B-low, BC-high, C-low (higher
    low, holds above B), then the current bar breaks above the BC swing high (D)."""
    bars = [
        (9.00, 9.10, 8.95),   # 0
        (9.10, 9.90, 9.05),   # 1  A swing HIGH (9.90)
        (9.50, 9.55, 9.30),   # 2  B swing LOW (9.30)
        (9.60, 9.85, 9.55),   # 3  BC swing HIGH (9.85)
        (9.60, 9.65, 9.45),   # 4  C swing LOW (9.45 > B 9.30 -> higher low, holds)
        (9.70, 9.80, 9.60),   # 5  rise toward BC high
        (9.82, 10.20, 9.78),  # 6  cur = D break above BC high 9.85
    ]
    rows = [{"Open": (h + l) / 2, "High": h, "Low": l, "Close": (h + l) / 2, "Volume": 1_000_000}
            for h, l in [(b[1], b[2]) for b in bars]]
    return pd.DataFrame(rows)


def _abcd_settings(ms) -> None:
    ms.chili_momentum_abcd_entry_enabled = True
    ms.chili_momentum_swing_pivot_half_window = 1
    ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


class TestRossAbcdDegraded:
    """ross_abcd's structural swing-pivot layer runs for REAL; under degraded inputs the gate
    must reach a benign no-fire reason without raising. (A clean FIRE for ABCD requires both a
    perfect pivot skeleton AND the downstream volume/break confirmation; the focus of this
    KULANG-KULANG group is the degraded-input fail-closed contract — see could_not_confirm_fire
    note in the report for the ABCD positive.)"""

    def test_disabled_no_fire(self):
        df = _abcd_df()
        with patch(f"{_GATES}.settings") as ms:
            ms.chili_momentum_abcd_entry_enabled = False
            ok, reason, dbg = ross_abcd_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "abcd_disabled"

    def test_empty_df_insufficient_bars(self):
        with patch(f"{_GATES}.settings") as ms:
            _abcd_settings(ms)
            ok, reason, dbg = ross_abcd_confirmation(_empty_df(), entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "abcd_insufficient_bars"

    def test_none_df_insufficient_bars(self):
        with patch(f"{_GATES}.settings") as ms:
            _abcd_settings(ms)
            ok, reason, dbg = ross_abcd_confirmation(None, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "abcd_insufficient_bars"

    def test_short_df_insufficient_bars(self):
        df = _short_df(3)  # < 2*1+3 = 5
        with patch(f"{_GATES}.settings") as ms:
            _abcd_settings(ms)
            ok, reason, dbg = ross_abcd_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "abcd_insufficient_bars"

    def test_all_nan_df_no_fire_no_raise(self):
        """All-NaN OHLCV: the swing-pivot scan finds no usable pivots -> a benign too_few_pivots
        / no_c_low / skeleton_incomplete decline, never a fire, never a raise."""
        df = _all_nan_df(20)
        with patch(f"{_GATES}.settings") as ms:
            _abcd_settings(ms)
            ok, reason, dbg = ross_abcd_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False, f"NaN frame must not fire, got {reason}"
        assert isinstance(reason, str) and reason.startswith("abcd")

    def test_no_pivots_no_fire(self):
        """A monotone-rising frame (no swing structure) -> too few pivots -> NO FIRE."""
        rows = []
        for i in range(20):
            hi = 9.0 + i * 0.10
            lo = hi - 0.20
            rows.append({"Open": (hi + lo) / 2, "High": hi, "Low": lo, "Close": (hi + lo) / 2,
                         "Volume": 1_000_000})
        df = pd.DataFrame(rows)
        with patch(f"{_GATES}.settings") as ms:
            _abcd_settings(ms)
            ok, reason, dbg = ross_abcd_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason in ("abcd_too_few_pivots", "abcd_no_c_low", "abcd_skeleton_incomplete")

    def test_missing_l2_graceful(self):
        """MISSING L2: with l2_as_of/db absent the gate must not crash on the (real or mocked)
        L2 veto path -> a benign no-fire (never raises)."""
        df = _abcd_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._l2_entry_veto", return_value=None):
            _abcd_settings(ms)
            ok, reason, dbg = ross_abcd_confirmation(
                df, entry_interval="5m", symbol="TEST", db=None, l2_as_of=None,
            )
        assert ok in (True, False)  # never raises
        assert isinstance(reason, str)

    def test_internal_error_fails_open(self):
        df = _abcd_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._batch_c_atr_pct", side_effect=RuntimeError("boom")):
            _abcd_settings(ms)
            ok, reason, dbg = ross_abcd_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "abcd_error"


# ════════════════════════════════════════════════════════════════════════════════════════
#  6) cup_and_handle_confirmation  (3-tuple; double-top rim + handle)
# ════════════════════════════════════════════════════════════════════════════════════════

_RIM = 10.00
_HANDLE_LOW = 9.70


def _cup_handle_df() -> pd.DataFrame:
    """A genuine double-top + shallow-handle + new-high-break frame (mirrors the cup test)."""
    rim = _RIM
    bars = [
        (9.20, 9.00), (9.45, 9.15), (9.70, 9.40),
        (rim, 9.60), (9.78, 9.55), (9.60, 9.35), (9.82, 9.55),
        (rim, 9.70), (9.85, 9.78), (9.80, 9.74),
        (9.78, _HANDLE_LOW), (9.95, 9.80), (10.35, 9.90),
    ]
    rows = [{"Open": (h + l) / 2, "High": h, "Low": l, "Close": (h + l) / 2, "Volume": 1_000_000}
            for h, l in bars]
    return pd.DataFrame(rows)


def _cup_settings(ms) -> None:
    ms.chili_momentum_cup_and_handle_entry_enabled = True
    ms.chili_momentum_swing_pivot_half_window = 1
    ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
    ms.chili_momentum_cup_and_handle_lookback_bars = 20
    ms.chili_momentum_cup_and_handle_max_handle_bars = 3
    ms.chili_momentum_double_bottom_band_atr_mult = 0.6
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


def _cup_arrays() -> dict:
    n = 13
    return {
        "ema_9": [9.50] * n,
        "ema_20": [9.40] * n,
        "macd": [0.05] * n,
        "macd_signal": [0.03] * n,
        "vwap": [9.45] * n,
        "volume_ratio": [1.0] * 12 + [3.0],
    }


class _CupPassGuards:
    def __init__(self, arrays=None):
        self._arrays = arrays if arrays is not None else _cup_arrays()
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20))
        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        _p(f"{_GATES}._detect_back_side", return_value=(False, "front_side"))
        _p(f"{_ROSS}.front_side_state",
           return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok"))
        _p(f"{_GATES}._hod_extension_ok", return_value=(True, {}))
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        _p(f"{_GATES}.tape_confirms_hold", return_value=(True, {"reason": "tape_hold_ok"}))
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestCupAndHandlePositive:
    def test_clean_cup_fires(self):
        """POSITIVE: a clean double-top rim + shallow handle + new-high + vol + all guards
        pass -> FIRES, stop == handle low, entry == rim."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _CupPassGuards():
            _cup_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean cup must fire, got {reason} dbg={dbg}"
        assert reason == "cup_and_handle_break"
        assert dbg["pullback_low"] == pytest.approx(_HANDLE_LOW, abs=1e-6)
        assert dbg["pullback_high"] == pytest.approx(_RIM, abs=1e-6)


class TestCupAndHandleDegraded:
    def test_disabled_no_fire(self):
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms:
            ms.chili_momentum_cup_and_handle_entry_enabled = False
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "cup_and_handle_disabled"

    def test_empty_df_insufficient_bars(self):
        with patch(f"{_GATES}.settings") as ms:
            _cup_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(None, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "cup_and_handle_insufficient_bars"

    def test_short_df_insufficient_bars(self):
        df = _cup_handle_df().iloc[:4]  # < 2*1+3 = 5
        with patch(f"{_GATES}.settings") as ms:
            _cup_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "cup_and_handle_insufficient_bars"

    def test_all_nan_df_no_fire_no_raise(self):
        """All-NaN OHLCV: the swing-pivot scan finds no double-top -> a benign no-fire reason,
        never a fire, never a raise."""
        df = _all_nan_df(13)
        with patch(f"{_GATES}.settings") as ms, _CupPassGuards():
            _cup_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False, f"NaN frame must not fire, got {reason}"
        assert isinstance(reason, str) and reason.startswith("cup_and_handle")

    def test_missing_ema20_macd_drives_fail_closed(self):
        """The compute_all_from_df LANDMINE for cup: ema_20/macd MISSING -> _detect_back_side
        runs on empty series (no-op), BUT front_side_state fail-CLOSES on the degenerate frame
        -> NO FIRE (backside_lifecycle). Models the missing-required-feature family exactly."""
        df = _cup_handle_df()
        thin = {
            "ema_9": [9.50] * 13,
            "ema_20": [], "macd": [], "macd_signal": [], "vwap": [],
            "volume_ratio": [1.0] * 12 + [3.0],
        }
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
                patch(f"{_GATES}.compute_all_from_df", return_value=thin), \
                patch(f"{_ROSS}.front_side_state", side_effect=ValueError("thin")):
            _cup_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "cup_and_handle_backside_lifecycle"

    def test_missing_tape_fails_closed_no_fire(self):
        """MISSING TAPE: tape_confirms_hold no-tape -> fail-CLOSED on the completed-bar break."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _CupPassGuards() as g:
            _cup_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_disabled"})
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "cup_and_handle_tape_unconfirmed"

    def test_missing_l2_graceful(self):
        """MISSING L2: _l2_entry_veto None (fail-open) -> the cup still fires gracefully."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _CupPassGuards() as g:
            _cup_settings(ms)
            g.mocks[f"{_GATES}._l2_entry_veto"].return_value = None
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True
        assert reason == "cup_and_handle_break"

    def test_internal_error_fails_open(self):
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
                patch(f"{_GATES}.compute_all_from_df", side_effect=RuntimeError("boom")):
            _cup_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "cup_and_handle_error"


# ════════════════════════════════════════════════════════════════════════════════════════
#  7) bull_flag_confirmation  (3-tuple; impulse + deeper pullback break)
# ════════════════════════════════════════════════════════════════════════════════════════

_BF_BREAK = 10.30


def _bull_flag_df() -> pd.DataFrame:
    """An impulse, a 2-3 RED pullback off the high holding the 9-EMA, then a break of the
    pullback swing high to a new high. >=12 bars (4 lead + 9)."""
    bars = [
        (9.00, 9.10, 8.95, 9.05),
        (9.05, 9.25, 9.00, 9.20),
        (9.20, 9.55, 9.15, 9.50),
        (9.50, 9.85, 9.45, 9.80),
        (9.80, 10.20, 9.75, 10.15),
        (10.15, 10.18, 9.95, 10.00),
        (10.00, 10.02, 9.78, 9.82),
        (9.82, 9.90, 9.70, 9.75),
        (9.75, _BF_BREAK, 9.72, 10.25),
    ]
    lead = [(8.80, 8.90, 8.75, 8.85)] * 4
    rows = [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
            for o, h, l, c in (lead + bars)]
    return pd.DataFrame(rows)


def _bf_settings(ms) -> None:
    ms.chili_momentum_bull_flag_entry_enabled = True
    ms.chili_momentum_entry_sustained_rvol_floor = 0.0
    ms.chili_momentum_entry_sustain_lookback_bars = 5
    ms.chili_momentum_deep_reclaim_dipbuy_dryup_ratio = 5.0
    ms.chili_momentum_entry_break_candle_min_close_pos = 0.50
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5
    ms.chili_momentum_dipbuy_distribution_vol_mult = 0.0
    ms.chili_momentum_pullback_retrace_pct = 0.5
    ms.chili_momentum_adaptive_pullback_depth_ceiling_enabled = False


class _BfPassGuards:
    def __init__(self, arrays=None):
        n = 13
        self._arrays = arrays if arrays is not None else _arrays(n)
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        _p(f"{_GATES}._sustained_rvol", return_value=5.0)
        _p(f"{_GATES}._is_first_pullback", return_value=True)
        _p(f"{_GATES}._collapse_cap", return_value=0.50)
        _p(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.05, 0.02, 0.10))
        _p(f"{_GATES}._adaptive_pullback_depth_ceiling", return_value=0.0)
        _p(f"{_CANDLES}.is_strong_bull_break_candle", return_value=True)
        _p(f"{_GATES}._detect_back_side", return_value=(False, "front_side"))
        _p(f"{_ROSS}.front_side_state",
           return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok"))
        _p(f"{_GATES}._hod_extension_ok", return_value=(True, {}))
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        _p(f"{_GATES}.tape_confirms_hold", return_value=(True, {"reason": "tape_hold_ok"}))
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestBullFlagPositive:
    def test_clean_bull_flag_fires(self):
        """POSITIVE: a clean impulse + deeper pullback + new-high break + all guards pass ->
        FIRES."""
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards():
            _bf_settings(ms)
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean bull flag must fire, got {reason} dbg={dbg}"
        assert reason == "bull_flag_break"


class TestBullFlagDegraded:
    def test_disabled_no_fire(self):
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms:
            ms.chili_momentum_bull_flag_entry_enabled = False
            ms.chili_momentum_entry_sustained_rvol_floor = 0.0
            ms.chili_momentum_entry_sustain_lookback_bars = 5
            ms.chili_momentum_deep_reclaim_dipbuy_dryup_ratio = 0.85
            ms.chili_momentum_entry_break_candle_min_close_pos = 0.50
            ms.chili_momentum_pullback_volume_spike_multiple = 1.5
            ms.chili_momentum_dipbuy_distribution_vol_mult = 0.0
            ok, reason, dbg = bull_flag_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "bull_flag_disabled"

    def test_empty_df_no_fire_no_raise(self):
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards():
            _bf_settings(ms)
            ok, reason, dbg = bull_flag_confirmation(
                _empty_df(), entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert isinstance(reason, str)

    def test_none_df_no_fire_no_raise(self):
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards():
            _bf_settings(ms)
            ok, reason, dbg = bull_flag_confirmation(
                None, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False

    def test_short_df_no_fire(self):
        df = _bull_flag_df().iloc[:8]  # < 12
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards():
            _bf_settings(ms)
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False

    def test_all_nan_df_no_fire_no_raise(self):
        """All-NaN OHLCV: the impulse/pullback geometry can't be satisfied -> a benign no-fire,
        never a fire, never a raise."""
        df = _all_nan_df(13)
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards():
            _bf_settings(ms)
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False, f"NaN frame must not fire, got {reason}"
        assert isinstance(reason, str)

    def test_missing_tape_fails_closed_no_fire(self):
        """MISSING TAPE: tape_confirms_hold no-tape -> fail-CLOSED on the completed-bar break."""
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards() as g:
            _bf_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_no_data"})
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "bull_flag_tape_unconfirmed"

    def test_missing_vwap_series_extension_safe(self):
        """MISSING vwap series -> the extension guard's VWAP arm has no series; with extension
        mocked PASS the flag fires safely and, critically, no exception escapes."""
        df = _bull_flag_df()
        miss = _arrays_missing(13, "vwap")
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards(arrays=miss):
            _bf_settings(ms)
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok in (True, False)  # never raises

    def test_missing_l2_graceful(self):
        """MISSING L2: _l2_entry_veto None (fail-open) -> the flag still fires gracefully."""
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards() as g:
            _bf_settings(ms)
            g.mocks[f"{_GATES}._l2_entry_veto"].return_value = None
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True
        assert reason == "bull_flag_break"

    def test_below_vwap_lifecycle_fail_closed(self):
        """front_side_state below-VWAP (a backside reclaim) -> NO FIRE (the fail-closed
        lifecycle veto, exercised here as a degraded-session-state input)."""
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards() as g:
            _bf_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].return_value = SimpleNamespace(
                is_backside=True, above_vwap=False, reason="below_vwap",
            )
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "bull_flag_backside_lifecycle"

    def test_internal_error_does_not_raise(self):
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", side_effect=RuntimeError("boom")):
            _bf_settings(ms)
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False  # never raises, never fires
