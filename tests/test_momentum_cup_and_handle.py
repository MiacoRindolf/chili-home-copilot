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


# ───────────────────────── compute_all_from_df SERIES REQUIREMENT (chase-hole fence) ─────
#
# The gate's docstring + the inline comment (entry_gates.py ~5923-5926) call out the EXACT
# chase-hole that bit cup_and_handle on first ship: compute_all_from_df only computes what is
# REQUESTED, so if ema_20/macd/macd_signal/vwap are NOT in the ``needed`` set the NOT-BACKSIDE
# + NOT-PARABOLIC guards silently run on empty series and no-op (a chase hole). These tests
# pin the request set so a regression that drops a key (re-opening the hole) FAILS here.

class TestCupAndHandleSeriesRequirement:
    def test_compute_all_requests_all_six_chase_guard_series(self):
        """compute_all_from_df must be asked for ema_9 + ema_20 + macd + macd_signal + vwap +
        volume_ratio. Dropping ema_20/macd is the exact chase-hole the gate guards against
        (backside read silently no-ops) -> assert the FULL request set."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"clean cup must fire, got {reason}"
        caf = g.mocks[f"{_GATES}.compute_all_from_df"]
        caf.assert_called_once()
        needed = caf.call_args.kwargs.get("needed")
        assert needed is not None, "compute_all_from_df must be called with a needed= set"
        assert {"ema_9", "ema_20", "macd", "macd_signal", "vwap", "volume_ratio"}.issubset(set(needed)), (
            f"missing chase-guard series in request set -> chase hole; got {needed}"
        )

    def test_backside_runs_on_real_series_not_empty(self):
        """The ema_9/ema_20/macd/macd_signal arrays returned by compute_all_from_df must be
        PASSED to _detect_back_side (the guard must see real data, not [] ). Assert the call
        received the cur index + non-empty ema9 series."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        dbs = g.mocks[f"{_GATES}._detect_back_side"]
        dbs.assert_called_once()
        # positional: (ema9, ema20, macd, macd_signal, cur, ...)
        args = dbs.call_args.args
        assert len(args[0]) == 13, "ema_9 series passed to backside must be the real (len-13) array"
        assert args[4] == 12, "cur index passed to backside must be the last bar"


# ───────────────────────── EQUAL-HIGHS BAND BOUNDARY (eps-below vs eps-above) ────────────
#
# atr_abs is mocked to 0.20, band_mult=0.6 -> equal-highs band = 0.12. The two tops are
# "equal" iff abs(h1-h2) <= 0.12. Test exactly-at / eps-below / eps-above the band edge.

class TestCupAndHandleEqualHighsBoundary:
    def _run_with_top1(self, top1_high):
        df = _cup_handle_df()
        # idx 3 stays a confirmed swing high as long as its High exceeds neighbours (9.70/9.78).
        df.loc[3, "High"] = top1_high
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            return cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())

    def test_tops_just_inside_band_fires(self):
        """|h1-h2| just BELOW the 0.12 band (0.11 apart) -> equal enough -> FIRES."""
        # top1 = 9.89, top2 = 10.00 -> 0.11 apart (< 0.12). peak=10.00 so the break geometry holds.
        ok, reason, dbg = self._run_with_top1(9.89)
        assert ok is True, f"0.11-apart tops are within the 0.12 band -> must fire, got {reason}"
        assert reason == "cup_and_handle_break"
        assert dbg["equal_band"] == pytest.approx(0.12, abs=1e-6)

    def test_tops_at_band_edge_fires(self):
        """|h1-h2| right at the band edge (0.1199.. <= 0.12 band) -> ``> band`` is False ->
        still equal -> FIRES. (Boundary: the reject is strict ``>``, so at/just-under passes.)"""
        ok, reason, dbg = self._run_with_top1(9.88)  # 10.00-9.88 = 0.1199.. <= 0.12 band
        assert ok is True, f"at-band-edge must pass the strict > reject, got {reason}"
        assert reason == "cup_and_handle_break"
        assert dbg["equal_band"] == pytest.approx(0.12, abs=1e-9)

    def test_tops_just_outside_band_no_fire(self):
        """|h1-h2| just ABOVE the band (0.13 apart) -> unequal tops -> NO FIRE."""
        ok, reason, dbg = self._run_with_top1(9.87)  # 0.13 apart > 0.12
        assert ok is False
        assert reason == "cup_and_handle_tops_unequal"


