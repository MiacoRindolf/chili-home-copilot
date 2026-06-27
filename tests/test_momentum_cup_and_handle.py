"""Cup-and-handle breakout entry (Ross SS101 #016) — adversarial no-chase tests.

``cup_and_handle_confirmation`` encodes Ross's double-top rim + shallow handle + new-high
break, hardened with the SAME four shared chase-guards every live breakout trigger carries
(mirrors ``wedge_break_entry`` / ``hod_break_confirmation`` / ``absorption_snap_entry``):

  1. NOT BACKSIDE / NOT BELOW-VWAP -- ``_detect_back_side`` + ``front_side_state``
     (front_side fails CLOSED on a thin/degenerate frame; this is a new-conviction fire).
  2. NOT PARABOLIC -- ``_hod_extension_ok`` vs the 9-EMA AND VWAP (rejects a vertical
     run INTO the rim as a blow-off).
  3. L2 hidden-/big-seller veto -- ``_l2_entry_veto`` (Ross: "a big seller right around five").
  4. TAPE REQUIRED + FAIL-CLOSED -- ``tape_confirms_hold`` is the LAST gate before EITHER
     fire path (tick-break OR completed-bar break); disabled / no-tape / thin / stale ⇒ NO fire.

PLUS the structural guards (ATR-filtered swing pivots so the two tops are REAL pivots, the
ATR-derived equal-highs band, the vol-aware shallow handle cap, the ``_collapse_cap`` depth
gate, the 9-EMA hold) and the volume surge on the completed-bar break.

These are PURE-LOGIC tests on a synthetic OHLCV frame. The structural layer (swing pivots
off High/Low) runs for real; the indicator layer (``compute_all_from_df`` / ATR) and the
four guards are mocked so each guard can be regressed INDEPENDENTLY: a "clean cup" baseline
FIRES with ``stop == handle low``, and flipping exactly ONE guard to fail (or removing a
required series) ⇒ NO FIRE. A guard that silently stopped blocking would make its adversarial
test fail. Flag OFF ⇒ byte-identical NO FIRE.

Entry = cup rim (double-top peak, ``pullback_high``); stop = handle low (``pullback_low``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.trading.momentum_neural import entry_gates
from app.services.trading.momentum_neural.entry_gates import cup_and_handle_confirmation

_GATES = "app.services.trading.momentum_neural.entry_gates"
# front_side_state is imported INSIDE the function via ``from .ross_momentum import
# front_side_state`` -> it must be patched at its SOURCE module, not the entry_gates
# namespace (patching entry_gates.front_side_state would not intercept the local import).
_ROSS = "app.services.trading.momentum_neural.ross_momentum"

# Cup-rim level the synthetic geometry breaks. The handle low sits a shallow pull below it.
_RIM = 10.00
_HANDLE_LOW = 9.70  # ~3% handle depth off the rim -> shallow (within the vol-aware cap)


def _cup_handle_df() -> pd.DataFrame:
    """A GENUINE double-top + shallow-handle + new-high-break OHLCV frame.

    Geometry (half_window=1, atr_noise_frac=0 -> a bar is a swing High iff its High is the
    max of itself and its two neighbours; the last bar is never a confirmed pivot):

        idx:   0    1    2    3*   4    5    6    7*   8    9    10    11   12(cur)
        role:  -    -    -  TOP1   -  LOW    -  TOP2 hndl hndl HLOW  hndl  BREAK

      * TOP1 (idx 3) and TOP2 (idx 7) both peak at the rim (10.00) -> the equal-high double
        top (the cup). They are the two most-recent CONFIRMED swing Highs.
      * The HANDLE = the last ``max_handle`` (=3) completed bars before cur -> the slice
        ``[9:12]`` (idx 9,10,11). The handle low (9.70) sits at idx 10 INSIDE that slice, so
        it is the ``pullback_low`` the gate reports. idx 8-11 all stay strictly below the rim
        (they are NOT new swing Highs above it).
      * idx 12 = cur = the BREAK bar whose High pushes a NEW HIGH above the rim.

    Only High/Low geometry matters to the (real) swing-pivot scanner; Open/Close/Volume are
    benign. The indicator/ATR layer + the four guards are mocked by the caller.
    """
    rim = _RIM
    # per-bar (high, low). lows kept well clear so no bar is a degenerate both-high-and-low.
    bars = [
        (9.20, 9.00),         # 0  run-up
        (9.45, 9.15),         # 1
        (9.70, 9.40),         # 2
        (rim, 9.60),          # 3  TOP1 (swing high at the rim)
        (9.78, 9.55),         # 4  dip after top1
        (9.60, 9.35),         # 5  cup bottom (swing low)
        (9.82, 9.55),         # 6  rise back toward the rim
        (rim, 9.70),          # 7  TOP2 (equal swing high -> the double top)
        (9.85, 9.78),         # 8  early handle drift (OUTSIDE the 3-bar handle window)
        (9.80, 9.74),         # 9  HANDLE bar (in [9:12])
        (9.78, _HANDLE_LOW),  # 10 HANDLE LOW = pullback_low (in [9:12])
        (9.95, 9.80),         # 11 last completed handle bar (h_end exclusive == cur)
        (10.35, 9.90),        # 12 cur = BREAK: new high above the rim
    ]
    rows = []
    for hi, lo in bars:
        o = (hi + lo) / 2.0
        rows.append({"Open": o, "High": hi, "Low": lo, "Close": o, "Volume": 1_000_000})
    return pd.DataFrame(rows)


def _base_settings(mock_settings) -> None:
    """Settings shared by every structural/guard test (flag ON; the structural knobs the
    geometry was built around). Guard-internal flags are irrelevant because the guards are
    mocked at the call boundary."""
    mock_settings.chili_momentum_cup_and_handle_entry_enabled = True
    mock_settings.chili_momentum_swing_pivot_half_window = 1
    mock_settings.chili_momentum_swing_pivot_atr_noise_frac = 0.0
    mock_settings.chili_momentum_cup_and_handle_lookback_bars = 20
    mock_settings.chili_momentum_cup_and_handle_max_handle_bars = 3
    mock_settings.chili_momentum_double_bottom_band_atr_mult = 0.6
    mock_settings.chili_momentum_pullback_volume_spike_multiple = 1.5


# Controlled indicator arrays (len 13, cur=12). The handle low (9.70) holds ABOVE the
# 9-EMA (9.50) so the structural 9-EMA-hold passes; volume_ratio[cur] clears the surge.
def _good_arrays() -> dict:
    n = 13
    return {
        "ema_9": [9.50] * n,
        "ema_20": [9.40] * n,
        "macd": [0.05] * n,
        "macd_signal": [0.03] * n,
        "vwap": [9.45] * n,
        "volume_ratio": [1.0] * 12 + [3.0],  # break bar has a big surge
    }


class _PassAllGuards:
    """Context manager: mock the indicator layer + the four chase-guards to ALL PASS, so a
    structurally-clean cup fires. Each adversarial test enters this and then overrides ONE
    guard to fail -> proving that guard alone blocks the chase. Returns the patch handles so
    a test can assert call-throughs / flip a single mock."""

    def __init__(self, arrays=None):
        self._arrays = arrays if arrays is not None else _good_arrays()
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        # Indicator layer: deterministic ATR + the requested arrays.
        _p(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20))
        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        # Guard 1a: NOT backside (structural EMA/MACD read).
        _p(f"{_GATES}._detect_back_side", return_value=(False, "front_side"))
        # Guard 1b: NOT below-VWAP / not lifecycle-backside.
        _p(f"{_ROSS}.front_side_state",
           return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok"))
        # Guard 2: NOT parabolic.
        _p(f"{_GATES}._hod_extension_ok", return_value=(True, {}))
        # Guard 3: no L2 big-/hidden-seller veto.
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        # Guard 4: tape CONFIRMS (buyers lifting the ask this tick).
        _p(f"{_GATES}.tape_confirms_hold", return_value=(True, {"reason": "tape_hold_ok"}))
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


# ───────────────────────── BASELINE: the clean cup FIRES ─────────────────────────────────

class TestCupAndHandleCleanFire:
    def test_clean_cup_fires_with_stop_at_handle_low(self):
        """CLEAN favorable cup (double-top rim + shallow handle on the 9-EMA + new high +
        vol surge + tape + front-side + not-extended + no L2 veto) -> FIRES, stop == handle
        low (``pullback_low``), entry == cup rim (``pullback_high``)."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"clean cup must fire, got {reason} dbg={dbg}"
        assert reason == "cup_and_handle_break"
        # STRUCTURAL STOP = the handle low; entry = the cup rim. (vol-floor widens downstream.)
        assert dbg["pullback_low"] == pytest.approx(_HANDLE_LOW, abs=1e-6)
        assert dbg["pullback_high"] == pytest.approx(_RIM, abs=1e-6)
        assert dbg["pullback_low"] < dbg["pullback_high"]

    def test_clean_cup_tick_break_fires_through_rim(self):
        """A live tick already trading through the rim fires the tick-break path (still tape-
        gated). stop == handle low."""
        df = _cup_handle_df()
        # Make the completed bar NOT break (so only the tick-break path can fire) to prove
        # the tick path is live + tape-gated.
        df.loc[12, "High"] = _RIM - 0.01
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=_RIM + 0.20,
            )
        assert ok is True, f"tick-break must fire, got {reason} dbg={dbg}"
        assert reason == "cup_and_handle_break_tick_ok"
        assert dbg["pullback_low"] == pytest.approx(_HANDLE_LOW, abs=1e-6)


