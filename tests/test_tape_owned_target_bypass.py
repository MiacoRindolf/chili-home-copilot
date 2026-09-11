"""Actual live-engine target ownership; fake broker and blocked external HTTP.

Alpaca PAPER uses this engine. DB-paper/ReplayV2 target fallback is separately
covered in test_exit_whole_target_paper_replay: they do not implement held G/D.
"""
from copy import deepcopy

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from tests.test_exit_verdict_held_priority import _held_tick, _no_external_market_or_broker_http
from tests.test_held_tick_bbo_iqfeed_l1_first import _wired


@pytest.mark.parametrize("state,unreadable,supported", [
    (lr.STATE_LIVE_ENTERED, False, True),
    (lr.STATE_LIVE_ENTERED, True, True),
    (lr.STATE_LIVE_SCALING_OUT, True, True),
    (lr.STATE_LIVE_SCALING_OUT, False, False),
])
def test_actual_above_target_tick_uses_ownership_even_when_tape_unreadable(
    db, monkeypatch, _wired, state, unreadable, supported,
):
    sess, le, adapter, tape, _ = _held_tick(db, monkeypatch, rollover=False, supported=supported)
    sess.state = state
    le["position"]["target_price"] = 10.2  # fixture bid10.24 is above the fixed target
    protected = deepcopy(le["deadman_stop"])
    lr._commit_le(sess, le)
    db.commit()
    if unreadable:
        tape.fail_with = {"why": "sql_unreadable", "error": "fixture read unavailable"}
    requested = []
    monkeypatch.setattr(lr, "_submit_live_market_exit", lambda *a, **kw: requested.append(kw) or {"ok": False})
    monkeypatch.setattr(lr, "_live_exit_submit_succeeded", lambda *a, **kw: False)
    result = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    db.refresh(sess)
    saved = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    if supported:
        # The independent end-of-tick trail activation still runs after the
        # fixed-target transition yields. It must not enter SCALING_OUT.
        expected_state = lr.STATE_LIVE_TRAILING if state == lr.STATE_LIVE_ENTERED else state
        assert requested == [] and result["state"] == expected_state
        if state == lr.STATE_LIVE_ENTERED:
            assert any(event == "live_trailing_armed" for event, _ in _wired)
        assert lr._exit_verdict_active(sess, saved)
        assert saved["position"]["quantity"] == 10.0
        assert saved["deadman_stop"] == protected
        assert not saved.get("pending_exit_reason")
        if unreadable:
            assert saved["exit_verdict"]["unreadable_why"] == "sql_unreadable"
            authority = lr._exit_verdict_trail_authority(saved, as_of=lr._utcnow())
            assert authority["bypass"] is False and authority["fallback_reason"] == "tape_unreadable"
        else:
            assert saved["exit_verdict"]["last"]["rollover"]["fired"] is False
    else:
        assert result.get("exit_submit_failed") is True
        assert len(requested) == 1 and requested[0]["reason"] == "target"
        assert requested[0]["quantity"] == 10.0
        assert requested[0]["extra"]["exit_shape_basis"] == "legacy_scale_policy"
    assert adapter.market_calls == adapter.limit_calls == []


@pytest.mark.parametrize("trigger", ["accel_rollover", "tick_deadman"])
def test_fixed_target_bypass_preserves_whole_tape_exit_for_legacy_remainder(
    db, monkeypatch, _wired, trigger,
):
    sess, le, adapter, tape, _ = _held_tick(db, monkeypatch, rollover=trigger == "accel_rollover")
    sess.state = lr.STATE_LIVE_TRAILING
    le["position"].update(partial_taken=True, original_quantity=20.0, target_price=10.2)
    if trigger == "tick_deadman":
        tape.add(3.5, 8.5, aggressor=-1)
    lr._commit_le(sess, le)
    db.commit()
    requested = []
    monkeypatch.setattr(lr, "_submit_live_market_exit", lambda *a, **kw: requested.append(kw) or {"ok": False})
    monkeypatch.setattr(lr, "_live_exit_submit_succeeded", lambda *a, **kw: False)
    result = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    # The held-verdict branch reports its decision even if the captured submit
    # is unsuccessful; it uses its own result contract, not the target branch's.
    assert result["ok"] is False and result["exit_verdict"] == trigger, result
    assert result["trigger"] == trigger
    assert len(requested) == 1 and requested[0]["quantity"] == 10.0
    assert requested[0]["reason"] == ("tape_accel_rollover" if trigger == "accel_rollover" else "tick_deadman_stop")
    saved = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert saved["exit_verdict"]["phase"] == "exit_pending"
    assert saved["exit_verdict"]["exit"]["trigger"] == trigger
    assert saved["exit_verdict"]["exit"]["exit_fraction"] == 1.0
    assert adapter.market_calls == adapter.limit_calls == []
