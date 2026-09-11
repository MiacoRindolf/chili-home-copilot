"""A fresh equity fill must reach full protection despite optional audit failure."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.config import settings
from app.models.trading import TradingAutomationEvent
from app.services.trading.momentum_neural import live_runner as lr
from tests.test_momentum_macro_lifecycle_happy import (
    FakeVenueAdapter, _filled_buy, _seed_pending_submitted,
)
from tests.test_momentum_emergency_exit_recovery import TEST_ALPACA_ACCOUNT_ID


@pytest.fixture(autouse=True)
def _no_external_http(monkeypatch):
    import httpx
    import requests
    from curl_cffi import requests as curl_requests

    def unavailable(*args, **kwargs):
        raise AssertionError("External HTTP is forbidden in fresh-fill policy tests")

    async def unavailable_async(*args, **kwargs):
        unavailable(*args, **kwargs)

    monkeypatch.setattr(requests.sessions.Session, "request", unavailable)
    monkeypatch.setattr(curl_requests.Session, "request", unavailable)
    monkeypatch.setattr(httpx.Client, "request", unavailable)
    monkeypatch.setattr(httpx.AsyncClient, "request", unavailable_async)


class _FilledEquityAdapter(FakeVenueAdapter):
    def bind_account_id(self, expected_account_id):
        return expected_account_id == TEST_ALPACA_ACCOUNT_ID

    def get_account_snapshot(self):
        return {"ok": True, "paper": True, "account_id": TEST_ALPACA_ACCOUNT_ID}

    def get_position_quantity(self, product_id):
        return 20.0

    def place_protected_partial_oco(self, **kwargs):
        raise AssertionError("A newly confirmed supported fill must not create a tranche OCO")


@pytest.mark.parametrize("old_lineage", [False, True], ids=["empty-lineage", "recycled-lineage"])
@pytest.mark.parametrize("audit_failure", [None, "exception", "sql_error"], ids=["audit-ok", "audit-raises", "audit-sql-rollback"])
def test_fresh_fill_establishes_lineage_and_full_protection_despite_optional_audit(
    db, monkeypatch, old_lineage, audit_failure,
):
    symbol = "EVFRESH"
    old_anchor = "2026-08-01T14:00:00+00:00"
    sess = _seed_pending_submitted(db, monkeypatch, symbol, atr_pct=.02)
    sess.execution_family = "alpaca_spot"
    sess.venue = "alpaca"
    snapshot = deepcopy(sess.risk_snapshot_json)
    snapshot.update(alpaca_account_scope="alpaca:paper", alpaca_account_id=TEST_ALPACA_ACCOUNT_ID)
    seed_le = snapshot[lr.KEY_LIVE_EXEC]
    if old_lineage:
        seed_le.update(entry_filled_at_utc=old_anchor, entry_fill_event_id=-99,
                       exit_verdict={"phase": "exit_pending", "entry_at": old_anchor,
                                     "exit": {"trigger": "old-leg-only"}})
    sess.risk_snapshot_json = snapshot
    db.commit()

    adapter = _FilledEquityAdapter(symbol, base_increment=1.0, base_min=1.0)
    adapter.set_quote(10.0, 10.01)
    adapter.set_order("ord-entry", _filled_buy("ord-entry", 20.0, 10.0, symbol))
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", TEST_ALPACA_ACCOUNT_ID)
    monkeypatch.setattr(settings, "chili_momentum_live_capture_features", False)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda family: True)
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda *a, **k: False)
    monkeypatch.setattr(lr, "runner_boundary_risk_ok", lambda *a, **k: (True, {"allowed": True}))
    monkeypatch.setattr(lr, "_replay_aware_fetch_ohlcv_df", lambda *a, **k: None)
    # Entry ownership persistence and protective transport have their own seam
    # suites. Exercise the actual fill handler and optional-placement policy,
    # stopping only at these external durable/broker boundaries.
    monkeypatch.setattr(lr, "_resolve_alpaca_entry_claim_from_terminal_order", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_entry_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_live_tick_bbo", lambda *a, **k: (*adapter.get_best_bid_ask(symbol), {"reason": "test_quote"}))

    real_emit = lr._emit
    audit_attempts = []
    protection_calls = []

    def emit(db_, sess_, kind, payload):
        if kind == "scale_out_limit_suppressed":
            current = lr._live_exec(sess_.risk_snapshot_json)
            audit_attempts.append(deepcopy(current))
            assert current["entry_fill_event_id"] > 0
            assert current["entry_filled_at_utc"] != old_anchor
            assert not current.get("exit_verdict")
            if audit_failure == "exception":
                raise RuntimeError("optional suppression audit unavailable")
            if audit_failure == "sql_error":
                # A real PostgreSQL error aborts the savepoint. Swallowing the
                # exception without rolling back an isolated savepoint cannot
                # satisfy the following protection/db-commit assertions.
                db_.execute(text("SELECT 1 / 0"))
        return real_emit(db_, sess_, kind, payload)

    def ensure_full_protection(db_, sess_, adapter_, **kwargs):
        current = kwargs["le"]
        assert db_.execute(text("SELECT 1")).scalar_one() == 1
        assert current["entry_fill_event_id"] > 0
        assert current["entry_filled_at_utc"] != old_anchor
        assert not current.get("exit_verdict")
        assert not current.get("scale_limit_order_id")
        assert not current.get("scale_limit_place_intent")
        assert kwargs["quantity"] == 20.0
        assert kwargs["avg_entry_price"] == 10.0
        assert 0 < kwargs["software_stop_price"] < 10.0
        protection_calls.append(deepcopy(kwargs))
        current["deadman_stop"] = {"order_id": "test-full-stop", "qty": 20.0}
        lr._commit_le(sess_, current)
        real_emit(db_, sess_, "test_full_protection_boundary_reached", {"quantity": 20.0})
        return {"ok": True, "order_id": "test-full-stop", "qty": 20.0}

    monkeypatch.setattr(lr, "_emit", emit)
    monkeypatch.setattr(lr, "_ensure_alpaca_deadman_stop", ensure_full_protection)
    lr._reconcile_counters.pop(int(sess.id), None)
    result = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    db.refresh(sess)
    saved = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert result.get("ok") is True, result
    assert sess.state == lr.STATE_LIVE_ENTERED
    assert len(audit_attempts) == 1 and len(protection_calls) == 1
    assert saved["position"]["quantity"] == 20.0
    assert saved["position"]["avg_entry_price"] == 10.0
    assert saved["deadman_stop"] == {"order_id": "test-full-stop", "qty": 20.0}
    assert saved["scale_limit_policy"]["new_fractional_order"] is False
    assert not saved.get("scale_limit_order_id")
    assert not saved.get("exit_verdict")
    assert not saved.get("operator_flatten_requested_utc")
    assert adapter.market_calls == [] and adapter.limit_calls == []
    fill_events = db.query(TradingAutomationEvent).filter_by(
        session_id=sess.id, event_type="live_entry_filled",
    ).all()
    assert len(fill_events) == 1
    assert saved["entry_fill_event_id"] == fill_events[0].id
    assert fill_events[0].payload_json["order_id"] == "ord-entry"
    assert datetime.fromisoformat(saved["entry_filled_at_utc"]).tzinfo == timezone.utc