# ───────────────────────── CUP LOOKBACK BOUNDARY (tops too far apart) ────────────────────

class TestCupAndHandleLookbackBoundary:
    def test_tops_within_lookback_fires(self):
        """The two tops are 4 bars apart (idx 3 & 7); with lookback=4 that is exactly at the
        ceiling (``> lookback`` is False) -> readable cup -> FIRES."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ms.chili_momentum_cup_and_handle_lookback_bars = 4  # i2-i1 == 4, not > 4
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"4-apart tops with lookback=4 must fire, got {reason}"
        assert dbg["cup_bars_apart"] == 4

    def test_tops_beyond_lookback_no_fire(self):
        """The two tops are 4 bars apart but lookback=3 -> ``4 > 3`` -> tops too far apart
        (two unrelated highs, not a readable cup) -> NO FIRE."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ms.chili_momentum_cup_and_handle_lookback_bars = 3  # 4 > 3
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_tops_too_far"
        assert dbg["cup_bars_apart"] == 4
        assert dbg["cup_lookback"] == 3


# ───────────────────────── HANDLE DEPTH BOUNDARY (shallow cap edge) ──────────────────────
#
# eff_shallow = _vol_aware_pullback_tolerances(atr_pct=0.02, 0.50)[0] = min(0.75, 0.50+0.02*1.5)
#             = 0.53.  collapse_cap = _collapse_cap(0.02) = min(0.25, max(0.06, 6*0.02)) = 0.12.
# So the BINDING handle-depth cap is collapse_cap=0.12 (12% off the rim). Test the edge.

class TestCupAndHandleDepthBoundary:
    def _run_with_handle_low(self, handle_low):
        df = _cup_handle_df()
        df.loc[10, "Low"] = handle_low  # idx 10 is the handle low inside the [9:12] window
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            return cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())

    def test_handle_just_inside_collapse_cap_fires(self):
        """Handle depth just UNDER the 12% collapse cap (rim 10.00, low 8.85 -> 11.5%) -> a
        shallow handle -> FIRES (and the EMA-hold must still pass; 9-EMA mocked at 9.50, so
        keep the low above 9.50*(1-wick))."""
        # 11.5% depth is below collapse_cap but the handle low 8.85 < 9.50 EMA would trip the
        # EMA-hold first. Use a depth just under 12% that is also above the EMA: low 9.55 -> 4.5%.
        ok, reason, dbg = self._run_with_handle_low(9.55)
        assert ok is True, f"shallow handle (4.5%) must fire, got {reason}"
        assert dbg["handle_depth_pct"] == pytest.approx(4.5, abs=0.05)

    def test_handle_at_collapse_cap_edge_fires(self):
        """Depth EXACTLY at the cap is allowed: the reject is strict ``depth > cap``. A handle
        low of 8.80 (rim 10.00) gives depth == collapse_cap (0.12) -> ``0.12 > 0.12`` is False
        -> the cap passes. The 9-EMA is mocked low (8.00) so ONLY the depth cap is the binding
        boundary under test (the EMA-hold can't pre-reject)."""
        df = _cup_handle_df()
        df.loc[10, "Low"] = 8.80  # rim 10.00 -> depth = 0.12 == collapse_cap (strict > fails)
        low_ema = {**_good_arrays(), "ema_9": [8.00] * 13}  # EMA below the handle low -> EMA-hold OK
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards(arrays=low_ema):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"depth exactly at collapse cap must pass strict > reject, got {reason}"
        assert dbg["handle_depth_pct"] == pytest.approx(12.0, abs=0.05)

    def test_handle_eps_beyond_collapse_cap_no_fire(self):
        """Depth one tick BEYOND the cap (low 8.79 -> 12.1%) -> a breakdown, not a handle ->
        NO FIRE (even with the EMA-hold satisfied so the cap is the binding reject)."""
        df = _cup_handle_df()
        df.loc[10, "Low"] = 8.79  # depth 0.121 > 0.12
        low_ema = {**_good_arrays(), "ema_9": [8.00] * 13}
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards(arrays=low_ema):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_handle_too_deep"


