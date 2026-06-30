"""Mock-FIRE tests for the PULLBACK / CONTINUATION family of momentum entry gates.

For EACH setup we prove BOTH directions of the fire contract on a synthetic OHLCV frame:

  * POSITIVE — the IDEAL pattern geometry is built AND every chase-guard is mocked to PASS
    at the call boundary (``compute_all_from_df`` / ATR + the shared guards
    ``_detect_back_side`` / ``front_side_state`` / ``_hod_extension_ok`` / ``_l2_entry_veto`` /
    ``tape_confirms_hold``), so the gate FIRES with its setup fire reason (NOT a
    ``*_unconfirmed`` / ``*_veto`` / ``waiting_*`` reason). On a fire ``debug`` carries
    ``pullback_high`` (the entry / break level) and ``pullback_low`` (the structural stop).
  * NEGATIVE — either the pattern is ABSENT or exactly ONE guard is tripped, so the gate
    does NOT fire (a benign decline / veto / wait reason).

This MIRRORS the proven firing-mock scaffolding in ``test_momentum_cup_and_handle.py`` and
``test_momentum_setup_guard_parity.py``: settings patched at the gate's module namespace, the
indicator layer (``compute_all_from_df`` / ``_batch_c_atr_pct``) and the guards mocked so the
clean baseline reaches the fire path, and the synthetic frame built with the High/Low geometry
the (real) structural layer needs. ``front_side_state`` is imported INSIDE each gate via
``from .ross_momentum import front_side_state`` so it is patched at its SOURCE module.

SETUPS (PULLBACK / CONTINUATION family):
  first_pullback_break, micro_pullback_primary_confirmation, pullback_break_confirmation,
  bull_flag_confirmation, ma_vwap_pullback_confirmation, ross_abcd_confirmation,
  pulling_away_roc_entry, momentum_continuation_trigger.

TESTS-ONLY — never edits source.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import (
    bull_flag_confirmation,
    first_pullback_break,
    ma_vwap_pullback_confirmation,
    micro_pullback_primary_confirmation,
    momentum_continuation_trigger,
    pulling_away_roc_entry,
    pullback_break_confirmation,
    ross_abcd_confirmation,
)

_GATES = "app.services.trading.momentum_neural.entry_gates"
# front_side_state is imported INSIDE each gate from its source module -> patch it there.
_ROSS = "app.services.trading.momentum_neural.ross_momentum"
_CANDLES = "app.services.trading.momentum_neural.candles"


def _ohlcv(bars):
    """Build an OHLCV DataFrame from (open, high, low, close[, volume]) tuples."""
    rows = []
    for b in bars:
        if len(b) == 5:
            o, h, l, c, v = b
        else:
            o, h, l, c = b
            v = 1_000_000
        rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})
    return pd.DataFrame(rows)


def _arrays(n: int) -> dict:
    """Clean indicator arrays: front-side EMA stack (9>20), bullish MACD, a LOW VWAP so the
    below-VWAP / extension arms never trip by accident, and a break-bar volume surge."""
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
#  1) first_pullback_break  (4-tuple: (verdict, level, stop, debug); FIRE verdict == "FIRE")
#     Ross's EARLIEST entry. Per-symbol (no batch): RVOL floor + already-moving impulse, then
#     first-pullback-only + shallow-depth + EMA-hold, then the L2 veto, then the new-high break.
# ════════════════════════════════════════════════════════════════════════════════════════

def _first_pullback_df() -> pd.DataFrame:
    """An up-impulse (idx 0..8) into a peak, a SHALLOW 2-3 bar pullback holding the 9-EMA
    (mocked at 9.50), then the current bar (idx 12) makes a NEW HIGH above the pullback's
    prior swing high. >=10 bars required.

        idx:  0..8  rising impulse to peak ~10.20  | 9,10,11 shallow pullback | 12 BREAK
    """
    bars = [
        (9.00, 9.10, 8.95, 9.05),
        (9.05, 9.30, 9.00, 9.25),
        (9.25, 9.55, 9.20, 9.50),
        (9.50, 9.80, 9.45, 9.75),
        (9.75, 10.05, 9.70, 10.00),
        (10.00, 10.20, 9.95, 10.15),   # 5 impulse peak (win_high 10.20)
        (10.15, 10.18, 10.00, 10.05),  # 6
        (10.05, 10.10, 9.95, 10.00),   # 7
        (10.00, 10.08, 9.92, 9.98),    # 8
        (9.98, 10.05, 9.90, 9.96),     # 9  pullback bar (shallow)
        (9.96, 10.02, 9.88, 9.95),     # 10 pullback LOW ~9.88 (the stop)
        (9.95, 10.10, 9.93, 10.05),    # 11 last completed pullback bar -> pb_high ~10.10
        (10.05, 10.40, 10.02, 10.35),  # 12 cur = BREAK new high above pb_high
    ]
    return _ohlcv(bars)


def _fp_settings(ms) -> None:
    ms.chili_momentum_entry_sustained_rvol_floor = 0.0   # lenient RVOL floor
    ms.chili_momentum_entry_sustain_lookback_bars = 5
    ms.chili_momentum_dipbuy_impulse_accum_min_slope = -1.0  # Gate 2b disabled
    ms.chili_momentum_dipbuy_distribution_vol_mult = 0.0     # Gate 2a disabled


class TestFirstPullbackBreak:
    def test_ideal_first_pullback_fires(self):
        """IDEAL shallow first-pullback + new-high break + RVOL/impulse OK + no L2 veto ->
        verdict FIRE, debug carries pullback_high (entry) + pullback_low (stop)."""
        df = _first_pullback_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(13)), \
                patch(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.95, 0.02, 0.10)), \
                patch(f"{_GATES}._collapse_cap", return_value=0.90), \
                patch(f"{_GATES}._is_first_pullback", return_value=True), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None):
            _fp_settings(ms)
            verdict, level, stop, dbg = first_pullback_break(df, symbol="TEST", db=MagicMock())
        assert verdict == "FIRE", f"ideal first-pullback must FIRE, got {verdict} dbg={dbg}"
        assert level is not None and stop is not None
        assert dbg["pullback_high"] == pytest.approx(level, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(stop, abs=1e-6)
        assert dbg["pullback_low"] < dbg["pullback_high"]
        assert dbg.get("pattern") == "first_pullback"

    def test_l2_big_seller_blocks_fire(self):
        """ONE guard tripped: the L2 big-seller veto -> verdict PASS (no fire)."""
        df = _first_pullback_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(13)), \
                patch(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.95, 0.02, 0.10)), \
                patch(f"{_GATES}._collapse_cap", return_value=0.90), \
                patch(f"{_GATES}._is_first_pullback", return_value=True), \
                patch(f"{_GATES}._l2_entry_veto", return_value=("l2_big_seller", {"l2_pctile": 0.05})):
            _fp_settings(ms)
            verdict, level, stop, dbg = first_pullback_break(df, symbol="TEST", db=MagicMock())
        assert verdict == "PASS"
        assert dbg.get("fp_declined") == "l2_big_seller"


# ════════════════════════════════════════════════════════════════════════════════════════
#  2) micro_pullback_primary_confirmation  (fire reason "micro_pullback_primary")
#     Hot-tape gate + the micro-pullback shelf/dip detector + bounce-curl, then backside + L2.
# ════════════════════════════════════════════════════════════════════════════════════════

def _micro_df() -> pd.DataFrame:
    """A simple up frame (>=10 bars); the micro geometry is supplied by the mocked detector,
    so only the cur bar's HIGH (the completed-bar break above the bounce level) matters."""
    bars = [
        (9.00, 9.10, 8.95, 9.05),
        (9.05, 9.30, 9.00, 9.25),
        (9.25, 9.55, 9.20, 9.50),
        (9.50, 9.80, 9.45, 9.75),
        (9.75, 9.95, 9.70, 9.90),
        (9.90, 10.05, 9.85, 10.00),
        (10.00, 10.10, 9.92, 9.98),   # micro dip
        (9.98, 10.06, 9.95, 10.02),
        (10.02, 10.12, 9.99, 10.08),
        (10.08, 10.30, 10.05, 10.25),  # cur = break above bounce_high (10.10)
    ]
    return _ohlcv(bars)


