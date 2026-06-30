"""GAP-B — chase-guard PARITY for the TIGHT-MOMENTUM false-break-reversal / VWAP-reclaim entry.

``false_break_reclaim_confirmation`` is a NEW LIVE entry trigger family (real money), so a
missing chase-guard = a CHASE = a loss. The hardening here MIRRORS the reference triggers
``wedge_break_entry`` / ``absorption_snap_entry`` — the four shared chase-guards EVERY breakout
trigger must carry:

  1. NOT BACKSIDE / NOT BELOW-VWAP -- ``_detect_back_side`` (1m EMA/MACD rollover, ARRAY
     signature) AND ``front_side_state`` (fails CLOSED on a thin/degenerate frame).
  2. NOT PARABOLIC -- ``_hod_extension_ok`` vs the 9-EMA AND VWAP (rejects a vertical run).
  3. L2 hidden-/big-seller veto -- ``_l2_entry_veto``.
  4. TAPE REQUIRED + FAIL-CLOSED -- ``tape_confirms_hold`` is the LAST gate before each
     return-True (in ADDITION to the core REQUIRED+fail-closed flow_ok gate).

Plus GAP-B's TIGHT-MOMENTUM CORE (each REQUIRED, fail-closed): compression (tight), flow_ok
(ofi_level > +T_flow_entry AND ofi_slope > 0), vol_ok (self-relative surge). And the geometry
(B.2 false-break-reversal OR B.3 VWAP-reclaim).

PURE-LOGIC tests on synthetic OHLCV frames: the indicator layer (``compute_all_from_df`` / ATR),
the live flow read (``_live_flow_slope``), and the four guards are mocked so each guard / core
leg can be regressed INDEPENDENTLY — a baseline FIRES, and flipping exactly ONE to fail ⇒ NO
FIRE. The B.2 false-break-reversal geometry is run FOR REAL on a hand-built pierce/flush/reclaim
frame; B.3 VWAP-reclaim is exercised on its own frame.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import (
    false_break_reclaim_confirmation,
)

_GATES = "app.services.trading.momentum_neural.entry_gates"
_ROSS = "app.services.trading.momentum_neural.ross_momentum"
_PIPE = "app.services.trading.momentum_neural.pipeline"


def _arrays(n: int) -> dict:
    """Clean indicator arrays: front-side EMA stack (9>20), bullish MACD, a low VWAP so
    extension/below-VWAP never trip by accident, and an ATR series."""
    return {
        "ema_9": [9.50] * n,
        "ema_20": [9.40] * n,
        "macd": [0.05] * n,
        "macd_signal": [0.03] * n,
        "vwap": [9.30] * n,
        "atr": [0.20] * n,
    }


# ════════════════════════════════════════════════════════════════════════════════════════
#  B.2 — FALSE-BREAKOUT REVERSAL frame (pierce L -> flush below L -> rip back & reclaim L)
# ════════════════════════════════════════════════════════════════════════════════════════
# the level L = the highest completed-bar HIGH in the window excluding the firing bar (the
# pierce bar's high) — that is the resistance that was pierced, lost, then reclaimed (the entry).
_FBR_L = 10.06


def _fbr_df() -> pd.DataFrame:
    """A name with a VOLATILE early history that has since COILED tight, then a pierce of
    L=10.00, a flush CLOSE back below it, then the current bar RIPS back above L. >=12 bars.

    Compression is measured as median(coil atr_pct) / median(Lc atr_pct), EXCLUDING the
    firing bar. The early bars (idx 0-5) are WIDE (big H-L) so the Lc baseline median is
    large; the recent coil (idx 6-10) is TIGHT so coil/baseline < theta_c -> TIGHT passes.
    The geometry window K (default 6) covers the pierce(idx9)/flush(idx10)/reclaim(idx11).

        idx:  0..5 WIDE history   6..8 tight coil   9 PIERCE   10 FLUSH   11(cur) RECLAIM
    """
    bars = [
        # (open, high, low, close)  -- WIDE early history (large atr_pct baseline)
        (9.00, 9.80, 8.60, 9.40),
        (9.40, 10.10, 9.10, 9.70),
        (9.70, 9.90, 8.90, 9.20),
        (9.20, 9.95, 8.95, 9.55),
        (9.55, 10.05, 9.15, 9.60),
        (9.60, 9.98, 9.05, 9.62),
        # tight COIL (small H-L) just under L=10.00
        (9.62, 9.66, 9.58, 9.63),    # 6
        (9.63, 9.67, 9.59, 9.64),    # 7
        (9.64, 9.68, 9.60, 9.65),    # 8
        # the false-break event (kept TIGHT-range so the coil stays compressed)
        (9.66, 10.06, 9.64, 10.02),  # 9  PIERCE: high 10.06 > L, closes above
        (9.98, 10.00, 9.70, 9.74),   # 10 FLUSH: closes 9.74 < L (failed breakout), low 9.70
        (9.74, 10.30, 9.72, 10.20),  # 11 cur = RECLAIM: high 10.30 > L, close 10.20 > L
    ]
    rows = [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
            for o, h, l, c in bars]
    df = pd.DataFrame(rows)
    df.loc[len(df) - 1, "Volume"] = 5_000_000  # vol surge (vol_ok)
    return df


def _fbr_settings(ms) -> None:
    ms.chili_momentum_entry_tight_false_break_reclaim_enabled = True
    ms.chili_momentum_tight_compression_lookback = 20
    ms.chili_momentum_tight_compression_pctile = 0.30
    ms.chili_momentum_tight_compression_coil_bars = 5
    ms.chili_momentum_tight_volume_lookback = 20
    ms.chili_momentum_tight_volume_pctile = 0.60
    ms.chili_momentum_tight_volume_mult_floor = 1.5
    ms.chili_momentum_tight_volume_mult_ceil = 3.0
    ms.chili_momentum_tight_flow_tail_q = 0.15
    ms.chili_momentum_ofi_threshold = 0.25
    ms.chili_momentum_tight_geometry_lookback = 6


class _FbrPassGuards:
    """Mock the indicator layer + the live flow read + the four guards to ALL PASS so a clean
    tight false-break-reversal fires. Each test then flips exactly ONE to fail."""

    def __init__(self, n: int = 12, arrays=None):
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
        _p(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20))
        # live flow read -> strong buy-side level + positive slope (flow_ok passes)
        _p(f"{_PIPE}._live_flow_slope", return_value={
            "ofi_level": 0.60, "ofi_slope": 0.05, "tick_rate": 3.0,
            "last_price": 10.20, "mid": 10.19, "grid_secs": 2.0, "n_ticks": 30, "n_grid": 8,
        })
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


class TestFalseBreakReversalBaseline:
    def test_clean_false_break_reversal_fires_completed_bar(self):
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards():
            _fbr_settings(ms)
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean tight false-break-reversal must fire, got {reason} dbg={dbg}"
        assert reason == "tight_false_break_reclaim"
        assert dbg["geometry"] == "false_break_reversal"
        assert dbg["pullback_high"] == pytest.approx(_FBR_L, abs=1e-6)   # reclaimed level = entry
        assert dbg["pullback_low"] == pytest.approx(9.70, abs=1e-6)      # flush low = stop

    def test_clean_false_break_reversal_tick_fires(self):
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards():
            _fbr_settings(ms)
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=_FBR_L + 0.40,
            )
        assert ok is True, f"tick-break must fire, got {reason} dbg={dbg}"
        assert reason == "tight_false_break_reclaim_tick"


class TestFalseBreakReversalCoreLegs:
    def test_not_tight_no_fire(self):
        """Compression at/above theta_c -> NO FIRE. Drive the theta_c quantile to 0.0 so
        theta_c = the MIN of the name's own compression distribution; comp_now can never be
        strictly below the min, so the TIGHT check fails — isolating the compression leg on
        the SAME clean false-break frame (only the percentile knob is flipped)."""
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards():
            _fbr_settings(ms)
            ms.chili_momentum_tight_compression_pctile = 0.0  # theta_c = min(dist) <= comp_now
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_not_tight"

    def test_no_flow_read_fails_closed(self):
        """A None live-flow read (stale/empty/thin tape) -> flow_ok=False -> NO FIRE."""
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            g.mocks[f"{_PIPE}._live_flow_slope"].return_value = None
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_no_flow"

    def test_weak_flow_level_no_fire(self):
        """ofi_level <= +T_flow_entry (below the floor) -> NO FIRE (REQUIRED flow)."""
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            g.mocks[f"{_PIPE}._live_flow_slope"].return_value = {
                "ofi_level": 0.10, "ofi_slope": 0.05, "tick_rate": 3.0,
            }
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_weak_flow"

    def test_rolling_flow_slope_no_fire(self):
        """ofi_slope <= 0 (flow not building / rolling over) -> NO FIRE (REQUIRED flow)."""
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            g.mocks[f"{_PIPE}._live_flow_slope"].return_value = {
                "ofi_level": 0.60, "ofi_slope": -0.02, "tick_rate": 3.0,
            }
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_weak_flow"

    def test_weak_volume_no_fire(self):
        """Current-bar volume not above vmult*median -> vol_ok=False -> NO FIRE."""
        df = _fbr_df()
        df.loc[len(df) - 1, "Volume"] = 900_000  # below the recent median surge floor
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards():
            _fbr_settings(ms)
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_weak_volume"


class TestFalseBreakReversalChaseGuards:
    def test_backside_ema_macd_no_fire(self):
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            g.mocks[f"{_GATES}._detect_back_side"].return_value = (True, "ema9_below_ema20")
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_back_side"

    def test_detect_back_side_called_with_array_signature(self):
        """_detect_back_side must be called with the ARRAY signature (ema9 LIST, …, cur),
        not a DataFrame — the silent-no-op bug fence."""
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            false_break_reclaim_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        dbs = g.mocks[f"{_GATES}._detect_back_side"]
        dbs.assert_called_once()
        args = dbs.call_args.args
        assert isinstance(args[0], list), "first arg must be the ema9 LIST, not a DataFrame"
        assert args[4] == len(df) - 1, "cur index (last bar) must be the 5th positional arg"
        assert "entry_interval" not in dbs.call_args.kwargs

    def test_below_vwap_lifecycle_no_fire(self):
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].return_value = SimpleNamespace(
                is_backside=True, above_vwap=False, reason="below_vwap",
            )
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_backside_lifecycle"

    def test_front_side_state_exception_fails_closed(self):
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            g.mocks[f"{_ROSS}.front_side_state"].side_effect = AttributeError("thin frame")
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_backside_lifecycle"

    def test_extended_parabolic_no_fire(self):
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            g.mocks[f"{_GATES}._hod_extension_ok"].return_value = (False, {"hod_extended_vs": "vwap"})
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_extended"

    def test_l2_short_circuits_before_tape(self):
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            g.mocks[f"{_GATES}._l2_entry_veto"].return_value = ("l2_big_seller", {})
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_l2_big_seller"
        g.mocks[f"{_GATES}.tape_confirms_hold"].assert_not_called()

    def test_no_tape_no_fire_completed_bar(self):
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_no_data"})
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_tape_unconfirmed"

    def test_no_tape_no_fire_tick_break(self):
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_disabled"})
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(), live_price=_FBR_L + 0.40,
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_tape_unconfirmed"


class TestSeriesRequirementAndFlag:
    def test_compute_all_requests_chase_guard_series(self):
        """The needed-set must include ema_9/ema_20/macd/macd_signal/vwap so the backside +
        extension guards see REAL series, not [] no-ops."""
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards() as g:
            _fbr_settings(ms)
            false_break_reclaim_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
        caf = g.mocks[f"{_GATES}.compute_all_from_df"]
        needed = caf.call_args.kwargs.get("needed")
        assert needed is not None
        assert {"ema_9", "ema_20", "macd", "macd_signal", "vwap"}.issubset(set(needed)), (
            f"missing chase-guard series -> chase hole; got {needed}"
        )

    def test_flag_off_byte_identical(self):
        df = _fbr_df()
        with patch(f"{_GATES}.settings") as ms:
            ms.chili_momentum_entry_tight_false_break_reclaim_enabled = False
            ok, reason, dbg = false_break_reclaim_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "tight_false_break_reclaim_disabled"


# ════════════════════════════════════════════════════════════════════════════════════════
#  B.3 — VWAP-RECLAIM geometry (prior bar below VWAP, current bar reclaims with up momentum)
# ════════════════════════════════════════════════════════════════════════════════════════
def _vwap_reclaim_df() -> pd.DataFrame:
    """A name with a VOLATILE early history that has since COILED tight just below VWAP, then
    the current bar reclaims VWAP from below. No clean false-break-reversal shape (so the gate
    takes the B.3 path). VWAP is mocked via the arrays (vwap=9.55); prior bar closes 9.50 <
    9.55 (below), current bar closes 9.62 > 9.55 AND > its open (up momentum). The early bars
    are WIDE so the recent coil reads TIGHT vs the Lc baseline.

        idx: 0..5 WIDE history   6..9 tight coil below-vwap   10 prior(below vwap)   11(cur) reclaim
    """
    wide = [
        (9.00, 9.70, 8.70, 9.30),
        (9.30, 9.90, 8.90, 9.50),
        (9.50, 9.80, 8.80, 9.10),
        (9.10, 9.85, 8.85, 9.45),
        (9.45, 9.95, 9.00, 9.50),
        (9.50, 9.88, 8.95, 9.48),
    ]
    coil = [(9.49, 9.53, 9.46, 9.50)] * 4   # tight, below vwap 9.55
    bars = wide + coil + [
        (9.49, 9.54, 9.47, 9.50),   # 10 prior: close 9.50 < vwap 9.55 (below before)
        (9.52, 9.65, 9.51, 9.62),   # 11 cur = reclaim: close 9.62 > vwap 9.55 AND > open 9.52
    ]
    rows = [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
            for o, h, l, c in bars]
    df = pd.DataFrame(rows)
    df.loc[len(df) - 1, "Volume"] = 5_000_000  # vol surge
    return df


def _vwap_reclaim_arrays(n: int) -> dict:
    a = _arrays(n)
    a["vwap"] = [9.55] * n   # the VWAP the current bar reclaims
    return a


class TestVwapReclaimGeometry:
    def test_clean_vwap_reclaim_fires(self):
        df = _vwap_reclaim_df()
        n = len(df)
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards(n=n, arrays=_vwap_reclaim_arrays(n)) as g:
            _fbr_settings(ms)
            # _hod_extension_ok uses vwap=9.55 as the ref; the reclaim level ~9.55 is not
            # extended, but keep the guard mocked to PASS so this isolates the geometry.
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean VWAP-reclaim must fire, got {reason} dbg={dbg}"
        assert reason == "tight_false_break_reclaim"
        assert dbg["geometry"] == "vwap_reclaim"
        assert dbg["pullback_high"] >= 9.55 - 1e-6   # reclaim level = max(vwap, prior high)

    def test_no_geometry_no_fire(self):
        """A TIGHT tape (wide history -> recent tight coil) with flow+volume but NEITHER a
        false-break-reversal NOR a VWAP reclaim (price sits well ABOVE VWAP the whole recent
        window, no pierce/flush, no below->above cross) -> no geometry -> NO FIRE."""
        wide = [
            (9.80, 10.40, 9.40, 10.00), (10.00, 10.60, 9.60, 10.10),
            (10.10, 10.50, 9.50, 9.90), (9.90, 10.55, 9.55, 10.05),
            (10.05, 10.65, 9.65, 10.10), (10.10, 10.58, 9.62, 10.08),
        ]
        # tight coil entirely above VWAP=9.55, monotone-ish (no pierce-then-fail, no reclaim)
        coil = [(9.80, 9.83, 9.79, 9.81)] * 6
        rows = [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
                for o, h, l, c in (wide + coil)]
        df = pd.DataFrame(rows)
        df.loc[len(df) - 1, "Volume"] = 5_000_000
        n = len(df)
        with patch(f"{_GATES}.settings") as ms, _FbrPassGuards(n=n, arrays=_vwap_reclaim_arrays(n)):
            _fbr_settings(ms)
            ok, reason, dbg = false_break_reclaim_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "tight_false_break_reclaim_no_geometry"
