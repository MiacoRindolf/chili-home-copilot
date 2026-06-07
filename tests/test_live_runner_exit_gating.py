"""A held momentum position must keep its stop/target exit management even when
the entry-oriented boundary risk eval refuses (the stop is a safety mechanism)."""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import (
    _held_position_keeps_exit_on_boundary_fail,
)
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_BAILOUT,
    STATE_LIVE_ENTERED,
    STATE_LIVE_SCALING_OUT,
    STATE_LIVE_TRAILING,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
    STATE_LIVE_PENDING_ENTRY,
)

_POS = {"quantity": 100.0, "stop_price": 1.0}


def test_held_states_with_position_keep_exit():
    for st in (STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING, STATE_LIVE_BAILOUT):
        assert _held_position_keeps_exit_on_boundary_fail(st, _POS) is True, st


def test_held_state_without_position_does_not_fall_through():
    # no position -> nothing to manage; the normal block applies
    assert _held_position_keeps_exit_on_boundary_fail(STATE_LIVE_ENTERED, None) is False
    assert _held_position_keeps_exit_on_boundary_fail(STATE_LIVE_ENTERED, {}) is False


def test_entry_states_still_block_on_boundary_fail():
    for st in (STATE_QUEUED_LIVE, STATE_WATCHING_LIVE, STATE_LIVE_PENDING_ENTRY):
        assert _held_position_keeps_exit_on_boundary_fail(st, _POS) is False, st