class TestMicroPullbackPrimary:
    _BOUNCE = 10.10
    _DIP = 9.92

    def _detect(self):
        return {"fire": True, "reason": "ok", "bounce_high": self._BOUNCE, "dip_low": self._DIP}

    def test_ideal_micro_pullback_fires(self):
        """IDEAL hot-tape + micro shelf/dip detector fires + bounce-curl + new-high break +
        front-side + no L2 veto -> FIRES 'micro_pullback_primary' with pullback_high/low."""
        df = _micro_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(10)), \
                patch(f"{_GATES}._is_hot_tape", return_value=True), \
                patch(f"{_GATES}._compute_confirmed_swing_low_last", return_value=9.85), \
                patch(f"{_GATES}.micro_pullback_reentry_detect", return_value=self._detect()), \
                patch(f"{_CANDLES}.bounce_curl_from_df", return_value=True), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None):
            ms.chili_momentum_micro_pullback_primary_enabled = True
            ms.chili_momentum_micropullback_reentry_max_dip_pct = 0.04
            ok, reason, dbg = micro_pullback_primary_confirmation(df, entry_interval="1m", symbol="TEST", db=MagicMock())
        assert ok is True, f"ideal micro-pullback must fire, got {reason} dbg={dbg}"
        assert reason == "micro_pullback_primary"
        assert dbg["pullback_high"] == pytest.approx(self._BOUNCE, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(self._DIP, abs=1e-6)

    def test_cold_tape_no_fire(self):
        """ONE guard tripped: cold tape (the mandatory hot-tape gate fails) -> NO FIRE."""
        df = _micro_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(10)), \
                patch(f"{_GATES}._is_hot_tape", return_value=False), \
                patch(f"{_GATES}.micro_pullback_reentry_detect", return_value=self._detect()), \
                patch(f"{_CANDLES}.bounce_curl_from_df", return_value=True), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None):
            ms.chili_momentum_micro_pullback_primary_enabled = True
            ms.chili_momentum_micropullback_reentry_max_dip_pct = 0.04
            ok, reason, dbg = micro_pullback_primary_confirmation(df, entry_interval="1m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "micro_primary_cold_tape"


# ════════════════════════════════════════════════════════════════════════════════════════
#  3) pullback_break_confirmation  (fire reason "pullback_break_ok" via the raw-break path)
#     The big live trigger. We drive the SIMPLEST raw-break (require_retest False, all optional
#     confirmations off) and mock the chase-guards. The explosive-floor / red-vol-exhaustion /
#     backside-veto flags are set OFF for the clean baseline (each is independently flag-gated);
#     the negative test trips the backside veto.
# ════════════════════════════════════════════════════════════════════════════════════════

def _pullback_df() -> pd.DataFrame:
    """Impulse -> shallow pullback holding the 9-EMA -> current bar breaks the pullback high."""
    bars = [
        (9.00, 9.10, 8.95, 9.05),
        (9.05, 9.30, 9.00, 9.25),
        (9.25, 9.55, 9.20, 9.50),
        (9.50, 9.80, 9.45, 9.75),
        (9.75, 10.05, 9.70, 10.00),
        (10.00, 10.20, 9.95, 10.15),   # 5 impulse peak (win_high 10.20)
        (10.15, 10.18, 10.00, 10.05),  # 6
        (10.05, 10.10, 9.95, 10.00),   # 7
        (10.00, 10.08, 9.92, 9.98),    # 8
        (9.98, 10.05, 9.90, 9.96),     # 9 pullback
        (9.96, 10.02, 9.88, 9.95),     # 10 pullback low ~9.88
        (9.95, 10.10, 9.93, 10.05),    # 11 last pullback bar -> pb_high ~10.10
        (10.05, 10.40, 10.02, 10.35),  # 12 cur = BREAK (green, strong)
    ]
    return _ohlcv(bars)


def _pb_settings(ms) -> None:
    # Independently-gated downstream vetoes OFF so the raw-break baseline isolates the trigger.
    ms.chili_momentum_entry_first_pullback_enabled = False  # use the plain raw-break path
    ms.chili_momentum_backside_veto_enabled = False
    ms.chili_momentum_candle_quality_multitf_veto_enabled = False
    ms.chili_momentum_red_vol_exhaustion_veto_enabled = False
    ms.chili_momentum_explosive_floor_enabled = False
    ms.chili_momentum_entry_verticality_atr_mult = 0.0      # verticality gate OFF
    ms.chili_momentum_entry_macd_open_strict = False


class TestPullbackBreakConfirmation:
    def test_ideal_pullback_break_fires(self):
        """IDEAL shallow pullback + completed-bar break + volume + front-side -> FIRES
        'pullback_break_ok' with pullback_high (entry) + pullback_low (stop)."""
        df = _pullback_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(13)), \
                patch(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.95, 0.02, 0.10)), \
                patch(f"{_GATES}.pullback_ordinal_recent", return_value=1), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")):
            _pb_settings(ms)
            ok, reason, dbg = pullback_break_confirmation(
                df, entry_interval="1m", symbol="TEST", db=MagicMock(),
                require_retest=False, require_sustained_volume=False,
                require_break_candle=False, require_vwap_hold=False,
                require_macd_bullish=False, volume_spike_multiple=1.5,
            )
        assert ok is True, f"ideal pullback-break must fire, got {reason} dbg={dbg}"
        assert reason == "pullback_break_ok"
        assert dbg["pullback_low"] < dbg["pullback_high"]

    def test_backside_rollover_no_fire(self):
        """ONE guard tripped: backside veto ON + a rolled-over read (9<20 EMA) -> NO FIRE."""
        df = _pullback_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(13)), \
                patch(f"{_GATES}._vol_aware_pullback_tolerances", return_value=(0.95, 0.02, 0.10)), \
                patch(f"{_GATES}.pullback_ordinal_recent", return_value=1), \
                patch(f"{_GATES}._detect_back_side", return_value=(True, "ema9_below_ema20")):
            _pb_settings(ms)
            # backside veto path is the EMA/MACD _detect_back_side gate (always-on for non-deep).
            ok, reason, dbg = pullback_break_confirmation(
                df, entry_interval="1m", symbol="TEST", db=MagicMock(),
                require_retest=False, require_sustained_volume=False,
                require_break_candle=False, require_vwap_hold=False,
                require_macd_bullish=False, volume_spike_multiple=1.5,
            )
        assert ok is False
        assert reason == "back_side_disabled"


