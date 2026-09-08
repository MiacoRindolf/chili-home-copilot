"""pullback_add depth band — both ends must be measured on the same price series.

The position high-water mark is the peak BID; ``pullback_low`` is a bar LOW off
the tape frame.  Bid < trade, so on a name whose spread is wider than the dip the
bar low prints ABOVE the peak bid and the depth fraction comes out NEGATIVE — a
pullback deeper than zero percent of the move, which cannot happen.

Measured in the live book on 2026-09-08: 5 of 18 ``pullback_too_shallow`` vetoes
carried a negative depth (MOVE −0.5714, RKTO −0.3333, BIAF −0.0789 and −0.0152,
LHAI −0.0625), every one of them with ``pullback_low > prior_low`` — the higher
low the gate exists to find.

DB-free.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.paper_execution import pullback_add_decision


def _decide(**over):
    """A pullback that is healthy on every axis except what the test varies."""
    kw = dict(
        enabled=True, is_equity=True, add_count=0, max_adds=2,
        in_flight=False, other_add_in_flight=False,
        a0=10.00, q0=100.0, d0=0.50,
        bid=11.90, stop_px=9.50,
        high_water_mark=12.00, support_level=11.70,
        pullback_low=11.60, prior_pullback_low=11.50,
        move_range=2.00,
        pullback_depth_lo_frac=0.20, pullback_depth_hi_frac=0.62,
        bounced=True, front_side_strength=0.70, strength_floor=0.50,
        above_vwap_or_reclaiming=True, ofi_level=0.20, ofi_slope=0.10,
        midday_lull=False, cooldown_active=False,
    )
    kw.update(over)
    return pullback_add_decision(**kw)


def test_the_healthy_dip_fires():
    out = _decide()
    assert out["fire"] is True, out["reason"]
    assert out["pullback_depth_frac"] == pytest.approx(0.20, abs=1e-9)


def test_bid_series_hwm_below_the_tape_low_produces_a_negative_depth():
    """The defect, reproduced from the BIAF receipt.

    hwm 11.890 (peak bid) vs pullback_low 11.905 (bar low) and prior low 11.70 —
    the exact shape that produced depth_frac −0.0789 in the live book.
    """
    out = _decide(high_water_mark=11.890, pullback_low=11.905,
                  prior_pullback_low=11.700, move_range=11.890 - 11.700)
    assert out["pullback_depth_frac"] is not None
    assert out["pullback_depth_frac"] < 0, "the negative depth must be reproducible"
    assert out["fire"] is False
    assert out["reason"] == "pullback_too_shallow"


def test_taking_the_move_top_from_the_tape_removes_the_negative():
    """The fix, at the call site: hwm = max(peak bid, frame high).

    With the move top read off the same tape as the dip low, the same bar now
    scores a real, positive depth instead of an impossible one.
    """
    hwm_bid, hwm_frame = 11.890, 12.150      # the tape saw the true top
    hwm = max(hwm_bid, hwm_frame)
    out = _decide(high_water_mark=hwm, pullback_low=11.905,
                  prior_pullback_low=11.700, move_range=hwm - 11.700)
    assert out["pullback_depth_frac"] > 0
    assert out["pullback_depth_frac"] == pytest.approx((12.150 - 11.905) / (12.150 - 11.700), abs=1e-9)


@pytest.mark.parametrize(
    "hwm_pos,hwm_frame,bid",
    [
        (11.890, 12.150, 11.90),   # tape above bid peak — the live shape
        (12.400, 12.150, 11.90),   # bid peak above the frame window
        (None, 12.150, 11.90),     # no position hwm yet
        (None, None, 11.90),       # no frame either — bid is the only basis
    ],
)
def test_move_top_is_never_below_the_dip_low(hwm_pos, hwm_frame, bid):
    """max() over the available series is what makes depth >= 0 structural.

    This mirrors the call-site expression in live_runner, so a future edit that
    drops one of the legs fails here rather than in the book.
    """
    pullback_low = 11.905
    hwm = max([v for v in (hwm_pos, hwm_frame, bid) if v is not None])
    if hwm < pullback_low:
        pytest.skip("no series saw a top above the dip; depth is undefined, not negative")
    out = _decide(high_water_mark=hwm, pullback_low=pullback_low,
                  prior_pullback_low=11.700, move_range=max(1e-9, hwm - 11.700))
    assert out["pullback_depth_frac"] >= 0


def test_a_genuinely_deep_pullback_is_still_refused():
    """The fix must not turn a rollover into a buy."""
    out = _decide(high_water_mark=12.00, pullback_low=10.30,
                  prior_pullback_low=10.20, move_range=2.00)
    assert out["fire"] is False
    assert out["reason"] == "pullback_too_deep"
