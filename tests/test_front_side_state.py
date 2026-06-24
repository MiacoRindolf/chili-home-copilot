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
    # QXL-shape EXTENDED-AND-ROLLING: long base, a parabolic spike to the HOD, then it comes
    # OFF the high making confirmed LOWER highs => top of day-range AND far above the
    # (base-dragged) VWAP AND rolled over => chasing_top. The trailing lower highs are the
    # OFF-THE-HIGH structure leg (the recalibration): pure extension alone no longer fires.
    fs = front_side_state(_df([10.0] * 12 + [13.0, 16.0, 20.0, 19.2, 19.5]))
    assert fs.is_backside is True
    assert fs.reason == "chasing_top"
    assert fs.day_range_pos >= 0.85
    assert fs.vwap_dist_sigma is not None and fs.vwap_dist_sigma >= 2.0
    assert fs.debug.get("rolled_over") is True


def test_clean_new_high_thrust_not_chasing_top():
    # The recalibration's core guarantee: a CLEAN front-side parabolic that breaks to a NEW
    # HIGH on the most recent bar (HOD == last bar -> no lower high after the HOD) is NOT
    # chasing_top even though it is top-of-range AND far above the base-dragged VWAP. (Before
    # the recalibration this exact shape mis-fired as chasing_top because vwap_dist_sigma
    # blows up on a low-noise climb -> it over-vetoed clean new-high breakouts.)
    fs = front_side_state(_df([10.0] * 12 + [13.0, 16.0, 20.0]))
    assert fs.day_range_pos >= 0.85                       # top of range
    assert fs.vwap_dist_sigma is not None and fs.vwap_dist_sigma >= 2.0   # far above VWAP
    assert fs.reason != "chasing_top"                    # but a FRESH high -> not rolled over
    assert fs.is_backside is False
    assert fs.debug.get("rolled_over") is False


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
    back = front_side_state(_df([10.0] * 12 + [13.0, 16.0, 20.0, 19.2, 19.5]))
    assert front.front_side_score > back.front_side_score


def test_score_is_byte_identical_to_pen_formula():
    # ISOLATION proof: front_side_score is a LIVE selection tilt and must NOT move with the
    # chasing_top recalibration. The score depends ONLY on the extension/below/fade penalties
    # (sigma, above_vwap, retrace) — NEVER on the new rolled_over structure leg. We re-derive
    # the published formula from the state's OWN exposed inputs and assert byte-identity, on
    # BOTH a rolled-over backside df and a fresh-new-high front-side df.
    ext_sigma, retrace_veto = 2.0, 0.66   # the function defaults (front_side_score basis)

    def _expected(fs):
        sigma = fs.vwap_dist_sigma
        pen_ext = 0.0 if sigma is None else max(0.0, min(1.0, max(0.0, sigma) / ext_sigma))
        pen_below = 0.0 if fs.above_vwap else 1.0
        pen_faded = max(0.0, min(1.0, fs.retrace_from_hod / retrace_veto))
        return round(max(0.0, min(1.0, 1.0 - (0.5 * pen_ext + 0.3 * pen_below + 0.2 * pen_faded))), 4)

    rolled = front_side_state(_df([10.0] * 12 + [13.0, 16.0, 20.0, 19.2, 19.5]))  # chasing_top
    fresh = front_side_state(_df([10.0] * 12 + [13.0, 16.0, 20.0]))               # front_side
    assert rolled.reason == "chasing_top" and fresh.reason == "front_side"
    # The structure leg flips is_backside but the score is exactly the pen formula either way.
    assert rolled.front_side_score == _expected(rolled)
    assert fresh.front_side_score == _expected(fresh)