# ════════════════════════════════════════════════════════════════════════════════════════
#  4) bull_flag_confirmation  (fire reason "bull_flag_break")
#     DEEPER (50-70%) pull than first_pullback. Full chase-guard chain (backside, front_side,
#     L2, extension, tape). Mirrors test_momentum_setup_guard_parity's _bull_flag_df / guards.
# ════════════════════════════════════════════════════════════════════════════════════════

_BF_BREAK = 10.30


def _bull_flag_df() -> pd.DataFrame:
    bars = [
        (9.00, 9.10, 8.95, 9.05),
        (9.05, 9.25, 9.00, 9.20),
        (9.20, 9.55, 9.15, 9.50),
        (9.50, 9.85, 9.45, 9.80),
        (9.80, 10.20, 9.75, 10.15),  # impulse peak 10.20
        (10.15, 10.18, 9.95, 10.00),  # pullback red
        (10.00, 10.02, 9.78, 9.82),   # pullback red (deeper)
        (9.82, 9.90, 9.70, 9.75),     # pullback red -> pb_low ~9.70
        (9.75, _BF_BREAK, 9.72, 10.25),  # cur = BREAK
    ]
    lead = [(8.80, 8.90, 8.75, 8.85)] * 4
    return _ohlcv(lead + bars)


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
    def __init__(self):
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=_arrays(13))
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


