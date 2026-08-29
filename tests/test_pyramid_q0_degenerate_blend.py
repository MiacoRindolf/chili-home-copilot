"""Pyramid blend q0=0 degenerate stop — huwag ilagay ang stop sa sariling fill.

Sinukat (XPON 2026-08-26 replay, cycle C7): naubos ng scale-outs ang starter
(q0=0), tapos ang pyramid add sa 8.40 ay nag-ratchet ng stop sa a1 = 8.40 —
ang MISMONG fill price ng add. Bid 8.28 ay lampas na agad, instant breach,
market exit 8.21 makalipas ang 8 segundo (−10.57). Ang BE-ratchet ay may
saysay lamang kapag may hawak na starter na nagbibigay ng cushion (a1 < Pa_f);
sa q0=0 ang tamang stop ay ang dala nang stop_px ng trail logic.

Runnable: pytest tests/test_pyramid_q0_degenerate_blend.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.paper_execution import (
    pyramid_blend_on_fill,
)


def test_q0_zero_keeps_prior_stop_not_add_fill_price():
    # Ang eksaktong XPON C7 shape: starter ubos, add 55.65 @ 8.40, dala na ang
    # trail stop 8.21028. Dating output: s1 = 8.40 (instant breach). Dapat:
    # s1 = 8.21028.
    out = pyramid_blend_on_fill(
        q0=0.0, a0=7.61, qa_f=55.65, Pa_f=8.40, stop_px=8.21028,
    )
    assert out["s1"] == 8.21028
    assert out["q1"] == 55.65
    assert out["a1"] == 8.40  # ang blended VWAP ay ang add mismo — tama iyon


def test_q0_zero_still_never_loosens_stop():
    # INVARIANT-A: kahit sa degenerate branch, hindi lumuluwag ang stop.
    out = pyramid_blend_on_fill(
        q0=0.0, a0=5.0, qa_f=10.0, Pa_f=6.0, stop_px=5.9,
    )
    assert out["s1"] == 5.9


def test_positive_q0_blend_unchanged():
    # Ang normal na landas: may starter, ang BE ratchet ay nagbibigay ng
    # cushion sa pagitan ng a0 at Pa_f — hindi ginagalaw ng fix.
    out = pyramid_blend_on_fill(
        q0=100.0, a0=7.61, qa_f=50.0, Pa_f=8.40, stop_px=7.70,
    )
    expected_a1 = (7.61 * 100.0 + 8.40 * 50.0) / 150.0
    assert abs(out["a1"] - expected_a1) < 1e-9
    assert out["s1"] == max(7.70, expected_a1)
    assert out["s1"] < 8.40  # may tunay na cushion sa ilalim ng add fill


def test_positive_q0_ratchet_still_tighten_only():
    out = pyramid_blend_on_fill(
        q0=100.0, a0=7.61, qa_f=50.0, Pa_f=8.40, stop_px=8.10,
    )
    assert out["s1"] >= 8.10


def test_original_quantity_growth_unchanged():
    out = pyramid_blend_on_fill(
        q0=0.0, a0=7.61, qa_f=55.65, Pa_f=8.40, stop_px=8.21,
        original_quantity=122.85,
    )
    assert out["original_quantity"] == 122.85 + 55.65
