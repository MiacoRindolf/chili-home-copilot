"""WAVE-1 FIX-7 — SCORE-FLOOR RAISE-ONLY INTEGRITY.

The entry viability floor is composed as ``min(0.95, flat_min + midday_bump + run_r_bump)``
— every risk factor RAISES the bar, none lowers it. The codex fork carried a
``ross_audio_starter`` override that computed ``min(eff_min, entry_revalidate_floor)``,
discarding the run-R deweight RAISE and lowering the effective floor (IPW: 0.683 -> 0.5445,
which un-blocked the whole -$136.93). That override is ABSENT on main — the composition is
already raise-only. FIX-7 adds a defensive INVARIANT guard (``_raise_only_entry_floor``) so
no override / min() inserted between the bumps and the gate on a future merge can silently
lower the risk-raised bar.

These tests pin:
  * the pure invariant helper: max(current, snapshot); flag-off passthrough; bad-input safe.
  * the raise-only property: an override that LOWERS the floor is neutralized (clamped back
    up to the raised snapshot) when the flag is on, and passed through when off.
"""

from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.live_runner import _raise_only_entry_floor


def test_identity_when_no_override_lowers_the_floor():
    # Main today: the value at the gate equals the raised snapshot => identity.
    assert _raise_only_entry_floor(0.72, 0.72, enabled=True) == pytest.approx(0.72)
    assert _raise_only_entry_floor(0.56, 0.56, enabled=True) == pytest.approx(0.56)


def test_override_that_lowers_is_clamped_back_up_to_the_raised_floor():
    # A hostile override lowered the current eff_min (0.5445) below the run-R-raised
    # snapshot (0.683) — the exact IPW shape. The guard restores the raised floor.
    assert _raise_only_entry_floor(0.5445, 0.683, enabled=True) == pytest.approx(0.683)


def test_flag_off_passes_the_lowered_value_through_unchanged():
    # Rollback path: with the guard OFF the lowered override survives (legacy behavior).
    assert _raise_only_entry_floor(0.5445, 0.683, enabled=False) == pytest.approx(0.5445)


def test_a_higher_current_than_snapshot_is_preserved():
    # If some later logic RAISES the floor further, the guard keeps the higher value
    # (it is a floor, not a clamp-down): max(0.80, 0.70) = 0.80.
    assert _raise_only_entry_floor(0.80, 0.70, enabled=True) == pytest.approx(0.80)


def test_bad_inputs_return_current_unchanged():
    assert _raise_only_entry_floor("x", 0.5, enabled=True) == "x"
    assert _raise_only_entry_floor(0.5, None, enabled=True) == pytest.approx(0.5)


def test_raise_components_compose_last_and_are_raise_only_end_to_end():
    """Property: for the documented composition flat + midday + run_r, the guarded floor
    is NEVER below flat_min plus the applied raises, regardless of a lowering override."""
    flat_min = 0.56
    midday_bump = 0.05
    run_r_bump = 0.05
    raised = min(0.95, flat_min + midday_bump + run_r_bump)  # 0.66
    # A codex-style override tries to drop the floor down to the revalidate floor (0.50).
    hostile_override = 0.50
    guarded = _raise_only_entry_floor(hostile_override, raised, enabled=True)
    assert guarded >= flat_min + midday_bump + run_r_bump - 1e-9
    assert guarded == pytest.approx(raised)
