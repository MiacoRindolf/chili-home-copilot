"""Chase-guard PARITY for four ALREADY-LIVE-but-guard-incomplete momentum setups.

These four entry triggers fire LIVE entries (real money), so a missing chase-guard = a CHASE
= a loss. The hardening here MIRRORS the reference triggers ``wedge_break_entry`` /
``absorption_snap_entry`` / ``cup_and_handle_confirmation`` — the four shared chase-guards
EVERY breakout trigger must carry:

  1. NOT BACKSIDE / NOT BELOW-VWAP -- ``_detect_back_side`` (1m EMA/MACD rollover, ARRAY
     signature) AND ``front_side_state`` (fails CLOSED on a thin/degenerate frame).
  2. NOT PARABOLIC -- ``_hod_extension_ok`` vs the 9-EMA AND VWAP (rejects a vertical run).
  3. L2 hidden-/big-seller veto -- ``_l2_entry_veto`` (already present in all four).
  4. TAPE REQUIRED + FAIL-CLOSED -- ``tape_confirms_hold`` is the LAST gate before each
     return-True (the dip family ``ma_vwap_pullback`` substitutes tick-thrust+volume for
     inline tape, so it carries front_side_state but NOT tape — by design).

Per setup, these tests assert each newly-added guard, when tripped, blocks the fire with the
expected ``<setup>_<reason>``. CRITICAL extra fence (fix #1): ``inverse_head_shoulders`` had a
CONFIRMED LIVE BUG — it called ``_detect_back_side(df, entry_interval=...)`` (a DataFrame as
the ema9 arg + a bad kwarg) which raised a TypeError swallowed fail-OPEN, so the backside veto
SILENTLY NO-OPPED on every call. The test ``test_detect_back_side_called_with_array_signature``
pins the corrected ARRAY signature so the no-op can never silently return.

These are PURE-LOGIC tests on synthetic OHLCV frames. The structural layer is either run for
real (where the geometry is simple) or mocked at the call boundary (where it is intricate); the
indicator layer (``compute_all_from_df`` / ATR) and the four guards are mocked so each guard can
be regressed INDEPENDENTLY: a baseline FIRES, and flipping exactly ONE guard to fail ⇒ NO FIRE.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import (
    bottom_reversal_confirmation,
    bull_flag_confirmation,
    inverse_head_shoulders_confirmation,
    ma_vwap_pullback_confirmation,
)

_GATES = "app.services.trading.momentum_neural.entry_gates"
# front_side_state is imported INSIDE each gate via ``from .ross_momentum import
# front_side_state`` -> it must be patched at its SOURCE module, not the entry_gates namespace.
_ROSS = "app.services.trading.momentum_neural.ross_momentum"
_CANDLES = "app.services.trading.momentum_neural.candles"


def _arrays(n: int) -> dict:
    """Clean indicator arrays: front-side EMA stack (9>20), bullish MACD, a low VWAP so
    extension/below-VWAP never trip by accident, and a break-bar volume surge."""
    return {
        "ema_9": [9.50] * n,
        "ema_20": [9.40] * n,
        "macd": [0.05] * n,
        "macd_signal": [0.03] * n,
        "vwap": [9.30] * n,
        "volume_ratio": [1.0] * (n - 1) + [3.0],
        "atr": [0.20] * n,
    }


# ════════════════════════════════════════════════════════════════════════════════════════
#  FIX #1 — inverse_head_shoulders_confirmation
#  (a) the _detect_back_side wrong-signature TypeError no-op bug is FIXED (array signature);
#  (b) tape + extension + front_side_state added (mirrors cup_and_handle).
# ════════════════════════════════════════════════════════════════════════════════════════

# Inverse-H&S skeleton (half_window=1, atr_noise_frac=0): three swing LOWS
# L(left-shoulder) H L(head=lowest) H L(right-shoulder, holds above head), then a neckline
# break on the current bar. neckline = min(left_neck_high, right_neck_high).
_IHS_NECK = 10.00


def _ihs_df() -> pd.DataFrame:
    """A GENUINE inverse-H&S: LS low, left-neck high, HEAD (lowest) low, right-neck high,
    RS low (higher than head), then the current bar breaks above the neckline.

        idx:   0     1      2     3      4     5      6      7      8(cur)
        role:  -   LS-low  -  neckH  HEAD  neckH  RS-low  -    BREAK
    """
    neck = _IHS_NECK
    # (high, low) per bar. half_window=1 -> a swing pivot needs one bar each side.
    bars = [
        (9.50, 9.30),   # 0  lead-in (gives idx1 a left neighbour)
        (9.55, 9.00),   # 1  LEFT SHOULDER low (9.00)
        (neck, 9.40),   # 2  left-neck swing HIGH (10.00)
        (9.60, 8.50),   # 3  HEAD low (8.50 = lowest)
        (neck, 9.45),   # 4  right-neck swing HIGH (10.00)
        (9.70, 9.10),   # 5  RIGHT SHOULDER low (9.10 > head 8.50, holds)
        (9.90, 9.55),   # 6  rise toward the neckline (right neighbour for idx5 pivot)
        (9.95, 9.70),   # 7  just under the neckline
        (10.40, 9.90),  # 8  cur = BREAK above the neckline
    ]
    rows = [{"Open": (h + l) / 2, "High": h, "Low": l, "Close": (h + l) / 2, "Volume": 1_000_000}
            for h, l in bars]
    return pd.DataFrame(rows)


def _ihs_settings(ms) -> None:
    ms.chili_momentum_inverse_head_shoulders_entry_enabled = True
    ms.chili_momentum_swing_pivot_half_window = 1
    ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


class _IhsPassGuards:
    """Mock the indicator layer + the four guards to ALL PASS so a clean inverse-H&S fires."""

    def __init__(self, arrays=None):
        self._arrays = arrays if arrays is not None else _arrays(9)
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


class TestInverseHeadShouldersBaseline:
    def test_clean_ihs_fires_completed_bar(self):
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards():
            _ihs_settings(ms)
            ok, reason, dbg = inverse_head_shoulders_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean inverse-H&S must fire, got {reason} dbg={dbg}"
        assert reason == "inverse_head_shoulders_break"
        assert dbg["pullback_high"] == pytest.approx(_IHS_NECK, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(8.50, abs=1e-6)  # head low = stop

    def test_clean_ihs_tick_break_fires(self):
        df = _ihs_df()
        df.loc[8, "High"] = _IHS_NECK - 0.01  # no completed-bar break -> only the tick path
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards():
            _ihs_settings(ms)
            ok, reason, dbg = inverse_head_shoulders_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=_IHS_NECK + 0.20,
            )
        assert ok is True, f"tick-break must fire, got {reason} dbg={dbg}"
        assert reason == "inverse_head_shoulders_break_tick_ok"


class TestInverseHeadShouldersBacksideBugFix:
    def test_detect_back_side_called_with_array_signature(self):
        """THE BUG FIX: _detect_back_side must be called with the ARRAY signature
        (ema9, ema20, macd, macd_signal, cur, ...) — NOT the old ``_detect_back_side(df,
        entry_interval=...)`` that raised a swallowed TypeError and SILENTLY NO-OPPED.
        Assert the first positional arg is the real (len-9) ema_9 LIST and the 5th positional
        is the cur index — i.e. the veto is now LIVE, not a no-op."""
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards() as g:
            _ihs_settings(ms)
            inverse_head_shoulders_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        dbs = g.mocks[f"{_GATES}._detect_back_side"]
        dbs.assert_called_once()
        args = dbs.call_args.args
        # positional: (ema9, ema20, macd, macd_signal, cur)
        assert isinstance(args[0], list), "first arg must be the ema9 LIST, not a DataFrame"
        assert len(args[0]) == 9, "ema_9 series passed to backside must be the real array"
        assert args[4] == 8, "cur index (last bar) must be the 5th positional arg"
        # the old buggy call passed a kwarg ``entry_interval`` — assert it is GONE.
        assert "entry_interval" not in dbs.call_args.kwargs

    def test_backside_ema_macd_rollover_no_fire(self):
        """With the array signature LIVE, a reported rollover (9<20 EMA) now actually blocks."""
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards() as g:
            _ihs_settings(ms)
            g.mocks[f"{_GATES}._detect_back_side"].return_value = (True, "ema9_below_ema20")
            ok, reason, dbg = inverse_head_shoulders_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "inverse_head_shoulders_back_side"


class TestInverseHeadShouldersNewGuards:
    def test_below_vwap_lifecycle_no_fire(self):
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards() as g:
            _ihs_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].return_value = SimpleNamespace(
                is_backside=True, above_vwap=False, reason="below_vwap",
            )
            ok, reason, dbg = inverse_head_shoulders_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "inverse_head_shoulders_backside_lifecycle"

    def test_front_side_state_exception_fails_closed(self):
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards() as g:
            _ihs_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].side_effect = AttributeError("thin frame")
            ok, reason, dbg = inverse_head_shoulders_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "inverse_head_shoulders_backside_lifecycle"

    def test_extended_parabolic_no_fire(self):
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards() as g:
            _ihs_settings(ms)
            g.mocks[f"{_GATES}._hod_extension_ok"].return_value = (False, {"hod_extended_vs": "vwap"})
            ok, reason, dbg = inverse_head_shoulders_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "inverse_head_shoulders_extended"

    def test_no_tape_no_fire_completed_bar(self):
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards() as g:
            _ihs_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_no_data"})
            ok, reason, dbg = inverse_head_shoulders_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "inverse_head_shoulders_tape_unconfirmed"

    def test_no_tape_no_fire_tick_break(self):
        df = _ihs_df()
        df.loc[8, "High"] = _IHS_NECK - 0.01  # only the tick path
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards() as g:
            _ihs_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_disabled"})
            ok, reason, dbg = inverse_head_shoulders_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=_IHS_NECK + 0.20,
            )
        assert ok is False
        assert reason == "inverse_head_shoulders_tape_unconfirmed"

    def test_tape_is_last_gate_l2_short_circuits_before_tape(self):
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards() as g:
            _ihs_settings(ms)
            g.mocks[f"{_GATES}._l2_entry_veto"].return_value = ("l2_big_seller", {})
            ok, reason, dbg = inverse_head_shoulders_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "inverse_head_shoulders_l2_big_seller"
        g.mocks[f"{_GATES}.tape_confirms_hold"].assert_not_called()


class TestInverseHeadShouldersSeriesRequirement:
    def test_compute_all_requests_chase_guard_series(self):
        """The needed-set must include ema_20/macd/macd_signal/vwap (the exact chase-hole the
        fix closes) so the backside + extension guards see REAL series, not [] no-ops."""
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards() as g:
            _ihs_settings(ms)
            inverse_head_shoulders_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        caf = g.mocks[f"{_GATES}.compute_all_from_df"]
        # the gate calls compute_all_from_df once (the buggy version called it later with only
        # volume_ratio; the fix requests the full set up front).
        needed = caf.call_args.kwargs.get("needed")
        assert needed is not None
        assert {"ema_9", "ema_20", "macd", "macd_signal", "vwap", "volume_ratio"}.issubset(set(needed)), (
            f"missing chase-guard series -> chase hole; got {needed}"
        )

    def test_flag_off_byte_identical(self):
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms:
            ms.chili_momentum_inverse_head_shoulders_entry_enabled = False
            ok, reason, dbg = inverse_head_shoulders_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "inverse_head_shoulders_disabled"


# ════════════════════════════════════════════════════════════════════════════════════════
#  FIX #2 — bottom_reversal_confirmation (Jackknife-long; counter-trend knife-catch)
#  tape (both fire sites) + extension added; optional velocity floor (default OFF).
# ════════════════════════════════════════════════════════════════════════════════════════

_BR_LEVEL_HIGH = 9.80  # the green-bar HIGH (the break level the gate enters on)


def _bottom_reversal_df() -> pd.DataFrame:
    """N consecutive RED candles then a GREEN confirmation bar (the current bar).
    min_red=2 -> use 4 reds + 1 green. The green-bar HIGH = the break level."""
    bars = [
        # (open, high, low, close)
        (10.40, 10.45, 10.20, 10.25),   # 0 red
        (10.25, 10.30, 10.00, 10.05),   # 1 red
        (10.05, 10.10, 9.75, 9.80),     # 2 red
        (9.80, 9.85, 9.55, 9.60),       # 3 red (series low ~9.55)
        (9.60, _BR_LEVEL_HIGH, 9.58, 9.75),  # 4 cur = GREEN (close>open); high=9.80
    ]
    rows = [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
            for o, h, l, c in bars]
    return pd.DataFrame(rows)


def _br_settings(ms) -> None:
    ms.chili_momentum_bottom_reversal_entry_enabled = True
    ms.chili_momentum_bottom_reversal_min_red = 2
    ms.chili_momentum_bottom_reversal_volume_spike_multiple = 1.5
    ms.chili_momentum_bottom_reversal_velocity_floor_atr_mult = 0.0  # OFF by default


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
        # dip-family tick-break thrust helpers -> pass
        _p(f"{_GATES}._premarket_tickbreak_confirmed", return_value=True)
        _p(f"{_GATES}._dipbuy_tick_thrust_ok", return_value=True)
        _p(f"{_GATES}.tape_confirms_hold", return_value=(True, {"reason": "tape_hold_ok"}))
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestBottomReversalBaseline:
    def test_clean_bottom_reversal_fires_completed_bar(self):
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards():
            _br_settings(ms)
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean bottom-reversal must fire, got {reason} dbg={dbg}"
        assert reason == "bottom_reversal"
        assert dbg["red_bars_count"] >= 2

    def test_clean_bottom_reversal_tick_fires(self):
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards():
            _br_settings(ms)
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
                live_price=_BR_LEVEL_HIGH + 0.20,
            )
        assert ok is True, f"tick-break must fire, got {reason} dbg={dbg}"
        assert reason == "bottom_reversal_tick_ok"


class TestBottomReversalNewGuards:
    def test_extended_no_fire(self):
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards() as g:
            _br_settings(ms)
            g.mocks[f"{_GATES}._hod_extension_ok"].return_value = (False, {"hod_extended_vs": "vwap"})
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "bottom_reversal_extended"

    def test_no_tape_no_fire_completed_bar(self):
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards() as g:
            _br_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_no_data"})
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "bottom_reversal_tape_unconfirmed"

    def test_no_tape_no_fire_tick_break(self):
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards() as g:
            _br_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_disabled"})
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
                live_price=_BR_LEVEL_HIGH + 0.20,
            )
        assert ok is False
        assert reason == "bottom_reversal_tape_unconfirmed"

    def test_velocity_floor_off_is_byte_identical(self):
        """floor=0.0 -> the velocity gate is OFF -> the clean reversal fires unchanged."""
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards():
            _br_settings(ms)
            ms.chili_momentum_bottom_reversal_velocity_floor_atr_mult = 0.0
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True
        assert reason == "bottom_reversal"
        assert "flush_roc_per_bar" not in dbg  # the gate was never evaluated

    def test_velocity_floor_blocks_slow_grind(self):
        """floor>0 with a HUGE multiplier -> even a real flush is 'too slow' -> NO FIRE
        (proves the optional sharp-V gate is live and adaptive to the name's own ATR%)."""
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards():
            _br_settings(ms)
            ms.chili_momentum_bottom_reversal_velocity_floor_atr_mult = 1000.0  # absurd floor
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "bottom_reversal_flush_too_slow"
        assert "flush_roc_per_bar" in dbg

    def test_velocity_floor_passes_steep_v(self):
        """floor>0 with a tiny multiplier -> a real flush easily clears the floor -> FIRES."""
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards():
            _br_settings(ms)
            ms.chili_momentum_bottom_reversal_velocity_floor_atr_mult = 0.001
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True
        assert reason == "bottom_reversal"
        assert "flush_roc_per_bar" in dbg

    def test_series_request_includes_vwap(self):
        """The needed-set must include vwap so the extension VWAP arm gets a REAL series."""
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BrPassGuards() as g:
            _br_settings(ms)
            bottom_reversal_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        caf = g.mocks[f"{_GATES}.compute_all_from_df"]
        needed = caf.call_args.kwargs.get("needed")
        assert needed is not None
        assert "vwap" in set(needed), f"vwap missing from needed-set -> extension VWAP no-op; got {needed}"

    def test_flag_off_byte_identical(self):
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms:
            ms.chili_momentum_bottom_reversal_entry_enabled = False
            ok, reason, dbg = bottom_reversal_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "bottom_reversal_disabled"


# ════════════════════════════════════════════════════════════════════════════════════════
#  FIX #3 — bull_flag_confirmation
#  inline tape (both fire sites) + extension + front_side_state added (mirrors wedge_break).
#  The bull-flag STRUCTURAL geometry is intricate (6 guards); we run it for real on a frame
#  shaped to pass, and toggle exactly one chase-guard per test.
# ════════════════════════════════════════════════════════════════════════════════════════

_BF_BREAK = 10.30  # the pullback swing-high break level / entry


def _bull_flag_df() -> pd.DataFrame:
    """A 1-3 green-candle impulse, a 2-3 RED pullback "well off the high" (deeper than the
    shallow first_pullback cap, holding the 9-EMA), then the current bar breaks the pullback
    swing high to a new high. Tuned so retrace lands between eff_shallow and the flag ceiling.

        idx:  0     1     2     3     4      5      6      7      8      9     10    11(cur)
        role: ---- impulse (green run up) ----   --- 2-3 red pullback ---     -- BREAK --
    """
    bars = [
        # (open, high, low, close)
        (9.00, 9.10, 8.95, 9.05),   # 0  base
        (9.05, 9.25, 9.00, 9.20),   # 1  impulse green
        (9.20, 9.55, 9.15, 9.50),   # 2  impulse green
        (9.50, 9.85, 9.45, 9.80),   # 3  impulse green
        (9.80, 10.20, 9.75, 10.15), # 4  impulse green -> peak high 10.20 (the flag pole top)
        (10.15, 10.18, 9.95, 10.00),# 5  pullback red (off the high)
        (10.00, 10.02, 9.78, 9.82), # 6  pullback red (deeper)
        (9.82, 9.90, 9.70, 9.75),   # 7  pullback red -> pb_low ~9.70
        (9.75, _BF_BREAK, 9.72, 10.25),  # 8  cur = BREAK new high above pullback swing high
    ]
    # pad to >=12 bars (gate requires len>=12): prepend benign lead-in bars.
    lead = [(8.80, 8.90, 8.75, 8.85)] * 4
    rows = [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
            for o, h, l, c in (lead + bars)]
    return pd.DataFrame(rows)


def _bf_settings(ms) -> None:
    ms.chili_momentum_bull_flag_entry_enabled = True
    ms.chili_momentum_entry_sustained_rvol_floor = 0.0
    ms.chili_momentum_entry_sustain_lookback_bars = 5
    ms.chili_momentum_deep_reclaim_dipbuy_dryup_ratio = 5.0  # lenient (no dryup reject)
    ms.chili_momentum_entry_break_candle_min_close_pos = 0.50
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5
    ms.chili_momentum_dipbuy_distribution_vol_mult = 0.0  # distribution veto OFF
    ms.chili_momentum_pullback_retrace_pct = 0.5
    ms.chili_momentum_adaptive_pullback_depth_ceiling_enabled = False


class _BfPassGuards:
    """Mock the indicator layer + the chase-guards + the intricate STRUCTURAL helpers so a
    clean bull flag reaches the guard chain and fires. Each test then flips one chase-guard."""

    def __init__(self, arrays=None):
        n = 13  # len of _bull_flag_df (4 lead + 9)
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
        # structural helpers -> force a clean flag shape so we reach the guard chain.
        _p(f"{_GATES}._sustained_rvol", return_value=5.0)
        _p(f"{_GATES}._is_first_pullback", return_value=True)
        _p(f"{_GATES}._collapse_cap", return_value=0.50)
        _p(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.05, 0.02, 0.10))
        _p(f"{_GATES}._adaptive_pullback_depth_ceiling", return_value=0.0)
        _p(f"{_CANDLES}.is_strong_bull_break_candle", return_value=True)
        # the four chase-guards -> ALL PASS.
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


