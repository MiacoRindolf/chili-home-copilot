"""HUIZ 2026-08-20 second-leg lockout — ang double-bottom break ay EVENT, hindi STATE.

Ang degenerate fire: 40-minutong lows (1.49/1.52, neckline 1.87), presyo +55% sa
itaas ⇒ ang hubad na `price > level` ay LAGING totoo ⇒ 63 candidate na lahat
pinatay ng chase-veto (level 1.87 vs entry ~2.9-3.4), habang tinatabunan ang
sariwang-shelf triggers ng 12:20 second leg. Ang break ay dapat PAGTAWID ngayon:
ang bar na ito (o ang nakaraang close) ay dumampi sa level.
Runnable: pytest tests/test_double_bottom_break_freshness.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import (
    ross_double_bottom_confirmation,
)

_GATES = "app.services.trading.momentum_neural.entry_gates"
_NECK = 10.00


def _rows(bars):
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1_000_000}
         for o, h, l, c in bars]
    )


def _base_bars():
    """Ang parehong malinis na double-bottom frame ng test_momentum_mock_fire_reversal."""
    return [
        (9.30, 9.40, 9.20, 9.35),   # 0 lead-in
        (9.20, 9.25, 9.00, 9.05),   # 1 LOW1
        (9.10, 9.60, 9.05, 9.55),   # 2 rise
        (9.55, _NECK, 9.50, 9.95),  # 3 HIGH (neckline)
        (9.60, 9.65, 9.02, 9.55),   # 4 LOW2 (bottoming tail)
        (9.55, 9.80, 9.50, 9.75),   # 5 rise back
    ]


def _settings(ms) -> None:
    ms.chili_momentum_double_bottom_entry_enabled = True
    ms.chili_momentum_swing_pivot_half_window = 1
    ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
    ms.chili_momentum_double_bottom_band_atr_mult = 0.6
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5


def _run(df, live_price=None):
    n = len(df)
    with patch(f"{_GATES}.settings") as ms, \
            patch(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20)), \
            patch(f"{_GATES}._l2_entry_veto", return_value=None), \
            patch(f"{_GATES}.compute_all_from_df",
                  return_value={"volume_ratio": [1.0] * (n - 1) + [3.0]}):
        _settings(ms)
        return ross_double_bottom_confirmation(
            df, entry_interval="5m", symbol="TEST", db=MagicMock(),
            live_price=live_price,
        )


def test_fresh_tick_break_still_fires():
    """Ang bar na tumatawid sa neckline + live tick sa itaas -> tumutupok pa rin."""
    df = _rows(_base_bars() + [(9.80, 10.05, 9.78, 10.02)])  # cur low 9.78 <= 10.00
    ok, reason, dbg = _run(df, live_price=10.05)
    assert ok is True and reason == "double_bottom_break_tick_ok", (reason, dbg)
    assert dbg["break_is_fresh"] is True


def test_fresh_bar_break_still_fires():
    """Ang completed-bar + volume path -> hindi ginalaw (bar low dumampi sa level)."""
    df = _rows(_base_bars() + [(9.80, 10.35, 9.78, 10.30)])
    ok, reason, dbg = _run(df, live_price=None)
    assert ok is True and reason == "double_bottom_break", (reason, dbg)


def test_gap_over_stays_valid_via_prior_close():
    """Gap-over: buong bar sa itaas ng level PERO ang nakaraang close ay nasa ibaba
    -> sariwa pa rin (ang pagtawid ay kakaganap lang sa pagitan ng dalawang bar)."""
    bars = _base_bars() + [(10.20, 10.60, 10.15, 10.55)]  # prior close 9.75 <= 10.00
    ok, reason, dbg = _run(_rows(bars), live_price=10.62)
    assert ok is True and reason == "double_bottom_break_tick_ok", (reason, dbg)
    assert dbg["break_is_fresh"] is True


def test_ancient_break_tick_is_stale():
    """ANG HUIZ CASE: matagal nang lampas sa neckline ang presyo (walang bar na
    dumadampi pabalik) -> ang tick fire ay STALE, hindi na tumutupok."""
    bars = _base_bars() + [
        (9.80, 10.35, 9.78, 10.30),   # ang tunay na break (matagal na)
        (10.30, 12.00, 10.25, 11.90),  # tumakbo...
        (11.90, 14.00, 11.80, 13.80),
        (13.80, 15.50, 13.60, 15.20),  # cur: +52% sa itaas ng 10.00
    ]
    ok, reason, dbg = _run(_rows(bars), live_price=15.40)
    assert ok is False and reason == "double_bottom_break_stale", (reason, dbg)
    assert dbg["break_is_fresh"] is False


def test_ancient_break_bar_path_is_stale():
    """Ganoon din sa completed-bar/volume path — hindi rin dapat tumupok."""
    bars = _base_bars() + [
        (9.80, 10.35, 9.78, 10.30),
        (10.30, 12.00, 10.25, 11.90),
        (11.90, 14.00, 11.80, 13.80),
        (13.80, 15.50, 13.60, 15.20),
    ]
    ok, reason, dbg = _run(_rows(bars), live_price=None)
    assert ok is False and reason == "double_bottom_break_stale", (reason, dbg)