# ───────────────────────── GUARD 4: TAPE REQUIRED + FAIL-CLOSED ──────────────────────────

class TestCupAndHandleTapeGuard:
    def test_no_tape_no_fire_completed_bar(self):
        """tape_confirms_hold says NO (disabled / no-tape / thin / stale / error) -> NO FIRE
        on the completed-bar break. Tape is the LAST gate; fail-CLOSED."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_disabled"})
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_tape_unconfirmed"

    def test_no_tape_no_fire_tick_break(self):
        """No tape ALSO blocks the tick-break path (tape gates BOTH fire paths)."""
        df = _cup_handle_df()
        df.loc[12, "High"] = _RIM - 0.01  # only the tick path is available
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_no_data"})
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=_RIM + 0.20,
            )
        assert ok is False
        assert reason == "cup_and_handle_tape_unconfirmed"


# ───────────────────────── GUARD 1: NOT BACKSIDE / NOT BELOW-VWAP ────────────────────────

class TestCupAndHandleBacksideGuard:
    def test_backside_ema_macd_rollover_no_fire(self):
        """_detect_back_side reports a rolled-over back side (9<20 EMA) -> NO FIRE."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_GATES}._detect_back_side"].return_value = (True, "ema9_below_ema20")
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_back_side"

    def test_below_vwap_lifecycle_backside_no_fire(self):
        """front_side_state says the name is on the back side of its OWN session (below VWAP /
        faded) -> NO FIRE. Ross never buys below VWAP."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].return_value = SimpleNamespace(
                is_backside=True, above_vwap=False, reason="below_vwap",
            )
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_backside_lifecycle"

    def test_front_side_state_exception_fails_closed(self):
        """front_side_state raises on a thin/degenerate frame -> fail-CLOSED (NO FIRE) for this
        new-conviction fire path."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].side_effect = AttributeError("thin frame")
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_backside_lifecycle"