class TestBullFlagBaseline:
    def test_clean_bull_flag_fires_completed_bar(self):
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards():
            _bf_settings(ms)
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean bull flag must fire, got {reason} dbg={dbg}"
        assert reason == "bull_flag_break"


class TestBullFlagNewGuards:
    def test_below_vwap_lifecycle_no_fire(self):
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

    def test_front_side_state_exception_fails_closed(self):
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards() as g:
            _bf_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].side_effect = ValueError("thin frame")
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "bull_flag_backside_lifecycle"

    def test_extended_parabolic_no_fire(self):
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards() as g:
            _bf_settings(ms)
            g.mocks[f"{_GATES}._hod_extension_ok"].return_value = (False, {"hod_extended_vs": "ema9"})
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "bull_flag_extended"

    def test_no_tape_no_fire_completed_bar(self):
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards() as g:
            _bf_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_no_data"})
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "bull_flag_tape_unconfirmed"

    def test_no_tape_no_fire_tick_break(self):
        df = _bull_flag_df()
        df.loc[12, "High"] = _BF_BREAK - 0.01  # no completed-bar break -> only tick path
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards() as g, \
                patch(f"{_GATES}._premarket_tickbreak_confirmed", return_value=True), \
                patch(f"{_GATES}._dipbuy_tick_thrust_ok", return_value=True):
            _bf_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_disabled"})
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=_BF_BREAK + 0.20,
            )
        assert ok is False
        assert reason == "bull_flag_tape_unconfirmed"

    def test_l2_short_circuits_before_tape(self):
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards() as g:
            _bf_settings(ms)
            g.mocks[f"{_GATES}._l2_entry_veto"].return_value = ("l2_hidden_seller", {})
            ok, reason, dbg = bull_flag_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "bull_flag_l2_hidden_seller"
        g.mocks[f"{_GATES}.tape_confirms_hold"].assert_not_called()

    def test_series_request_includes_vwap(self):
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards() as g:
            _bf_settings(ms)
            bull_flag_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        caf = g.mocks[f"{_GATES}.compute_all_from_df"]
        needed = caf.call_args.kwargs.get("needed")
        assert needed is not None
        assert "vwap" in set(needed), f"vwap missing -> extension VWAP no-op; got {needed}"

    def test_flag_off_byte_identical(self):
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