class TestBullFlagConfirmation:
    def test_ideal_bull_flag_fires(self):
        """IDEAL deeper bull-flag pull + break + all guards pass -> FIRES 'bull_flag_break'."""
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards():
            _bf_settings(ms)
            ok, reason, dbg = bull_flag_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"ideal bull flag must fire, got {reason} dbg={dbg}"
        assert reason == "bull_flag_break"
        assert dbg["pullback_low"] < dbg["pullback_high"]

    def test_no_tape_no_fire(self):
        """ONE guard tripped: tape (the LAST gate) fails -> NO FIRE."""
        df = _bull_flag_df()
        with patch(f"{_GATES}.settings") as ms, _BfPassGuards() as g:
            _bf_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_no_data"})
            ok, reason, dbg = bull_flag_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "bull_flag_tape_unconfirmed"


# ════════════════════════════════════════════════════════════════════════════════════════
#  5) ma_vwap_pullback_confirmation  (fire reason "ma_vwap_pullback")
#     The cooler-grinder EMA-cascade dip-buy: impulse (3 green) -> consolidation grinding the
#     9-EMA -> reclaim. Guards: extension, backside, front_side (below-VWAP), L2. NO inline tape.
# ════════════════════════════════════════════════════════════════════════════════════════

def _ma_vwap_df() -> pd.DataFrame:
    bars = [
        (8.70, 8.75, 8.65, 8.72),
        (8.72, 8.80, 8.68, 8.78),
        (8.78, 8.90, 8.75, 8.88),
        (8.88, 9.00, 8.85, 8.98),
        (8.98, 9.10, 8.95, 9.08),
        (9.08, 9.20, 9.05, 9.18),
        (9.18, 9.40, 9.15, 9.38),   # 6 impulse green
        (9.38, 9.62, 9.35, 9.60),   # 7 impulse green
        (9.60, 9.82, 9.58, 9.80),   # 8 impulse green -> peak ~9.82
        (9.80, 9.81, 9.48, 9.52),   # 9 consolidation (low touches the 9.50 EMA band)
        (9.55, 9.85, 9.50, 9.70),   # 10 cur = reclaim (close 9.70 >= 9.50 EMA)
    ]
    return _ohlcv(bars)