# ───────────────────────── GUARD 2: NOT PARABOLIC (extension) ────────────────────────────

class TestCupAndHandleExtensionGuard:
    def test_extended_parabolic_into_rim_no_fire(self):
        """A vertical run INTO the rim leaves the entry excessively extended vs the 9-EMA /
        VWAP -> _hod_extension_ok fails -> NO FIRE (blow-off defense)."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_GATES}._hod_extension_ok"].return_value = (False, {"hod_extended_vs": "ema9"})
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_extended"


# ───────────────────────── GUARD 3: L2 hidden-/big-seller veto ───────────────────────────

class TestCupAndHandleL2Guard:
    def test_l2_big_seller_wall_no_fire(self):
        """_l2_entry_veto reports a big resting ASK wall at the rim -> NO FIRE."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_GATES}._l2_entry_veto"].return_value = ("l2_big_seller", {"l2_pctile": 0.05})
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_l2_big_seller"

    def test_l2_hidden_seller_absorption_no_fire(self):
        """_l2_entry_veto reports absorption / hidden-seller rollover -> NO FIRE."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_GATES}._l2_entry_veto"].return_value = ("l2_hidden_seller", {})
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_l2_hidden_seller"


# ───────────────────────── THIN FRAME / MISSING SERIES (fail-CLOSED) ─────────────────────

class TestCupAndHandleMissingSeries:
    def test_missing_ema20_macd_drives_backside_fail_closed(self):
        """If compute_all_from_df does NOT return ema_20/macd (the chase-hole the gate guards
        against), _detect_back_side runs on empty series. We assert the gate never fires when
        the backside read is unavailable: with the REAL _detect_back_side fed empty arrays it
        fails-OPEN, but front_side_state fails CLOSED on the degenerate session frame, so the
        net result is still NO FIRE -> no silent chase. Here we model the missing-series world
        by returning arrays WITHOUT ema_20/macd and letting the real guards run."""
        df = _cup_handle_df()
        thin_arrays = {
            "ema_9": [9.50] * 13,
            "ema_20": [],          # MISSING -> _detect_back_side gets nothing
            "macd": [],
            "macd_signal": [],
            "vwap": [],
            "volume_ratio": [1.0] * 12 + [3.0],
        }
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
                patch(f"{_GATES}.compute_all_from_df", return_value=thin_arrays), \
                patch(f"{_ROSS}.front_side_state", side_effect=ValueError("thin")):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        # front_side_state fails CLOSED on the thin frame -> backside_lifecycle (NO FIRE).
        assert ok is False
        assert reason == "cup_and_handle_backside_lifecycle"


# ───────────────────────── NON-CUP STRUCTURE (no chase on bad shape) ─────────────────────

class TestCupAndHandleStructure:
    def test_flag_off_byte_identical_no_fire(self):
        """Flag OFF -> (False, 'cup_and_handle_disabled') BEFORE any computation."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms:
            ms.chili_momentum_cup_and_handle_entry_enabled = False
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "cup_and_handle_disabled"

    def test_insufficient_bars_thin_frame(self):
        """Thin frame (< 2*half_w+3 bars) -> NO FIRE."""
        df = _cup_handle_df().iloc[:4]
        with patch(f"{_GATES}.settings") as ms:
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "cup_and_handle_insufficient_bars"

    def test_empty_dataframe_no_fire(self):
        """None / empty DataFrame -> NO FIRE."""
        with patch(f"{_GATES}.settings") as ms:
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(None, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "cup_and_handle_insufficient_bars"

    def test_no_double_top_too_few_highs(self):
        """A single rising leg (one swing high) -> no double-top rim -> NO FIRE."""
        # Monotone-rising highs: only the interior local maxima are pivots; build a frame with
        # at most one confirmed swing High by making highs strictly increase then have one peak.
        rows = []
        for i in range(13):
            hi = 9.0 + i * 0.10  # strictly increasing -> the LAST bar can't be a confirmed pivot
            lo = hi - 0.20
            o = (hi + lo) / 2.0
            rows.append({"Open": o, "High": hi, "Low": lo, "Close": o, "Volume": 1_000_000})
        df = pd.DataFrame(rows)
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_too_few_highs"

    def test_handle_too_deep_no_fire(self):
        """A handle DEEPER than the vol-aware shallow cap (a breakdown, not a handle) -> NO
        FIRE. Push the handle low far below the rim."""
        df = _cup_handle_df()
        df.loc[10, "Low"] = 8.30  # ~17% pull off the rim -> beyond the shallow / collapse cap
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_handle_too_deep"

    def test_handle_below_ema9_no_fire(self):
        """A handle whose low sits BELOW the 9-EMA (over-extended, the risky one Ross warns
        against) -> NO FIRE."""
        df = _cup_handle_df()
        # Raise the 9-EMA above the handle low so the handle-hold check rejects it.
        high_ema = {**_good_arrays(), "ema_9": [9.95] * 13}
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards(arrays=high_ema):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_handle_below_ema9"

    def test_tops_unequal_no_fire(self):
        """Two swing highs at clearly DIFFERENT levels (outside the ATR equal-highs band) ->
        not a double-top rim -> NO FIRE."""
        df = _cup_handle_df()
        # 9.85 is still the local max at idx 3 (neighbours 9.70 / 9.78) so it stays a swing
        # high, but 0.15 below the rim -> outside the ~0.12 ATR equal-highs band -> unequal.
        df.loc[3, "High"] = 9.85
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_tops_unequal"

    def test_no_break_waits_tick_armable(self):
        """A valid cup whose current bar has NOT yet made a new high (and no live tick through
        the rim) -> WAIT (tick-armable), NOT a fire. pullback_high is set for the live arm."""
        df = _cup_handle_df()
        # 9.97 is below the rim (no bar break) AND above idx-11's 9.95 (so idx 11 does NOT
        # become a third swing high that would shift the tops). No live tick either.
        df.loc[12, "High"] = 9.97
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "waiting_for_break"
        assert dbg["pullback_high"] == pytest.approx(_RIM, abs=1e-6)

    def test_low_volume_break_no_fire(self):
        """A completed-bar break WITHOUT the volume surge (Ross: 'high volume surge' on the
        first new-high candle) -> NO FIRE."""
        df = _cup_handle_df()
        weak_vol = {**_good_arrays(), "volume_ratio": [1.0] * 13}  # break bar has NO surge
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards(arrays=weak_vol):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_low_volume"


# ───────────────────────── FIRE-PATH ORDER (tape is the LAST gate) ───────────────────────

class TestCupAndHandleGuardOrdering:
    def test_tape_is_last_gate_for_completed_bar(self):
        """Regression fence: tape must be evaluated for the completed-bar fire path. With every
        OTHER guard passing and only tape failing, the result is the tape reason (not a fire,
        not an earlier reason) -> proves tape gates the final break."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_no_data"})
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_tape_unconfirmed"
        g.mocks[f"{_GATES}.tape_confirms_hold"].assert_called_once()
