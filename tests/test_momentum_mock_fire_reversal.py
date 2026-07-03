"""Mock-FIRE coverage for the REVERSAL / DIP / VWAP entry-gate family (Ross momentum lane).

Companion to ``tests/test_momentum_cup_and_handle.py`` (the reference firing-mock) and
``tests/test_momentum_setup_guard_parity.py`` (the same scaffold across several setups). This
file gives EACH reversal/dip/VWAP setup a matched PAIR:

  * a POSITIVE test — the IDEAL pattern frame is built and every guard the gate calls is
    mocked to PASS at the call boundary, so the gate FIRES; we assert ``ok is True``, the
    reason is the setup's own FIRE reason (never a ``*_unconfirmed`` / ``*_disabled`` / veto
    reason), and ``debug`` carries the ``pullback_high`` (entry level) + ``pullback_low``
    (structural stop) the runner's sizing/stop machinery consumes;
  * a NEGATIVE test — the pattern is absent OR exactly one guard is tripped -> NO FIRE.

The fire contract is the shared ``(ok, reason, debug)`` 3-tuple; on a fire ``debug`` carries
``pullback_high`` / ``pullback_low``. These are PURE-LOGIC tests on synthetic OHLCV frames:
the structural layer runs for real where the geometry is simple (or is mocked where it is
intricate — e.g. the swing-pivot scanner), and the indicator layer (``compute_all_from_df`` /
ATR) + the shared chase-guards (``tape_confirms_hold`` / ``_detect_back_side`` /
``front_side_state`` / ``_hod_extension_ok`` / ``_l2_entry_veto``) + the L2 ladder read are
mocked so the gate reaches its fire path deterministically.

SETUP GROUP: REVERSAL / DIP / VWAP family —
  flush_dip_buy_confirmation, vwap_reclaim_confirmation, sub_vwap_trap_entry,
  red_to_green_confirmation, bottom_reversal_confirmation, ross_double_bottom_confirmation,
  inverse_head_shoulders_confirmation, wick_reclaim_confirmation, absorption_snap_entry,
  halt_resume_dip_trigger.
(``cup_and_handle_confirmation`` is covered by ``tests/test_momentum_cup_and_handle.py`` and is
intentionally NOT duplicated here.)

TESTS-ONLY: this file never edits source. Each gate is exercised through its public signature.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import (
    absorption_snap_entry,
    bottom_reversal_confirmation,
    flush_dip_buy_confirmation,
    halt_resume_dip_trigger,
    inverse_head_shoulders_confirmation,
    red_to_green_confirmation,
    ross_double_bottom_confirmation,
    sub_vwap_trap_entry,
    vwap_reclaim_confirmation,
    wick_reclaim_confirmation,
)

_GATES = "app.services.trading.momentum_neural.entry_gates"
# front_side_state is imported INSIDE each gate via ``from .ross_momentum import
# front_side_state`` -> patch it at its SOURCE module (patching the entry_gates namespace
# would not intercept the local import). Same for candles / pipeline locals.
_ROSS = "app.services.trading.momentum_neural.ross_momentum"
_CANDLES = "app.services.trading.momentum_neural.candles"
_PIPELINE = "app.services.trading.momentum_neural.pipeline"


def _rows(bars):
    """bars: list of (open, high, low, close) -> a benign-volume OHLCV DataFrame."""
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
         for o, h, l, c in bars]
    )


# ════════════════════════════════════════════════════════════════════════════════════════
#  (1) flush_dip_buy_confirmation  — AS101 algo-flush V-bounce dip-buy
#  Inline structural gate. FIRE = "flush_dip_buy". Mocks: compute_all_from_df (ema_9 rising +
#  vwap + atr), candles.is_bounce_curl_candle (curl). _bottoming_tail runs for real on the
#  flush bar geometry. RTH gate defaults OFF -> fail-open.
# ════════════════════════════════════════════════════════════════════════════════════════

def _flush_dip_df() -> pd.DataFrame:
    """A front-side up name, a fast bottoming-tail flush bar INTO support, then a green curl
    bar reclaiming back above VWAP.  cur=11 (curl), flush=10.

    The flush bar (idx 10) has a long lower wick (low 9.20, close 9.85) = a bottoming tail; its
    low pierces the VWAP support (9.50). The curl bar (idx 11) closes 9.95 back above VWAP and
    holds the dip low.  Pre-flush bar (idx 9) closed 9.90 > VWAP (was front-side)."""
    bars = [
        (9.00, 9.10, 8.95, 9.05),   # 0  lead-in
        (9.05, 9.20, 9.00, 9.15),   # 1
        (9.15, 9.30, 9.10, 9.25),   # 2
        (9.25, 9.40, 9.20, 9.35),   # 3
        (9.35, 9.55, 9.30, 9.50),   # 4
        (9.50, 9.70, 9.45, 9.65),   # 5
        (9.65, 9.85, 9.60, 9.80),   # 6
        (9.80, 9.95, 9.75, 9.90),   # 7
        (9.90, 10.00, 9.85, 9.95),  # 8
        (9.95, 10.05, 9.88, 9.90),  # 9  pre-flush (close 9.90 > vwap 9.50)
        (9.88, 9.92, 9.20, 9.85),   # 10 FLUSH bar: long lower wick into VWAP (low 9.20)
        (9.85, 9.98, 9.80, 9.95),   # 11 cur = green CURL reclaiming above VWAP
    ]
    return _rows(bars)


def _flush_settings(ms) -> None:
    ms.chili_momentum_flush_dip_buy_enabled = True
    ms.chili_momentum_dip_buy_rth_only_enabled = False  # RTH gate OFF -> fail-open


def _flush_arrays(n: int = 12) -> dict:
    # 9-EMA strictly RISING (front-side), VWAP = 9.50 (the flush low 9.20 pierces it), atr.
    return {
        "ema_9": [9.30 + 0.01 * i for i in range(n)],
        "vwap": [9.50] * n,
        "atr": [0.20] * n,
    }


class TestFlushDipBuyMockFire:
    def test_positive_clean_flush_dip_fires(self):
        """Ideal AS101 flush: front-side rising 9-EMA, a fast bottoming-tail flush INTO VWAP,
        a green curl reclaim -> FIRES ``flush_dip_buy`` with the dip low as the stop."""
        df = _flush_dip_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_flush_arrays()), \
                patch(f"{_CANDLES}.is_bounce_curl_candle", return_value=True):
            _flush_settings(ms)
            ok, reason, dbg = flush_dip_buy_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is True, f"clean flush-dip must fire, got {reason} dbg={dbg}"
        assert reason == "flush_dip_buy"
        assert dbg["pullback_low"] == pytest.approx(9.20, abs=1e-6)   # the dip low = stop
        assert dbg["pullback_low"] < dbg["pullback_high"]

    def test_negative_weak_curl_no_fire(self):
        """The curl bar is NOT a bounce-curl (no per-bar conviction) -> NO FIRE. One guard
        tripped, pattern otherwise ideal."""
        df = _flush_dip_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_flush_arrays()), \
                patch(f"{_CANDLES}.is_bounce_curl_candle", return_value=False):
            _flush_settings(ms)
            ok, reason, dbg = flush_dip_buy_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "flush_dip_weak_curl"

    def test_negative_not_front_side_no_fire(self):
        """The 9-EMA is FALLING into the flush (not a front-side up name) -> NO FIRE."""
        df = _flush_dip_df()
        falling = {**_flush_arrays(), "ema_9": [9.50 - 0.01 * i for i in range(12)]}
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=falling), \
                patch(f"{_CANDLES}.is_bounce_curl_candle", return_value=True):
            _flush_settings(ms)
            ok, reason, dbg = flush_dip_buy_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "flush_dip_not_front_side"


# ════════════════════════════════════════════════════════════════════════════════════════
#  (2) vwap_reclaim_confirmation  — SCAL101 VWAP-reclaim
#  Inline gate. FIRE = "vwap_reclaim". Mocks: compute_all_from_df (vwap + volume_ratio).
#  K prior bars must CLOSE below VWAP; cur reclaims above on a volume spike.
# ════════════════════════════════════════════════════════════════════════════════════════

def _vwap_reclaim_df() -> pd.DataFrame:
    """Price closed below VWAP (9.50) for the prior K=2 bars, then the cur bar reclaims above
    it (close 9.70). cur=11."""
    bars = [(9.60, 9.65, 9.55, 9.60)] * 9  # benign lead-in (closes above vwap 9.50)
    bars += [
        (9.45, 9.48, 9.30, 9.40),   # 9  below VWAP (close 9.40 < 9.50)
        (9.40, 9.46, 9.28, 9.35),   # 10 below VWAP (close 9.35 < 9.50)
        (9.45, 9.85, 9.42, 9.70),   # 11 cur = RECLAIM (close 9.70 > 9.50)
    ]
    return _rows(bars)


def _vwap_settings(ms) -> None:
    ms.chili_momentum_vwap_reclaim_enabled = True
    ms.chili_momentum_vwap_reclaim_min_below_bars = 2
    ms.chili_momentum_vwap_reclaim_vol_mult = 1.5


def _vwap_arrays(n: int = 12) -> dict:
    return {"vwap": [9.50] * n, "volume_ratio": [1.0] * (n - 1) + [3.0]}


class TestVwapReclaimMockFire:
    def test_positive_clean_vwap_reclaim_fires(self):
        """K=2 closes below VWAP then a volume-spike reclaim above -> FIRES ``vwap_reclaim``;
        stop = reclaim-bar low, entry = reclaim-bar high."""
        df = _vwap_reclaim_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_vwap_arrays()):
            _vwap_settings(ms)
            ok, reason, dbg = vwap_reclaim_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is True, f"clean vwap-reclaim must fire, got {reason} dbg={dbg}"
        assert reason == "vwap_reclaim"
        assert dbg["pullback_high"] == pytest.approx(9.85, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(9.42, abs=1e-6)
        assert dbg["bars_below"] >= 2

    def test_negative_low_volume_no_fire(self):
        """The reclaim happens but WITHOUT the volume spike (a drift back over VWAP, not a
        conviction reclaim) -> NO FIRE."""
        df = _vwap_reclaim_df()
        weak = {**_vwap_arrays(), "volume_ratio": [1.0] * 12}  # no surge on the reclaim bar
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=weak):
            _vwap_settings(ms)
            ok, reason, dbg = vwap_reclaim_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "vwap_reclaim_low_volume"


# ════════════════════════════════════════════════════════════════════════════════════════
#  (3) sub_vwap_trap_entry  — LOCATE #4 bear-trap / short-cover long
#  Full chase-guard gate. FIRE = "sub_vwap_trap". Mocks: compute_all_from_df, _batch_c_atr_pct,
#  candles.is_strong_bull_break_candle, the 4 chase-guards. _bottoming_tail runs for real on
#  the trap bar. NOT an L2-ladder gate (no read_ladder_distribution).
# ════════════════════════════════════════════════════════════════════════════════════════

def _sub_vwap_trap_df() -> pd.DataFrame:
    """A trap bar (idx 10) that UNDERCUT VWAP (9.50) on its low (9.20) and printed a bottoming
    tail (close 9.55 back up), then the cur bar (idx 11) RECLAIMS above VWAP and holds the trap
    low.  cur=11, trap=10."""
    bars = [(9.70, 9.75, 9.62, 9.70)] * 9  # lead-in above vwap
    bars += [
        (9.65, 9.70, 9.55, 9.60),   # 9  drift down toward vwap
        (9.58, 9.62, 9.20, 9.55),   # 10 TRAP: low 9.20 undercuts vwap 9.50, bottoming tail
        (9.56, 9.90, 9.52, 9.80),   # 11 cur = RECLAIM above vwap, holds trap low
    ]
    return _rows(bars)


def _sub_vwap_settings(ms) -> None:
    ms.chili_momentum_sub_vwap_trap_entry_enabled = True
    ms.chili_momentum_entry_break_candle_min_close_pos = 0.50


def _sub_vwap_arrays(n: int = 12) -> dict:
    return {
        "ema_9": [9.50] * n, "ema_20": [9.40] * n,
        "macd": [0.05] * n, "macd_signal": [0.03] * n,
        "vwap": [9.50] * n, "atr": [0.20] * n,
    }


class _SubVwapPassGuards:
    def __enter__(self):
        self._patches, self.mocks = [], {}

        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=_sub_vwap_arrays())
        _p(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20))
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


class TestSubVwapTrapMockFire:
    def test_positive_clean_sub_vwap_trap_fires(self):
        """A sharp undercut-and-reclaim trap with all chase-guards passing -> FIRES
        ``sub_vwap_trap``; stop = trap low, entry = reclaim-bar high."""
        df = _sub_vwap_trap_df()
        with patch(f"{_GATES}.settings") as ms, _SubVwapPassGuards():
            _sub_vwap_settings(ms)
            ok, reason, dbg = sub_vwap_trap_entry(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean sub-vwap-trap must fire, got {reason} dbg={dbg}"
        assert reason == "sub_vwap_trap"
        assert dbg["pullback_low"] == pytest.approx(9.20, abs=1e-6)   # trap low = stop
        assert dbg["pullback_high"] == pytest.approx(9.90, abs=1e-6)  # reclaim-bar high

    def test_negative_no_tape_no_fire(self):
        """tape_confirms_hold says NO (the LAST gate, fail-CLOSED) -> NO FIRE."""
        df = _sub_vwap_trap_df()
        with patch(f"{_GATES}.settings") as ms, _SubVwapPassGuards() as g:
            _sub_vwap_settings(ms)
            g.mocks[f"{_GATES}.tape_confirms_hold"].return_value = (False, {"reason": "tape_hold_no_data"})
            ok, reason, dbg = sub_vwap_trap_entry(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "sub_vwap_trap_tape_unconfirmed"


# ════════════════════════════════════════════════════════════════════════════════════════
#  (4) red_to_green_confirmation  — Ross red-to-green PRIOR-CLOSE reclaim (R8 / WAVE-4 ITEM-3)
#  Inline gate on the session frame. FIRE = "red_to_green". Mocks: compute_all_from_df,
#  candles.is_bounce_curl_candle, _detect_back_side, _l2_entry_veto, _prior_day_close.
#  _bottoming_tail runs real. Single-session frame (default index) so _today_session_frame
#  returns it unchanged. R8: the ANCHOR is the PRIOR-DAY CLOSE (not the intraday session open).
# ════════════════════════════════════════════════════════════════════════════════════════

_R2G_PRIOR_CLOSE = 10.00  # the prior-day close = Ross's red->green anchor


def _red_to_green_df() -> pd.DataFrame:
    """GAPPER red-to-green: the prior-day close was 10.00; the name gapped DOWN and opened at
    9.55 (already red on the day), traded to a session low 9.40, the prior bar closed red
    (9.55 < 10.00 prior close), then the cur bar reclaims the PRIOR CLOSE (10.00) with a
    bottoming-tail curl (close 10.10, low 9.60 wick).  cur=4.

    Note the SESSION OPEN (9.55) was NEVER below where the name traded — a session-open cross
    alone is NOT a red-to-green (the name is still red vs the prior close). Only the reclaim
    of the prior CLOSE (10.00) flips it green on the day."""
    bars = [
        (9.55, 9.60, 9.50, 9.52),     # 0  session open = 9.55 (gapped down, red vs prior close 10.00)
        (9.52, 9.58, 9.45, 9.50),     # 1  red
        (9.50, 9.55, 9.40, 9.48),     # 2  red (session low 9.40)
        (9.48, 9.65, 9.45, 9.55),     # 3  red (prev_close 9.55 < prior close 10.00)
        (10.05, 10.30, 9.55, 10.10),  # 4  cur = reclaim the PRIOR CLOSE 10.00, bottoming-tail curl
    ]
    return _rows(bars)


def _r2g_settings(ms) -> None:
    ms.chili_momentum_red_to_green_entry_enabled = True
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


def _r2g_arrays(n: int = 5) -> dict:
    return {
        "ema_9": [9.50] * n, "ema_20": [9.40] * n,
        "macd": [0.05] * n, "macd_signal": [0.03] * n,
        "volume_ratio": [1.0] * (n - 1) + [3.0], "atr": [0.20] * n,
    }


class TestRedToGreenMockFire:
    def test_positive_gapper_reclaims_prior_close_fires(self):
        """R8: a red (gapped-down) session reclaiming the PRIOR-DAY CLOSE on a bottoming-tail
        curl + volume -> FIRES ``red_to_green``; entry = prior close, stop = session low."""
        df = _red_to_green_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_r2g_arrays()), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None), \
                patch(f"{_GATES}._prior_day_close", return_value=_R2G_PRIOR_CLOSE), \
                patch(f"{_CANDLES}.is_bounce_curl_candle", return_value=True):
            _r2g_settings(ms)
            ok, reason, dbg = red_to_green_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean red-to-green must fire, got {reason} dbg={dbg}"
        assert reason == "red_to_green"
        assert dbg["prior_close"] == pytest.approx(10.00, abs=1e-6)     # anchor = prior close
        assert dbg["pullback_high"] == pytest.approx(10.00, abs=1e-6)   # prior close = entry
        assert dbg["pullback_low"] == pytest.approx(9.40, abs=1e-6)     # session low = stop

    def test_session_open_cross_alone_does_not_fire(self):
        """R8 PARITY: crossing the intraday SESSION OPEN (9.55) while STILL below the prior
        close (10.00) is NOT a red-to-green — the name is still red on the day. With the cur
        bar closing at 9.70 (above the 9.55 open but below the 10.00 prior close), the gate
        ARMS a tick-watch at the prior close (waiting_for_reclaim), it does NOT fire."""
        bars = [
            (9.55, 9.60, 9.50, 9.52),
            (9.52, 9.58, 9.45, 9.50),
            (9.50, 9.55, 9.40, 9.48),
            (9.48, 9.65, 9.45, 9.55),     # prev_close 9.55 < prior close 10.00 (still red)
            (9.75, 9.80, 9.30, 9.70),     # cur: bottoming-tail curl, closes 9.70 (above 9.55 open, below 10.00 prior close)
        ]
        df = _rows(bars)
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_r2g_arrays()), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None), \
                patch(f"{_GATES}._prior_day_close", return_value=_R2G_PRIOR_CLOSE), \
                patch(f"{_CANDLES}.is_bounce_curl_candle", return_value=True):
            _r2g_settings(ms)
            ok, reason, dbg = red_to_green_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False, f"a session-open cross below the prior close must NOT fire: {reason}"
        assert reason == "waiting_for_reclaim"  # armed at the PRIOR CLOSE, not fired at the open

    def test_no_prior_close_fails_closed_skips(self):
        """R8: prior close unavailable -> FAIL-CLOSED skip (never fall back to the intraday
        open). The reclaim that WOULD fire on the open must not fire when the anchor is gone."""
        df = _red_to_green_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_r2g_arrays()), \
                patch(f"{_GATES}._detect_back_side", return_value=(False, "front_side")), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None), \
                patch(f"{_GATES}._prior_day_close", return_value=None), \
                patch(f"{_CANDLES}.is_bounce_curl_candle", return_value=True):
            _r2g_settings(ms)
            ok, reason, dbg = red_to_green_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "red_to_green_no_prior_close"

    def test_negative_backside_no_fire(self):
        """The anti-chase backside veto trips (rolled-over MACD/EMA = a bear-flag relief pop)
        -> NO FIRE."""
        df = _red_to_green_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_r2g_arrays()), \
                patch(f"{_GATES}._detect_back_side", return_value=(True, "ema9_below_ema20")), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None), \
                patch(f"{_GATES}._prior_day_close", return_value=_R2G_PRIOR_CLOSE), \
                patch(f"{_CANDLES}.is_bounce_curl_candle", return_value=True):
            _r2g_settings(ms)
            ok, reason, dbg = red_to_green_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "red_to_green_back_side"


# ════════════════════════════════════════════════════════════════════════════════════════
#  (5) bottom_reversal_confirmation  — Ross bottom reversal (N reds then a green)
#  FIRE = "bottom_reversal". Mocks: compute_all_from_df, _detect_back_side, _hod_extension_ok,
#  _l2_entry_veto, tape_confirms_hold. The red-run + green-bar geometry runs for real.
# ════════════════════════════════════════════════════════════════════════════════════════

def _bottom_reversal_df() -> pd.DataFrame:
    """Four consecutive RED candles then a GREEN confirmation bar (the cur bar).  cur=4.
    The green-bar HIGH (9.80) = the break level; the red-series low (9.55) = the stop."""
    bars = [
        (10.40, 10.45, 10.20, 10.25),   # 0 red
        (10.25, 10.30, 10.00, 10.05),   # 1 red
        (10.05, 10.10, 9.75, 9.80),     # 2 red
        (9.80, 9.85, 9.55, 9.60),       # 3 red (series low 9.55)
        (9.60, 9.80, 9.58, 9.75),       # 4 cur = GREEN (close 9.75 > open 9.60)
    ]
    return _rows(bars)


def _br_settings(ms) -> None:
    ms.chili_momentum_bottom_reversal_entry_enabled = True
    ms.chili_momentum_bottom_reversal_min_red = 2
    ms.chili_momentum_bottom_reversal_volume_spike_multiple = 1.5
    ms.chili_momentum_bottom_reversal_velocity_floor_atr_mult = 0.0  # velocity gate OFF


def _br_arrays(n: int = 5) -> dict:
    return {
        "ema_9": [9.50] * n, "ema_20": [9.40] * n,
        "macd": [0.05] * n, "macd_signal": [0.03] * n,
        "vwap": [9.30] * n, "volume_ratio": [1.0] * (n - 1) + [3.0], "atr": [0.20] * n,
    }


class _BottomReversalPassGuards:
    def __enter__(self):
        self._patches, self.mocks = [], {}

        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=_br_arrays())
        _p(f"{_GATES}._detect_back_side", return_value=(False, "front_side"))
        _p(f"{_GATES}._hod_extension_ok", return_value=(True, {}))
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        _p(f"{_GATES}.tape_confirms_hold", return_value=(True, {"reason": "tape_hold_ok"}))
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestBottomReversalMockFire:
    def test_positive_clean_bottom_reversal_fires(self):
        """Four reds then a volume-backed green with all chase-guards passing -> FIRES
        ``bottom_reversal``; entry = green-bar high, stop = red-series low."""
        df = _bottom_reversal_df()
        with patch(f"{_GATES}.settings") as ms, _BottomReversalPassGuards():
            _br_settings(ms)
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean bottom-reversal must fire, got {reason} dbg={dbg}"
        assert reason == "bottom_reversal"
        assert dbg["red_bars_count"] >= 2
        assert dbg["pullback_high"] == pytest.approx(9.80, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(9.55, abs=1e-6)

    def test_negative_not_enough_reds_no_fire(self):
        """Only ONE red bar before the green (a single down bar is not a reversal) -> NO
        FIRE (``min_red`` = 2)."""
        df = _rows([
            (9.70, 9.75, 9.62, 9.68),   # 0 green (close > open) -> breaks the red run
            (9.68, 9.72, 9.55, 9.60),   # 1 red
            (9.60, 9.66, 9.58, 9.63),   # 2 red
            (9.62, 9.66, 9.50, 9.55),   # 3 red (only the run immediately before cur counts)
            (9.55, 9.80, 9.52, 9.74),   # 4 cur = green
        ])
        # Make exactly ONE preceding red: idx 3 red, idx 2 green.
        df.loc[2, "Open"], df.loc[2, "Close"] = 9.58, 9.66  # green
        with patch(f"{_GATES}.settings") as ms, _BottomReversalPassGuards():
            _br_settings(ms)
            ok, reason, dbg = bottom_reversal_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "bottom_reversal_not_enough_reds"


# ════════════════════════════════════════════════════════════════════════════════════════
#  (6) ross_double_bottom_confirmation  — two equal swing lows + neckline break
#  Real swing-pivot scanner. FIRE = "double_bottom_break". Mocks: _batch_c_atr_pct,
#  _l2_entry_veto, compute_all_from_df (volume_ratio for the break surge). _bottoming_tail
#  runs real on the 2nd-low bar. half_window=1, atr_noise_frac=0.
# ════════════════════════════════════════════════════════════════════════════════════════

_DB_NECK = 10.00  # the intervening swing high (neckline) the break clears


def _double_bottom_df() -> pd.DataFrame:
    """Two swing LOWS at ~the same support (9.00) with an intervening swing HIGH (the
    neckline, 10.00), the 2nd low printing a bottoming tail, then the cur bar BREAKS the
    neckline.  half_window=1 so a bar is a swing low iff its low <= both neighbours.

        idx:  0    1     2     3      4     5      6(cur)
        role: -   LOW1  mid  HIGH   LOW2   -    BREAK
    """
    bars = [
        (9.30, 9.40, 9.20, 9.35),       # 0  lead-in (left neighbour for idx1)
        (9.20, 9.25, 9.00, 9.05),       # 1  LOW1 (swing low 9.00)
        (9.10, 9.60, 9.05, 9.55),       # 2  rise toward the neckline
        (9.55, _DB_NECK, 9.50, 9.95),   # 3  HIGH (neckline 10.00, swing high)
        (9.60, 9.65, 9.02, 9.55),       # 4  LOW2 (swing low 9.02 ~ LOW1; bottoming tail)
        (9.55, 9.80, 9.50, 9.75),       # 5  rise back (right neighbour for idx4)
        (9.80, 10.35, 9.78, 10.30),     # 6  cur = BREAK above the neckline
    ]
    return _rows(bars)


def _db_settings(ms) -> None:
    ms.chili_momentum_double_bottom_entry_enabled = True
    ms.chili_momentum_swing_pivot_half_window = 1
    ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
    ms.chili_momentum_double_bottom_band_atr_mult = 0.6
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


class TestRossDoubleBottomMockFire:
    def test_positive_clean_double_bottom_fires(self):
        """Two equal swing lows + a 2nd-low bottoming tail + a volume-backed neckline break
        -> FIRES ``double_bottom_break``; entry = neckline, stop = the double-bottom low."""
        df = _double_bottom_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
                patch(f"{_GATES}._l2_entry_veto", return_value=None), \
                patch(f"{_GATES}.compute_all_from_df", return_value={"volume_ratio": [1.0] * 6 + [3.0]}):
            _db_settings(ms)
            ok, reason, dbg = ross_double_bottom_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean double-bottom must fire, got {reason} dbg={dbg}"
        assert reason == "double_bottom_break"
        assert dbg["pullback_high"] == pytest.approx(_DB_NECK, abs=1e-6)  # neckline = entry
        assert dbg["pullback_low"] < dbg["pullback_high"]
        assert dbg["pullback_low"] == pytest.approx(9.00, abs=1e-6)       # double-bottom low

    def test_negative_l2_big_seller_no_fire(self):
        """The L2 hidden-/big-seller veto trips (a resting ask wall at the neckline) -> NO
        FIRE."""
        df = _double_bottom_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
                patch(f"{_GATES}._l2_entry_veto", return_value=("l2_big_seller", {"l2_pctile": 0.05})), \
                patch(f"{_GATES}.compute_all_from_df", return_value={"volume_ratio": [1.0] * 6 + [3.0]}):
            _db_settings(ms)
            ok, reason, dbg = ross_double_bottom_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "double_bottom_l2_big_seller"


# ════════════════════════════════════════════════════════════════════════════════════════
#  (7) inverse_head_shoulders_confirmation  — SS101 #017
#  Real swing-pivot scanner + full chase-guards. FIRE = "inverse_head_shoulders_break".
#  Mocks: _batch_c_atr_pct, _collapse_cap (lenient), compute_all_from_df, the 4 chase-guards.
#  Mirrors the IHS frame in test_momentum_setup_guard_parity.py. half_window=1.
# ════════════════════════════════════════════════════════════════════════════════════════

_IHS_NECK = 10.00


def _ihs_df() -> pd.DataFrame:
    """A GENUINE inverse-H&S: LS low, left-neck high, HEAD (lowest) low, right-neck high,
    RS low (higher than head), then the cur bar breaks above the neckline.

        idx:  0     1      2     3      4     5      6      7      8(cur)
        role: -   LS-low  -  neckH  HEAD  neckH  RS-low  -    BREAK
    """
    neck = _IHS_NECK
    bars = [
        (9.50, 9.55, 9.30, 9.45),   # 0  lead-in
        (9.45, 9.55, 9.00, 9.10),   # 1  LEFT SHOULDER low (9.00)
        (9.20, neck, 9.40, 9.95),   # 2  left-neck swing HIGH (10.00)
        (9.55, 9.60, 8.50, 8.70),   # 3  HEAD low (8.50 = lowest)
        (9.10, neck, 9.45, 9.90),   # 4  right-neck swing HIGH (10.00)
        (9.50, 9.70, 9.10, 9.60),   # 5  RIGHT SHOULDER low (9.10 > head, holds)
        (9.60, 9.90, 9.55, 9.85),   # 6  rise toward the neckline
        (9.85, 9.95, 9.70, 9.90),   # 7  just under the neckline
        (9.90, 10.40, 9.88, 10.35), # 8  cur = BREAK above the neckline
    ]
    return _rows(bars)


def _ihs_settings(ms) -> None:
    ms.chili_momentum_inverse_head_shoulders_entry_enabled = True
    ms.chili_momentum_swing_pivot_half_window = 1
    ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


def _ihs_arrays(n: int = 9) -> dict:
    return {
        "ema_9": [9.50] * n, "ema_20": [9.40] * n,
        "macd": [0.05] * n, "macd_signal": [0.03] * n,
        "vwap": [9.30] * n, "volume_ratio": [1.0] * (n - 1) + [3.0],
    }


class _IhsPassGuards:
    def __enter__(self):
        self._patches, self.mocks = [], {}

        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20))
        _p(f"{_GATES}._collapse_cap", return_value=0.50)  # lenient shoulder-depth cap
        _p(f"{_GATES}.compute_all_from_df", return_value=_ihs_arrays())
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


class TestInverseHeadShouldersMockFire:
    def test_positive_clean_ihs_fires(self):
        """A clean inverse-H&S (head below both shoulders, RS holds) breaking the neckline with
        all chase-guards passing -> FIRES ``inverse_head_shoulders_break``; entry = neckline,
        stop = head low."""
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards():
            _ihs_settings(ms)
            ok, reason, dbg = inverse_head_shoulders_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean inverse-H&S must fire, got {reason} dbg={dbg}"
        assert reason == "inverse_head_shoulders_break"
        assert dbg["pullback_high"] == pytest.approx(_IHS_NECK, abs=1e-6)  # neckline = entry
        assert dbg["pullback_low"] == pytest.approx(8.50, abs=1e-6)        # head low = stop

    def test_negative_extended_no_fire(self):
        """The NOT-PARABOLIC extension guard trips (the neckline break is excessively extended
        vs the 9-EMA/VWAP = a blow-off) -> NO FIRE."""
        df = _ihs_df()
        with patch(f"{_GATES}.settings") as ms, _IhsPassGuards() as g:
            _ihs_settings(ms)
            g.mocks[f"{_GATES}._hod_extension_ok"].return_value = (False, {"hod_extended_vs": "vwap"})
            ok, reason, dbg = inverse_head_shoulders_confirmation(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "inverse_head_shoulders_extended"


# ════════════════════════════════════════════════════════════════════════════════════════
#  (8) wick_reclaim_confirmation  — HVM101 #008 hot-tape wick reclaim
#  Inline gate, MANDATORY hot-tape gate (_is_hot_tape via ATR%/RVOL floors). FIRE =
#  "wick_reclaim". Mocks: compute_all_from_df (atr + volume_ratio). The rejection candle +
#  flush + retrace geometry runs for real; the hot-tape gate passes via a high RVOL.
# ════════════════════════════════════════════════════════════════════════════════════════

def _wick_reclaim_df() -> pd.DataFrame:
    """A big-upper-wick REJECTION candle (idx 9, high 11.00, close 10.10 -> dominant upper
    wick), an immediate flush on receding volume (idx 10 low 9.80), then the cur bar (idx 11)
    RETRACES back up into the wick (close 10.60 -> > 40% of the 11.00->9.80 wick span).
    cur=11, rejection=9."""
    bars = [(10.00, 10.10, 9.95, 10.05)] * 9  # benign lead-in
    bars += [
        (10.05, 11.00, 10.00, 10.10),  # 9  REJECTION: huge upper wick (high 11.00, close 10.10)
        (10.10, 10.15, 9.80, 9.90),    # 10 FLUSH on receding volume (low 9.80)
        (9.90, 10.70, 9.88, 10.60),    # 11 cur = RETRACE back into the wick (close 10.60)
    ]
    return _rows(bars)


def _wick_settings(ms) -> None:
    ms.chili_momentum_wick_reclaim_entry_enabled = True
    ms.chili_momentum_explosive_atr_pct_floor = 0.045
    ms.chili_momentum_explosive_rvol_floor = 3.0
    ms.chili_momentum_wick_reclaim_min_wick_frac = 0.5
    ms.chili_momentum_wick_reclaim_min_retrace_frac = 0.4
    ms.chili_momentum_vwap_reclaim_min_below_bars = 2
    ms.chili_momentum_wick_reclaim_slow_recovery_gate_enabled = False


def _wick_arrays(n: int = 12) -> dict:
    # RVOL: rejection bar (idx 9) HIGH so the flush (idx 10) recedes; cur (idx 11) RVOL high so
    # _is_hot_tape passes (>= 3.0 floor). atr small so range >= atr (outsized) holds.
    vr = [1.0] * n
    vr[9] = 5.0   # rejection bar — high volume
    vr[10] = 1.0  # flush — receding
    vr[11] = 4.0  # cur — hot tape (>= rvol floor 3.0)
    return {"atr": [0.20] * n, "volume_ratio": vr}


class TestWickReclaimMockFire:
    def test_positive_clean_wick_reclaim_fires(self):
        """Hot tape + a big-wick rejection + a dry flush + a >=40% retrace into the wick ->
        FIRES ``wick_reclaim``; entry = wick high, stop = flush/wick low."""
        df = _wick_reclaim_df()
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=_wick_arrays()):
            _wick_settings(ms)
            ok, reason, dbg = wick_reclaim_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is True, f"clean wick-reclaim must fire, got {reason} dbg={dbg}"
        assert reason == "wick_reclaim"
        assert dbg["pullback_high"] == pytest.approx(11.00, abs=1e-6)  # wick high = level
        assert dbg["pullback_low"] == pytest.approx(9.80, abs=1e-6)    # flush low = stop

    def test_negative_cold_tape_no_fire(self):
        """The MANDATORY hot-tape gate fails (cold tape: low RVOL + low ATR%) -> the trigger
        is INVALID here and NEVER fires -> ``wick_reclaim_cold_tape``."""
        df = _wick_reclaim_df()
        cold = {**_wick_arrays(), "volume_ratio": [1.0] * 12, "atr": [0.02] * 12}  # low rvol+atr%
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df", return_value=cold):
            _wick_settings(ms)
            ok, reason, dbg = wick_reclaim_confirmation(df, entry_interval="5m", symbol="TEST")
        assert ok is False
        assert reason == "wick_reclaim_cold_tape"


# ════════════════════════════════════════════════════════════════════════════════════════
#  (9) absorption_snap_entry  — GAP 3 absorption-then-snap L2/tape long
#  Full chase-guard gate + L2 ladder read. FIRE = "absorption_snap". Mocks: compute_all_from_df,
#  _batch_c_atr_pct, pipeline.read_ladder_distribution (OFI/ask_build), the 4 chase-guards.
#  Requires db + symbol. The absorption level/stop come from the real bar window.
# ════════════════════════════════════════════════════════════════════════════════════════

def _absorption_df() -> pd.DataFrame:
    """A name pinned just under a recent resistance (the absorption level = the recent
    completed-bar high, 10.00), holding a higher-low, then the cur bar SNAPS through the level
    (high 10.20).  >= 12 bars required."""
    bars = [(9.70, 9.80, 9.60, 9.75)] * 4   # lead-in
    bars += [
        (9.75, 9.90, 9.70, 9.85),   # 4
        (9.85, 9.95, 9.78, 9.90),   # 5
        (9.90, 10.00, 9.82, 9.95),  # 6  recent completed-bar high = 10.00 (absorption level)
        (9.95, 9.99, 9.85, 9.92),   # 7  pinned under the level
        (9.92, 9.98, 9.86, 9.94),   # 8  holding (higher-low)
        (9.94, 9.99, 9.88, 9.96),   # 9
        (9.96, 9.99, 9.90, 9.97),   # 10
        (9.97, 9.99, 9.92, 9.98),   # 11
        (9.98, 10.20, 9.95, 10.15), # 12 cur = SNAP through the level (high 10.20 > 10.00)
    ]
    return _rows(bars)


def _absorption_settings(ms) -> None:
    ms.chili_momentum_absorption_snap_entry_enabled = True
    ms.chili_momentum_swing_pivot_half_window = 2
    ms.chili_momentum_ofi_threshold = 0.25


def _absorption_arrays(n: int = 13) -> dict:
    return {
        "ema_9": [9.90] * n, "ema_20": [9.80] * n,
        "macd": [0.05] * n, "macd_signal": [0.03] * n,
        "vwap": [9.70] * n, "atr": [0.20] * n,
    }


class _AbsorptionPassGuards:
    def __init__(self, ofi=0.50, ask_build=0.30, n_snaps=10):
        self._lr = SimpleNamespace(ofi=ofi, ask_build=ask_build, n_snaps=n_snaps)

    def __enter__(self):
        self._patches, self.mocks = [], {}

        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}.compute_all_from_df", return_value=_absorption_arrays())
        _p(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20))
        _p(f"{_PIPELINE}.read_ladder_distribution", return_value=self._lr)
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


class TestAbsorptionSnapMockFire:
    def test_positive_clean_absorption_snap_fires(self):
        """Buy-side OFI absorbing a refilling ask wall, a higher-low hold, then a snap through
        the absorption level with all chase-guards passing -> FIRES ``absorption_snap``."""
        df = _absorption_df()
        with patch(f"{_GATES}.settings") as ms, _AbsorptionPassGuards():
            _absorption_settings(ms)
            ok, reason, dbg = absorption_snap_entry(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is True, f"clean absorption-snap must fire, got {reason} dbg={dbg}"
        assert reason == "absorption_snap"
        assert dbg["pullback_high"] == pytest.approx(10.00, abs=0.02)  # absorption level (~the break, penny-rounded)
        assert dbg["pullback_low"] < dbg["pullback_high"]

    def test_negative_weak_ofi_no_fire(self):
        """The L2 read shows OFI BELOW the threshold (no real demand pressing the offer = not
        absorption) -> NO FIRE."""
        df = _absorption_df()
        with patch(f"{_GATES}.settings") as ms, _AbsorptionPassGuards(ofi=0.05):
            _absorption_settings(ms)
            ok, reason, dbg = absorption_snap_entry(
                df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            )
        assert ok is False
        assert reason == "absorption_snap_weak_ofi"


# ════════════════════════════════════════════════════════════════════════════════════════
#  (10) halt_resume_dip_trigger  — Ross halt-resume dip buy
#  Distinct signature (halt_resumed_at_utc + a tz-aware DatetimeIndex). FIRE =
#  "halt_resume_dip_ok". Mocks: compute_all_from_df (atr + volume_ratio), _collapse_cap
#  (lenient deep-cap), candles.is_strong_bull_break_candle. The dip/reclaim geometry runs real.
# ════════════════════════════════════════════════════════════════════════════════════════

def _halt_resume_df(resumed) -> pd.DataFrame:
    """Post-resume: a pop to a reference high (10.00), a real dip off it (low 9.50), then the
    cur bar STABILIZES (holds the dip low) and reclaims above the prior bar's high (9.70). The
    cur high (9.95) stays BELOW the reference high so the dip-off-the-high geometry holds. The
    index is tz-aware UTC starting AT the resume timestamp (all bars are post-resume)."""
    bars = [
        (9.00, 9.20, 8.95, 9.15),     # 0  resume open
        (9.15, 9.60, 9.10, 9.55),     # 1  pop
        (9.55, 10.00, 9.50, 9.95),    # 2  reference HIGH (10.00)
        (9.95, 9.98, 9.50, 9.55),     # 3  dip (low 9.50)
        (9.55, 9.70, 9.52, 9.60),     # 4  dip bar (holds)
        (9.60, 9.95, 9.58, 9.90),     # 5  cur = STABILIZE + reclaim above prior high (9.70)
    ]
    df = _rows(bars)
    idx = pd.date_range(start=pd.Timestamp(resumed), periods=len(df), freq="1min", tz="UTC")
    df.index = idx
    return df


def _halt_settings(ms) -> None:
    ms.chili_momentum_halt_resume_dip_window_seconds = 600.0
    ms.chili_momentum_halt_resumption_direction_enabled = False
    ms.chili_momentum_false_halt_avoid_enabled = False
    ms.chili_momentum_entry_break_candle_min_close_pos = 0.50


class TestHaltResumeDipMockFire:
    def test_positive_clean_halt_resume_dip_fires(self):
        """Within the resume window, a real dip off the post-resume high that stabilizes +
        reclaims on a strong candle with sustained volume -> FIRES ``halt_resume_dip_ok``;
        entry = post-resume reference high, stop = dip low."""
        resumed = pd.Timestamp("2026-06-27 14:30:00", tz="UTC")
        now = resumed + pd.Timedelta(seconds=300)  # inside the 600s window
        df = _halt_resume_df(resumed)
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df",
                      return_value={"atr": [0.20] * 6, "volume_ratio": [2.0] * 6}), \
                patch(f"{_GATES}._collapse_cap", return_value=0.50), \
                patch(f"{_CANDLES}.is_strong_bull_break_candle", return_value=True):
            _halt_settings(ms)
            ok, reason, dbg = halt_resume_dip_trigger(
                df, entry_interval="1m", halt_resumed_at_utc=resumed, now=now,
            )
        assert ok is True, f"clean halt-resume dip must fire, got {reason} dbg={dbg}"
        assert reason == "halt_resume_dip_ok"
        assert dbg["pullback_high"] == pytest.approx(10.00, abs=1e-6)  # post-resume ref high
        assert dbg["pullback_low"] == pytest.approx(9.50, abs=1e-6)    # dip low = stop

    def test_negative_window_passed_no_fire(self):
        """The resume happened too long ago (past the dip window) -> the normal trigger ladder
        owns the tape -> NO FIRE (``resume_dip_window_passed``)."""
        resumed = pd.Timestamp("2026-06-27 14:30:00", tz="UTC")
        now = resumed + pd.Timedelta(seconds=1200)  # past the 600s window
        df = _halt_resume_df(resumed)
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}.compute_all_from_df",
                      return_value={"atr": [0.20] * 6, "volume_ratio": [2.0] * 6}), \
                patch(f"{_GATES}._collapse_cap", return_value=0.50), \
                patch(f"{_CANDLES}.is_strong_bull_break_candle", return_value=True):
            _halt_settings(ms)
            ok, reason, dbg = halt_resume_dip_trigger(
                df, entry_interval="1m", halt_resumed_at_utc=resumed, now=now,
            )
        assert ok is False
        assert reason == "resume_dip_window_passed"