def _mv_settings(ms) -> None:
    ms.chili_momentum_ma_vwap_pullback_enabled = True
    ms.chili_momentum_pullback_retrace_pct = 0.5
    ms.chili_momentum_ma_vwap_impulse_bars = 3
    ms.chili_momentum_ma_vwap_consolidation_bars = 2
    ms.chili_momentum_ma_vwap_vol_mult = 1.5
    ms.chili_momentum_vwap_reclaim_vol_mult = 1.5


def _mv_arrays() -> dict:
    n = 11
    return {
        "ema_9": [9.50] * n,
        "ema_20": [9.30] * n,
        "macd": [0.05] * n,
        "macd_signal": [0.03] * n,
        "vwap": [9.20] * n,
        "volume_ratio": [1.0] * (n - 1) + [3.0],
        "atr": [0.20] * n,
    }


class _MvPassGuards:
    def __init__(self):
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=_mv_arrays())
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


class TestMaVwapPullbackConfirmation:
    def test_ideal_ma_vwap_pullback_fires(self):
        """IDEAL impulse + EMA-cascade consolidation + 9-EMA reclaim + volume + all guards pass
        -> FIRES 'ma_vwap_pullback' with pullback_high (reclaim level) + pullback_low (stop)."""
        df = _ma_vwap_df()
        with patch(f"{_GATES}.settings") as ms, _MvPassGuards():
            _mv_settings(ms)
            ok, reason, dbg = ma_vwap_pullback_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"ideal ma/vwap pullback must fire, got {reason} dbg={dbg}"
        assert reason == "ma_vwap_pullback"
        assert dbg["pullback_low"] < dbg["pullback_high"]

    def test_below_vwap_lifecycle_no_fire(self):
        """ONE guard tripped: front_side_state reports below-VWAP (a backside reclaim Ross
        skips) -> NO FIRE."""
        df = _ma_vwap_df()
        with patch(f"{_GATES}.settings") as ms, _MvPassGuards() as g:
            _mv_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].return_value = SimpleNamespace(
                is_backside=True, above_vwap=False, reason="below_vwap",
            )
            ok, reason, dbg = ma_vwap_pullback_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "ma_vwap_pullback_backside_lifecycle"