# ───────────────────────── 9-EMA HOLD BOUNDARY + fail-OPEN on missing EMA ────────────────

class TestCupAndHandleEma9HoldBoundary:
    def test_ema9_none_fails_open_does_not_block(self):
        """ema_9[cur] is None (missing read) -> the EMA-hold is SKIPPED (fail-OPEN; an absent
        EMA must never block) -> the cup still FIRES on the other guards."""
        df = _cup_handle_df()
        none_ema = {**_good_arrays(), "ema_9": [None] * 13}
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards(arrays=none_ema):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"missing EMA must fail-open (not block), got {reason}"
        assert reason == "cup_and_handle_break"
        assert "ema9" not in dbg  # the below-ema9 branch was never entered

    def test_handle_just_above_ema9_band_fires(self):
        """handle_low just ABOVE the (vol-aware) 9-EMA tolerance band -> holds the EMA -> FIRES.
        ema_wick at atr_pct=0.02 = max(0.001, 0.02*0.5)=0.01 -> band edge = ema*(1-0.01).
        handle_low 9.70, set ema so 9.70 sits just inside: ema=9.79 -> 9.79*0.99=9.6921 < 9.70."""
        df = _cup_handle_df()
        edge_ema = {**_good_arrays(), "ema_9": [9.79] * 13}
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards(arrays=edge_ema):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"handle just inside the EMA band must fire, got {reason}"

    def test_handle_just_below_ema9_band_no_fire(self):
        """handle_low just BELOW the EMA tolerance band -> over-extended below the 9-EMA -> the
        risky handle Ross warns against -> NO FIRE. ema=9.81 -> 9.81*0.99=9.7119 > 9.70."""
        df = _cup_handle_df()
        edge_ema = {**_good_arrays(), "ema_9": [9.81] * 13}
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards(arrays=edge_ema):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_handle_below_ema9"
        assert dbg["ema9"] == pytest.approx(9.81, abs=1e-6)


# ───────────────────────── HANDLE WINDOW DEGENERACY (no handle bars) ─────────────────────

class TestCupAndHandleHandleWindow:
    def test_no_handle_when_top2_is_adjacent_to_cur(self):
        """When the second top is the bar immediately before cur (h_start == h_end) there are
        NO completed handle bars between the rim and the break -> ``cup_and_handle_no_handle``.
        Build a frame whose 2nd swing high sits at idx == cur-1."""
        # 13 bars; a clean cup with EXACTLY two swing highs at the rim (idx 3 & idx 11) and
        # monotone legs (no interior local maxima). i2 = 11 = cur-1 -> h_start = i2+1 = 12 ==
        # h_end = cur = 12 -> ``h_end <= h_start`` -> no completed handle bars.
        bars = [
            (9.20, 9.00), (9.60, 9.30), (9.85, 9.55),
            (10.00, 9.70),   # 3 TOP1 (rim)
            (9.70, 9.45), (9.50, 9.25), (9.40, 9.15),   # cup decline (single valley at idx 6)
            (9.45, 9.20), (9.55, 9.30), (9.70, 9.45), (9.85, 9.60),  # monotone rise back
            (10.00, 9.75),   # 11 TOP2 (rim, idx == cur-1)
            (9.96, 9.85),    # 12 cur (does NOT break on the bar; no handle bars exist)
        ]
        rows = [{"Open": (h + l) / 2, "High": h, "Low": l, "Close": (h + l) / 2, "Volume": 1_000_000}
                for h, l in bars]
        df = pd.DataFrame(rows)
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_no_handle"


