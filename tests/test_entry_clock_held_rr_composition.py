"""Actual broker-clock fill adoption followed by original held SQL in one RR epoch.

Reuse the reviewed fill lifecycle's complete/terminal-partial fixtures and all
its assertions. At its second real tick, replace only its FakeTape read seam
with the original SQL functions and an explicitly injected dedicated reader.
No production change, HTTP, real broker request or application startup.
"""
from copy import deepcopy
from datetime import timezone
from functools import wraps

import pytest

from app.models.trading import TradingAutomationEvent
from app.services.trading.momentum_neural import captured_paper_dispatcher as dispatch
from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import held_evaluation_audit as audit
from app.services.trading.momentum_neural import live_runner as lr
from tests import test_entry_fill_clock_lifecycle as lifecycle
from tests.test_exit_verdict_f_state_machine import T_ENTRY
from tests.test_exit_whole_target_fresh_fill import _no_external_http  # noqa: F401
from tests.test_held_reader_postgres import admitted, market  # noqa: F401


@pytest.mark.parametrize("terminal_partial", [False, True], ids=["complete", "canceled-156-of-530"])
def test_adopted_broker_clock_drives_actual_RR_base_walk_and_persisted_audit(
    db, monkeypatch, market, terminal_partial,
):
    original_tick = lr.tick_live_session
    originals = {name: getattr(eg, name) for name in
                 ("leg_prints_between", "leg_prints_since_high", "signed_tape_accel_features")}
    original_returned = audit.QueryObservation.returned
    tick_ids, membership, payloads = [], {}, []

    def observed(self, rows=None, error=None):
        original_returned(self, rows=rows, error=error)
        if error is None:
            membership[self.record["role"]] = [int(r[5 if self.exact_n is not None else 6]) for r in rows]

    monkeypatch.setattr(audit.QueryObservation, "returned", observed)

    def composed_tick(caller, sid, **kwargs):
        tick_ids.append(sid)
        if len(tick_ids) == 1:
            return original_tick(caller, sid, **kwargs)
        assert len(tick_ids) == 2
        tape = eg.leg_prints_between.__self__
        symbol = "CLOCKPART" if terminal_partial else "CLOCKFULL"
        for price, size, bid, ask, _, observed_at, identifier in tape.rows:
            market.put(identifier, (observed_at - T_ENTRY).total_seconds(), price,
                       size=size, buy=price >= ask, symbol=symbol)
        # Recording call arguments preserves the reviewed lifecycle assertions;
        # every result is produced by the unchanged original SQL/math function.
        def traced(name, role):
            function = originals[name]
            @wraps(function)
            def run(symbol, **inputs):
                tape.reads.append((role, dict(inputs)))
                return function(symbol, **inputs)
            return run
        for name, role in (("leg_prints_between", "between"),
                           ("leg_prints_since_high", "since"),
                           ("signed_tape_accel_features", "feats")):
            monkeypatch.setattr(eg, name, traced(name, role))
        # The first fill is already committed by the reviewed lifecycle. End
        # its later read-only receipt lookup before reader admission/next lock.
        caller.rollback()
        with admitted(market, session_id=sid) as lease:
            result = dispatch._ordinary_tick(caller, sid, non_paper_tick=lambda d, s: original_tick(d, s, **kwargs))
            assert not lease.slot.connection.in_transaction()
            assert lease.slot.dbapi.get_transaction_status() == 0
            event = caller.query(TradingAutomationEvent).filter_by(session_id=sid, event_type="live_exit_evaluation").one()
            payloads.append(deepcopy(event.payload_json))
        return result

    monkeypatch.setattr(lr, "tick_live_session", composed_tick)
    lifecycle.test_actual_fill_records_broker_clock_before_sidework_then_new_held_tick(
        db, monkeypatch, terminal_partial,
    )
    assert len(tick_ids) == 2 and len(set(tick_ids)) == 1 and len(payloads) == 1
    receipt = payloads[0]
    assert receipt["anchor"]["entry_filled_at_utc"] == T_ENTRY.replace(tzinfo=timezone.utc).isoformat()
    assert receipt["observations"]["entry_fill_clock"]["source"] == "broker_order_reported_fill_at"
    assert receipt["observations"]["entry_fill_clock"]["quantity_kind"] == (
        "terminal_partial_quantity" if terminal_partial else "completed_request")
    assert receipt["market_snapshot"]["common_snapshot"] is True
    assert receipt["market_snapshot"]["cleanup"] == "transaction_ended"
    assert receipt["captured_prefix"] is receipt["replay_authority"] is False
    assert membership["entry_base"] == [10000, 10001, 10002, 10003]
    assert membership["walk"] == [10004, 10005, 10006]
    assert membership["G"] == [10000, 10001, 10002, 10003, 10004, 10005, 10006]
    assert receipt["previous_feature"]["status"] == "prior_receipt_unknown"
    assert receipt["pre"]["verdict"].get("accel_prev") is None
    assert receipt["feature_pointer_advanced"] is True