# ════════════════════════════════════════════════════════════════════════════════════════
#  FIX #4 — ma_vwap_pullback_confirmation
#  front_side_state below-VWAP lifecycle veto added (NO inline tape — dip family substitutes
#  tick-thrust+volume). The other guards (extension, backside, L2) were already present.
# ════════════════════════════════════════════════════════════════════════════════════════

def _ma_vwap_df() -> pd.DataFrame:
    """An impulse (3+ green) then a 2-bar consolidation grinding at the 9-EMA, then the
    current bar reclaims the 9-EMA. The EMA arrays are mocked (9-EMA = 9.50) so the touch +
    reclaim land deterministically.

    With 11 bars (cur=10) and consol_bars=2 / impulse_bars=3 the gate windows are:
      consolidation = idx [9, 10] (the last two bars; cur is the reclaim bar),
      impulse       = idx [6, 7, 8] (must ALL be green AND rise).
    """
    bars = [
        # (open, high, low, close)  -- lead-in (>=10 bars total required by the gate)
        (8.70, 8.75, 8.65, 8.72),   # 0
        (8.72, 8.80, 8.68, 8.78),   # 1
        (8.78, 8.90, 8.75, 8.88),   # 2
        (8.88, 9.00, 8.85, 8.98),   # 3
        (8.98, 9.10, 8.95, 9.08),   # 4
        (9.08, 9.20, 9.05, 9.18),   # 5
        (9.18, 9.40, 9.15, 9.38),   # 6  impulse green
        (9.38, 9.62, 9.35, 9.60),   # 7  impulse green
        (9.60, 9.82, 9.58, 9.80),   # 8  impulse green -> imp peak ~9.82
        (9.80, 9.81, 9.48, 9.52),   # 9  consolidation (low 9.48 touches the 9.50 EMA band)
        (9.55, 9.85, 9.50, 9.70),   # 10 cur = reclaim (close 9.70 >= 9.50 EMA; low touches band)
    ]
    rows = [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
            for o, h, l, c in bars]
    return pd.DataFrame(rows)


