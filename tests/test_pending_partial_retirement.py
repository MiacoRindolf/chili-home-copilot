"""Actual pending-loop retirement: strict zero truth, unlocked I/O and restart."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models.trading import TradingAutomationSession
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import pending_partial_retirement as proof
from tests.test_exit_verdict_held_priority import _held_tick, _no_external_market_or_broker_http
from tests.test_held_tick_bbo_iqfeed_l1_first import _wired
from tests.test_momentum_emergency_exit_recovery import _order


def _pending(db, monkeypatch, *, status="cancelled", filled=0, lost_ack=False):
    sess, le, adapter, tape, submits = _held_tick(db, monkeypatch)
    sess.state = lr.STATE_LIVE_SCALING_OUT
    le.update({
        "pending_exit_reason": "scale_out_target", "pending_exit_is_scale_out": True,
        "pending_exit_quantity": 5.0, "exit_order_id": "old-exact-order",
        "exit_client_order_id": "old-exact-cid", "alpaca_exit_applied_fill_watermarks": [],
        "pending_exit_submitted_at_utc": "2026-09-10T14:00:00+00:00",
    })
    lr._commit_le(sess, le)
    db.commit()
    sid = int(sess.id)
    order = SimpleNamespace(**vars(_order(
        oid="old-exact-order", cid="old-exact-cid", symbol=sess.symbol,
        side="sell", status=status, filled=filled, avg=None, qty=5,
    )))
    reads = []
    unlocked = []
    wakes = []

    def strict(oid):
        assert not db.in_transaction(), "broker read reacquired the tick transaction"
        with Session(db.get_bind()) as other:
            row = (other.query(TradingAutomationSession)
                   .filter(TradingAutomationSession.id == sid)
                   .with_for_update(nowait=True).one())
            assert row.risk_snapshot_json[lr.KEY_LIVE_EXEC][proof.KEY]["phase"] == "strict_truth_pending"
            unlocked.append(True)
        reads.append(oid)
        return {"readable": True, "found": True, "order": order}

    def cancel(oid):
        assert not db.in_transaction()
        adapter.cancel_calls.append(oid)
        order.status = "cancelled"
        if lost_ack:
            raise TimeoutError("cancel ACK lost")
        return {"ok": True}

    monkeypatch.setattr(adapter, "get_order_truth", strict, raising=False)
    monkeypatch.setattr(adapter, "cancel_order", cancel)
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda sid: wakes.append(sid) or True)
    return sess, adapter, order, reads, unlocked, submits, wakes


def _tick(db, sess, adapter):
    return lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)


@pytest.mark.parametrize("status,lost_ack", [("cancelled", False), ("open", True)])
def test_actual_pending_zero_retires_unlocked_then_fresh_tick_uses_whole_seam(
    db, monkeypatch, _wired, status, lost_ack,
):
    sess, adapter, order, reads, unlocked, submits, wakes = _pending(
        db, monkeypatch, status=status, lost_ack=lost_ack,
    )
    sid = int(sess.id)
    out = _tick(db, sess, adapter)
    assert out["pending_partial_retirement"] == "retired_terminal_zero", out
    # The helper committed the receipt. A caller rollback cannot restore the old
    # pointer after its broker cancellation/terminal observation already happened.
    db.rollback()
    db.refresh(sess)
    saved = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert "exit_order_id" not in saved and "pending_exit_reason" not in saved
    assert "exit_client_order_id" not in saved and proof.KEY not in saved
    assert saved["position"]["quantity"] == 10
    history = saved[proof.HISTORY_KEY]
    assert len(history) == 1 and history[0]["proof"]["cumulative_quantity"] == 0
    assert history[0]["binding"]["pending"]["exit_order_id"] == "old-exact-order"
    assert history[0]["proof"]["financial_observation"]["status"].startswith("unresolved")
    original_g = deepcopy(saved["exit_verdict"]["exit"])
    assert original_g["trigger"] == "accel_rollover"
    assert unlocked and wakes == [sid] and submits == []
    assert reads == ["old-exact-order"] * (2 if status == "open" else 1)
    assert adapter.market_calls == [] and adapter.limit_calls == []
    result = _tick(db, sess, adapter)
    db.commit()
    db.refresh(sess)
    assert len(submits) == 1 and submits[0]["quantity"] == 10
    assert submits[0]["extra"]["resubmit"] is True
    assert submits[0]["reason"] == original_g["reason"]
    assert sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]["exit_verdict"]["exit"] == original_g
    assert len(sess.risk_snapshot_json[lr.KEY_LIVE_EXEC][proof.HISTORY_KEY]) == 1
    assert adapter.market_calls == [] and adapter.limit_calls == []


@pytest.mark.parametrize("kind", ["missing", "unknown", "positive", "open_ack", "account", "remaining"])
def test_actual_pending_unproven_keeps_pointer_and_resumes_without_legacy_poll(
    db, monkeypatch, _wired, kind,
):
    sess, adapter, order, reads, _, submits, _ = _pending(db, monkeypatch)
    if kind in {"missing", "unknown"}:
        monkeypatch.setattr(adapter, "get_order_truth", lambda oid: {
            "readable": kind == "missing", "found": False if kind == "missing" else None,
        })
    elif kind == "positive":
        order.filled_size = 2
    elif kind == "open_ack":
        order.status = "open"
        monkeypatch.setattr(adapter, "cancel_order", lambda oid: {"ok": True})
    elif kind == "account":
        monkeypatch.setattr(adapter, "get_account_snapshot", lambda: {"ok": True, "paper": True, "account_id": "other"})
    elif kind == "remaining":
        monkeypatch.setattr(adapter, "get_position_quantity", lambda symbol: 9)
    first = _tick(db, sess, adapter)
    assert first["pending_partial_retirement"] == "unresolved", first
    db.rollback()
    db.refresh(sess)
    saved = deepcopy(sess.risk_snapshot_json[lr.KEY_LIVE_EXEC])
    assert saved["exit_order_id"] == "old-exact-order" and saved["position"]["quantity"] == 10
    assert proof.HISTORY_KEY not in saved
    assert saved[proof.KEY]["phase"] == "unresolved"
    token = saved[proof.KEY]["attempt_token"]
    # Restart with a fresh ORM session; any normal pending handling is forbidden.
    monkeypatch.setattr(lr, "_poll_live_exit_fill", lambda *a, **k: pytest.fail("legacy poll escaped"))
    db.expunge_all()
    sess = db.get(TradingAutomationSession, int(first["session_id"]))
    second = _tick(db, sess, adapter)
    assert second["pending_partial_retirement"] == "unresolved", second
    db.refresh(sess)
    again = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert again[proof.KEY]["attempt_token"] == token
    assert again["exit_verdict"]["exit"] == saved["exit_verdict"]["exit"]
    assert not submits and not adapter.market_calls and not adapter.limit_calls


@pytest.mark.parametrize("change", ["pointer", "entry", "request", "account", "token", "position"])
def test_concurrent_binding_change_cannot_be_retired(db, monkeypatch, _wired, change):
    sess, adapter, order, _, _, submits, _ = _pending(db, monkeypatch)
    sid = int(sess.id)
    original = adapter.get_order_truth

    def changed(oid):
        result = original(oid)
        with Session(db.get_bind()) as other:
            current = other.get(TradingAutomationSession, sid)
            snapshot = deepcopy(current.risk_snapshot_json)
            le = snapshot[lr.KEY_LIVE_EXEC]
            if change == "pointer":
                le["exit_order_id"] = "successor-order"
            elif change == "entry":
                le["entry_filled_at_utc"] = "2026-09-10T15:00:00"
            elif change == "request":
                le["pending_exit_quantity"] = 4
            elif change == "account":
                snapshot["alpaca_account_id"] = "new-account"
            elif change == "token":
                le[proof.KEY]["attempt_token"] = "new-mirror-owner"
            elif change == "position":
                le["position"]["quantity"] = 9
            current.risk_snapshot_json = snapshot
            other.commit()
        return result

    monkeypatch.setattr(adapter, "get_order_truth", changed)
    result = _tick(db, sess, adapter)
    assert result["reason"] == "pending_partial_binding_changed", result
    db.refresh(sess)
    saved = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert proof.HISTORY_KEY not in saved and proof.KEY in saved
    assert saved["exit_order_id"] == ("successor-order" if change == "pointer" else "old-exact-order")
    assert not submits


@pytest.mark.parametrize("boundary", ["before_stage_commit", "retirement_commit", "receipt"])
def test_commit_failure_restart_does_not_retire_or_cancel_twice_unnecessarily(
    db, monkeypatch, _wired, boundary,
):
    sess, adapter, _, reads, _, submits, _ = _pending(db, monkeypatch)
    sid = int(sess.id)
    original_commit = db.commit
    commits = []

    def commit():
        commits.append(1)
        if len(commits) == (1 if boundary == "before_stage_commit" else 2):
            raise RuntimeError("commit failed")
        original_commit()

    if boundary == "receipt":
        original_emit = lr._emit
        def emit(db, sess, kind, payload):
            if kind == "live_pending_partial_retired_terminal_zero":
                raise RuntimeError("receipt failed")
            return original_emit(db, sess, kind, payload)
        monkeypatch.setattr(lr, "_emit", emit)
    else:
        monkeypatch.setattr(db, "commit", commit)
    with pytest.raises(RuntimeError):
        _tick(db, sess, adapter)
    db.rollback()
    current = db.get(TradingAutomationSession, sid)
    le = current.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert le["exit_order_id"] == "old-exact-order" and proof.HISTORY_KEY not in le
    assert bool(reads) is (boundary != "before_stage_commit")
    assert (proof.KEY in le) is (boundary != "before_stage_commit")
    monkeypatch.setattr(db, "commit", original_commit)
    if boundary == "receipt":
        monkeypatch.setattr(lr, "_emit", original_emit)
    assert _tick(db, current, adapter)["pending_partial_retirement"] == "retired_terminal_zero"
    assert not submits


@pytest.mark.parametrize("context", ["nested", "begin", "replay"])
def test_caller_managed_transaction_and_replay_do_not_commit(db, monkeypatch, _wired, context):
    sess, adapter, _, reads, _, _, _ = _pending(db, monkeypatch)
    if context == "replay":
        # A persisted marker must be stopped before replay's other input gates
        # (this test does not manufacture a captured scanner context).
        le = deepcopy(sess.risk_snapshot_json[lr.KEY_LIVE_EXEC])
        le[proof.KEY] = {}
        lr._commit_le(sess, le)
        db.commit()
    sid = int(sess.id)
    db.rollback()
    manager = (db.begin_nested() if context == "nested" else
               db.begin() if context == "begin" else lr.replay_clock(lr._utcnow()))
    with manager:
        result = lr.tick_live_session(db, sid, adapter_factory=lambda: adapter)
        assert result["reason"] == "pending_partial_transaction_owner_unsupported", result
        assert not reads
    db.rollback()


@pytest.mark.parametrize("filled", [None, True, False, "NaN", "Infinity", -1, "unknown", {}, 1])
def test_zero_proof_never_defaults_invalid_or_positive_quantity(filled):
    frozen = {"pending": {"exit_order_id": "oid", "exit_client_order_id": "cid", "pending_exit_quantity": 5},
              "session": {"symbol": "ABC"}, "snapshot": {"alpaca_account_id": "acct"}}
    order = _order(oid="oid", cid="cid", symbol="ABC", side="sell", status="cancelled",
                   filled=filled, avg=None, qty=5)
    assert proof.order_error(order, frozen) is not None


def test_legacy_poll_is_inert_while_retirement_owns_request():
    result = lr._poll_live_exit_fill(None, None, object(), le={proof.KEY: {}}, reason="bailout", quantity=10)
    assert result["why"] == "pending_partial_retirement_owns_request"


def test_concurrent_same_request_finishes_once_without_rotating_mirror_token(db, monkeypatch, _wired):
    sess, adapter, _, _, _, submits, _ = _pending(db, monkeypatch)
    sid = int(sess.id)
    original = adapter.get_order_truth
    nested = []

    def concurrent(oid):
        outer_truth = original(oid)
        monkeypatch.setattr(adapter, "get_order_truth", lambda oid: outer_truth)
        with Session(db.get_bind()) as other:
            result = lr.tick_live_session(other, sid, adapter_factory=lambda: adapter)
            other.commit()
            nested.append(result)
        return outer_truth

    monkeypatch.setattr(adapter, "get_order_truth", concurrent)
    result = _tick(db, sess, adapter)
    assert nested[0]["pending_partial_retirement"] == "retired_terminal_zero"
    assert result["reason"] == "pending_partial_binding_changed"
    db.refresh(sess)
    le = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert len(le[proof.HISTORY_KEY]) == 1 and proof.KEY not in le
    assert not submits


def test_retirement_replays_independently_committed_exact_owner_history(db, monkeypatch, _wired):
    from app.services.trading.momentum_neural.alpaca_orphan_claims import (
        advance_owner_transport, lease_owner_transport, read_action_claim,
        resolve_owner_transport_terminal,
    )

    sess, adapter, order, _, _, submits, _ = _pending(db, monkeypatch)
    le = deepcopy(sess.risk_snapshot_json[lr.KEY_LIVE_EXEC])
    context = lr._alpaca_owner_transport_context(sess)
    deadman = le["deadman_stop"]
    assert resolve_owner_transport_terminal(
        db, **context, client_order_id=deadman["client_order_id"],
        broker_order_id=deadman["order_id"], broker_order_status="cancelled",
        filled_size=0, remaining_quantity=10,
    )
    le.pop("deadman_stop")
    request = {
        "account_scope": context["account_scope"], "alpaca_account_id": context["alpaca_account_id"],
        "product_id": sess.symbol, "side": "sell", "base_size": "5",
        "client_order_id": "old-exact-cid", "position_intent": "sell_to_close",
        "order_type": "market", "time_in_force": "day", "extended_hours": False,
        "limit_price": None,
    }
    lease = lease_owner_transport(
        db, **context, transport_kind="ordinary_exit", client_order_id="old-exact-cid",
        order_request=request, lease_token="old-lease",
    )
    assert lease["ok"], lease
    assert advance_owner_transport(db, **context, client_order_id="old-exact-cid",
                                   lease_token="old-lease", phase="submitted", broker_order_id="old-exact-order")
    readable, claim = read_action_claim(db, symbol=sess.symbol, account_scope=context["account_scope"])
    assert readable
    le["alpaca_active_exit_owner_transport"] = deepcopy(claim["metadata"]["owner_transport"])
    lr._commit_le(sess, le)
    db.commit()
    original_emit = lr._emit

    def fail_after_owner(db, sess, kind, payload):
        if kind == "live_pending_partial_retired_terminal_zero":
            raise RuntimeError("local receipt failed after owner terminal commit")
        return original_emit(db, sess, kind, payload)

    monkeypatch.setattr(lr, "_emit", fail_after_owner)
    with pytest.raises(RuntimeError):
        _tick(db, sess, adapter)
    db.rollback()
    db.refresh(sess)
    assert sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]["exit_order_id"] == "old-exact-order"
    readable, claim = read_action_claim(db, symbol=sess.symbol, account_scope=context["account_scope"])
    assert readable and claim["metadata"]["owner_transport"]["phase"] == "resolved"
    db.rollback()
    monkeypatch.setattr(lr, "_emit", original_emit)
    out = _tick(db, sess, adapter)
    assert out["pending_partial_retirement"] == "retired_terminal_zero", out
    db.refresh(sess)
    saved = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert "alpaca_active_exit_owner_transport" not in saved
    assert len(saved[proof.HISTORY_KEY]) == 1 and not submits


@pytest.mark.parametrize("field,value", [
    ("order_id", "OID"), ("client_order_id", "CID"), ("product_id", "WRONG"),
    ("side", "buy"), ("status", "filled"),
])
def test_zero_quantity_does_not_override_contradictory_broker_identity(field, value):
    frozen = {"pending": {"exit_order_id": "oid", "exit_client_order_id": "cid", "pending_exit_quantity": 5},
              "session": {"symbol": "ABC"}, "snapshot": {"alpaca_account_id": "acct"}}
    order = SimpleNamespace(**vars(_order(oid="oid", cid="cid", symbol="ABC", side="sell",
                                         status="cancelled", filled=0, avg=None, qty=5)))
    setattr(order, field, value)
    assert proof.order_error(order, frozen) is not None


@pytest.mark.parametrize("echo,bad_value", [
    (None, None), ("broker_account_id_echo", "OTHER-ACCOUNT"),
    ("broker_order_id_echo", "other-order"), ("broker_client_order_id_echo", "other-cid"),
    ("broker_symbol_echo", "WRONG"), ("broker_side_echo", "buy"),
    ("broker_quantity_echo", "6"), ("broker_filled_quantity_echo", "1"),
    ("alpaca_filled_qty", "1"), ("broker_order_status_echo", "filled"),
    ("broker_order_type_echo", "stop"), ("broker_time_in_force_echo", "gtc"),
    ("broker_extended_hours_echo", True), ("broker_limit_price_echo", "1.2"),
    ("broker_position_intent_echo", "sell_to_open"), ("broker_asset_class_echo", "crypto"),
    ("fill_truth_readable", False),
])
def test_actual_alpaca_normalizer_echoes_cannot_contradict_terminal_zero(echo, bad_value):
    from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter

    provider = SimpleNamespace(
        id="oid", client_order_id="cid", symbol="ABC", side="sell", status="canceled",
        filled_qty="0", filled_avg_price=None, qty="5", type="market", order_type="market",
        time_in_force="day", extended_hours=False, position_intent="sell_to_close",
        account_id="acct", asset_class="us_equity",
    )
    order = AlpacaSpotAdapter._normalize_order(None, provider)
    frozen = {"pending": {"exit_order_id": "oid", "exit_client_order_id": "cid", "pending_exit_quantity": 5},
              "session": {"symbol": "ABC"}, "snapshot": {"alpaca_account_id": "acct"}}
    assert proof.order_error(order, frozen) is None
    if echo:
        order.raw[echo] = bad_value
        assert proof.order_error(order, frozen) is not None


def test_restart_cannot_replace_observed_positive_fill_with_regressed_zero(db, monkeypatch, _wired):
    sess, adapter, order, reads, _, submits, _ = _pending(db, monkeypatch, filled=2)
    first = _tick(db, sess, adapter)
    assert first["reason"] == "pending_partial_prior_accounting_unproven"
    db.rollback()
    db.refresh(sess)
    saved = deepcopy(sess.risk_snapshot_json[lr.KEY_LIVE_EXEC])
    observed = saved[proof.KEY]["observed_positive_cumulative"]
    assert observed["cumulative_quantity"] == 2 and observed["applied_accounting"] == "unproven"
    order.filled_size = 0
    sid = int(sess.id)
    db.expunge_all()
    sess = db.get(TradingAutomationSession, sid)
    second = _tick(db, sess, adapter)
    assert second["reason"] == "pending_partial_prior_accounting_unproven"
    db.refresh(sess)
    current = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert current[proof.KEY]["observed_positive_cumulative"] == observed
    assert current["exit_order_id"] == "old-exact-order" and current["position"]["quantity"] == 10
    assert len(reads) == 1 and proof.HISTORY_KEY not in current and not submits


@pytest.mark.parametrize("sibling", ["deadman", "scale"])
def test_pending_alias_cannot_cancel_a_known_protective_sibling(db, monkeypatch, _wired, sibling):
    sess, adapter, _, reads, _, submits, _ = _pending(db, monkeypatch)
    le = deepcopy(sess.risk_snapshot_json[lr.KEY_LIVE_EXEC])
    if sibling == "deadman":
        le["exit_order_id"] = le["deadman_stop"]["order_id"]
    else:
        le["scale_limit_order_id"] = le["exit_order_id"]
    lr._commit_le(sess, le)
    db.commit()
    result = _tick(db, sess, adapter)
    assert result["reason"] == "pending_partial_protective_identity_conflict", result
    assert not reads and not submits and not adapter.cancel_calls


def test_external_connection_transaction_cannot_enter_internal_commit_boundary(db, monkeypatch, _wired):
    sess, adapter, order, _, _, _, _ = _pending(db, monkeypatch)
    sid = int(sess.id)
    monkeypatch.setattr(adapter, "get_order_truth", lambda oid: {"readable": False, "found": None})
    assert _tick(db, sess, adapter)["pending_partial_retirement"] == "unresolved"
    db.rollback()
    calls = []

    def read(oid):
        calls.append(oid)
        return {"readable": True, "found": True, "order": order}

    monkeypatch.setattr(adapter, "get_order_truth", read)
    with db.get_bind().connect() as connection:
        with connection.begin():
            with Session(bind=connection) as external:
                out = lr.tick_live_session(external, sid, adapter_factory=lambda: adapter)
                assert out["reason"] == "pending_partial_transaction_owner_unsupported", out
                assert calls == []