# ───────────────────────── TICK-BREAK vs VOLUME (vol only gates completed bar) ──────────

class TestCupAndHandleTickBreakVolume:
    def test_tick_break_fires_without_volume_surge(self):
        """The tick-break path returns BEFORE the completed-bar volume gate, so a live tick
        through the rim fires even with NO volume surge (volume only gates the completed-bar
        break). Proves the two fire paths have DISTINCT post-tape requirements."""
        df = _cup_handle_df()
        df.loc[12, "High"] = _RIM - 0.01  # no completed-bar break -> only the tick path
        weak_vol = {**_good_arrays(), "volume_ratio": [1.0] * 13}  # no surge anywhere
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards(arrays=weak_vol):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=_RIM + 0.20,
            )
        assert ok is True, f"tick-break must fire without a volume surge, got {reason}"
        assert reason == "cup_and_handle_break_tick_ok"
        assert dbg["live_price"] == pytest.approx(_RIM + 0.20, abs=1e-6)

    def test_volume_at_threshold_fires(self):
        """Completed-bar break with volume_ratio EXACTLY at the spike multiple (1.5) -> the
        reject is strict ``< mult`` -> at-threshold passes -> FIRES."""
        df = _cup_handle_df()
        at_vol = {**_good_arrays(), "volume_ratio": [1.0] * 12 + [1.5]}  # exactly 1.5
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards(arrays=at_vol):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"volume exactly at the spike multiple must pass strict < reject, got {reason}"
        assert reason == "cup_and_handle_break"
        assert dbg["vol_ratio"] == pytest.approx(1.5, abs=1e-6)

    def test_volume_eps_below_threshold_no_fire(self):
        """volume_ratio one tick BELOW the multiple (1.49 < 1.5) -> dead break -> NO FIRE."""
        df = _cup_handle_df()
        lo_vol = {**_good_arrays(), "volume_ratio": [1.0] * 12 + [1.49]}
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards(arrays=lo_vol):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_low_volume"
        assert dbg["vol_ratio"] == pytest.approx(1.49, abs=1e-6)

    def test_volume_ratio_fallback_from_raw_volume(self):
        """When compute_all_from_df returns NO volume_ratio (vr empty / None at cur), the gate
        falls back to a raw 21-bar relvol off the Volume column. Build a break bar with a big
        raw-volume surge so the fallback clears the multiple -> FIRES."""
        df = _cup_handle_df()
        # make the break bar's raw Volume 5x the trailing mean so the fallback relvol >> 1.5.
        df.loc[12, "Volume"] = 5_000_000
        no_vr = {**_good_arrays(), "volume_ratio": []}  # empty -> fallback path
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards(arrays=no_vr):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"raw-volume fallback surge must fire, got {reason}"
        assert reason == "cup_and_handle_break"
        assert dbg["vol_ratio"] >= 1.5


# ───────────────────────── GUARD ORDERING: tape strictly AFTER L2 / extension ───────────

