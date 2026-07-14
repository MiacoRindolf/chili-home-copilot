"""Momentum-orphan adopt-on-cancel-fill root fix (2026-06-17).

The bug (CRVO/FTHM 2026-06-16): ``cancel_automation_session`` marked a LIVE session
terminal BEFORE sweeping its entry orders; when the sweep found the entry FILLED it
only logged ``FILLED_NEEDS_ADOPTION`` and never adopted -> the broker position was
orphaned. The legacy ``bracket_reconciliation_service`` ALSO backstops the broker
position (mints a Trade + places a stop) -> a naive momentum-adopt would create TWO
managers = double-sell. This fix coordinates them via a single-writer
``management_scope='momentum_neural'`` baton:

  Step 1  broker-sync stamps ``momentum_neural`` (not ``broker_sync``) on a synced
          position whose symbol had a recent live momentum session.
  Step 2  the reconciler SKIPS a ``momentum_neural`` row while a NON-TERMINAL live
          momentum session exists -> the momentum lane is the sole writer.
  Step 4  ``cancel_automation_session`` ADOPTS a filled entry (re-point + walk the
          live FSM to pending-entry) instead of orphaning it.

Adoption remains gated behind ``chili_momentum_adopt_on_cancel_fill_enabled``. A
disabled adoption switch no longer authorizes terminalizing through a known fill;
the fail-closed terminal-truth fence quarantines it for reconciliation.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.trading import (
    MomentumStrategyVariant,
    Trade,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import automation_query as aq
from app.services.trading import bracket_reconciliation_service as brs
from app.services import broker_service as bsvc


# ── seeding helpers ───────────────────────────────────────────────────────
_variant_seq = 0


def _variant(db):
    global _variant_seq
    _variant_seq += 1
    v = MomentumStrategyVariant(
        family="test_family",
        variant_key=f"adopt_{_variant_seq}",
        label="adopt test variant",
        params_json={},
    )
    db.add(v)
    db.flush()
    return v


def _live_session(
    db, *, symbol, state, entry_order_id="ORD-1",
    entry_client_order_id="CID-1", execution_family="robinhood_spot",
    account_scope=None,
):
    """A LIVE momentum session whose live-exec snapshot points at one entry order."""
    v = _variant(db)
    le = {
        "entry_order_id": entry_order_id,
        "entry_order_ids_all": ([entry_order_id] if entry_order_id else []),
        "entry_orders_resolved": {},
        "entry_submitted": True,
        "entry_want_qty": 100.0,
    }
    if entry_client_order_id:
        le["entry_client_order_id"] = entry_client_order_id
        le["entry_reconcile_pending_client_order_id"] = entry_client_order_id
    snapshot = {"momentum_live_execution": le}
    if account_scope is not None:
        snapshot["alpaca_account_scope"] = account_scope
    if execution_family in {"alpaca_spot", "alpaca_short"}:
        snapshot["alpaca_account_id"] = "acct-adopt-test"
    else:
        snapshot["non_alpaca_account_identity"] = "adopt-test-account-v1"
    sess = TradingAutomationSession(
        user_id=None,
        venue="test",
        execution_family=execution_family,
        mode="live",
        symbol=symbol,
        variant_id=v.id,
        state=state,
        risk_snapshot_json=snapshot,
        correlation_id="corr-adopt",
    )
    db.add(sess)
    db.flush()
    return sess


def _fake_adapter(*, filled_size, status="filled", symbol: str):
    """A venue adapter double: get_order -> (NormalizedOrder-like, FreshnessMeta)."""

    class _A:
        def __init__(self):
            self.status = status

        def _order(self, oid):
            return SimpleNamespace(
                order_id=str(oid),
                client_order_id="CID-1",
                product_id=symbol,
                filled_size=filled_size,
                status=self.status,
                side="buy",
                raw={"quantity": 100.0},
            )

        def get_account_identity_truth(self):
            return {"readable": True, "identity": "adopt-test-account-v1"}

        def get_position_quantity_truth(self, _product_id):
            return {"readable": True, "quantity": 0.0}

        def get_order(self, oid):
            return self._order(oid), None

        def get_order_truth(self, oid):
            return {"readable": True, "found": True, "order": self._order(oid)}

        def list_open_orders_truth(self, *, product_id=None, limit=250):
            terminal = {
                "filled", "cancelled", "canceled", "rejected", "failed", "expired",
            }
            orders = [] if self.status.lower() in terminal else [self._order("ORD-1")]
            return {"readable": True, "orders": orders}

        def cancel_order(self, oid):  # never reached on the adopt path
            self.status = "cancelled"
            return {"ok": True}

    return _A()


def _patch_adapter(monkeypatch, adapter):
    monkeypatch.setattr(
        "app.services.trading.venue.factory.get_adapter", lambda ef: adapter
    )


def _events(db, session_id):
    return [
        e.event_type
        for e in db.query(TradingAutomationEvent)
        .filter(TradingAutomationEvent.session_id == session_id)
        .all()
    ]


def _event_payload(db, session_id, event_type):
    e = (
        db.query(TradingAutomationEvent)
        .filter(
            TradingAutomationEvent.session_id == session_id,
            TradingAutomationEvent.event_type == event_type,
        )
        .first()
    )
    return e.payload_json if e is not None else None


def _set_flag(monkeypatch, value: bool):
    monkeypatch.setattr(
        aq.settings, "chili_momentum_adopt_on_cancel_fill_enabled", value
    )
    monkeypatch.setattr(
        brs.settings, "chili_momentum_adopt_on_cancel_fill_enabled", value
    )
    monkeypatch.setattr(
        bsvc.settings, "chili_momentum_adopt_on_cancel_fill_enabled", value
    )
    monkeypatch.setattr(
        aq.settings, "chili_alpaca_expected_account_id", "acct-adopt-test"
    )
    monkeypatch.setattr(
        aq,
        "_reaper_broker_position_truth",
        lambda _sess: (True, {"broker_quantity": 0.0}),
    )


# ── 1. adopt on filled entry ───────────────────────────────────────────────
def test_cancel_adopts_on_filled_entry(db, monkeypatch):
    _set_flag(monkeypatch, True)
    sess = _live_session(db, symbol="CRVO", state=aq.STATE_WATCHING_LIVE)
    _patch_adapter(
        monkeypatch,
        _fake_adapter(filled_size=100.0, status="filled", symbol="CRVO"),
    )

    res = aq.cancel_automation_session(db, user_id=None, session_id=sess.id)

    assert res["ok"] is True
    assert res["adopted"] is True
    db.refresh(sess)
    # adopted -> walked to pending-entry, NOT cancelled
    assert sess.state == aq.STATE_LIVE_PENDING_ENTRY
    assert sess.state != aq.STATE_LIVE_CANCELLED
    # le re-pointed + marked resolved 'adopted'
    le = sess.risk_snapshot_json["momentum_live_execution"]
    assert le["entry_order_id"] == "ORD-1"
    assert le["entry_submitted"] is True
    assert le["entry_orders_resolved"]["ORD-1"] == "adopted"
    # the adopt event was emitted; NO cancellation events
    evs = _events(db, sess.id)
    assert "entry_adopted_on_cancel" in evs
    assert "live_cancelled" not in evs
    assert "session_cancelled" not in evs


# ── 2. PARITY: no fill -> byte-identical cancel ─────────────────────────────
def test_cancel_no_fill_byte_identical(db, monkeypatch):
    _set_flag(monkeypatch, True)
    sess = _live_session(db, symbol="NOFILL", state=aq.STATE_WATCHING_LIVE)
    _patch_adapter(
        monkeypatch,
        _fake_adapter(filled_size=0.0, status="cancelled", symbol="NOFILL"),
    )

    res = aq.cancel_automation_session(db, user_id=None, session_id=sess.id)

    assert res == {"ok": True, "session_id": sess.id, "state": aq.STATE_LIVE_CANCELLED}
    db.refresh(sess)
    assert sess.state == aq.STATE_LIVE_CANCELLED
    evs = _events(db, sess.id)
    assert "session_cancelled" in evs
    assert "live_cancelled" in evs
    assert "entry_adopted_on_cancel" not in evs


def test_cancel_legacy_missing_scope_never_adopts_from_client_id(db, monkeypatch):
    _set_flag(monkeypatch, True)
    sess = _live_session(
        db,
        symbol="ACTU",
        state=aq.STATE_WATCHING_LIVE,
        entry_order_id=None,
        entry_client_order_id="chili_ml_e_actu",
        execution_family="alpaca_spot",
    )
    class _A:
        def get_order_by_client_order_id(self, cid):
            raise AssertionError("missing-scope quarantine must precede broker lookup")

        def get_order(self, oid):
            raise AssertionError("missing-scope quarantine must precede broker lookup")

    _patch_adapter(monkeypatch, _A())

    res = aq.cancel_automation_session(db, user_id=None, session_id=sess.id)

    assert res["pending"] == "execution_quarantine"
    assert res["quarantine_reason"] == "alpaca_account_scope_unfrozen_or_mismatched"
    db.refresh(sess)
    assert sess.state == aq.STATE_WATCHING_LIVE
    assert "operator_stop_execution_quarantined" in _events(db, sess.id)
    assert "entry_client_id_recovered_on_cancel" not in _events(db, sess.id)
    assert "session_cancelled" not in _events(db, sess.id)


def test_scoped_alpaca_cancel_defers_client_id_to_claim_aware_runner(db, monkeypatch):
    _set_flag(monkeypatch, True)
    sess = _live_session(
        db,
        symbol="ACTU",
        state=aq.STATE_WATCHING_LIVE,
        entry_order_id=None,
        entry_client_order_id="chili_ml_e_actu",
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
    )

    class _A:
        def get_order_by_client_order_id(self, cid):
            raise AssertionError("operator cancel persists authority; runner owns lookup")

    _patch_adapter(monkeypatch, _A())

    res = aq.cancel_automation_session(db, user_id=None, session_id=sess.id)

    assert res["pending"] == "entry_order_truth_reconcile"
    db.refresh(sess)
    assert sess.state == aq.STATE_WATCHING_LIVE
    assert "operator_cancel_emergency_requested" in _events(db, sess.id)
    assert "session_cancelled" not in _events(db, sess.id)


# ── 3. kill switch OFF restores the orphan (pre-fix) behavior ───────────────
def test_kill_switch_off_quarantines_known_fill(db, monkeypatch):
    _set_flag(monkeypatch, False)
    sess = _live_session(db, symbol="CRVO", state=aq.STATE_WATCHING_LIVE)
    # Flag OFF prevents adoption, but it must never authorize an orphaning death.
    _patch_adapter(
        monkeypatch,
        _fake_adapter(filled_size=100.0, status="filled", symbol="CRVO"),
    )

    res = aq.cancel_automation_session(db, user_id=None, session_id=sess.id)

    assert "adopted" not in res
    assert res["quarantine_reason"] == "terminalization_filled_order_requires_management"
    db.refresh(sess)
    assert sess.state == aq.STATE_WATCHING_LIVE
    assert sess.ended_at is None
    assert sess.risk_snapshot_json.get("operator_pause")
    evs = _events(db, sess.id)
    assert "entry_adopted_on_cancel" not in evs
    assert "live_cancelled" not in evs
    assert "session_cancelled" not in evs
    assert "live_terminalization_quarantined" in evs


# ── 4. reconciler skips momentum-owned by scope ─────────────────────────────
def _open_trade(db, *, ticker, management_scope, broker_source="robinhood"):
    t = Trade(
        user_id=None,
        ticker=ticker,
        direction="long",
        entry_price=10.0,
        quantity=100,
        status="open",
        broker_source=broker_source,
        management_scope=management_scope,
        entry_date=datetime.utcnow(),
    )
    db.add(t)
    db.flush()
    return t


def _tickers(rows):
    return {r["ticker"] for r in rows}


def test_reconciler_skips_momentum_owned(db, monkeypatch):
    # momentum_neural trade + a NON-TERMINAL live momentum session for the ticker
    _open_trade(db, ticker="CRVO", management_scope="momentum_neural")
    _live_session(db, symbol="CRVO", state=aq.STATE_WATCHING_LIVE)
    # a plain broker_sync trade (equity unaffected — always covered)
    _open_trade(db, ticker="AAPL", management_scope="broker_sync")
    db.flush()

    _set_flag(monkeypatch, True)
    rows_on = brs._load_local_view(db, user_id=None)
    assert "CRVO" not in _tickers(rows_on)   # excluded (momentum lane owns it)
    assert "AAPL" in _tickers(rows_on)        # broker_sync always covered

    _set_flag(monkeypatch, False)
    rows_off = brs._load_local_view(db, user_id=None)
    assert "CRVO" in _tickers(rows_off)       # flag OFF -> no exclusion (byte-identical)
    assert "AAPL" in _tickers(rows_off)


# ── 5. reconciler resumes coverage after the session is terminal ────────────
def test_reconciler_resumes_after_terminal(db, monkeypatch):
    _open_trade(db, ticker="FTHM", management_scope="momentum_neural")
    # session is TERMINAL (live_exited) -> no longer the active writer
    _live_session(db, symbol="FTHM", state=aq.STATE_LIVE_EXITED)
    db.flush()

    _set_flag(monkeypatch, True)
    rows = brs._load_local_view(db, user_id=None)
    assert "FTHM" in _tickers(rows)  # coverage resumes once the lane is done


# ── 6. stamp decision helper ────────────────────────────────────────────────
def test_stamp_momentum_on_synced_trade(db, monkeypatch):
    _set_flag(monkeypatch, True)
    # symbol with a recent live momentum session -> stamp momentum_neural
    _live_session(db, symbol="CRVO", state=aq.STATE_WATCHING_LIVE)
    db.flush()
    assert bsvc._synced_position_management_scope(db, "CRVO") == "momentum_neural"
    # a symbol with NO momentum session -> the broker_sync default
    assert bsvc._synced_position_management_scope(db, "AAPL") == "broker_sync"
    # flag OFF -> always broker_sync (byte-identical), even with a session present
    _set_flag(monkeypatch, False)
    assert bsvc._synced_position_management_scope(db, "CRVO") == "broker_sync"


# ── 7. equity / non-momentum trades are untouched ───────────────────────────
def test_equity_untouched(db, monkeypatch):
    _set_flag(monkeypatch, True)
    # auto_trader_v1 + plain broker_sync open trades, no momentum session anywhere
    _open_trade(db, ticker="MSFT", management_scope="auto_trader_v1")
    _open_trade(db, ticker="NVDA", management_scope="broker_sync")
    _open_trade(db, ticker="TSLA", management_scope=None)
    db.flush()

    rows = brs._load_local_view(db, user_id=None)
    t = _tickers(rows)
    assert {"MSFT", "NVDA", "TSLA"}.issubset(t)  # all covered, exclusion never fires

    # and a momentum_neural row WITHOUT any live session is still covered
    _open_trade(db, ticker="ORPHANED", management_scope="momentum_neural")
    db.flush()
    rows2 = brs._load_local_view(db, user_id=None)
    assert "ORPHANED" in _tickers(rows2)  # no non-terminal session -> not excluded


# ── 8. cancel-attribution label reflects the TRUE initiator ──────────────────
# BTCT sess 9871 (2026-06-29): an AUTOMATED monitor cancel (post-recycle) logged
# session_cancelled {"by": "operator"} even though no human cancelled it. The "by"
# field must record the caller's real identity: the default (automated) callers
# record "automation_monitor"; only the operator HTTP endpoint records "operator".
def test_cancel_default_records_automation_monitor(db, monkeypatch):
    """An automated caller (the default — auto-arm reaper, confirm-block release,
    stale-session reaper) must NOT be mislabeled as an operator action."""
    _set_flag(monkeypatch, True)
    sess = _live_session(db, symbol="BTCT", state=aq.STATE_WATCHING_LIVE)
    _patch_adapter(
        monkeypatch,
        _fake_adapter(filled_size=0.0, status="cancelled", symbol="BTCT"),
    )

    res = aq.cancel_automation_session(db, user_id=None, session_id=sess.id)

    assert res["ok"] is True
    payload = _event_payload(db, sess.id, "session_cancelled")
    assert payload is not None
    assert payload["by"] == "automation_monitor"
    assert payload["by"] != "operator"  # the 9871 mislabel must not recur


def test_cancel_operator_records_operator(db, monkeypatch):
    """A genuine operator-initiated cancel (the HTTP endpoint passes cancelled_by=
    'operator') still records 'operator'."""
    _set_flag(monkeypatch, True)
    sess = _live_session(db, symbol="BTCT", state=aq.STATE_WATCHING_LIVE)
    _patch_adapter(
        monkeypatch,
        _fake_adapter(filled_size=0.0, status="cancelled", symbol="BTCT"),
    )

    res = aq.cancel_automation_session(
        db, user_id=None, session_id=sess.id, cancelled_by="operator"
    )

    assert res["ok"] is True
    payload = _event_payload(db, sess.id, "session_cancelled")
    assert payload is not None
    assert payload["by"] == "operator"


def test_adopt_on_cancel_records_caller_identity(db, monkeypatch):
    """The adopt-on-cancel-fill path also attributes to the real caller (the
    entry_adopted_on_cancel event's "by"), not a hardcoded 'operator'."""
    _set_flag(monkeypatch, True)
    sess = _live_session(db, symbol="BTCT", state=aq.STATE_WATCHING_LIVE)
    _patch_adapter(
        monkeypatch,
        _fake_adapter(filled_size=100.0, status="filled", symbol="BTCT"),
    )

    res = aq.cancel_automation_session(db, user_id=None, session_id=sess.id)

    assert res.get("adopted") is True
    payload = _event_payload(db, sess.id, "entry_adopted_on_cancel")
    assert payload is not None
    assert payload["by"] == "automation_monitor"  # default automated identity
