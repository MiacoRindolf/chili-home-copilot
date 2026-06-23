"""front_side_state — session-anchored front-side vs backside lifecycle read (the
2026-06-22 QXL/NXTS fix). The crux: a fresh breakout NEAR VWAP (low vwap_dist_sigma) is
KEPT even if it's near the HOD, while an extended top (near-HOD AND far-above-VWAP) is
vetoed as chasing_top — so it does NOT repeat the L3 entry-filter winner-kill."""
from __future__ import annotations

import pandas as pd

from app.services.trading.momentum_neural.ross_momentum import front_side_state


def _df(closes, vol=1000):
    return pd.DataFrame({
        "Open": closes,
        "High": [c * 1.001 for c in closes],
        "Low": [c * 0.999 for c in closes],
        "Close": closes,
        "Volume": [vol] * len(closes),
    })


def test_backside_chasing_top_vetoed():
    # QXL-shape: long base then a parabolic spike to the HOD => top of day-range AND far
    # above the (base-dragged) VWAP => chasing_top.
    fs = front_side_state(_df([10.0] * 12 + [13.0, 16.0, 20.0]))
    assert fs.is_backside is True
    assert fs.reason == "chasing_top"
    assert fs.day_range_pos >= 0.85
    assert fs.vwap_dist_sigma is not None and fs.vwap_dist_sigma >= 2.0


def test_frontside_pullback_kept():
    # NXTS-shape: thrust to a high, then a SHALLOW pullback toward VWAP. Near the HOD in
    # day-range terms, but NEAR VWAP (low sigma) => NOT chasing_top => KEPT (the L3 guard).
    fs = front_side_state(_df([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 14.6, 14.3, 14.4]))
    assert fs.is_backside is False
    assert fs.reason == "front_side"
    assert fs.above_vwap is True


def test_below_vwap_vetoed():
    # SAGT-shape: popped early then faded BELOW VWAP.
    fs = front_side_state(_df([10.0, 13.0, 15.0, 14.0, 12.0, 11.0, 10.5, 10.2]))
    assert fs.is_backside is True
    assert fs.reason in ("below_vwap", "already_faded")


def test_thin_data_fail_open():
    fs = front_side_state(_df([10.0, 10.1]))  # < 5 bars
    assert fs.is_backside is False
    assert fs.reason == "insufficient_bars"


def test_score_frontside_beats_backside():
    front = front_side_state(_df([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 14.6, 14.3, 14.4]))
    back = front_side_state(_df([10.0] * 12 + [13.0, 16.0, 20.0]))
    assert front.front_side_score > back.front_side_score
