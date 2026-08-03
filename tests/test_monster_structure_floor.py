"""L10 — monster-conditioned 15s structure-floor trail candidate (pure, walang I/O).

HYFM 500% autopsy (2026-08-03): ang 15s ascending lows 3.48→3.70 ay buo hanggang
0.95s bago ang band fill sa 3.57; ang structure break sa 3.70 ay exitable sa
~3.69. Ang JLHL control ay protektado ng maturity rule (2 kumpletong bar lang sa
exit moments) at ng band-inadequacy separator (2.2-3.3% retraces < 5% band).
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.paper_execution import (
    monster_structure_floor_candidate,
)


def _floor(**kw):
    # HYFM exit#1 measured geometry: ascending 3.48→3.70, hwm 4.12, composed
    # band stop ~3.91 (5% ng 4.12 ≈ band width 5.1%), retrace amp 9.12%,
    # entry 3.76, atr_pct 0.05 → buffer = 3.76*max(0.001, 0.0125) = 0.047.
    base = dict(
        enabled=True,
        monster_ctx=True,
        halt_lit=False,
        leg_age_seconds=30.0,
        last15_low=3.70,
        prev15_low=3.48,
        retrace_amp_pct=0.0912,
        hwm=4.12,
        composed_stop=3.91,
        entry=3.76,
        atr_pct=0.05,
    )
    base.update(kw)
    return monster_structure_floor_candidate(**base)


def test_hyfm_exit1_floor_computed():
    floor, reason = _floor()
    assert reason == "structure_floor"
    assert floor == pytest.approx(3.70 - 3.76 * 0.0125, abs=1e-9)  # ~3.653


def test_jlhl_maturity_rule_walang_candidate():
    # JLHL exits: lampas 3 min ang mga leg (halt-stairs) → 1m/5m path ang may-ari.
    floor, reason = _floor(leg_age_seconds=200.0)
    assert floor is None and reason == "leg_mature_1m_owns"


def test_jlhl_band_adequate_walang_candidate():
    # JLHL retraces 2.2-3.3% < 5.1% band → hindi kailangan ang structure floor.
    floor, reason = _floor(retrace_amp_pct=0.028)
    assert floor is None and reason == "band_adequate"


def test_halt_lit_walang_candidate():
    floor, reason = _floor(halt_lit=True)
    assert floor is None and reason == "halt_lit"


def test_not_monster_walang_candidate():
    floor, reason = _floor(monster_ctx=False)
    assert floor is None and reason == "not_monster"


def test_descending_lows_walang_candidate():
    floor, reason = _floor(last15_low=3.40, prev15_low=3.48)
    assert floor is None and reason == "not_ascending"


def test_flag_off():
    floor, reason = _floor(enabled=False)
    assert floor is None and reason == "disabled"


def test_fail_toward_legacy_sa_sirang_inputs():
    for kw, expected in (
        ({"leg_age_seconds": None}, "leg_age_unknown"),
        # float(None) ay TypeError → ang catch-all na "bad_inputs" (fail-toward-legacy pa rin)
        ({"last15_low": None}, "bad_inputs"),
        ({"prev15_low": None}, "bad_inputs"),
        ({"retrace_amp_pct": None}, "bad_inputs"),
        ({"hwm": None}, "bad_inputs"),
        ({"entry": None}, "bad_inputs"),
    ):
        floor, reason = _floor(**kw)
        assert floor is None, kw
        assert reason == expected, (kw, reason)


def test_atr_buffer_floor_binds():
    # atr_pct=None → buffer = entry*0.001 (ang minimum wick buffer).
    floor, reason = _floor(atr_pct=None)
    assert reason == "structure_floor"
    assert floor == pytest.approx(3.70 - 3.76 * 0.001, abs=1e-9)