# ════════════════════════════════════════════════════════════════════════════════════════
#  6) ross_abcd_confirmation  (fire reason "abcd_break")
#     A=impulse high, B=pullback low, BC-high, C=higher-low; D = break above BC-high. The
#     swing-pivot scanner is mocked at the boundary so the ABCD skeleton is exact; L2 veto +
#     a volume confirm gate the completed-bar break.
# ════════════════════════════════════════════════════════════════════════════════════════

def _abcd_df() -> pd.DataFrame:
    """The geometry that matters to the gate (after _swing_pivots is mocked) is: enough bars
    (>= 2*half_w+3) and the current bar's HIGH breaking above the BC swing high (level 10.10)."""
    bars = [
        (9.00, 9.20, 8.95, 9.15),
        (9.15, 9.60, 9.10, 9.55),
        (9.55, 10.00, 9.50, 9.95),   # A region high
        (9.95, 9.98, 9.55, 9.60),    # B low region
        (9.60, 10.05, 9.58, 10.00),  # BC high region (10.10 via pivot mock)
        (10.00, 10.02, 9.70, 9.75),  # C low region
        (9.75, 9.95, 9.72, 9.90),
        (9.90, 10.40, 9.88, 10.35),  # cur = BREAK above BC high
    ]
    return _ohlcv(bars)


def _abcd_pivots():
    """A=high 10.00, B=low 9.55, BC=high 10.10, C=low 9.70 (higher low; holds above B)."""
    return [
        {"idx": 1, "price": 9.30, "kind": "L"},
        {"idx": 2, "price": 10.00, "kind": "H"},   # A
        {"idx": 3, "price": 9.55, "kind": "L"},     # B
        {"idx": 4, "price": 10.10, "kind": "H"},    # BC swing high (break level)
        {"idx": 5, "price": 9.70, "kind": "L"},     # C (higher low than B)
    ]