def _mv_settings(ms) -> None:
    ms.chili_momentum_ma_vwap_pullback_enabled = True
    ms.chili_momentum_pullback_retrace_pct = 0.5
    ms.chili_momentum_ma_vwap_impulse_bars = 3
    ms.chili_momentum_ma_vwap_consolidation_bars = 2
    ms.chili_momentum_ma_vwap_vol_mult = 1.5
    ms.chili_momentum_vwap_reclaim_vol_mult = 1.5


class _MvPassGuards:
    def __init__(self, arrays=None):
        n = 11
        # 9-EMA = 9.50 (the consolidation lows touch it; cur close 9.70 reclaims it);
        # 20-EMA below; vwap LOW so front-side is above-vwap; volume surge on cur.
        self._arrays = arrays if arrays is not None else {
            "ema_9": [9.50] * n,
            "ema_20": [9.30] * n,
            "macd": [0.05] * n,
            "macd_signal": [0.03] * n,
            "vwap": [9.20] * n,
            "volume_ratio": [1.0] * (n - 1) + [3.0],
            "atr": [0.20] * n,
        }
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        _p(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.05, 0.02, 0.10))
        _p(f"{_GATES}._collapse_cap", return_value=0.50)
        _p(f"{_GATES}._hod_extension_ok", return_value=(True, {}))
        _p(f"{_GATES}._detect_back_side", return_value=(False, "front_side"))
        _p(f"{_ROSS}.front_side_state",
           return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok"))
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        _p(f"{_GATES}._premarket_tickbreak_confirmed", return_value=True)
        _p(f"{_GATES}._dipbuy_tick_thrust_ok", return_value=True)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestMaVwapPullbackBaseline:
    def test_clean_ma_vwap_pullback_fires(self):
        df = _ma_vwap_df()
        with patch(f"{_GATES}.settings") as ms, _MvPassGuards():
            _mv_settings(ms)
            ok, reason, dbg = ma_vwap_pullback_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean ma/vwap pullback must fire, got {reason} dbg={dbg}"
        assert reason == "ma_vwap_pullback"


class TestMaVwapPullbackFrontSideVeto:
    def test_below_vwap_lifecycle_no_fire(self):
        """THE FIX: a 9/20-EMA reclaim while price sits BELOW VWAP (a backside reclaim Ross
        skips) is now vetoed by front_side_state -> NO FIRE."""
        df = _ma_vwap_df()
        with patch(f"{_GATES}.settings") as ms, _MvPassGuards() as g:
            _mv_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].return_value = SimpleNamespace(
                is_backside=True, above_vwap=False, reason="below_vwap",
            )
            ok, reason, dbg = ma_vwap_pullback_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "ma_vwap_pullback_backside_lifecycle"

    def test_front_side_state_exception_fails_closed(self):
        """A thin/degenerate frame -> front_side_state raises -> fail-CLOSED (NO FIRE)."""
        df = _ma_vwap_df()
        with patch(f"{_GATES}.settings") as ms, _MvPassGuards() as g:
            _mv_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].side_effect = AttributeError("thin frame")
            ok, reason, dbg = ma_vwap_pullback_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "ma_vwap_pullback_backside_lifecycle"

    def test_front_side_runs_after_backside_before_l2(self):
        """front_side_state is the distinct below-VWAP arm: the EMA/MACD _detect_back_side
        runs first (short-circuits before front_side), and L2 runs AFTER front_side."""
        df = _ma_vwap_df()
        with patch(f"{_GATES}.settings") as ms, _MvPassGuards() as g:
            _mv_settings(ms)
            # backside EMA/MACD veto fires first -> front_side_state never reached.
            g.mocks[f"{_GATES}._detect_back_side"].return_value = (True, "ema9_below_ema20")
            ok, reason, dbg = ma_vwap_pullback_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "ma_vwap_pullback_back_side"

    def test_no_inline_tape_call(self):
        """By design the dip family does NOT call tape_confirms_hold (it substitutes
        tick-thrust + volume). Assert the clean fire path never invoked it."""
        df = _ma_vwap_df()
        with patch(f"{_GATES}.settings") as ms, _MvPassGuards(), \
                patch(f"{_GATES}.tape_confirms_hold") as tape:
            _mv_settings(ms)
            ok, reason, dbg = ma_vwap_pullback_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean fire expected, got {reason}"
        tape.assert_not_called()

    def test_flag_off_byte_identical(self):
        df = _ma_vwap_df()
        with patch(f"{_GATES}.settings") as ms:
            ms.chili_momentum_ma_vwap_pullback_enabled = False
            ok, reason, dbg = ma_vwap_pullback_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "ma_vwap_pullback_disabled"
