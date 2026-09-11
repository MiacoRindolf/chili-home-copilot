"""Actual ordinary held callers use original SQL and finish RR before transport.

Only a dedicated test database is used. The broker boundary and all HTTP are
blocked; these tests prove caller ordering/continuation, never actual execution.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text

from app.services.trading.momentum_neural import captured_paper_dispatcher as dispatch
from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import live_runner as lr
from tests.test_exit_verdict_f_state_machine import T_ENTRY
from tests.test_exit_verdict_held_priority import _held_tick, _no_external_market_or_broker_http
from tests.test_held_tick_bbo_iqfeed_l1_first import _wired
from tests.test_held_reader_postgres import admitted, market  # noqa: F401


@pytest.mark.parametrize("route", ["held", "quote_unavailable", "pending_partial"])
@pytest.mark.parametrize("cleanup_fault", [False, True], ids=["normal", "rollback-fault"])
def test_all_actual_held_callers_close_epoch_and_preserve_decision_before_broker(
    db, monkeypatch, _wired, market, route, cleanup_fault,
):
    monkeypatch.setattr("httpx.Client.request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP forbidden")))
    originals = {name: getattr(eg, name) for name in
                 ("leg_prints_between", "leg_prints_since_high", "signed_tape_accel_features")}
    sess, le, adapter, tape, _ = _held_tick(db, monkeypatch)
    for name, function in originals.items():
        monkeypatch.setattr(eg, name, function)
    # These rows are committed before admission. Only the four actual market
    # role reads see the UUID-owned schema; caller ORM remains on its own DB port.
    for price, size, bid, ask, _, observed, identifier in tape.rows:
        market.put(identifier, (observed - T_ENTRY).total_seconds(), price,
                   size=size, buy=price >= ask, symbol=sess.symbol)
    pending = {
        "pending_exit_reason": "scale_out_target", "pending_exit_is_scale_out": True,
        "pending_exit_quantity": 5., "exit_order_id": "exact-old-oid",
        "exit_client_order_id": "exact-old-cid", "alpaca_exit_applied_fill_watermarks": [],
    }
    if route == "pending_partial":
        sess.state = lr.STATE_LIVE_SCALING_OUT
        le.update(deepcopy(pending))
        lr._commit_le(sess, le)
    if route == "quote_unavailable":
        monkeypatch.setattr(lr, "_live_tick_bbo", lambda *a, **k: (None, None, {
            "reason": "held_bbo_unavailable", "counts_toward_halt": False,
        }))
    db.commit()
    session_id = int(sess.id)
    db.rollback()  # Admission precedes any new caller-held connection/row lock.
    emitted, boundaries = [], []
    prior_emit = lr._emit

    def emit(receipt_db, session, name, payload):
        if name == "live_exit_evaluation":
            emitted.append(deepcopy(payload))
            return SimpleNamespace(id=987001)
        return prior_emit(receipt_db, session, name, payload)

    monkeypatch.setattr(lr, "_emit", emit)
    with admitted(market, session_id=session_id) as lease:
        connection, physical = lease.slot.connection, lease.slot.dbapi
        if cleanup_fault:
            def reject_rollback(conn):
                raise RuntimeError("actual RR rollback control fault")
            event.listen(connection, "rollback", reject_rollback)

        def boundary(caller_db, current, *args, **kwargs):
            assert not connection.in_transaction()
            assert physical.closed or physical.get_transaction_status() == 0
            assert caller_db.execute(text("SELECT 42")).scalar_one() == 42
            current_le = kwargs["le"]
            assert current_le["exit_verdict"]["phase"] == "exit_pending"
            assert current_le["exit_verdict"]["exit"]["trigger"] == "accel_rollover"
            assert current_le["exit_verdict"]["exit"]["exit_fraction"] == 1.
            boundaries.append(deepcopy(kwargs))
            if route == "pending_partial":
                assert all(current_le[key] == value for key, value in pending.items())
                return {"ok": True, "pending_exit": True, "whole_exit_decision_pending": True,
                        "pending_partial_retirement": "test_boundary"}
            assert kwargs["quantity"] == 10.
            return {"ok": False, "deferred": True, "pre_place_blocked": True, "error": "test_seam_only"}

        monkeypatch.setattr(lr, "_submit_live_market_exit", boundary)
        monkeypatch.setattr(lr, "_retire_pending_partial_zero", boundary)
        monkeypatch.setattr(lr, "_live_exit_submit_succeeded", lambda *a, **k: False)
        result = dispatch._ordinary_tick(
            db, session_id, non_paper_tick=lambda caller, sid: lr.tick_live_session(
                caller, sid, adapter_factory=lambda: adapter),
        )
        assert not connection.in_transaction()
        assert physical.closed or physical.get_transaction_status() == 0
        assert len(emitted) == 1
        receipt = emitted[0]["market_snapshot"]
        assert receipt["common_snapshot"] is True
        assert receipt["cleanup"] == ("connection_invalidated_physical_closed" if cleanup_fault else "transaction_ended")
        assert emitted[0]["read_roles"]["walk"]["status"] == "query_success"
        assert emitted[0]["read_roles"]["G"]["status"] == "query_success"
        assert emitted[0]["read_roles"]["D"]["status"] == "not_reached"
    assert len(boundaries) == (0 if route == "quote_unavailable" else 1), result
    db.commit()
    db.refresh(sess)
    saved = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert saved["exit_verdict"]["phase"] == "exit_pending"
    assert saved["exit_verdict"]["exit"]["trigger"] == "accel_rollover"
    if route == "quote_unavailable":
        assert saved["deadman_stop"]["qty"] == 10.
    assert not adapter.limit_calls and not adapter.market_calls