class TestRossAbcdConfirmation:
    def test_ideal_abcd_fires(self):
        """IDEAL ABCD coil (C holds above B, shallow retraces) + D break + volume + no L2 veto
        -> FIRES 'abcd_break' with pullback_high (BC break level) + pullback_low (C low stop)."""
        df = _abcd_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
                patch(f"{_GATES}._swing_pivots", return_value=_abcd_pivots()), \
                patch(f"{_GATES}._collapse_cap", return_value=0.90), \
                patch(f"{_GATES}.compute_all_from_df", return_value={"volume_ratio": [1.0] * 7 + [3.0]}), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None):
            ms.chili_momentum_abcd_entry_enabled = True
            ms.chili_momentum_swing_pivot_half_window = 1
            ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
            ms.chili_momentum_pullback_volume_spike_multiple = 1.5
            ok, reason, dbg = ross_abcd_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"ideal ABCD must fire, got {reason} dbg={dbg}"
        assert reason == "abcd_break"
        assert dbg["pullback_high"] == pytest.approx(10.10, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(9.70, abs=1e-6)

    def test_c_broke_b_low_no_fire(self):
        """Pattern ABSENT: C made a new LOW below B (the coil broke down, not a hold) -> NO FIRE."""
        df = _abcd_df()
        broken = [
            {"idx": 1, "price": 9.30, "kind": "L"},
            {"idx": 2, "price": 10.00, "kind": "H"},   # A
            {"idx": 3, "price": 9.55, "kind": "L"},     # B
            {"idx": 4, "price": 10.10, "kind": "H"},    # BC
            {"idx": 5, "price": 9.40, "kind": "L"},     # C BELOW B -> not a hold
        ]
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
                patch(f"{_GATES}._swing_pivots", return_value=broken), \
                patch(f"{_GATES}._collapse_cap", return_value=0.90), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None):
            ms.chili_momentum_abcd_entry_enabled = True
            ms.chili_momentum_swing_pivot_half_window = 1
            ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
            ms.chili_momentum_pullback_volume_spike_multiple = 1.5
            ok, reason, dbg = ross_abcd_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "abcd_c_broke_b_low"


# ════════════════════════════════════════════════════════════════════════════════════════
#  7) pulling_away_roc_entry  (fire reason "pulling_away_roc")
#     Multi-tap resistance then a ROC acceleration break. Pivots mocked (>=2 high taps in band
#     + a swing low for the stop). Guards: backside, front_side, extension, L2, tape (LAST).
# ════════════════════════════════════════════════════════════════════════════════════════

def _pulling_away_df() -> pd.DataFrame:
    """Flat taps near a 10.00 ceiling then a fast acceleration bar that breaks it. The current
    bar's close must spike ROC above the recent flat baseline (so the last bar jumps)."""
    bars = [
        (9.50, 9.55, 9.45, 9.50),
        (9.50, 10.00, 9.48, 9.52),   # tap 1
        (9.52, 9.60, 9.48, 9.55),
        (9.55, 10.00, 9.50, 9.56),   # tap 2
        (9.56, 9.62, 9.52, 9.58),
        (9.58, 9.66, 9.54, 9.60),
        (9.60, 9.68, 9.56, 9.62),    # flat baseline (small ROC)
        (9.62, 10.40, 9.60, 10.35),  # cur = ROC acceleration break above 10.00
    ]
    return _ohlcv(bars)


def _pulling_away_pivots():
    """Two high taps at the 10.00 resistance band + a swing low at 9.45 (the stop)."""
    return [
        {"idx": 1, "price": 10.00, "kind": "H"},
        {"idx": 2, "price": 9.45, "kind": "L"},
        {"idx": 3, "price": 10.00, "kind": "H"},
    ]


