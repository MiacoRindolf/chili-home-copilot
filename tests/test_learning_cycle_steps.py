"""Learning cycle step modules: imports and stable entrypoints."""

from __future__ import annotations


def test_learning_cycle_steps_exports() -> None:
    from app.services.trading.learning_cycle_steps import (
        load_prescreen_scan_and_universe,
        run_secondary_miners_phase,
    )

    assert callable(load_prescreen_scan_and_universe)
    assert callable(run_secondary_miners_phase)
