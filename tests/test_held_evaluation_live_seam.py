"""Actual held caller still reaches the whole-sell seam after receipt INSERT failure.

Dedicated test DB; all HTTP and the broker submission boundary are blocked/faked.
This proves caller continuation, not broker execution or protection certification.
"""
import pytest
from sqlalchemy import text

from app.services.trading.momentum_neural import live_runner as lr
from tests.test_held_tick_bbo_iqfeed_l1_first import _wired
from tests.test_exit_verdict_held_priority import _held_tick, _no_external_market_or_broker_http


@pytest.mark.parametrize("held_state", [lr.STATE_LIVE_ENTERED, lr.STATE_LIVE_TRAILING])
def test_receipt_insert_failure_does_not_prevent_actual_held_caller_sell_seam(db, monkeypatch, _wired, held_state):
    monkeypatch.setattr("httpx.Client.request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP forbidden")))
    sess, le, adapter, tape, _ = _held_tick(db, monkeypatch)
    sess.state = held_state
    db.commit()
    db.execute(text("CREATE TEMP TABLE held_receipt_reject (value integer CHECK(value>0)) ON COMMIT DROP"))
    prior_emit = lr._emit
    attempted = []

    def emit(receipt_db, session, name, payload):
        if name == "live_exit_evaluation":
            attempted.append(payload)
            assert receipt_db is not db and receipt_db.connection() is db.connection()
            # Real PostgreSQL failure INSIDE the optional receipt's savepoint.
            receipt_db.execute(text("INSERT INTO held_receipt_reject VALUES(-1)"))
        return prior_emit(receipt_db, session, name, payload)

    monkeypatch.setattr(lr, "_emit", emit)
    submissions = []

    def submit(db_, sess_, adapter, **kwargs):
        assert db_.execute(text("SELECT 42")).scalar() == 42
        assert kwargs["le"]["exit_verdict"]["phase"] == "exit_pending"
        assert kwargs["le"]["exit_verdict"]["evaluation_audit"]["last_attempt"]["status"] == "receipt_missing"
        submissions.append(kwargs)
        return {"ok": False, "deferred": True, "pre_place_blocked": True, "error": "test_seam_only"}

    monkeypatch.setattr(lr, "_submit_live_market_exit", submit)
    monkeypatch.setattr(lr, "_live_exit_submit_succeeded", lambda *a, **k: False)
    lr._reconcile_counters.pop(int(sess.id), None)
    result = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    db.refresh(sess)
    assert len(attempted) == len(submissions) == 1
    assert result["exit_verdict"] == "accel_rollover"
    assert submissions[0]["quantity"] == 10.
    assert submissions[0]["extra"]["exit_fraction"] == 1.
    assert submissions[0]["reason"] == "tape_accel_rollover"
    saved = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert saved["pending_exit_reason"] == "tape_accel_rollover"
    assert saved["exit_verdict"]["phase"] == "exit_pending"
    assert adapter.limit_calls == adapter.market_calls == []
