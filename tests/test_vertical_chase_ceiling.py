"""Unit tests for the CONFIRMED-THRUST halt-resume vertical chase-ceiling raise
(spec #1, 2026-06-29). Pure helpers in live_runner — NO DB.

Covers: byte-identical parity (flag off / no confluence / below floor), the linear
adaptive raise from abs_cap@floor to max_bps@1.0, the hard cap, and the fail-closed
thrust-confluence builder (halt-resume + tape REQUIRED).
"""

import math

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr


@pytest.fixture(autouse=True)
def _restore_flags():
    """Snapshot + restore the flags this module mutates (settings is a singleton)."""
    keys = (
        "chili_momentum_vertical_chase_enabled",
        "chili_momentum_vertical_chase_max_bps",
        "chili_momentum_vertical_chase_min_confluence",
        "chili_momentum_explosive_rvol_floor",
    )
    saved = {k: getattr(settings, k) for k in keys}
    yield
    for k, v in saved.items():
        setattr(settings, k, v)


# Use a non-None expected_move so the abs_cap is a meaningful base (not the 12bps floor).
# expected_move_bps=2000 → _adaptive_live_max_spread_bps caps at the 300bps abs_cap.
_EM = 2000.0


def _abs_cap():
    return lr._adaptive_live_max_spread_bps(_EM)


def test_abs_cap_is_the_documented_300_default():
    # Sanity: a big-move name's base cap is the 300bps abs_cap (the binding parity value).
    assert _abs_cap() == pytest.approx(300.0)


def test_parity_flag_off():
    settings.chili_momentum_vertical_chase_enabled = False
    assert lr._vertical_chase_ceiling_bps(expected_move_bps=_EM, confluence=1.0) == pytest.approx(_abs_cap())


def test_parity_confluence_none():
    settings.chili_momentum_vertical_chase_enabled = True
    assert lr._vertical_chase_ceiling_bps(expected_move_bps=_EM, confluence=None) == pytest.approx(_abs_cap())


def test_parity_confluence_nan():
    settings.chili_momentum_vertical_chase_enabled = True
    assert lr._vertical_chase_ceiling_bps(expected_move_bps=_EM, confluence=float("nan")) == pytest.approx(_abs_cap())


def test_parity_below_floor():
    settings.chili_momentum_vertical_chase_enabled = True
    settings.chili_momentum_vertical_chase_min_confluence = 0.5
    # just under the floor -> abs_cap, no raise
    assert lr._vertical_chase_ceiling_bps(expected_move_bps=_EM, confluence=0.4999) == pytest.approx(_abs_cap())


def test_raise_at_floor_equals_abs_cap():
    settings.chili_momentum_vertical_chase_enabled = True
    settings.chili_momentum_vertical_chase_min_confluence = 0.5
    settings.chili_momentum_vertical_chase_max_bps = 800.0
    assert lr._vertical_chase_ceiling_bps(expected_move_bps=_EM, confluence=0.5) == pytest.approx(_abs_cap())


def test_raise_at_one_equals_hard_max():
    settings.chili_momentum_vertical_chase_enabled = True
    settings.chili_momentum_vertical_chase_min_confluence = 0.5
    settings.chili_momentum_vertical_chase_max_bps = 800.0
    assert lr._vertical_chase_ceiling_bps(expected_move_bps=_EM, confluence=1.0) == pytest.approx(800.0)


def test_raise_scales_linearly_at_midpoint():
    settings.chili_momentum_vertical_chase_enabled = True
    settings.chili_momentum_vertical_chase_min_confluence = 0.5
    settings.chili_momentum_vertical_chase_max_bps = 800.0
    base = _abs_cap()
    # midpoint between floor(0.5) and 1.0 -> base + 0.5*(800-base)
    expect = base + 0.5 * (800.0 - base)
    assert lr._vertical_chase_ceiling_bps(expected_move_bps=_EM, confluence=0.75) == pytest.approx(expect)


def test_never_exceeds_hard_max():
    settings.chili_momentum_vertical_chase_enabled = True
    settings.chili_momentum_vertical_chase_min_confluence = 0.5
    settings.chili_momentum_vertical_chase_max_bps = 800.0
    # confluence clamps to [0,1]; even >1 cannot exceed the hard max
    assert lr._vertical_chase_ceiling_bps(expected_move_bps=_EM, confluence=5.0) <= 800.0 + 1e-6


def test_hard_max_below_abs_cap_is_inert():
    # If someone sets the hard max below the abs_cap, it must NEVER lower the ceiling.
    settings.chili_momentum_vertical_chase_enabled = True
    settings.chili_momentum_vertical_chase_min_confluence = 0.5
    settings.chili_momentum_vertical_chase_max_bps = 50.0  # < 300 abs_cap
    assert lr._vertical_chase_ceiling_bps(expected_move_bps=_EM, confluence=1.0) == pytest.approx(_abs_cap())


# ── thrust-confluence builder (fail-closed) ──

def test_thrust_none_without_halt_resume():
    assert lr._vertical_thrust_confluence(
        halt_resume_active=False, tape_thrust_ok=True, squeeze_pct=0.9, rvol=10.0
    ) is None


def test_thrust_none_without_tape():
    assert lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=None, squeeze_pct=0.9, rvol=10.0
    ) is None
    assert lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=False, squeeze_pct=0.9, rvol=10.0
    ) is None


def test_thrust_floor_is_half_with_halt_and_tape_only():
    c = lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=True, squeeze_pct=None, rvol=None
    )
    assert c == pytest.approx(0.5)


def test_thrust_full_confluence_caps_at_one():
    settings.chili_momentum_explosive_rvol_floor = 3.0
    c = lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=True, squeeze_pct=1.0, rvol=50.0
    )
    assert c == pytest.approx(1.0)


def test_thrust_squeeze_and_rvol_add_bounded_share():
    settings.chili_momentum_explosive_rvol_floor = 3.0
    # squeeze 0.7 adds (0.7-0.5)*0.5=0.10; rvol 5 adds (5-3)*0.05=0.10 -> 0.70
    c = lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=True, squeeze_pct=0.7, rvol=5.0
    )
    assert c == pytest.approx(0.70)


def test_thrust_below_neutral_squeeze_does_not_subtract():
    # squeeze_pct < 0.5 contributes 0 (max(0, ...)), never below the 0.5 floor.
    c = lr._vertical_thrust_confluence(
        halt_resume_active=True, tape_thrust_ok=True, squeeze_pct=0.2, rvol=None
    )
    assert c == pytest.approx(0.5)


def test_repeg_uses_raised_ceiling_on_vertical():
    """End-to-end through _entry_repeg_price: a gap that exceeds the 300bps abs_cap is
    abandoned without confluence, but FILLS with full confluence (raised ceiling)."""
    settings.chili_momentum_vertical_chase_enabled = True
    settings.chili_momentum_vertical_chase_min_confluence = 0.5
    settings.chili_momentum_vertical_chase_max_bps = 800.0
    L0 = 10.0
    # ask 5% above the original limit = 500bps > 300 abs_cap, < 800 hard max
    ask = L0 * 1.05
    # No confluence -> past the cumulative ceiling -> None
    assert lr._entry_repeg_price(original_limit_px=L0, live_ask=ask, expected_move_bps=_EM) is None
    # Full confluence -> ceiling raised to 800bps -> repeg returns a usable price
    px = lr._entry_repeg_price(
        original_limit_px=L0, live_ask=ask, expected_move_bps=_EM, vertical_confluence=1.0
    )
    assert px is not None and px >= ask
