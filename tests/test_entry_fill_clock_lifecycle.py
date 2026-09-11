"""Actual fill adoption and held evaluation, fake broker, real isolated persistence."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from app.config import settings
from app.models.trading import TradingAutomationEvent
from app.services.trading.momentum_neural import live_runner as lr, entry_gates as eg
from app.services.trading.momentum_neural.alpaca_orphan_claims import acquire_action_claim, update_action_claim_phase
from tests.test_exit_whole_target_fresh_fill import _FilledEquityAdapter, _no_external_http
from tests.test_momentum_emergency_exit_recovery import TEST_ALPACA_ACCOUNT_ID
from tests.test_momentum_macro_lifecycle_happy import _filled_buy, _seed_pending_submitted
from tests.test_exit_verdict_f_state_machine import Env, T_ENTRY, _le, _sess, _spike_tape, _tick


@pytest.mark.parametrize("terminal_partial", [False, True], ids=["complete", "canceled-156-of-530"])
def test_actual_fill_records_broker_clock_before_sidework_then_new_held_tick(db, monkeypatch, terminal_partial):
    symbol = "CLOCKPART" if terminal_partial else "CLOCKFULL"
    qty, requested = (156., 530.) if terminal_partial else (20., 20.)
    clock = {"now": T_ENTRY+timedelta(seconds=2)}
    monkeypatch.setattr(lr, "_utcnow", lambda: clock["now"])
    sess = _seed_pending_submitted(db, monkeypatch, symbol, atr_pct=.02)
    sess.execution_family, sess.venue = "alpaca_spot", "alpaca"
    snap = deepcopy(sess.risk_snapshot_json)
    snap.update(alpaca_account_scope="alpaca:paper", alpaca_account_id=TEST_ALPACA_ACCOUNT_ID,
                alpaca_symbol_claim_token="clock-owner")
    snap[lr.KEY_LIVE_EXEC].update(
        entry_client_order_id="cid-e", entry_filled_at_utc="2025-01-01T00:00:00+00:00",
        entry_fill_event_id=-1, entry_fill_clock={"source":"previous-leg"},
        entry_fill_recorded_at_utc="2025-01-01T00:00:01+00:00",
        exit_verdict={"phase":"armed", "frontier_at":"2025-01-01", "accel_prev":9999.},
    )
    sess.risk_snapshot_json = snap
    request = dict(product_id=symbol, side="buy", client_order_id="cid-e", base_size=str(requested),
                   order_type="limit", position_intent="buy_to_open", limit_price="10.01",
                   time_in_force="day", extended_hours=True)
    db.flush()
    claim = acquire_action_claim(db, symbol=symbol, action="entry", claim_token="clock-owner",
        owner_session_id=sess.id, client_order_id="cid-e", account_scope="alpaca:paper",
        metadata={"alpaca_account_id":TEST_ALPACA_ACCOUNT_ID, "legacy_timeshare_sizing":True,
                  "order_request":request, "order_role":"primary"})
    assert claim["ok"]
    assert update_action_claim_phase(db, symbol=symbol, claim_token="clock-owner", phase="submitted",
        client_order_id="cid-e", broker_order_id="ord-entry", account_scope="alpaca:paper")
    db.commit()
    adapter = _FilledEquityAdapter(symbol, base_increment=1., base_min=1.)
    adapter.get_position_quantity = lambda product_id: qty
    adapter.set_quote(10., 10.01)
    order = _filled_buy("ord-entry", qty, 10., symbol)
    order = replace(order, status="canceled" if terminal_partial else "filled",
                    created_time=(T_ENTRY-timedelta(seconds=1)).replace(tzinfo=timezone.utc).isoformat())
    order = replace(order, raw={"qty":requested, "filled_size":int(qty), "alpaca_filled_qty":str(qty),
        "fill_truth_readable":True, "alpaca_status":order.status,
        "filled_at":T_ENTRY.replace(tzinfo=timezone.utc).isoformat(),
        "submitted_at":order.created_time, "time_in_force":"day", "position_intent":"buy_to_open",
        "extended_hours":True, "limit_price":10.01,
        "broker_order_id_echo":"ord-entry", "broker_client_order_id_echo":"cid-e",
        "broker_symbol_echo":symbol, "broker_side_echo":"buy"})
    adapter.set_order("ord-entry", order)
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", TEST_ALPACA_ACCOUNT_ID)
    monkeypatch.setattr(settings, "chili_momentum_live_capture_features", False)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda *a: True)
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda *a, **k: False)
    monkeypatch.setattr(lr, "runner_boundary_risk_ok", lambda *a, **k: (True,{"allowed":True}))
    monkeypatch.setattr(lr, "_replay_aware_fetch_ohlcv_df", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_live_tick_bbo", lambda *a, **k: (*adapter.get_best_bid_ask(symbol), {"reason":"test_quote"}))
    monkeypatch.setattr(lr, "_record_live_entry_ledger_safe", lambda *a, **k: clock.update(now=T_ENTRY+timedelta(seconds=3)))
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: clock.update(now=T_ENTRY+timedelta(seconds=4)))
    protection = []
    def protect(db_, sess_, adapter_, **kwargs):
        protection.append(kwargs["quantity"])
        kwargs["le"]["deadman_stop"] = {"order_id":"fixture-stop", "qty":qty}
        lr._commit_le(sess_, kwargs["le"])
        return {"ok":True}
    monkeypatch.setattr(lr, "_ensure_alpaca_deadman_stop", protect)
    lr._reconcile_counters.pop(sess.id, None)
    result = lr.tick_live_session(db, sess.id, adapter_factory=lambda:adapter)
    db.commit();db.refresh(sess)
    first = deepcopy(sess.risk_snapshot_json[lr.KEY_LIVE_EXEC])
    assert result["ok"] and sess.state == lr.STATE_LIVE_ENTERED
    assert protection == [qty]
    assert first["entry_filled_at_utc"] == T_ENTRY.replace(tzinfo=timezone.utc).isoformat(), first.get("entry_fill_clock")
    assert first["entry_fill_clock"]["observed_at_utc"] == (T_ENTRY+timedelta(seconds=2)).replace(tzinfo=timezone.utc).isoformat()
    assert first["entry_fill_recorded_at_utc"] == (T_ENTRY+timedelta(seconds=4)).replace(tzinfo=timezone.utc).isoformat()
    assert first["entry_fill_clock"]["source"] == "broker_order_reported_fill_at"
    assert first["entry_fill_clock"]["quantity_kind"] == ("terminal_partial_quantity" if terminal_partial else "completed_request")
    assert first["entry_fill_event_id"] > 0 and not first.get("exit_verdict")
    event = db.query(TradingAutomationEvent).filter_by(session_id=sess.id, event_type="live_entry_filled").one()
    assert event.payload_json["entry_fill_clock"] == first["entry_fill_clock"]
    # A fresh Session/identity-map is the restart boundary. Later broker clock
    # changes cannot upgrade the already-held leg or its immutable fill event.
    sid = sess.id
    db.expunge_all()
    order.raw["filled_at"] = (T_ENTRY+timedelta(seconds=3)).replace(tzinfo=timezone.utc).isoformat()
    tape = _spike_tape()
    monkeypatch.setattr(eg, "leg_prints_between", tape.leg_prints_between)
    monkeypatch.setattr(eg, "leg_prints_since_high", tape.leg_prints_since_high)
    monkeypatch.setattr(eg, "signed_tape_accel_features", tape.signed_tape_accel_features)
    clock["now"] = T_ENTRY+timedelta(seconds=5)
    held = lr.tick_live_session(db, sid, adapter_factory=lambda:adapter)
    db.commit()
    sess = db.get(lr.TradingAutomationSession, sid)
    current = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert held["ok"]
    assert current["entry_fill_clock"] == first["entry_fill_clock"]
    assert current["entry_fill_event_id"] == first["entry_fill_event_id"]
    assert current["exit_verdict"]["entry_at"] == T_ENTRY.isoformat()
    base = [p for role,p in tape.reads if role=="feats" and p["as_of"]==T_ENTRY]
    assert base and base[0]["available_by"] == clock["now"]
    walk = [p for role,p in tape.reads if role=="between"]
    assert walk and walk[0]["after"] == T_ENTRY.isoformat()
    assert adapter.market_calls == adapter.limit_calls == []


def test_future_anchor_cannot_advance_but_durable_whole_sale_still_resubmits(monkeypatch):
    tape = _spike_tape();env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le()
    le["entry_filled_at_utc"] = (T_ENTRY+timedelta(seconds=8)).isoformat()
    result = _tick(env, le, seconds=4)
    assert result["unreadable"] == "entry_anchor_after_decision_as_of"
    assert not le["exit_verdict"].get("frontier_at") and not tape.reads
    le["exit_verdict"] = {"phase":"exit_pending", "exit":{"trigger":"accel_rollover", "exit_fraction":1.0}}
    result = _tick(env, le, seconds=4)
    assert result["action"] == "resubmit" and result["exit"]["exit_fraction"] == 1.0


def test_recycle_and_safety_clear_clock_provenance():
    le = dict(entry_fill_clock={"source":"broker"}, entry_fill_recorded_at_utc="old",
              entry_filled_at_utc="old", entry_fill_event_id=7, exit_verdict={"phase":"armed"})
    original = deepcopy(le)
    lr._clear_position_entry_anchor(le)
    assert not le
    lr._reset_entry_state_on_recycle(original)
    assert not original
