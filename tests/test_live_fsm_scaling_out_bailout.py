"""Every position-holding live state can bail (2026-09-05).

MEASURED: Ross Parity Bench, DFNS 2026-07-29 t4 (Robinhood family) on main + #1354. After a
first-target scale-out the runner breached the #769 max-loss circuit; `_transition_to_bailout`
called `assert_transition_live('live_scaling_out', 'live_bailout')` and the FSM raised
`Invalid live FSM transition` inside `tick_live_session` — the replay driver died with the
position still open (`driver_failed rc=1` after 455 s). In production the same raise would
surface as a tick exception on a held position. The edge had never been in the table; the
burst-stamp fix exposed it because re-entries now live long enough to scale out.

DB-free. Runnable: pytest tests/test_live_fsm_scaling_out_bailout.py -v
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import live_fsm as fsm


def test_scaling_out_can_bail():
    assert fsm.can_transition_live(fsm.STATE_LIVE_SCALING_OUT, fsm.STATE_LIVE_BAILOUT)
    fsm.assert_transition_live(fsm.STATE_LIVE_SCALING_OUT, fsm.STATE_LIVE_BAILOUT)  # no raise


def test_every_position_holding_state_can_bail_and_bailout_can_exit():
    holding = set(fsm.LIVE_POSITION_HOLDING_STATES) - {fsm.STATE_LIVE_BAILOUT}
    assert holding == {fsm.STATE_LIVE_ENTERED, fsm.STATE_LIVE_SCALING_OUT, fsm.STATE_LIVE_TRAILING}
    for s in sorted(holding):
        assert fsm.can_transition_live(s, fsm.STATE_LIVE_BAILOUT), s
    assert fsm.can_transition_live(fsm.STATE_LIVE_BAILOUT, fsm.STATE_LIVE_EXITED)


def test_the_new_edge_does_not_open_pre_entry_states_to_bailout():
    for s in (fsm.STATE_WATCHING_LIVE, fsm.STATE_LIVE_ENTRY_CANDIDATE, fsm.STATE_LIVE_PENDING_ENTRY,
              fsm.STATE_QUEUED_LIVE, fsm.STATE_ARMED_PENDING_RUNNER):
        assert not fsm.can_transition_live(s, fsm.STATE_LIVE_BAILOUT), s
        with pytest.raises(ValueError):
            fsm.assert_transition_live(s, fsm.STATE_LIVE_BAILOUT)
