"""One actual recycled fill-poll -> held evaluation; fake broker and blocked HTTP."""
from copy import deepcopy
from datetime import timedelta

from app.config import settings
from app.models.trading import TradingAutomationEvent
from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import live_runner as lr
from tests.test_exit_whole_target_fresh_fill import _FilledEquityAdapter, _no_external_http
from tests.test_momentum_macro_lifecycle_happy import _filled_buy, _seed_pending_submitted
from tests.test_momentum_emergency_exit_recovery import TEST_ALPACA_ACCOUNT_ID, _ensure_retained_entry_owner
from tests.test_exit_verdict_f_state_machine import T_ENTRY, _spike_tape


def test_actual_recycled_fill_clears_legacy_comparison_then_first_held_tick_uses_count(db, monkeypatch):
    symbol = "EVNEWCOUNT"
    old_anchor = "2026-08-01T14:00:00+00:00"
    clock = {"now": T_ENTRY}
    monkeypatch.setattr(lr, "_utcnow", lambda: clock["now"])
    sess = _seed_pending_submitted(db, monkeypatch, symbol, atr_pct=.02)
    sess.execution_family, sess.venue = "alpaca_spot", "alpaca"
    snapshot = deepcopy(sess.risk_snapshot_json)
    snapshot.update(alpaca_account_scope="alpaca:paper", alpaca_account_id=TEST_ALPACA_ACCOUNT_ID)
    snapshot[lr.KEY_LIVE_EXEC].update(
        entry_filled_at_utc=old_anchor, entry_fill_event_id=-99,
        exit_verdict={"phase": "armed", "entry_at": old_anchor, "accel_prev": 999999.,
                      "accel_prev_contract": "legacy_time_split", "accel_prev_as_of": old_anchor,
                      "evaluation_audit": {"previous_feature": {"evaluation_id": "old-leg-only"}}},
    )
    sess.risk_snapshot_json = snapshot
    db.commit()
    adapter = _FilledEquityAdapter(symbol, base_increment=1., base_min=1.)
    adapter.set_quote(10., 10.01)
    adapter.set_order("ord-entry", _filled_buy("ord-entry", 20., 10., symbol))
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", TEST_ALPACA_ACCOUNT_ID)
    monkeypatch.setattr(settings, "chili_momentum_live_capture_features", False)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda *a: True)
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda *a, **k: False)
    monkeypatch.setattr(lr, "runner_boundary_risk_ok", lambda *a, **k: (True, {"allowed": True}))
    monkeypatch.setattr(lr, "_replay_aware_fetch_ohlcv_df", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_resolve_alpaca_entry_claim_from_terminal_order", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_entry_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_live_tick_bbo", lambda *a, **k: (*adapter.get_best_bid_ask(symbol), {"reason": "test_quote"}))
    protected = []

    def protect(db_, sess_, adapter_, **kwargs):
        current = kwargs["le"]
        protected.append(kwargs["quantity"])
        assert current["entry_fill_event_id"] > 0
        current["deadman_stop"] = {"order_id": "fixture-full-stop", "qty": 20.}
        lr._commit_le(sess_, current)
        return {"ok": True, "order_id": "fixture-full-stop", "qty": 20.}

    monkeypatch.setattr(lr, "_ensure_alpaca_deadman_stop", protect)
    lr._reconcile_counters.pop(int(sess.id), None)
    entered = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    db.refresh(sess)
    first = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert entered["ok"] is True and sess.state == lr.STATE_LIVE_ENTERED
    assert not first.get("exit_verdict") and first["entry_fill_event_id"] > 0
    assert first["entry_filled_at_utc"] == T_ENTRY.isoformat()+"+00:00"
    assert protected == [20.]
    _ensure_retained_entry_owner(db, sess)
    tape = _spike_tape()
    monkeypatch.setattr(eg, "leg_prints_between", tape.leg_prints_between)
    monkeypatch.setattr(eg, "leg_prints_since_high", tape.leg_prints_since_high)
    monkeypatch.setattr(eg, "signed_tape_accel_features", tape.signed_tape_accel_features)
    clock["now"] = T_ENTRY+timedelta(seconds=4)
    held = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    db.refresh(sess)
    current = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    marker = current["exit_verdict"]
    assert marker["phase"] == "armed" and marker["accel_prev"] > 0
    assert marker["last"]["rollover"]["accel_prev"] is None
    assert marker["last"]["rollover"]["binding"] == "no_previous_evaluation"
    assert marker["accel_prev_contract"] == lr._exit_verdict_settings()["contract_id"]
    assert marker["deadman"]["base_feature_geometry"]["split"] == "count"
    receipt = db.query(TradingAutomationEvent).filter_by(session_id=sess.id, event_type="live_exit_evaluation").one().payload_json
    assert receipt["previous_feature"]["status"] == "prior_receipt_unknown"
    assert receipt["anchor"]["entry_fill_event_id"] == first["entry_fill_event_id"]
    assert receipt["observations"]["G_features"]["feature_contract"] == "count_v1"
    base_reads = [p for role, p in tape.reads if role == "feats" and p["as_of"] == T_ENTRY]
    assert base_reads[0]["available_by"] == clock["now"]
    assert all(p["feature_contract"] == "count_v1" for role, p in tape.reads if role == "feats")
    assert adapter.market_calls == adapter.limit_calls == []
