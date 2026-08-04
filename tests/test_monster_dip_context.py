"""L7 — monster-dip context (pure helper, no I/O).

Distribution study 2026-08-01 (JLHL +784% intraday at JEM +224% bilang monsters
vs JZXN fade day bilang guard): ang recent-impulse retrace yardstick ay sira sa
monster days (winner dips 0.71-1.0 retrace vs saturated cap ~0.61-0.69), at ang
day-context pair na (up_off_low ≥ 1.5 AT day-retrace ≤ 0.35) ay may ZERO-overlap
na separation: 7/8 JLHL winner dips admitted, 0/19 JZXN fades. Ang mga fixture
dito ay ang mismong measured na mga kaso.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.entry_gates import monster_dip_context


def _ctx(**kw) -> bool:
    base = dict(
        enabled=True,
        decision_px=9.61,
        day_high=12.0,
        day_low=3.71,
        pb_low=10.15,
        up_off_low_floor=1.5,
        day_retrace_cap=0.35,
    )
    base.update(kw)
    return monster_dip_context(**base)


def test_jlhl_winner_dip_admitted():
    # Ang verified 19:15 episode: retrace_day 0.223, up_off_low ~2.6 → admitted
    # (forward +40.8%/+22.9%/+21.5%).
    assert _ctx() is True


def test_jlhl_climax_excluded_by_day_retrace():
    # Ang verified 20:30 climax: retrace_day 0.68 → hindi admitted (forward −13%).
    assert _ctx(decision_px=22.0, day_high=32.81, day_low=3.71, pb_low=13.03) is False


def test_jzxn_fade_excluded_by_up_off_low():
    # Fade day: up_off_low max 1.247 < 1.5 → hindi admitted kahit mababaw ang
    # day-retrace (backside consolidations 0.03-0.33).
    assert _ctx(decision_px=2.10, day_high=2.38, day_low=1.74, pb_low=2.05) is False


def test_flag_off_never_admits():
    assert _ctx(enabled=False) is False


def test_missing_inputs_fail_toward_legacy():
    for kw in (
        {"decision_px": None},
        {"day_low": None},
        {"day_high": None},
        {"pb_low": None},
        {"day_low": 0.0},
        {"day_high": 3.71},  # day_range <= 0
        {"decision_px": "oops"},
    ):
        assert _ctx(**kw) is False, kw


def test_floors_bind_verbatim():
    # up_off_low eksaktong sa floor ⇒ pasok (>= semantics; FP-clean na numero:
    # 3.0/2.0 = eksaktong 1.5); bahagyang ilalim ⇒ hindi.
    assert _ctx(decision_px=3.0, day_low=2.0, day_high=12.0, pb_low=11.0) is True
    assert _ctx(decision_px=2.9, day_low=2.0, day_high=12.0, pb_low=11.0) is False
    # day-retrace eksaktong sa cap ⇒ pasok; lampas ⇒ hindi.
    hi, lo = 12.0, 3.71
    pb_at_cap = hi - 0.35 * (hi - lo)
    assert _ctx(pb_low=pb_at_cap) is True
    assert _ctx(pb_low=pb_at_cap - 0.05) is False


@pytest.mark.parametrize("floor,expected", [(1.0, True), (2.6, False)])
def test_up_off_low_floor_knob_binds(floor: float, expected: bool):
    # decision 9.61 / low 3.71 = 2.59x — ang floor knob ay verbatim.
    assert _ctx(up_off_low_floor=floor) is expected