class TestPullingAwayRocEntry:
    def test_ideal_pulling_away_fires(self):
        """IDEAL multi-tap ceiling + ROC acceleration break + all guards pass + tape confirms
        -> FIRES 'pulling_away_roc' with pullback_high (resistance) + pullback_low (stop)."""
        df = _pulling_away_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(8)), \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
                patch(f"{_GATES}._swing_pivots", return_value=_pulling_away_pivots()), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")), \
                patch(f"{_ROSS}.front_side_state",
                      return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok")), \
                patch(f"{_GATES}._hod_extension_ok", return_value=(True, {})), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None), \
                patch(f"{_GATES}.tape_confirms_hold", return_value=(True, {"reason": "tape_hold_ok"})):
            ms.chili_momentum_pulling_away_roc_entry_enabled = True
            ms.chili_momentum_swing_pivot_half_window = 1
            ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
            ms.chili_momentum_pulling_away_min_taps = 2
            ok, reason, dbg = pulling_away_roc_entry(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is True, f"ideal pulling-away must fire, got {reason} dbg={dbg}"
        assert reason == "pulling_away_roc"
        assert dbg["pullback_high"] == pytest.approx(10.00, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(9.45, abs=1e-6)

    def test_no_tape_no_fire(self):
        """ONE guard tripped: tape (the LAST gate) fails -> NO FIRE."""
        df = _pulling_away_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(8)), \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
                patch(f"{_GATES}._swing_pivots", return_value=_pulling_away_pivots()), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")), \
                patch(f"{_ROSS}.front_side_state",
                      return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok")), \
                patch(f"{_GATES}._hod_extension_ok", return_value=(True, {})), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None), \
                patch(f"{_GATES}.tape_confirms_hold", return_value=(False, {"reason": "tape_hold_no_data"})):
            ms.chili_momentum_pulling_away_roc_entry_enabled = True
            ms.chili_momentum_swing_pivot_half_window = 1
            ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
            ms.chili_momentum_pulling_away_min_taps = 2
            ok, reason, dbg = pulling_away_roc_entry(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        assert ok is False
        assert reason == "pulling_away_tape_unconfirmed"


# ════════════════════════════════════════════════════════════════════════════════════════
#  8) momentum_continuation_trigger  (fire reason "momentum_continuation")
#     The straight-up runner that never pulls back: a fresh new high over the recent swing high
#     on a non-parabolic front-side name. Guards: backside, front_side, extension, L2. No tape.
# ════════════════════════════════════════════════════════════════════════════════════════

def _continuation_df() -> pd.DataFrame:
    """A steady up-trend (>=12 bars) where the current bar's CLOSE makes a fresh new high above
    the recent COMPLETED-bar swing high (the prior K bars)."""
    bars = [
        (9.00, 9.10, 8.95, 9.05),
        (9.05, 9.20, 9.00, 9.15),
        (9.15, 9.30, 9.10, 9.25),
        (9.25, 9.40, 9.20, 9.35),
        (9.35, 9.50, 9.30, 9.45),
        (9.45, 9.60, 9.40, 9.55),
        (9.55, 9.70, 9.50, 9.65),
        (9.65, 9.80, 9.60, 9.75),
        (9.75, 9.90, 9.70, 9.85),
        (9.85, 10.00, 9.80, 9.95),   # recent high ~10.00 in the swing window
        (9.95, 10.05, 9.90, 10.00),  # 10
        (10.00, 10.40, 9.98, 10.35),  # 11 cur = fresh NEW HIGH (close 10.35 > recent high)
    ]
    return _ohlcv(bars)


class TestMomentumContinuationTrigger:
    def test_ideal_continuation_fires(self):
        """IDEAL fresh new-high continuation on a front-side, non-parabolic runner + no L2 veto
        -> FIRES 'momentum_continuation' with pullback_high (broken high) + pullback_low (stop)."""
        df = _continuation_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(12)), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")), \
                patch(f"{_ROSS}.front_side_state",
                      return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok")), \
                patch(f"{_GATES}._hod_extension_ok", return_value=(True, {})), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None):
            ms.chili_momentum_momentum_continuation_entry_enabled = True
            ok, reason, dbg = momentum_continuation_trigger(
                df, live_price=None, entry_interval="5m", swing_lookback=6, symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"ideal continuation must fire, got {reason} dbg={dbg}"
        assert reason == "momentum_continuation"
        assert dbg["pullback_low"] < dbg["pullback_high"]

    def test_extended_parabolic_no_fire(self):
        """ONE guard tripped: the extension guard reports a parabolic blow-off -> NO FIRE."""
        df = _continuation_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_arrays(12)), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")), \
                patch(f"{_ROSS}.front_side_state",
                      return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok")), \
                patch(f"{_GATES}._hod_extension_ok", return_value=(False, {"hod_extended_vs": "ema9"})), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None):
            ms.chili_momentum_momentum_continuation_entry_enabled = True
            ok, reason, dbg = momentum_continuation_trigger(
                df, live_price=None, entry_interval="5m", swing_lookback=6, symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "momentum_continuation_extended"
