"""run-R instrumentation (L0.5) — the MESO follow-through separator surfaced by the
2026-06-22 loss decomposition (wf w6c11y2s9): winners thrust (>=~1.3R), losers fade
(~0R) before stopping. These cover the pure run_r math; the live winner/loser
separation is the replay verification gate (re-run a real date, check the medians)."""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.replay_v2 import _run_r_value


def test_winner_thrusts_positive_r():
    # entry 10, stop 9 => risk 1; ran to 11.3 => 1.3R (a clean follow-through winner)
    assert _run_r_value(10.0, 9.0, 11.3) == pytest.approx(1.3, rel=1e-6)


def test_loser_never_ran_is_zero():
    # entered then faded immediately, MFE == entry => 0R (the top-of-leg loser shape)
    assert _run_r_value(5.41, 5.20, 5.41) == 0.0


def test_loser_small_run_matches_formula():
    # ran a fraction of R before fading
    assert _run_r_value(3.58, 3.40, 3.61) == pytest.approx((3.61 - 3.58) / (3.58 - 3.40), rel=1e-6)


def test_never_negative():
    # MFE below entry is floored to 0R (can't realize a negative favorable excursion)
    assert _run_r_value(10.0, 9.0, 9.5) == 0.0


def test_degenerate_risk_returns_zero():
    assert _run_r_value(10.0, 10.0, 12.0) == 0.0   # zero structural risk
    assert _run_r_value(10.0, 11.0, 12.0) == 0.0   # inverted stop (above entry)


def test_bad_inputs_fail_safe():
    assert _run_r_value(None, 9.0, 11.0) == 0.0    # type: ignore[arg-type]
    assert _run_r_value(10.0, None, 11.0) == 0.0   # type: ignore[arg-type]
    assert _run_r_value(10.0, 9.0, None) == 0.0    # type: ignore[arg-type]
