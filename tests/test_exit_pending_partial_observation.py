"""An old request must not hide a new whole-sale decision or lose its identity."""
from copy import deepcopy

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from tests.test_exit_verdict_held_priority import _held_tick, _no_external_market_or_broker_http
from tests.test_held_tick_bbo_iqfeed_l1_first import _wired


@pytest.mark.parametrize('rollover,supported,old_fractional', [
    (True, True, True), (False, True, True), (True, False, True), (True, True, False),
])
def test_actual_pending_tick_preserves_old_order_while_observing_whole_intent(
    db, monkeypatch, _wired, rollover, supported, old_fractional,
):
    sess, le, adapter, tape, submits = _held_tick(
        db, monkeypatch, rollover=rollover, supported=supported,
    )
    sess.state = lr.STATE_LIVE_SCALING_OUT
    original = {
        'pending_exit_reason': 'scale_out_target' if old_fractional else 'target',
        'pending_exit_is_scale_out': old_fractional,
        'pending_exit_quantity': 5.0 if old_fractional else 10.0,
        'pending_exit_submitted_at_utc': '2026-09-10T14:00:00+00:00',
        'exit_order_id': 'old-exact-order',
        'exit_client_order_id': 'old-exact-cid',
        'alpaca_exit_applied_fill_watermarks': [],
    }
    le.update(deepcopy(original))
    lr._commit_le(sess, le)
    db.commit()
    polls = []

    def pending_poll(_db, _sess, _adapter, *, le, reason, quantity):
        polls.append((deepcopy(le), reason, quantity))
        return {'pending': True, 'filled': False, 'partial': False}

    monkeypatch.setattr(lr, '_poll_live_exit_fill', pending_poll)
    result = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    db.refresh(sess)
    saved = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    expected_decision = rollover and supported and old_fractional
    assert result['pending_exit'] is True
    assert result['whole_exit_decision_pending'] is expected_decision
    assert len(polls) == 1
    before_poll, reason, quantity = polls[0]
    for key, value in original.items():
        assert before_poll[key] == value
        assert saved[key] == value
    assert reason == original['pending_exit_reason']
    assert quantity == original['pending_exit_quantity']
    assert saved['position']['quantity'] == 10.0
    if expected_decision:
        assert before_poll['exit_verdict']['phase'] == 'exit_pending'
        assert saved['exit_verdict']['exit']['trigger'] == 'accel_rollover'
        assert saved['exit_verdict']['exit']['exit_fraction'] == 1.0
    else:
        assert lr._exit_verdict_phase(saved) != 'exit_pending'
    assert bool(tape.reads) is (supported and old_fractional)
    assert not submits
    assert adapter.market_calls == [] and adapter.limit_calls == []
