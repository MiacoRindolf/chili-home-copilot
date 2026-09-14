"""The exit receipt distinguishes runner observations from broker event clocks."""
from datetime import timedelta
from types import SimpleNamespace

from app.services.trading.momentum_neural import live_runner as lr
from tests.test_exit_tail_receipt import T0, _Clock, _long_le


def test_seam_response_and_fill_observations_have_distinct_clock_sources(monkeypatch):
    clock = _Clock(T0)
    monkeypatch.setattr(lr, "_utcnow", clock)
    monkeypatch.setattr(lr, "_emit", lambda *a, **k: None)
    le = _long_le(exit_order_type="limit", exit_limit_price="2.50")
    lr._exit_tail_stamp_decision(le, reason="tick_deadman_stop", bid=2.56)
    clock.advance(2)
    posted = lr._exit_tail_note_post(
        None, SimpleNamespace(id=1), le, reason="tick_deadman_stop",
        result={"ok": True, "order_id": "observed-order"}, client_order_id="cid",
        quantity=100, bid_priced=2.50, submit_started_at=T0, via_handoff_recovery=False,
    )
    assert posted["decided_at_source"] == "first_submit_seam_observation"
    assert posted["submitted_at_source"] == "local_broker_response_observation"
    assert posted["decision_to_submit_s"] == 2.0
    clock.advance(3)
    tail = lr._exit_tail_receipt(le, fill_price=2.49, quantity=100)
    assert tail["decided_at_source"] == "first_submit_seam_observation"
    assert tail["submitted_at_source"] == "live_exit_order_posted"
    assert tail["submitted_at_clock"] == "local_broker_response_observation"
    assert tail["fill_confirmed_at_source"] == "local_fill_confirmation_observation"
    assert tail["decision_to_fill_s"] == 5.0
    assert tail["submit_to_fill_s"] == 3.0


def test_legacy_pending_stamp_does_not_inherit_response_clock_authority(monkeypatch):
    monkeypatch.setattr(lr, "_utcnow", _Clock(T0 + timedelta(seconds=4)))
    le = _long_le(pending_exit_submitted_at_utc=T0.isoformat())
    tail = lr._exit_tail_receipt(le, fill_price=2.49, quantity=100)
    assert tail["decided_at_source"] is None
    assert tail["submitted_at_source"] == "pending_exit_submitted_at_utc_named_fallback"
    assert tail["submitted_at_clock"] == "legacy_pending_exit_stamp_unspecified_phase"
    assert tail["decision_to_fill_s"] is None


def test_supplied_fill_confirmation_is_identified_as_caller_evidence(monkeypatch):
    monkeypatch.setattr(lr, "_utcnow", _Clock(T0 + timedelta(seconds=9)))
    tail = lr._exit_tail_receipt(_long_le(), fill_price=2.49, quantity=100, filled_at=T0)
    assert tail["fill_confirmed_at_source"] == "caller_supplied_confirmation_observation"
    assert tail["fill_confirmed_at_utc"] == T0.isoformat()
