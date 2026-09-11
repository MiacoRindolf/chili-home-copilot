"""The first target must not retreat as the name runs.

WHAT WAS WRONG. `chili_momentum_mfe_target_live_enabled`'s own description promises two things:
"With 0 samples it IS the base R:R (byte-identical to today's plan floor)" and that it is
"replacing the fixed rr_cap=6 / room_capture=0.5 magic realized-HOD lift" (config.py:5365). The
code did the opposite — it shrank toward that lift:

    _prior_rr, _ = adaptive_first_target_reward_risk(..., realized_high=entry_realized_high)
    _dd_meta     = mfe_percentile_target_r(samples, base_rr=_prior_rr, min_samples=30)

so below 30 samples the "replacement" WAS the thing it replaces. And that lift is backwards
(paper_execution.py:448):

    first-target R:R = clamp(max(base, room_capture * room_R), base, rr_cap=6)
    room_R           = (realized_high - entry) / (entry - stop)

`room_R` is how far the name had ALREADY run BEFORE the entry, so the more a name has moved the
FURTHER AWAY its first target goes — the profit-taking level retreats exactly when the move is
most spent.

MEASURED (118 legs / 61 symbol-days, 2026-07-06 .. 09-09, from momentum_mfe_realized):
  * 13 legs carried target_r > 4R; together they realized -7.21 R.
  * NO leg whose target_r exceeded 2.5 has EVER realized more than 2.5.
  * 9 legs reached >= 2.5 R; 7 of them (78%) failed to capture it and 2 finished NEGATIVE
    after having been up more than 2.5 R.
  * A low target costs nothing at the top: the two largest winners on record — VRAX 07-09
    (peak 36.2 R, realized 25.6) and JZXN 07-10 (peak 29.1 R, realized 16.4) — both ran with
    targets of 1.37 and 1.68. The first target does not cap the trade; the RUNNER carries the
    tail, which is what the lift's own docstring says it is for.
  * 2026-09-09 live: 8 of 21 legs were handed the 6.0 cap off n_samples of 0/1/2/9/10 (one at
    pctl_r 0.01); none came close; all left via the trail. The only `target` exits that day
    were the three FTFT legs, whose targets sat at the base.

THE FIX is one argument — shrink toward the plan's base R:R — so the shrinkage design is
untouched and only its destination is corrected. The legacy lift is still COMPUTED for the
receipt (`legacy_lift_r`, `legacy_lift_delta_r`) so the change stays measurable, and it still
APPLIES on the fallback path, which is the documented kill-switch.

Runnable: pytest tests/test_first_target_shrinks_to_the_plan_not_the_lift.py -v
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.services.trading.momentum_neural.exit_calibration import mfe_percentile_target_r
from app.services.trading.momentum_neural.paper_execution import (
    adaptive_first_target_reward_risk,
)

_SRC = (Path(__file__).resolve().parents[1]
        / "app/services/trading/momentum_neural/live_runner.py")


@pytest.fixture(scope="module")
def src() -> str:
    return _SRC.read_text(encoding="utf-8", errors="replace")


# ── the defect, stated as an executable fact ────────────────────────────────────
def test_the_legacy_lift_really_does_retreat_as_the_name_runs():
    """Not an accusation — a measurement. Same entry and stop, only the prior run differs."""
    common = dict(base_reward_risk=2.5, entry=10.0, stop=9.0, side_long=True)
    near, _ = adaptive_first_target_reward_risk(realized_high=10.2, **common)   # barely ran
    far, _ = adaptive_first_target_reward_risk(realized_high=25.0, **common)    # ran hard
    assert far > near, "the lift is supposed to widen with realized room"
    assert far >= 6.0 - 1e-9, (
        f"a name that already ran 15R of room should hit the rr_cap; got {far}"
    )
    assert near == pytest.approx(2.5), "a name that has not run should sit at the plan base"


# ── the fix ─────────────────────────────────────────────────────────────────────
def test_zero_samples_gives_the_plan_base_exactly_as_documented(src: str):
    """config.py:5365 promises 'With 0 samples it IS the base R:R'. Make that true."""
    out = mfe_percentile_target_r([], percentile=0.6, base_rr=2.5, min_samples=30)
    assert out["target_r"] == pytest.approx(2.5)
    assert out["n"] == 0


def test_the_prior_is_the_plan_base_not_the_lift(src: str):
    """The one-line change, guarded at source: the shrinkage destination must be the plan base."""
    m = re.search(r"_dd_meta = mfe_percentile_target_r\((.*?)\n\s*\)", src, re.S)
    assert m, "the mfe_percentile_target_r call site moved"
    call = m.group(1)
    assert "base_rr=float(_base_rr)" in call, (
        "the first target is shrinking toward the realized-HOD lift again — it retreats as the "
        "name runs"
    )
    assert "_prior_rr" not in call


def test_the_legacy_prior_is_still_recorded(src: str):
    """Changing a live target without recording the counterfactual throws away the only evidence
    that could ever reverse it."""
    assert "legacy_lift_r" in src and "legacy_lift_delta_r" in src
    assert "_legacy_lift_rr" in src


def test_the_lift_still_exists_for_the_fallback(src: str):
    """This is a prior change, not a deletion. The lift must still apply where _dd_rr is None —
    that path is the documented kill-switch."""
    assert "adaptive_first_target_reward_risk" in src
    assert 'realized_high=(None if (_dd_rr is not None and _dd_rr > 0)' in src, (
        "the fallback must still consult realized_high when the data-derived target is absent"
    )


# ── the guarantees that must survive ────────────────────────────────────────────
def test_the_target_never_goes_below_the_plan_floor():
    """Ross's floor: never first-scale below the plan's minimum R:R, however weak the sample."""
    weak = mfe_percentile_target_r([0.1, 0.2, 0.05], percentile=0.6, base_rr=2.5, min_samples=30)
    assert weak["target_r"] >= 2.5 - 1e-9


# ── [27b]: the base is now the FIRST-PARTIAL level (0.8R), not the plan R:R (2.5) ──
def test_zero_samples_gives_the_first_partial_base_exactly():
    """The same documented promise, at the new base."""
    out = mfe_percentile_target_r([], percentile=0.6, base_rr=0.8, min_samples=30)
    assert out["target_r"] == pytest.approx(0.8)
    assert out["source"] == "prior_only"


def test_a_weak_family_cannot_drag_the_target_below_the_new_base():
    """MEASURED (momentum_mfe_target_applied, live 2026-09-10): every deep family sits at
    pctl_r 0.00-0.23 on n = 14-16. At base 0.8 the blend lands near 0.46 and max(base,
    blended) returns 0.8 — a lower base does NOT hand the shrinkage a lower target."""
    for pctl in (0.000, 0.134, 0.154, 0.229):
        out = mfe_percentile_target_r([pctl] * 16, percentile=0.6, base_rr=0.8, min_samples=30)
        assert out["target_r"] == pytest.approx(0.8), (
            f"family with pctl_r {pctl} moved the target off the base"
        )
        assert out["n"] == 16


def test_the_one_family_that_does_lift_at_the_new_base():
    """momentum_ok_tick_surge: n = 2, pctl_r 7.040 -> w = 2/30, blend = 1.216."""
    out = mfe_percentile_target_r([7.04, 7.04], percentile=0.6, base_rr=0.8, min_samples=30)
    assert out["target_r"] == pytest.approx(1.216, abs=0.002)
    assert out["target_r"] > 0.8


def test_the_pull_in_is_a_documented_no_op_below_one_R():
    """`_FIRST_SCALE_MIN_R` is 1.0; at a 0.8R plan target the band [floor, target) is EMPTY
    by construction, so the round-number pull-in cannot move the level — and it must never
    pull it BELOW the plan (that would sell inside the measured 0.37-0.58R fill floor)."""
    from app.services.trading.momentum_neural.paper_execution import stop_target_prices

    for entry, atr in ((5.20, 0.05), (4.01, 0.0156), (12.30, 0.02), (0.86, 0.08)):
        stop, target = stop_target_prices(
            entry, atr_pct=atr, reward_risk=0.8, partial_capable=True,
        )
        assert target == pytest.approx(entry + 0.8 * (entry - stop)), (
            f"the pull-in moved a sub-1R target on {entry}/{atr}"
        )


def test_the_pull_in_still_works_at_and_above_one_R():
    """Byte-identical where it always applied — the floor clamp only binds below 1R."""
    from app.services.trading.momentum_neural.paper_execution import (
        round_number_first_scale_target,
    )

    # entry 5.20, stop 4.90 (1R = 0.30), rr_target 6.10 -> $5.50 still qualifies.
    assert round_number_first_scale_target(5.20, 4.90, 6.10) == 5.5
    # a sub-1R rr_target: the floor clamps to the target itself -> empty band -> no-op.
    assert round_number_first_scale_target(5.20, 4.90, 5.44) == 5.44


def test_real_data_still_lifts_the_target_once_it_is_trustworthy():
    """The point is not a fixed 2.5 — it is that the LIFT must come from the tape, not from how
    far the name ran before we arrived. With a full sample of big MFEs the target rises."""
    rich = mfe_percentile_target_r([8.0] * 40, percentile=0.6, base_rr=2.5, min_samples=30)
    assert rich["target_r"] > 2.5
    assert rich["n"] == 40


def test_the_module_still_parses(src: str):
    ast.parse(src)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
