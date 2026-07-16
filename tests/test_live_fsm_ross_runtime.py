from __future__ import annotations

from app.services.trading.momentum_neural.live_fsm import (
    LIVE_RUNNER_RUNNABLE_STATES,
    LIVE_RUNNER_TERMINAL_STATES,
    STATE_LIVE_CANCELLED,
)


def test_live_cancelled_is_terminal_not_runnable() -> None:
    assert STATE_LIVE_CANCELLED in LIVE_RUNNER_TERMINAL_STATES
    assert STATE_LIVE_CANCELLED not in LIVE_RUNNER_RUNNABLE_STATES