class TestCupAndHandleGuardChain:
    def test_l2_veto_short_circuits_before_tape(self):
        """L2 veto fires BEFORE the tape gate -> when L2 vetoes, tape_confirms_hold is NEVER
        called (proves the structural/L2 chase-guards run ahead of the last tape gate)."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_GATES}._l2_entry_veto"].return_value = ("l2_big_seller", {})
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_l2_big_seller"
        g.mocks[f"{_GATES}.tape_confirms_hold"].assert_not_called()

    def test_extension_short_circuits_before_l2_and_tape(self):
        """The parabolic-extension guard runs BEFORE L2 and tape -> when it vetoes, neither
        _l2_entry_veto nor tape_confirms_hold is called."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_GATES}._hod_extension_ok"].return_value = (False, {"hod_extended_vs": "vwap"})
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_extended"
        g.mocks[f"{_GATES}._l2_entry_veto"].assert_not_called()
        g.mocks[f"{_GATES}.tape_confirms_hold"].assert_not_called()

    def test_backside_short_circuits_before_extension(self):
        """The backside guard (the #1 chase guard) runs BEFORE the extension/L2/tape chain ->
        when it vetoes, _hod_extension_ok is never reached."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            g.mocks[f"{_GATES}._detect_back_side"].return_value = (True, "macd_cross_down")
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_back_side"
        g.mocks[f"{_GATES}._hod_extension_ok"].assert_not_called()


# ───────────────────────── FLAG DEFAULT + TOP-LEVEL FAIL-OPEN (except path) ──────────────

class TestCupAndHandleFlagAndFailOpen:
    def test_flag_defaults_off_when_attribute_absent(self):
        """The flag default is False: a settings object WITHOUT the attribute -> getattr default
        False -> disabled (NO FIRE) BEFORE any computation. (Conservative default-off; the gate
        is opt-in.)"""
        df = _cup_handle_df()
        # A bare settings stand-in with NO cup_and_handle flag attribute.
        bare = SimpleNamespace()
        with patch(f"{_GATES}.settings", bare):
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "cup_and_handle_disabled"

    def test_internal_exception_fails_open_to_error_decline(self):
        """Any unexpected exception inside the gate -> caught -> fail-OPEN to a BENIGN decline
        ``cup_and_handle_error`` (never a fire, never a raise that crashes the runner tick).
        Force compute_all_from_df to raise AFTER the structure passes."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
                patch(f"{_GATES}.compute_all_from_df", side_effect=RuntimeError("boom")):
            _base_settings(ms)
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "cup_and_handle_error"
        assert dbg == {"entry_interval": "5m"}

    def test_flag_off_skips_all_indicator_computation(self):
        """Flag OFF must short-circuit BEFORE compute_all_from_df / _batch_c_atr_pct run
        (byte-identical no-op; no indicator work, no guard calls)."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df") as caf, \
                patch(f"{_GATES}._batch_c_atr_pct") as atr:
            ms.chili_momentum_cup_and_handle_entry_enabled = False
            ok, reason, dbg = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "cup_and_handle_disabled"
        caf.assert_not_called()
        atr.assert_not_called()


# ───────────────────── A6: TOPPING-TAIL ANTICIPATORY EARLY FIRE ──────────────────────────
# Ross's biggest challenge winner: "jumped in a little early to anticipate the breakthrough …
# these were BOTH topping tails … got in as volume started to pick up." When BOTH rim-high
# bars are topping tails, fire EARLY on a live uptick through handle_low x (1 + min_reclaim_bps)
# + the volume-surge leg, BEFORE a full new high above the rim. Every guard still runs ahead;
# tape is the last fail-closed gate. Stop UNCHANGED (handle low).

# A live price above the handle-low reclaim level (9.70 x 1.0008 ~= 9.7078) but BELOW the rim
# (10.00) — proves the fire is EARLY (anticipatory), not the standard rim/tick break.
_ANTICIPATORY_LIVE_PX = 9.85


def _cup_handle_topping_tail_df() -> pd.DataFrame:
    """The clean-cup geometry, but the TWO rim bars (idx 3, idx 7) are TOPPING TAILS: a long
    upper wick (High at the rim) with the body pinned near the bar's low. High/Low geometry is
    UNCHANGED (so the swing-pivot double top is identical); only Open/Close move down into a
    small low body -> is_topping_tail(o,h,l,c) True on both rims."""
    df = _cup_handle_df()
    # idx 3: High=10.00, Low=9.60 -> body near the low (o=9.63,c=9.65) -> upper wick 0.35 dominates.
    df.loc[3, "Open"], df.loc[3, "Close"] = 9.63, 9.65
    # idx 7: High=10.00, Low=9.70 -> body near the low (o=9.73,c=9.75) -> upper wick 0.25 dominates.
    df.loc[7, "Open"], df.loc[7, "Close"] = 9.73, 9.75
    # keep the break bar (idx 12) from making a NEW HIGH so ONLY the anticipatory path can fire.
    df.loc[12, "High"] = _RIM - 0.01
    return df


class TestCupAndHandleAnticipatoryToppingTail:
    def test_topping_tail_rims_fire_early_pre_break(self):
        """BOTH rim bars are topping tails + a live uptick through the handle-low reclaim level
        (below the rim) + volume surge + tape -> EARLY FIRE, before any new high. Stop == handle
        low; entry-side is the anticipatory reclaim (NOT a rim/tick break)."""
        df = _cup_handle_topping_tail_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ms.chili_momentum_cup_handle_anticipatory_enabled = True
            ms.chili_momentum_tick_first_pullback_min_reclaim_bps = 8.0
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
                live_price=_ANTICIPATORY_LIVE_PX,
            )
        assert ok is True, f"topping-tail anticipatory must fire, got {reason} dbg={dbg}"
        assert reason == "cup_and_handle_anticipatory_topping_tail"
        assert dbg.get("rim_both_topping_tails") is True
        assert dbg.get("anticipatory_topping_tail") is True
        # fired BELOW the rim (early), and the stop is the unchanged handle low.
        assert _ANTICIPATORY_LIVE_PX < _RIM
        assert dbg["pullback_low"] == pytest.approx(_HANDLE_LOW, abs=1e-6)

    def test_no_topping_tails_waits(self):
        """Rim bars are NOT topping tails -> no early path; with no new high the gate WAITS
        (falls through to the rim-break path, which returns waiting_for_break)."""
        df = _cup_handle_df()  # standard rims (Close = midpoint, NOT topping tails)
        df.loc[12, "High"] = _RIM - 0.01  # no completed-bar break
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ms.chili_momentum_cup_handle_anticipatory_enabled = True
            ms.chili_momentum_tick_first_pullback_min_reclaim_bps = 8.0
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
                live_price=_ANTICIPATORY_LIVE_PX,  # below the rim -> no tick break either
            )
        assert ok is False, f"no topping tails must not early-fire, got {reason} dbg={dbg}"
        assert reason == "waiting_for_break"
        assert dbg.get("rim_both_topping_tails") in (False, None)

    def test_reclaim_without_tape_no_fire(self):
        """Topping-tail rims + reclaim + volume but tape UNCONFIRMED -> NO early fire (tape is
        the last fail-closed gate); with no new high the gate then WAITS."""
        df = _cup_handle_topping_tail_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards() as g:
            _base_settings(ms)
            ms.chili_momentum_cup_handle_anticipatory_enabled = True
            ms.chili_momentum_tick_first_pullback_min_reclaim_bps = 8.0
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_disabled"})
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
                live_price=_ANTICIPATORY_LIVE_PX,
            )
        assert ok is False, f"no tape must block the early fire, got {reason} dbg={dbg}"
        assert dbg.get("rim_both_topping_tails") is True
        assert reason == "waiting_for_break"  # fell through (no new high)

    def test_flag_off_is_byte_identical(self):
        """Flag OFF -> the anticipatory path never runs; with topping-tail rims + a below-rim
        live price the gate behaves EXACTLY as the rim-break-only path (waiting_for_break)."""
        df = _cup_handle_topping_tail_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ms.chili_momentum_cup_handle_anticipatory_enabled = False
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
                live_price=_ANTICIPATORY_LIVE_PX,
            )
        assert ok is False
        assert reason == "waiting_for_break"
        assert "rim_both_topping_tails" not in dbg  # the anticipatory block never executed

    def test_reclaim_below_level_no_early_fire(self):
        """Topping-tail rims but the live price is at/below the handle-low reclaim level (no
        uptick through it) -> no early fire (waits)."""
        df = _cup_handle_topping_tail_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _base_settings(ms)
            ms.chili_momentum_cup_handle_anticipatory_enabled = True
            ms.chili_momentum_tick_first_pullback_min_reclaim_bps = 8.0
            ok, reason, dbg = cup_and_handle_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
                live_price=_HANDLE_LOW - 0.05,  # below the reclaim level
            )
        assert ok is False
        assert reason == "waiting_for_break"
