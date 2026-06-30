"""Mock-FIRE tests for the BREAKOUT-family momentum entry gates (Ross momentum lane).

Each setup in the BREAKOUT family gets a POSITIVE test (the IDEAL pattern + all four shared
chase-guards passed -> the gate FIRES with its setup fire reason) and a NEGATIVE test (the
pattern is absent OR exactly one guard is tripped -> NO fire). The firing-mock scaffolding
MIRRORS ``test_momentum_cup_and_handle.py`` / ``test_momentum_setup_guard_parity.py``: the
intricate indicator/structural layer (``compute_all_from_df`` / ``_batch_c_atr_pct`` /
``_swing_pivots``) and the four shared chase-guards are patched at the CALL BOUNDARY so a
structurally-clean pattern reaches the guard chain and fires, while the synthetic OHLCV frame
carries the geometry the trigger needs.

The four shared chase-guards (every breakout trigger must carry them):
  1. NOT BACKSIDE / NOT BELOW-VWAP -- ``_detect_back_side`` + ``front_side_state``.
  2. NOT PARABOLIC -- ``_hod_extension_ok`` vs the 9-EMA AND VWAP.
  3. L2 hidden-/big-seller veto -- ``_l2_entry_veto``.
  4. TAPE REQUIRED + FAIL-CLOSED (or, for the HOD/ORB/blue-sky tick family, the
     tick-thrust + volume confirm) is the LAST gate before a fire.

Fire contract: ``(ok, reason, debug)``; on a fire ``debug`` carries ``pullback_high`` (the
entry/break level) and ``pullback_low`` (the structural stop).

SETUPS in this group (BREAKOUT family):
  * hod_break_confirmation              -> hod_break / hod_break_tick_ok
  * blue_sky_break_confirmation         -> blue_sky_break / blue_sky_break_tick_ok
  * opening_range_breakout_confirmation -> orb_break / orb_break_tick_ok
  * wedge_break_entry                   -> wedge_break / wedge_break_tick
  * premarket_pivot_macd_entry          -> premarket_pivot_macd / ..._tick
  * round_number_entry_context          -> DEFER-only modifier (NEVER fires; tested for the
                                           defer ``round_number_into_overhead`` vs the permit
                                           ``round_number_break_and_hold`` / disabled paths).

TESTS-ONLY: no source is edited. ``front_side_state`` is imported INSIDE each gate via
``from .ross_momentum import front_side_state`` -> it must be patched at its SOURCE module.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import (
    blue_sky_break_confirmation,
    hod_break_confirmation,
    opening_range_breakout_confirmation,
    premarket_pivot_macd_entry,
    round_number_entry_context,
    wedge_break_entry,
)

_GATES = "app.services.trading.momentum_neural.entry_gates"
_ROSS = "app.services.trading.momentum_neural.ross_momentum"
_PROFILE = "app.services.trading.momentum_neural.market_profile"


def _arrays(n: int) -> dict:
    """Clean indicator arrays: a front-side EMA stack (9>20), bullish MACD, a LOW VWAP so the
    below-VWAP / extension arms never trip by accident, and a break-bar volume surge."""
    return {
        "ema_9": [9.50] * n,
        "ema_20": [9.40] * n,
        "macd": [0.05] * n,
        "macd_signal": [0.03] * n,
        "vwap": [9.20] * n,
        "volume_ratio": [1.0] * (n - 1) + [3.0],
        "atr": [0.20] * n,
    }


# ════════════════════════════════════════════════════════════════════════════════════════
#  hod_break_confirmation — HOD / new-high break off a tight consolidation base.
#  Structure runs for REAL on the synthetic frame; the indicator layer + the chase-guards
#  (backside, front_side, extension, L2) are mocked at the boundary. The HOD break confirms
#  via tick-thrust+volume (no inline tape_confirms_hold), so the LAST gate is the volume
#  spike (completed bar) or the tick-thrust contract (live tick).
# ════════════════════════════════════════════════════════════════════════════════════════

_HOD_LEVEL = 10.00  # the base resistance / break level


def _hod_df() -> pd.DataFrame:
    """A run-up into a TIGHT consolidation base just under the day high, then a break bar.

    12 bars (cur=11). base_bars=4 -> the base = idx [7,8,9,10] (the last 4 COMPLETED bars
    before cur). Its high (10.00) is the resistance the break clears; its low (9.92) is the
    tight structural stop. The base sits AT the highs (sess HOD on completed bars == base
    high) and is tight (range ~0.8% < the ATR-relative width). idx 11 = cur = BREAK to a new
    high above the base.
    """
    bars = [
        # (open, high, low) run-up well below the base
        (9.00, 9.10, 8.95),   # 0
        (9.10, 9.30, 9.05),   # 1
        (9.30, 9.55, 9.25),   # 2
        (9.55, 9.80, 9.50),   # 3
        (9.80, 9.95, 9.75),   # 4
        (9.92, 9.98, 9.88),   # 5
        (9.95, 9.99, 9.90),   # 6
        (9.95, 10.00, 9.93),  # 7  base bar (high == 10.00 resistance)
        (9.96, 9.99, 9.92),   # 8  base bar (base low 9.92)
        (9.97, 10.00, 9.94),  # 9  base bar (taps the flat top)
        (9.98, 9.99, 9.95),   # 10 base bar
        (9.99, 10.35, 9.96),  # 11 cur = BREAK: new high above the base/HOD
    ]
    rows = [{"Open": o, "High": h, "Low": l, "Close": (h + l) / 2.0, "Volume": 1_000_000}
            for o, h, l in bars]
    return pd.DataFrame(rows)


def _hod_settings(ms) -> None:
    ms.chili_momentum_hod_break_entry_enabled = True
    ms.chili_momentum_flat_top_entry_enabled = True
    ms.chili_momentum_hod_base_bars = 4
    ms.chili_momentum_hod_base_atr_mult = 1.5
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


class _HodPassGuards:
    """Mock the indicator layer + the chase-guards so a clean HOD base+break fires."""

    def __init__(self, arrays=None):
        self._arrays = arrays if arrays is not None else _arrays(12)
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
        _p(f"{_GATES}._premarket_tickbreak_confirmed", return_value=True)
        _p(f"{_GATES}._dipbuy_tick_thrust_ok", return_value=True)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestHodBreak:
    def test_positive_clean_hod_break_fires(self):
        """IDEAL: a tight base under the HOD + a completed-bar break to a new high + volume
        surge + all guards pass -> FIRES (``hod_break``), stop == base low, entry == HOD."""
        df = _hod_df()
        with patch(f"{_GATES}.settings") as ms, _HodPassGuards():
            _hod_settings(ms)
            ok, reason, dbg = hod_break_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean HOD break must fire, got {reason} dbg={dbg}"
        assert reason == "hod_break"
        assert dbg["pullback_high"] == pytest.approx(_HOD_LEVEL, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(9.92, abs=1e-6)
        assert dbg["pullback_low"] < dbg["pullback_high"]

    def test_negative_backside_no_fire(self):
        """NEGATIVE: the #1 chase-guard tripped -> ``_detect_back_side`` reports a rolled-over
        top (9<20 EMA) -> NO fire (``hod_break_back_side``)."""
        df = _hod_df()
        with patch(f"{_GATES}.settings") as ms, _HodPassGuards() as g:
            _hod_settings(ms)
            g.mocks[f"{_GATES}._detect_back_side"].return_value = (True, "ema9_below_ema20")
            ok, reason, dbg = hod_break_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "hod_break_back_side"


# ════════════════════════════════════════════════════════════════════════════════════════
#  blue_sky_break_confirmation — break of a multi-period / all-time high with CLEAR sky.
#  REUSES the HOD base machinery + adds the DailyContext clear-sky requirement. The clear-sky
#  read (``entry_is_clear_sky``) is patched to True for the positive case (the daily layer is
#  not the subject of THIS gate's firing geometry); the chase-guards are mocked as usual.
# ════════════════════════════════════════════════════════════════════════════════════════

_DAILY = "app.services.trading.momentum_neural.daily_levels"


def _blue_sky_settings(ms) -> None:
    ms.chili_momentum_blue_sky_entry_enabled = True
    ms.chili_momentum_hod_base_bars = 4
    ms.chili_momentum_hod_base_atr_mult = 1.5
    ms.chili_momentum_blue_sky_entry_min_room_atr = 1.5
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


class _BlueSkyPassGuards:
    def __init__(self, arrays=None, clear_sky=True):
        self._arrays = arrays if arrays is not None else _arrays(12)
        self._clear_sky = clear_sky
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        # daily_levels.entry_is_clear_sky is imported INSIDE the gate -> patch at the source.
        _p(f"{_DAILY}.entry_is_clear_sky", return_value=self._clear_sky)
        _p(f"{_GATES}._detect_back_side", return_value=(False, "front_side"))
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        _p(f"{_GATES}._premarket_tickbreak_confirmed", return_value=True)
        _p(f"{_GATES}._dipbuy_tick_thrust_ok", return_value=True)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestBlueSkyBreak:
    def test_positive_clean_blue_sky_break_fires(self):
        """IDEAL: a tight base + clear-sky new-high break + volume surge + guards pass ->
        FIRES (``blue_sky_break``), entry == base high, stop == base low."""
        df = _hod_df()  # reuses the HOD base+break geometry
        daily_ctx = SimpleNamespace(is_blue_sky=True, room_to_gap_top_atr=3.0)
        with patch(f"{_GATES}.settings") as ms, _BlueSkyPassGuards(clear_sky=True):
            _blue_sky_settings(ms)
            ok, reason, dbg = blue_sky_break_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), daily_ctx=daily_ctx,
            )
        assert ok is True, f"clean blue-sky break must fire, got {reason} dbg={dbg}"
        assert reason == "blue_sky_break"
        assert dbg["pullback_high"] == pytest.approx(_HOD_LEVEL, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(9.92, abs=1e-6)

    def test_negative_not_clear_sky_no_fire(self):
        """NEGATIVE: the defining gate is absent -> ``entry_is_clear_sky`` is False (a ceiling
        sits overhead) -> NO fire (``blue_sky_break_not_clear_sky``)."""
        df = _hod_df()
        daily_ctx = SimpleNamespace(is_blue_sky=False, room_to_gap_top_atr=0.2)
        with patch(f"{_GATES}.settings") as ms, _BlueSkyPassGuards(clear_sky=False):
            _blue_sky_settings(ms)
            ok, reason, dbg = blue_sky_break_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), daily_ctx=daily_ctx,
            )
        assert ok is False
        assert reason == "blue_sky_break_not_clear_sky"


# ════════════════════════════════════════════════════════════════════════════════════════
#  opening_range_breakout_confirmation — break above the opening-range high.
#  The session-window read (``minutes_since_regular_open``) is patched so we are INSIDE the
#  ORB window; the OR high/low is built from the COMPLETED bars of the synthetic frame (the
#  non-datetime fallback path: first K bars). The chase-guards are mocked as usual.
# ════════════════════════════════════════════════════════════════════════════════════════

_ORB_HIGH = 10.00  # the opening-range high / break level


def _orb_df() -> pd.DataFrame:
    """An opening-range coil (first bars define OR-high 10.00 / OR-low 9.90) then a break.

    13 bars; with the non-datetime fallback the OR = the first ``_orb_bar_count`` bars of the
    session frame (5m interval, orb_minutes=5 -> ~1 bar, but ``end`` clamps to len-1). The
    first several bars hold the [9.90, 10.00] range; idx 12 = cur = the break above 10.00.
    To make the fallback OR deterministic regardless of bar-count rounding, EVERY pre-cur bar
    stays within [9.90, 10.00] so OR-high==10.00 / OR-low==9.90 no matter the slice end.
    """
    bars = [
        (9.92, 10.00, 9.90),  # 0  OR bar (high 10.00, low 9.90)
        (9.95, 9.99, 9.91),   # 1
        (9.94, 9.98, 9.90),   # 2
        (9.96, 10.00, 9.92),  # 3
        (9.95, 9.99, 9.91),   # 4
        (9.96, 9.98, 9.93),   # 5
        (9.95, 9.99, 9.92),   # 6
        (9.96, 10.00, 9.94),  # 7
        (9.97, 9.99, 9.93),   # 8
        (9.96, 9.98, 9.94),   # 9
        (9.97, 9.99, 9.95),   # 10
        (9.98, 9.99, 9.95),   # 11
        (9.98, 10.35, 9.96),  # 12 cur = BREAK above the OR-high
    ]
    rows = [{"Open": o, "High": h, "Low": l, "Close": (h + l) / 2.0, "Volume": 1_000_000}
            for o, h, l in bars]
    return pd.DataFrame(rows)


def _orb_settings(ms) -> None:
    ms.chili_momentum_orb_entry_enabled = True
    ms.chili_momentum_orb_minutes = 5
    ms.chili_momentum_orb_window_minutes = 60.0
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


class _OrbPassGuards:
    def __init__(self, arrays=None, mins_since_open=15.0):
        self._arrays = arrays if arrays is not None else _arrays(13)
        self._mins = mins_since_open
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        # market_profile.minutes_since_regular_open is imported INSIDE the gate.
        _p(f"{_PROFILE}.minutes_since_regular_open", return_value=self._mins)
        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        _p(f"{_GATES}._detect_back_side", return_value=(False, "front_side"))
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        _p(f"{_GATES}._premarket_tickbreak_confirmed", return_value=True)
        _p(f"{_GATES}._dipbuy_tick_thrust_ok", return_value=True)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestOpeningRangeBreakout:
    def test_positive_clean_orb_break_fires(self):
        """IDEAL: inside the ORB window + a completed-bar break above the OR-high + volume
        surge + guards pass -> FIRES (``orb_break``), entry == OR-high, stop == OR-low."""
        df = _orb_df()
        with patch(f"{_GATES}.settings") as ms, _OrbPassGuards(mins_since_open=15.0):
            _orb_settings(ms)
            ok, reason, dbg = opening_range_breakout_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean ORB break must fire, got {reason} dbg={dbg}"
        assert reason == "orb_break"
        assert dbg["pullback_high"] == pytest.approx(_ORB_HIGH, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(9.90, abs=1e-6)
        assert dbg["pullback_low"] < dbg["pullback_high"]

    def test_negative_low_volume_break_no_fire(self):
        """NEGATIVE: the LAST gate tripped -> a completed-bar break with NO volume surge
        (volume_ratio below the spike multiple) -> NO fire (``orb_low_volume``)."""
        df = _orb_df()
        weak_vol = {**_arrays(13), "volume_ratio": [1.0] * 13}  # no surge on the break bar
        with patch(f"{_GATES}.settings") as ms, _OrbPassGuards(arrays=weak_vol, mins_since_open=15.0):
            _orb_settings(ms)
            ok, reason, dbg = opening_range_breakout_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "orb_low_volume"


# ════════════════════════════════════════════════════════════════════════════════════════
#  wedge_break_entry — a converging (falling/symmetric) wedge break with TAPE.
#  The pivot scanner (``_swing_pivots``) + ATR (``_batch_c_atr_pct``) are intricate, so we
#  patch them at the boundary (mirrors the bull_flag structural-helper mocks) to inject a
#  clean falling wedge: 2 upper taps (descending) + 2 lower taps (ascending), converging.
#  The four shared chase-guards (backside, front_side, extension, L2) + tape are mocked.
# ════════════════════════════════════════════════════════════════════════════════════════

_WEDGE_LEVEL = 10.00  # the most-recent upper tap (the projected break level clamps to it)


def _wedge_df() -> pd.DataFrame:
    """A 12-bar frame; the pivots are MOCKED so only the break bar's geometry matters: cur
    high/close must trade through the wedge upper line (10.00). idx 11 = cur breaks out."""
    rows = []
    for i in range(11):
        rows.append({"Open": 9.90, "High": 9.98, "Low": 9.85, "Close": 9.92, "Volume": 1_000_000})
    rows.append({"Open": 9.98, "High": 10.30, "Low": 9.95, "Close": 10.25, "Volume": 1_000_000})  # 11 break
    return pd.DataFrame(rows)


def _wedge_settings(ms) -> None:
    ms.chili_momentum_wedge_break_entry_enabled = True
    ms.chili_momentum_swing_pivot_half_window = 1
    ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0


# A clean FALLING wedge: upper taps DESCEND (10.20 -> 10.00), lower taps ASCEND (9.30 ->
# 9.60); the newer gap (10.00-9.60=0.40) < the older gap (10.20-9.30=0.90) -> converging.
# stop = the newer lower tap (9.60). idx ordering: h2<h1 and l2<l1 indices (older->newer).
_WEDGE_PIVOTS = [
    {"idx": 2, "price": 10.20, "kind": "H"},   # h2 (older upper tap)
    {"idx": 3, "price": 9.30, "kind": "L"},    # l2 (older lower tap)
    {"idx": 8, "price": _WEDGE_LEVEL, "kind": "H"},  # h1 (newer upper tap = break level)
    {"idx": 9, "price": 9.60, "kind": "L"},    # l1 (newer lower tap = stop)
]


class _WedgePassGuards:
    def __init__(self, arrays=None, pivots=None):
        self._arrays = arrays if arrays is not None else _arrays(12)
        self._pivots = pivots if pivots is not None else list(_WEDGE_PIVOTS)
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        _p(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20))
        _p(f"{_GATES}._swing_pivots", return_value=self._pivots)
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


class TestWedgeBreak:
    def test_positive_clean_falling_wedge_break_fires(self):
        """IDEAL: a converging falling wedge (3+ taps) + a break OUT of the upper line + tape
        confirms + guards pass -> FIRES (``wedge_break``), entry == upper-line level, stop ==
        apex (newer lower) pivot low."""
        df = _wedge_df()
        with patch(f"{_GATES}.settings") as ms, _WedgePassGuards():
            _wedge_settings(ms)
            ok, reason, dbg = wedge_break_entry(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean falling-wedge break must fire, got {reason} dbg={dbg}"
        assert reason == "wedge_break"
        assert dbg["pullback_high"] == pytest.approx(_WEDGE_LEVEL, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(9.60, abs=1e-6)
        assert dbg["pullback_low"] < dbg["pullback_high"]

    def test_negative_no_tape_no_fire(self):
        """NEGATIVE: the LAST gate tripped -> ``tape_confirms_hold`` says NO (no buyers on the
        tape) -> NO fire (``wedge_break_tape_unconfirmed``); tape is fail-closed."""
        df = _wedge_df()
        with patch(f"{_GATES}.settings") as ms, _WedgePassGuards() as g:
            _wedge_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_no_data"})
            ok, reason, dbg = wedge_break_entry(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "wedge_break_tape_unconfirmed"

    def test_negative_rising_wedge_skipped(self):
        """NEGATIVE (structure absent): a RISING wedge (both lines rising = bearish exhaustion)
        is never bought -> NO fire (``wedge_break_rising_skip``)."""
        df = _wedge_df()
        # Both lines RISE: upper 9.80 -> 10.00 (up), lower 9.20 -> 9.60 (up); newer gap 0.40 <
        # older gap 0.60 -> converging, but rising -> skip.
        rising = [
            {"idx": 2, "price": 9.80, "kind": "H"},
            {"idx": 3, "price": 9.20, "kind": "L"},
            {"idx": 8, "price": 10.00, "kind": "H"},
            {"idx": 9, "price": 9.60, "kind": "L"},
        ]
        with patch(f"{_GATES}.settings") as ms, _WedgePassGuards(pivots=rising):
            _wedge_settings(ms)
            ok, reason, dbg = wedge_break_entry(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "wedge_break_rising_skip"


# ════════════════════════════════════════════════════════════════════════════════════════
#  premarket_pivot_macd_entry — premarket pivot break + a fresh MACD re-cross, EQUITY-ONLY.
#  The pivots (``_swing_pivots``) + ATR are mocked to inject a clean pivot (level + stop); the
#  MACD arrays carry a fresh re-cross (line crossed ABOVE signal within the lookback and is
#  still above now); rvol clears the cold floor. The four chase-guards + tape are mocked.
# ════════════════════════════════════════════════════════════════════════════════════════

_PMP_LEVEL = 10.00  # the premarket pivot (swing high) break level


def _pmp_df() -> pd.DataFrame:
    """A 12-bar frame; pivots are MOCKED so only the break bar matters: cur high/close trade
    through the pivot level (10.00). idx 11 = cur = the gap-and-go break."""
    rows = []
    for i in range(11):
        rows.append({"Open": 9.85, "High": 9.95, "Low": 9.80, "Close": 9.90, "Volume": 1_000_000})
    rows.append({"Open": 9.95, "High": 10.30, "Low": 9.92, "Close": 10.25, "Volume": 1_000_000})  # 11 break
    return pd.DataFrame(rows)


def _pmp_arrays(n: int, *, recross: bool = True) -> dict:
    """Indicator arrays with a controllable MACD re-cross at the last two bars.

    recross=True: macd[cur-1] <= signal[cur-1] AND macd[cur] > signal[cur] (a fresh bullish
    re-cross within the lookback AND still above now). recross=False: macd stays above signal
    throughout (no crossing event in the window) -> ``premarket_pivot_no_macd_recross``."""
    a = _arrays(n)
    if recross:
        # below at cur-1, above at cur -> a fresh re-cross on the last bar.
        macd = [0.05] * (n - 1) + [0.10]
        sig = [0.03] * (n - 2) + [0.08, 0.04]   # sig[cur-1]=0.08 > macd[cur-1]=0.05 (below)
    else:
        macd = [0.10] * n
        sig = [0.03] * n   # macd always > signal -> no crossing event
    a["macd"] = macd
    a["macd_signal"] = sig
    return a


def _pmp_settings(ms) -> None:
    ms.chili_momentum_premarket_pivot_macd_entry_enabled = True
    ms.chili_momentum_swing_pivot_half_window = 1
    ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
    ms.chili_momentum_premarket_pivot_cold_rvol_floor = 1.5
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


# A clean pivot: the newest swing HIGH = the break level (10.00); the newest swing LOW = the
# stop (9.70). 0 < stop < level.
_PMP_PIVOTS = [
    {"idx": 3, "price": 9.40, "kind": "L"},
    {"idx": 5, "price": 9.95, "kind": "H"},
    {"idx": 8, "price": 9.70, "kind": "L"},          # newest low = stop
    {"idx": 9, "price": _PMP_LEVEL, "kind": "H"},    # newest high = break level
]


class _PmpPassGuards:
    def __init__(self, arrays=None, pivots=None):
        # rvol = volume_ratio[cur] = 3.0 (clears the 1.5 cold floor).
        self._arrays = arrays if arrays is not None else _pmp_arrays(12, recross=True)
        self._pivots = pivots if pivots is not None else list(_PMP_PIVOTS)
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=self._arrays)
        _p(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20))
        _p(f"{_GATES}._swing_pivots", return_value=self._pivots)
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


class TestPremarketPivotMacd:
    def test_positive_clean_premarket_pivot_macd_fires(self):
        """IDEAL: a premarket pivot break + a fresh MACD re-cross + warm RVOL + tape + guards
        pass -> FIRES (``premarket_pivot_macd``), entry == pivot level, stop == pivot low."""
        df = _pmp_df()
        with patch(f"{_GATES}.settings") as ms, _PmpPassGuards():
            _pmp_settings(ms)
            ok, reason, dbg = premarket_pivot_macd_entry(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean premarket-pivot MACD break must fire, got {reason} dbg={dbg}"
        assert reason == "premarket_pivot_macd"
        assert dbg["macd_recross"] is True
        assert dbg["pullback_high"] == pytest.approx(_PMP_LEVEL, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(9.70, abs=1e-6)
        assert dbg["pullback_low"] < dbg["pullback_high"]

    def test_negative_no_macd_recross_no_fire(self):
        """NEGATIVE (signal absent): MACD never re-crossed within the lookback (it stayed above
        signal throughout, no crossing event) -> NO fire (``premarket_pivot_no_macd_recross``)."""
        df = _pmp_df()
        no_recross = _pmp_arrays(12, recross=False)
        with patch(f"{_GATES}.settings") as ms, _PmpPassGuards(arrays=no_recross):
            _pmp_settings(ms)
            ok, reason, dbg = premarket_pivot_macd_entry(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "premarket_pivot_no_macd_recross"

    def test_negative_crypto_exempt_no_fire(self):
        """NEGATIVE (not applicable): premarket pivot is EQUITY-ONLY -> a ``-USD`` crypto symbol
        is exempt -> NO fire (``premarket_pivot_crypto_exempt``)."""
        df = _pmp_df()
        with patch(f"{_GATES}.settings") as ms, _PmpPassGuards():
            _pmp_settings(ms)
            ok, reason, dbg = premarket_pivot_macd_entry(
                df, entry_interval="5m", symbol="BTC-USD", db=MagicMock(),
            )
        assert ok is False
        assert reason == "premarket_pivot_crypto_exempt"


# ════════════════════════════════════════════════════════════════════════════════════════
#  round_number_entry_context — a DEFER-ONLY modifier (NEVER fires a NEW entry).
#  It returns ``ok=False`` (defer) ONLY when a round number sits in the OVERHEAD band just
#  above the entry AND the breakout level has not yet cleared+held it (buying INTO overhead
#  supply). When the level cleared+holds the round number, or the flag is off / no round
#  number, it PERMITS (``ok=True``). There is no fire reason — the POSITIVE assertion here is
#  that it correctly DEFERS near a round number (the intended behaviour for this modifier).
# ════════════════════════════════════════════════════════════════════════════════════════

class _RnSettings(SimpleNamespace):
    pass


def _rn_settings(*, enabled=True) -> _RnSettings:
    return _RnSettings(
        chili_momentum_round_number_entry_timing_enabled=enabled,
        chili_momentum_entry_extension_floor_pct=0.08,
    )


class TestRoundNumberEntryContext:
    def test_positive_defers_into_overhead_round_number(self):
        """The intended DEFER behaviour: entry 9.97 sits just UNDER the round number 10.00
        (in the overhead band) AND the breakout level 9.98 has NOT cleared 10.00 -> we would
        buy straight INTO the psych-level overhead supply -> DEFER (``round_number_into_overhead``,
        ok=False). atr_pct=0.05 -> _round_number_near(9.97) tol = 0.25*0.05*9.97 ≈ 0.125 >=
        |9.97-10.00| -> 10.00 is the nearby round number."""
        s = _rn_settings(enabled=True)
        ok, reason, dbg = round_number_entry_context(
            entry_price=9.97, breakout_level=9.98, atr_pct=0.05, settings_obj=s,
        )
        assert ok is False, f"must DEFER into the overhead round number, got {reason} dbg={dbg}"
        assert reason == "round_number_into_overhead"
        assert dbg["round_number"] == pytest.approx(10.00, abs=1e-6)

    def test_negative_break_and_hold_over_round_number_permits(self):
        """NEGATIVE (no defer): the breakout LEVEL 10.05 has CLEARED + holds the round number
        10.00 (a break-and-hold OVER it, exactly what Ross wants) -> PERMIT (ok=True,
        ``round_number_break_and_hold``) — the modifier does NOT defer a confirmed hold-over."""
        s = _rn_settings(enabled=True)
        ok, reason, dbg = round_number_entry_context(
            entry_price=9.97, breakout_level=10.05, atr_pct=0.05, settings_obj=s,
        )
        assert ok is True
        assert reason == "round_number_break_and_hold"
        assert dbg.get("round_number_held") is True

    def test_negative_flag_off_permits_byte_identical(self):
        """NEGATIVE (modifier off): flag OFF -> the modifier is byte-identical no-op -> PERMIT
        (ok=True, ``round_number_disabled``) before any round-number computation."""
        s = _rn_settings(enabled=False)
        ok, reason, dbg = round_number_entry_context(
            entry_price=9.97, breakout_level=9.98, atr_pct=0.05, settings_obj=s,
        )
        assert ok is True
        assert reason == "round_number_disabled"
        assert dbg == {}
