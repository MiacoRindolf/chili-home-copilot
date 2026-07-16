"""Crash/restart boundaries for the momentum emergency-exit state machine.

These tests deliberately exercise the broker-identity and broker-position seams.  An
emergency signal may be delivered more than once and a broker acknowledgement may be
lost, but one unresolved authority must never become two close orders.  Conversely, a
pause or missing strategy quote must never suppress an already-authorized close.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    acquire_action_claim,
    advance_owner_transport,
    lease_owner_transport,
    prepare_deadman_close_handoff,
    read_action_claim,
    update_action_claim_phase,
)
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_CANCELLED,
    STATE_LIVE_ENTERED,
    STATE_LIVE_EXITED,
    STATE_LIVE_PENDING_ENTRY,
)
from app.services.trading.momentum_neural.persistence import (
    create_trading_automation_session,
)
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.momentum_neural.session_lifecycle import OPERATOR_PAUSE_KEY
from app.services.trading.venue.protocol import (
    FreshnessMeta,
    NormalizedOrder,
    NormalizedTicker,
)

from tests.test_momentum_paper_runner import _seed_live_eligible_row, _uid


TEST_ALPACA_ACCOUNT_ID = "acct-emergency-exit-test"


def _fresh() -> FreshnessMeta:
    return FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc),
        max_age_seconds=2.0,
    )


def _order(
    *,
    oid: str,
    cid: str,
    symbol: str,
    side: str,
    status: str,
    filled: float,
    avg: float | None,
    qty: float | None = None,
    order_type: str = "market",
    time_in_force: str = "day",
    extended_hours: bool = False,
    position_intent: str | None = None,
    limit_price: float | None = None,
    raw_overrides: dict[str, Any] | None = None,
) -> NormalizedOrder:
    raw = {
        "qty": (None if qty is None else str(qty)),
        "limit_price": limit_price,
        "time_in_force": time_in_force,
        "extended_hours": extended_hours,
        "position_intent": (
            position_intent
            or ("sell_to_close" if side == "sell" else "buy_to_open")
        ),
    }
    if raw_overrides:
        raw.update(raw_overrides)
    return NormalizedOrder(
        order_id=oid,
        client_order_id=cid,
        product_id=symbol,
        side=side,
        status=status,
        order_type=order_type,
        filled_size=filled,
        average_filled_price=avg,
        raw=raw,
    )


class _ScriptedAlpaca:
    """Small strict adapter double; missing broker facts stay missing, not MagicMock."""

    def __init__(self, *, positions: list[float | None] | None = None) -> None:
        self._positions = deque(positions or [None])
        self._last_position = self._positions[-1]
        self.orders: dict[str, Any] = {}
        self.cid_orders: dict[str, Any] = {}
        self.truth: dict[str, Any] = {}
        self.market_results: deque[Any] = deque()
        self.limit_results: deque[Any] = deque()
        self.execution_bbo: Any = None
        self.market_calls: list[dict[str, Any]] = []
        self.limit_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[str] = []
        self.cancel_result: Any = True
        self.execution_bbo_calls: list[tuple[str, float]] = []
        self.bound_account_id: str | None = None

    @staticmethod
    def _resolve(value: Any, owner: "_ScriptedAlpaca", key: str) -> Any:
        if callable(value):
            return value(owner, key)
        if isinstance(value, deque):
            if len(value) > 1:
                return value.popleft()
            return value[0] if value else None
        return value

    def is_enabled(self) -> bool:
        return True

    def bind_account_id(self, expected_account_id: str) -> bool:
        if str(expected_account_id or "").strip() != TEST_ALPACA_ACCOUNT_ID:
            return False
        self.bound_account_id = TEST_ALPACA_ACCOUNT_ID
        return True

    def get_account_snapshot(self) -> dict[str, Any]:
        return {
            "ok": True,
            "paper": True,
            "account_id": TEST_ALPACA_ACCOUNT_ID,
        }

    def get_position_quantity(self, _product_id: str) -> float | None:
        if self._positions:
            self._last_position = self._positions.popleft()
        return self._last_position

    def get_order(self, order_id: str):
        value = self._resolve(self.orders.get(order_id), self, order_id)
        return value, _fresh()

    def get_order_by_client_order_id(self, client_order_id: str):
        value = self._resolve(self.cid_orders.get(client_order_id), self, client_order_id)
        return value, _fresh()

    def get_order_by_client_order_id_truth(self, client_order_id: str) -> dict[str, Any]:
        value = self._resolve(self.truth.get(client_order_id, "absent"), self, client_order_id)
        if isinstance(value, NormalizedOrder):
            return {"readable": True, "found": True, "order": value}
        if value == "absent":
            return {"readable": True, "found": False, "order": None}
        return {"readable": False, "found": None, "order": None}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        self.cancel_calls.append(order_id)
        return {"ok": True}

    def cancel_order_by_id(self, order_id: str) -> bool:
        self.cancel_calls.append(order_id)
        value = self.cancel_result
        if isinstance(value, BaseException):
            raise value
        return bool(value)

    def place_market_order(self, **kwargs: Any) -> dict[str, Any]:
        self.market_calls.append(dict(kwargs))
        value = self.market_results.popleft() if self.market_results else {
            "ok": True,
            "order_id": f"exit-{len(self.market_calls)}",
            "client_order_id": kwargs.get("client_order_id"),
        }
        return self._resolve(value, self, str(kwargs.get("client_order_id") or ""))

    def place_limit_order_gtc(self, **kwargs: Any) -> dict[str, Any]:
        self.limit_calls.append(dict(kwargs))
        value = self.limit_results.popleft() if self.limit_results else {
            "ok": True,
            "order_id": f"limit-exit-{len(self.limit_calls)}",
            "client_order_id": kwargs.get("client_order_id"),
        }
        return self._resolve(value, self, str(kwargs.get("client_order_id") or ""))

    def get_execution_bbo(self, product_id: str, *, max_age_seconds: float):
        self.execution_bbo_calls.append((product_id, max_age_seconds))
        return self._resolve(self.execution_bbo, self, product_id)


def _seed_session(
    db,
    *,
    symbol: str,
    state: str = STATE_LIVE_ENTERED,
    quantity: float | None = 100.0,
    avg_entry_price: float | None = 10.0,
    le_extra: dict[str, Any] | None = None,
    paused: bool = False,
):
    variant_id, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, f"emergency_{symbol}")
    le: dict[str, Any] = {
        "entry_sizing": {"model": "risk_first", "stop_distance": 0.20},
        "entry_stop_atr_pct": 0.02,
        "admission_viability_score": 0.9,
    }
    if quantity is not None:
        le["position"] = {
            "product_id": symbol,
            "side": "long",
            "quantity": quantity,
            "original_quantity": quantity,
            "avg_entry_price": avg_entry_price,
            "notional_usd": (
                quantity * avg_entry_price if avg_entry_price is not None else None
            ),
            "opened_at_utc": datetime.now(timezone.utc).isoformat(),
            "high_water_mark": avg_entry_price,
            "stop_price": 8.0,
            "target_price": 12.0,
        }
    le.update(dict(le_extra or {}))
    snapshot: dict[str, Any] = {
        "alpaca_account_scope": "alpaca:paper",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        RISK_SNAPSHOT_KEY: {"allowed": True},
        "momentum_risk_policy_summary": {
            "disable_live_if_governance_inhibit": True,
        },
        "momentum_policy_caps": {
            "max_notional_per_trade_usd": 5_000.0,
            "max_hold_seconds": 86_400,
        },
        "momentum_live_execution": le,
    }
    if paused:
        snapshot[OPERATOR_PAUSE_KEY] = {
            "active": True,
            "paused_at_utc": datetime.now(timezone.utc).isoformat(),
            "resume_state": state,
        }
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        venue="alpaca",
        execution_family="alpaca_spot",
        mode="live",
        symbol=symbol,
        variant_id=variant_id,
        state=state,
        risk_snapshot_json=snapshot,
        correlation_id=f"emergency-{symbol}",
    )
    db.commit()
    db.refresh(sess)
    return sess


def _run_tick(db, sess, adapter: _ScriptedAlpaca):
    _ensure_retained_entry_owner(db, sess)
    # Keep this suite independent of the module-global every-fifth-tick diagnostic.
    lr._reconcile_counters.pop(int(sess.id), None)
    out = lr.tick_live_session(db, int(sess.id), adapter_factory=lambda: adapter)
    db.commit()
    db.refresh(sess)
    return out


def _ensure_retained_entry_owner(db, sess) -> dict[str, Any]:
    """Give focused emergency tests the owner generation production requires.

    Other suites import ``_seed_session`` and install their own exact claim, so this
    is deliberately called only by this module's tick/direct-submit helpers.
    """
    snapshot = dict(sess.risk_snapshot_json or {})
    existing_token = str(snapshot.get("alpaca_symbol_claim_token") or "").strip()
    if existing_token:
        return {
            "symbol": str(sess.symbol).strip().upper(),
            "claim_token": existing_token,
            "owner_session_id": int(sess.id),
            "account_scope": "alpaca:paper",
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        }

    le = dict(snapshot.get("momentum_live_execution") or {})
    symbol = str(sess.symbol).strip().upper()
    token = f"emergency-owner-{uuid.uuid4().hex}"
    cid = str(le.get("entry_client_order_id") or f"entry-owner-{sess.id}").strip()
    entry_oid = str(le.get("entry_order_id") or "").strip()
    position = le.get("position") if isinstance(le.get("position"), dict) else {}
    quantity = float(
        position.get("original_quantity")
        or position.get("quantity")
        or 100.0
    )
    request = {
        "account_scope": "alpaca:paper",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        "product_id": symbol,
        "side": "buy",
        "base_size": str(quantity),
        "client_order_id": cid,
        "position_intent": "buy_to_open",
        "order_type": "market",
        "time_in_force": "day",
        "extended_hours": False,
        "limit_price": None,
    }
    snapshot["alpaca_symbol_claim_token"] = token
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.flush()
    acquired = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=int(sess.id),
        client_order_id=cid,
        metadata={
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            "order_request": request,
            "order_role": "primary",
        },
        account_scope="alpaca:paper",
    )
    assert acquired["ok"] is True
    if entry_oid:
        assert update_action_claim_phase(
            db,
            symbol=symbol,
            claim_token=token,
            phase="submitted",
            client_order_id=cid,
            broker_order_id=entry_oid,
            metadata={"order_role": "primary"},
            account_scope="alpaca:paper",
        )
    authority = le.get("emergency_exit_authority")
    if isinstance(authority, dict) and str(authority.get("order_id") or "").strip():
        exit_cid = str(authority.get("client_order_id") or "").strip()
        exit_oid = str(authority.get("order_id") or "").strip()
        exit_request = dict(authority.get("order_request") or {})
        exit_request["account_scope"] = "alpaca:paper"
        exit_request["alpaca_account_id"] = TEST_ALPACA_ACCOUNT_ID
        lease_token = f"existing-emergency-{uuid.uuid4().hex}"
        leased = lease_owner_transport(
            db,
            symbol=symbol,
            claim_token=token,
            owner_session_id=int(sess.id),
            account_scope="alpaca:paper",
            alpaca_account_id=TEST_ALPACA_ACCOUNT_ID,
            transport_kind="emergency_exit",
            client_order_id=exit_cid,
            order_request=exit_request,
            lease_token=lease_token,
        )
        assert leased["ok"] is True
        assert advance_owner_transport(
            db,
            symbol=symbol,
            claim_token=token,
            owner_session_id=int(sess.id),
            account_scope="alpaca:paper",
            alpaca_account_id=TEST_ALPACA_ACCOUNT_ID,
            client_order_id=exit_cid,
            lease_token=lease_token,
            phase="submitted",
            broker_order_id=exit_oid,
        )
        readable, owner_claim = read_action_claim(
            db,
            symbol=symbol,
            account_scope="alpaca:paper",
        )
        assert readable and owner_claim is not None
        le["alpaca_active_exit_owner_transport"] = dict(
            owner_claim["metadata"]["owner_transport"]
        )
        snapshot = {
            **snapshot,
            "momentum_live_execution": dict(le),
        }
        sess.risk_snapshot_json = snapshot
        db.add(sess)
    db.commit()
    db.refresh(sess)
    if isinstance(authority, dict) and str(authority.get("order_id") or "").strip():
        assert isinstance(
            sess.risk_snapshot_json["momentum_live_execution"].get(
                "alpaca_active_exit_owner_transport"
            ),
            dict,
        )
    return {
        "symbol": symbol,
        "claim_token": token,
        "owner_session_id": int(sess.id),
        "account_scope": "alpaca:paper",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
    }


@pytest.fixture(autouse=True)
def _emergency_test_boundaries(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        TEST_ALPACA_ACCOUNT_ID,
        raising=False,
    )
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda _family: True)
    monkeypatch.setattr(lr, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_finalize_live_decision_after_exit", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda _symbol: "regular",
    )


def test_duplicate_kill_signal_reuses_one_authority_and_never_resubmits(
    db, monkeypatch
) -> None:
    sess = _seed_session(db, symbol="EKIL")
    adapter = _ScriptedAlpaca(positions=[100.0, 100.0, 100.0, 100.0])
    adapter.market_results.append(
        lambda owner, cid: {
            "ok": True,
            "order_id": "exit-kill-1",
            "client_order_id": cid,
        }
    )
    adapter.orders["exit-kill-1"] = lambda owner, _oid: _order(
        oid="exit-kill-1",
        cid=owner.market_calls[0]["client_order_id"],
        symbol="EKIL",
        side="sell",
        status="open",
        filled=0.0,
        avg=None,
        qty=100.0,
    )
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: True)

    first = _run_tick(db, sess, adapter)
    first_le = sess.risk_snapshot_json["momentum_live_execution"]
    first_cid = first_le["emergency_exit_authority"]["client_order_id"]
    second = _run_tick(db, sess, adapter)
    second_le = sess.risk_snapshot_json["momentum_live_execution"]

    assert first == {"ok": True, "blocked": True, "reason": "kill_switch"}
    assert second["emergency_authority"] == "kill_switch_flatten"
    assert second["flattened"] is False
    assert len(adapter.market_calls) == 1
    assert adapter.limit_calls == []
    assert second_le["emergency_exit_authority"]["client_order_id"] == first_cid
    assert second_le["emergency_exit_authority"]["order_id"] == "exit-kill-1"
    assert second_le["emergency_exit_authority"]["phase"] == "submitted"
    assert sess.state == STATE_LIVE_ENTERED


def test_accepted_timeout_is_recovered_by_exact_cid_on_next_tick_without_resubmit(
    db, monkeypatch
) -> None:
    sess = _seed_session(
        db,
        symbol="ETMO",
        le_extra={"operator_flatten_requested_utc": "2026-07-13T18:00:00+00:00"},
    )
    adapter = _ScriptedAlpaca(positions=[100.0, 100.0, 100.0, 0.0])
    adapter.market_results.append(
        lambda _owner, cid: {
            "ok": False,
            "error": "ReadTimeout after submit",
            "client_order_id": cid,
            "submit_outcome": "indeterminate",
        }
    )
    visible = {"now": False}

    def _truth(owner: _ScriptedAlpaca, cid: str):
        if not visible["now"]:
            return "absent"
        return _order(
            oid="exit-timeout-1",
            cid=cid,
            symbol="ETMO",
            side="sell",
            status="filled",
            filled=100.0,
            avg=9.90,
            qty=100.0,
        )

    # The key is unknown until the deterministic authority is created; use a
    # mapping with a dynamic fallback through these method wrappers.
    adapter.get_order_by_client_order_id_truth = lambda cid: (
        {"readable": True, "found": False, "order": None}
        if not visible["now"]
        else {"readable": True, "found": True, "order": _truth(adapter, cid)}
    )
    adapter.get_order_by_client_order_id = lambda cid: (
        (_truth(adapter, cid), _fresh()) if visible["now"] else (None, _fresh())
    )
    adapter.orders["exit-timeout-1"] = lambda owner, _oid: _truth(
        owner, owner.market_calls[0]["client_order_id"]
    )
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)

    first = _run_tick(db, sess, adapter)
    first_le = sess.risk_snapshot_json["momentum_live_execution"]
    assert first["operator_flatten"] is False
    assert first_le["emergency_exit_authority"]["phase"] == "submit_indeterminate"
    assert "operator_flatten_requested_utc" in first_le
    assert len(adapter.market_calls) == 1

    visible["now"] = True
    second = _run_tick(db, sess, adapter)
    second_le = sess.risk_snapshot_json["momentum_live_execution"]

    assert second["flattened"] is True
    assert len(adapter.market_calls) == 1
    assert second_le["emergency_exit_authority"]["order_id"] == "exit-timeout-1"
    assert second_le["emergency_exit_authority"]["phase"] == "resolved"
    assert "operator_flatten_requested_utc" not in second_le
    assert second_le["position"] is None
    assert sess.state == STATE_LIVE_EXITED


def test_pending_entry_cancel_race_adopts_late_fill_before_one_close(
    db, monkeypatch
) -> None:
    sess = _seed_session(
        db,
        symbol="ERAC",
        state=STATE_LIVE_PENDING_ENTRY,
        quantity=None,
        le_extra={
            "entry_order_id": "entry-race-1",
            "entry_client_order_id": "entry-race-cid",
            "entry_submitted": True,
            "operator_flatten_requested_utc": "2026-07-13T18:01:00+00:00",
        },
    )
    adapter = _ScriptedAlpaca(positions=[20.0, 20.0])
    adapter.orders["entry-race-1"] = deque(
        [
            _order(
                oid="entry-race-1",
                cid="entry-race-cid",
                symbol="ERAC",
                side="buy",
                status="partially_filled",
                filled=10.0,
                avg=10.05,
                qty=100.0,
            ),
            _order(
                oid="entry-race-1",
                cid="entry-race-cid",
                symbol="ERAC",
                side="buy",
                status="cancelled",
                filled=20.0,
                avg=10.05,
                qty=100.0,
            ),
        ]
    )
    adapter.market_results.append(
        lambda _owner, cid: {
            "ok": True,
            "order_id": "exit-race-1",
            "client_order_id": cid,
        }
    )
    adapter.orders["exit-race-1"] = lambda owner, _oid: _order(
        oid="exit-race-1",
        cid=owner.market_calls[0]["client_order_id"],
        symbol="ERAC",
        side="sell",
        status="open",
        filled=0.0,
        avg=None,
        qty=20.0,
    )
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)

    first = _run_tick(db, sess, adapter)
    assert first["operator_flatten"] is False
    assert len(adapter.market_calls) == 1
    assert float(adapter.market_calls[0]["base_size"]) == pytest.approx(20.0)
    assert adapter.market_calls[0]["position_intent"] == "sell_to_close"
    assert sess.state == STATE_LIVE_ENTERED

    second = _run_tick(db, sess, adapter)
    le = sess.risk_snapshot_json["momentum_live_execution"]

    assert second["flattened"] is False
    assert len(adapter.market_calls) == 1  # exact terminal reread never duplicates the close
    assert float(adapter.market_calls[0]["base_size"]) == pytest.approx(20.0)
    assert adapter.market_calls[0]["position_intent"] == "sell_to_close"
    assert le["position"]["quantity"] == pytest.approx(20.0)
    assert le["position"]["avg_entry_price"] == pytest.approx(10.05)
    assert sess.state == STATE_LIVE_ENTERED


def test_pending_entry_exact_terminal_zero_fill_cancels_without_close(
    db, monkeypatch
) -> None:
    sess = _seed_session(
        db,
        symbol="EZRO",
        state=STATE_LIVE_PENDING_ENTRY,
        quantity=None,
        le_extra={
            "entry_order_id": "entry-zero-1",
            "entry_client_order_id": "entry-zero-cid",
            "entry_submitted": True,
            "operator_flatten_requested_utc": "2026-07-13T18:02:00+00:00",
        },
    )
    adapter = _ScriptedAlpaca(positions=[0.0])
    adapter.orders["entry-zero-1"] = _order(
        oid="entry-zero-1",
        cid="entry-zero-cid",
        symbol="EZRO",
        side="buy",
        status="cancelled",
        filled=0.0,
        avg=None,
        qty=100.0,
    )
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)

    out = _run_tick(db, sess, adapter)
    le = sess.risk_snapshot_json["momentum_live_execution"]

    assert out["operator_flatten"] is True
    assert sess.state == STATE_LIVE_CANCELLED
    assert le["position"] is None
    assert le["entry_orders_resolved"]["entry-zero-1"] == "void"
    assert adapter.market_calls == []
    assert adapter.limit_calls == []


def test_paused_session_with_explicit_authority_still_dispatches_close(
    db, monkeypatch
) -> None:
    sess = _seed_session(
        db,
        symbol="EPAU",
        paused=True,
        le_extra={"operator_flatten_requested_utc": "2026-07-13T18:03:00+00:00"},
    )
    adapter = _ScriptedAlpaca(positions=[100.0, 100.0])
    adapter.market_results.append(
        lambda _owner, cid: {
            "ok": True,
            "order_id": "exit-paused-1",
            "client_order_id": cid,
        }
    )
    adapter.orders["exit-paused-1"] = lambda owner, _oid: _order(
        oid="exit-paused-1",
        cid=owner.market_calls[0]["client_order_id"],
        symbol="EPAU",
        side="sell",
        status="open",
        filled=0.0,
        avg=None,
        qty=100.0,
    )
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)

    out = _run_tick(db, sess, adapter)
    le = sess.risk_snapshot_json["momentum_live_execution"]

    assert out["operator_flatten"] is False  # accepted, awaiting broker fill
    assert len(adapter.market_calls) == 1
    assert adapter.market_calls[0]["position_intent"] == "sell_to_close"
    assert "operator_flatten_requested_utc" in le
    assert le["emergency_exit_authority"]["phase"] == "submitted"
    assert sess.state == STATE_LIVE_ENTERED


class _AgeSequence:
    def __init__(self, *ages: float) -> None:
        self._ages = deque(ages)
        self._last = ages[-1]

    def age_seconds(self) -> float:
        if self._ages:
            self._last = self._ages.popleft()
        return self._last


def _direct_session(symbol: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=91001,
        state=STATE_LIVE_ENTERED,
        mode="live",
        user_id=None,
        symbol=symbol,
        execution_family="alpaca_spot",
        correlation_id=f"direct-{symbol}",
        risk_snapshot_json={},
    )


def _direct_submit(
    db,
    monkeypatch,
    *,
    session: str,
    quote_independent: bool,
    freshness: Any = None,
    quote: bool = True,
    side_long: bool = True,
    deadman: bool = False,
    positions: list[float | None] | None = None,
    cancel_result: Any = True,
    close_only: bool = False,
) -> tuple[dict[str, Any], _ScriptedAlpaca, dict[str, Any]]:
    symbol = "EDIR"
    sess = _seed_session(db, symbol=symbol, quantity=10.0, avg_entry_price=10.0)
    le = {
        "side_long": side_long,
        "position": {
            "product_id": symbol,
            "side": "long" if side_long else "short",
            "quantity": 10.0,
            "avg_entry_price": 10.0,
        }
    }
    authority = None
    snapshot = dict(sess.risk_snapshot_json or {})
    snapshot["momentum_live_execution"] = le
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.commit()
    owner_context = _ensure_retained_entry_owner(db, sess)
    snapshot = dict(sess.risk_snapshot_json or {})
    if quote_independent:
        # Quote-independent liquidation is valid only behind the durable
        # emergency authority used by the real caller.  The submit seam now
        # freezes that authority's complete request before broker transport.
        authority = lr._emergency_exit_authority(
            sess,
            le,
            reason="operator_flatten",
        )
    if close_only:
        assert authority is not None
        authority.update(
            {
                "client_order_id": "direct-exit-cid",
                "close_claim_token": "close-claim-1",
                "close_claim_action": "orphan_flatten",
                "max_close_qty": 10.0,
                "broker_unattributed_quantity_floor": 0.0,
            }
        )
        le["alpaca_entries_quarantined"] = True
        snapshot["alpaca_close_only_recertification"] = {
            "scope": "alpaca:paper",
            "entries_quarantined": True,
            "close_claim_token": "close-claim-1",
            "close_claim_action": "orphan_flatten",
            "close_client_order_id": "direct-exit-cid",
            "max_close_qty": 10.0,
            "broker_position_qty_at_recertification": 10.0,
        }
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.commit()
    adapter = _ScriptedAlpaca(
        positions=(positions if positions is not None else [10.0 if side_long else -10.0])
    )
    adapter.cancel_result = cancel_result
    if deadman:
        assert authority is not None
        deadman_cid = "direct-deadman-cid"
        deadman_oid = "deadman-1"
        deadman_request = {
            "account_scope": "alpaca:paper",
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            "product_id": symbol,
            "side": "sell",
            "base_size": "10",
            "client_order_id": deadman_cid,
            "position_intent": "sell_to_close",
            "order_type": "stop",
            "time_in_force": "gtc",
            "extended_hours": False,
            "stop_price": 9.50,
        }
        lease_token = f"direct-deadman-{uuid.uuid4().hex}"
        leased = lease_owner_transport(
            db,
            **owner_context,
            transport_kind="deadman",
            client_order_id=deadman_cid,
            order_request=deadman_request,
            lease_token=lease_token,
        )
        assert leased["ok"] is True
        assert advance_owner_transport(
            db,
            **owner_context,
            client_order_id=deadman_cid,
            lease_token=lease_token,
            phase="submitted",
            broker_order_id=deadman_oid,
        )
        readable, owner_claim = read_action_claim(
            db,
            symbol=symbol,
            account_scope="alpaca:paper",
        )
        assert readable and owner_claim is not None
        deadman_transport = dict(owner_claim["metadata"]["owner_transport"])
        le["deadman_stop"] = {
            "order_id": deadman_oid,
            "client_order_id": deadman_cid,
            "stop_price": 9.50,
            "qty": 10.0,
            "phase": "submitted",
            "owner_transport": deadman_transport,
        }
        successor_request = {
            "account_scope": "alpaca:paper",
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            "product_id": symbol,
            "side": "sell",
            "base_size": "10",
            "client_order_id": str(authority["client_order_id"]),
            "position_intent": "sell_to_close",
            "order_type": "market",
            "time_in_force": "day",
            "extended_hours": False,
            "limit_price": None,
        }
        prepared = prepare_deadman_close_handoff(
            db,
            **owner_context,
            handoff_token=f"direct-handoff-{uuid.uuid4().hex}",
            deadman_client_order_id=deadman_cid,
            deadman_broker_order_id=deadman_oid,
            deadman_order_request=deadman_request,
            successor_transport_kind="emergency_exit",
            successor_intent=successor_request,
            reason="operator_flatten",
        )
        assert prepared["ok"] is True
        active_deadman = _order(
            oid=deadman_oid,
            cid=deadman_cid,
            symbol=symbol,
            side="sell",
            status="open",
            filled=0.0,
            avg=None,
            qty=10.0,
            order_type="stop",
            time_in_force="gtc",
            position_intent="sell_to_close",
            raw_overrides={"stop_price": 9.50},
        )
        terminal_deadman = _order(
            oid=deadman_oid,
            cid=deadman_cid,
            symbol=symbol,
            side="sell",
            status=("open" if cancel_result is False else "cancelled"),
            filled=0.0,
            avg=None,
            qty=10.0,
            order_type="stop",
            time_in_force="gtc",
            position_intent="sell_to_close",
            raw_overrides={"stop_price": 9.50},
        )
        adapter.truth[deadman_cid] = deque([active_deadman, terminal_deadman])
        adapter.cid_orders[deadman_cid] = terminal_deadman
        adapter.orders[deadman_oid] = terminal_deadman
        snapshot = dict(sess.risk_snapshot_json or {})
        snapshot["momentum_live_execution"] = le
        sess.risk_snapshot_json = snapshot
        db.add(sess)
        db.commit()
    meta = freshness if freshness is not None else _fresh()
    adapter.execution_bbo = (
        (
            NormalizedTicker(
                product_id=symbol,
                bid=9.95,
                ask=9.97,
                mid=9.96,
                freshness=meta,
                raw={"feed": "iqfeed_l1", "tape_row_id": 9901},
            ),
            meta,
        )
        if quote
        else (None, meta)
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda _symbol: session,
    )
    monkeypatch.setattr(lr, "_record_live_exit_intent_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_emit", lambda *a, **k: None)
    out = lr._submit_live_market_exit(
        db,
        sess,
        adapter,
        le=le,
        product_id=symbol,
        quantity=10.0,
        client_order_id=(
            str(authority["client_order_id"])
            if authority is not None
            else "direct-exit-cid"
        ),
        reason="operator_flatten" if quote_independent else "stop",
        bid=None if quote_independent else 9.95,
        ask=None if quote_independent else 9.97,
        mid=None if quote_independent else 9.96,
        quote_independent_authority=quote_independent,
        emergency_exit_authority=authority,
    )
    return out, adapter, le


def test_ordinary_exit_quote_expiring_at_literal_post_is_blocked(
    db, monkeypatch,
) -> None:
    out, adapter, le = _direct_submit(
        db, monkeypatch,
        session="regular",
        quote_independent=False,
        freshness=_AgeSequence(0.10, 3.10),
    )

    assert out["error"] == "execution_bbo_stale_at_exit_place"
    assert out["pre_place_blocked"] is True
    assert adapter.market_calls == []
    assert adapter.limit_calls == []
    assert le["exit_submit_attempts"] == 0
    assert "exit_next_retry_at_utc" not in le


def test_rth_emergency_uses_market_sell_to_close_without_quote(
    db, monkeypatch,
) -> None:
    out, adapter, _le = _direct_submit(
        db, monkeypatch,
        session="regular",
        quote_independent=True,
        quote=False,
    )

    assert out["ok"] is True, out
    assert len(adapter.market_calls) == 1
    assert adapter.limit_calls == []
    assert adapter.execution_bbo_calls == []
    assert adapter.market_calls[0]["side"] == "sell"
    assert adapter.market_calls[0]["position_intent"] == "sell_to_close"


def test_rth_short_emergency_is_quarantined_without_order_post(db, monkeypatch) -> None:
    out, adapter, _le = _direct_submit(
        db, monkeypatch,
        session="regular",
        quote_independent=True,
        quote=False,
        side_long=False,
    )

    assert out["ok"] is False
    assert out["pre_place_blocked"] is True
    assert out["error"] == "alpaca_emergency_close_request_invalid"
    assert adapter.market_calls == []
    assert adapter.limit_calls == []


@pytest.mark.parametrize("session", ["premarket", "afterhours"])
def test_extended_hours_emergency_uses_fresh_marketable_limit_and_close_intent(
    db, monkeypatch, session: str
) -> None:
    out, adapter, _le = _direct_submit(
        db, monkeypatch,
        session=session,
        quote_independent=True,
        quote=True,
    )

    assert out["ok"] is True, out
    assert adapter.market_calls == []
    assert len(adapter.limit_calls) == 1
    kwargs = adapter.limit_calls[0]
    assert kwargs["extended_hours"] is True
    assert kwargs["position_intent"] == "sell_to_close"
    assert kwargs["side"] == "sell"
    assert float(kwargs["limit_price"]) < 9.95  # aggressively marketable vs fresh bid
    # One quote selects the marketable limit and one literal pre-POST reread
    # proves the frozen instruction is still executable.
    assert adapter.execution_bbo_calls == [("EDIR", 2.0), ("EDIR", 2.0)]


def test_extended_hours_emergency_missing_quote_fails_closed_without_post(
    db, monkeypatch,
) -> None:
    out, adapter, le = _direct_submit(
        db, monkeypatch,
        session="premarket",
        quote_independent=True,
        quote=False,
    )

    assert out["pre_place_blocked"] is True
    assert out["error"] == "execution_bbo_unavailable"
    assert adapter.market_calls == []
    assert adapter.limit_calls == []
    assert le["emergency_exit_extended_bbo"]["reason"] == "execution_bbo_unavailable"


def test_closed_session_emergency_stays_unresolved_without_broker_post(
    db, monkeypatch,
) -> None:
    out, adapter, _le = _direct_submit(
        db, monkeypatch,
        session="closed",
        quote_independent=True,
        quote=True,
    )

    assert out["error"] == "alpaca_equity_exit_session_closed"
    assert out["pre_place_blocked"] is True
    assert adapter.market_calls == []
    assert adapter.limit_calls == []
    assert adapter.execution_bbo_calls == []


def test_closed_session_keeps_deadman_stop_and_never_calls_cancel(db, monkeypatch) -> None:
    out, adapter, le = _direct_submit(
        db, monkeypatch,
        session="closed",
        quote_independent=True,
        quote=True,
        deadman=True,
    )

    assert out["error"] == "alpaca_equity_exit_session_closed"
    assert adapter.cancel_calls == []
    assert le["deadman_stop"]["order_id"] == "deadman-1"


def test_missing_extended_bbo_keeps_deadman_stop_and_never_calls_cancel(
    db, monkeypatch,
) -> None:
    out, adapter, le = _direct_submit(
        db, monkeypatch,
        session="premarket",
        quote_independent=True,
        quote=False,
        deadman=True,
    )

    assert out["pre_place_blocked"] is True
    assert adapter.cancel_calls == []
    assert le["deadman_stop"]["order_id"] == "deadman-1"


def test_deadman_cancel_failure_blocks_exit_transport(db, monkeypatch) -> None:
    out, adapter, le = _direct_submit(
        db, monkeypatch,
        session="regular",
        quote_independent=True,
        quote=False,
        deadman=True,
        cancel_result=False,
    )

    assert out["error"] == "deadman_cancel_not_terminal"
    assert adapter.cancel_calls == ["deadman-1"]
    assert adapter.market_calls == []
    assert adapter.limit_calls == []
    assert le["deadman_stop"]["order_id"] == "deadman-1"
    assert le["exit_submit_attempts"] == 0


def test_deadman_release_blocks_changed_quantity_generation_before_submit(
    db, monkeypatch,
) -> None:
    out, adapter, le = _direct_submit(
        db, monkeypatch,
        session="regular",
        quote_independent=True,
        quote=False,
        deadman=True,
        positions=[10.0, 6.0],
    )

    assert out["ok"] is False, out
    assert out["error"] == "deadman_successor_quantity_generation_mismatch"
    assert out["pre_place_blocked"] is True
    assert adapter.cancel_calls == ["deadman-1"]
    assert adapter.market_calls == []
    assert adapter.limit_calls == []
    assert "deadman_stop" not in le
    assert le["deadman_stop_history"][-1]["terminal_status"] == "cancelled"
    assert "operator_flatten_requested_utc" in le


def test_close_only_literal_position_drop_blocks_without_order_post(
    db, monkeypatch,
) -> None:
    out, adapter, le = _direct_submit(
        db, monkeypatch,
        session="regular",
        quote_independent=True,
        quote=False,
        close_only=True,
        positions=[10.0, 0.0],
    )

    assert out["error"] == "alpaca_close_only_no_attributable_quantity_at_post"
    assert out["pre_place_blocked"] is True
    assert adapter.market_calls == []
    assert adapter.limit_calls == []
    assert le["exit_submit_attempts"] == 0


def test_operator_and_eod_reasons_share_one_unresolved_deterministic_authority() -> None:
    sess = SimpleNamespace(id=77123)
    le = {"eod_flatten_requested_utc": "2026-07-13T19:55:00+00:00"}

    eod = lr._emergency_exit_authority(sess, le, reason="eod_flatten")
    cid = eod["client_order_id"]
    le["operator_flatten_requested_utc"] = "2026-07-13T19:55:01+00:00"
    operator = lr._emergency_exit_authority(sess, le, reason="operator_flatten")

    assert operator is eod
    assert operator["client_order_id"] == cid
    assert operator["phase"] == "prepared"
    assert operator["requested_reasons"] == ["eod_flatten", "operator_flatten"]


def _strict_market_close_authority() -> dict[str, Any]:
    return {
        "client_order_id": "strict-close-cid",
        "order_id": "strict-close-oid",
        "identity_contract": "alpaca_close_v1",
        "order_request": {
            "account_scope": "alpaca:paper",
            "product_id": "ACTU",
            "side": "sell",
            "base_size": "10",
            "client_order_id": "strict-close-cid",
            "position_intent": "sell_to_close",
            "order_type": "market",
            "time_in_force": "day",
            "extended_hours": False,
            "limit_price": None,
        },
    }


def test_emergency_close_identity_requires_every_frozen_broker_echo() -> None:
    authority = _strict_market_close_authority()
    valid = _order(
        oid="strict-close-oid",
        cid="strict-close-cid",
        symbol="ACTU",
        side="sell",
        status="open",
        filled=0.0,
        avg=None,
        qty=10.0,
    )
    assert lr._emergency_exit_order_matches(
        valid,
        authority,
        product_id="ACTU",
        side="sell",
    ) is True

    mutations = [
        {"qty": "9"},
        {"time_in_force": "gtc"},
        {"extended_hours": True},
        {"limit_price": 9.90},
        {"position_intent": "buy_to_close"},
    ]
    for raw_mutation in mutations:
        wrong = _order(
            oid="strict-close-oid",
            cid="strict-close-cid",
            symbol="ACTU",
            side="sell",
            status="open",
            filled=0.0,
            avg=None,
            qty=10.0,
            raw_overrides=raw_mutation,
        )
        assert lr._emergency_exit_order_matches(
            wrong,
            authority,
            product_id="ACTU",
            side="sell",
        ) is False

    wrong_type = _order(
        oid="strict-close-oid",
        cid="strict-close-cid",
        symbol="ACTU",
        side="sell",
        status="open",
        filled=0.0,
        avg=None,
        qty=10.0,
        order_type="limit",
    )
    assert lr._emergency_exit_order_matches(
        wrong_type,
        authority,
        product_id="ACTU",
        side="sell",
    ) is False

    missing_qty = _order(
        oid="strict-close-oid",
        cid="strict-close-cid",
        symbol="ACTU",
        side="sell",
        status="open",
        filled=0.0,
        avg=None,
        qty=10.0,
    )
    missing_qty.raw.pop("qty")
    assert lr._emergency_exit_order_matches(
        missing_qty,
        authority,
        product_id="ACTU",
        side="sell",
    ) is False


def test_alpaca_extended_close_adapter_encodes_day_tif_and_sell_to_close(
    monkeypatch,
) -> None:
    """The runner's ``extended_hours=True`` envelope must become Alpaca LIMIT+DAY."""
    import sys
    from enum import Enum
    from types import ModuleType

    class _OrderSide(Enum):
        BUY = "buy"
        SELL = "sell"

    class _TimeInForce(Enum):
        DAY = "day"
        GTC = "gtc"

    class _PositionIntent(Enum):
        BUY_TO_OPEN = "buy_to_open"
        BUY_TO_CLOSE = "buy_to_close"
        SELL_TO_OPEN = "sell_to_open"
        SELL_TO_CLOSE = "sell_to_close"

    class _Request:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    # The production image installs alpaca-py; the lean unit-test environment does
    # not.  Supply its tiny request/enum surface so this remains a hermetic contract
    # test of our adapter rather than an optional-dependency test.
    alpaca_pkg = ModuleType("alpaca")
    trading_pkg = ModuleType("alpaca.trading")
    enums_mod = ModuleType("alpaca.trading.enums")
    requests_mod = ModuleType("alpaca.trading.requests")
    enums_mod.OrderSide = _OrderSide
    enums_mod.TimeInForce = _TimeInForce
    enums_mod.PositionIntent = _PositionIntent
    requests_mod.LimitOrderRequest = _Request
    requests_mod.MarketOrderRequest = _Request
    monkeypatch.setitem(sys.modules, "alpaca", alpaca_pkg)
    monkeypatch.setitem(sys.modules, "alpaca.trading", trading_pkg)
    monkeypatch.setitem(sys.modules, "alpaca.trading.enums", enums_mod)
    monkeypatch.setitem(sys.modules, "alpaca.trading.requests", requests_mod)

    from app.services.trading.venue import alpaca_spot as alpaca_mod
    from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter

    captured: list[Any] = []

    class _Client:
        def submit_order(self, *, order_data):
            captured.append(order_data)
            return SimpleNamespace(
                id="extended-close-1",
                client_order_id="extended-close-cid",
                status="accepted",
                position_intent=getattr(order_data, "position_intent", None),
            )

    monkeypatch.setattr(alpaca_mod, "_trading_client", lambda: _Client())
    result = AlpacaSpotAdapter().place_limit_order_gtc(
        product_id="EDIR",
        side="sell",
        base_size="10",
        limit_price="9.90",
        client_order_id="extended-close-cid",
        extended_hours=True,
        position_intent="sell_to_close",
    )

    assert result["ok"] is True
    assert len(captured) == 1
    request = captured[0]
    assert request.time_in_force == _TimeInForce.DAY
    assert request.extended_hours is True
    assert request.position_intent == _PositionIntent.SELL_TO_CLOSE


def test_replayed_terminal_partial_uses_watermark_then_rotates_one_successor(
    db, monkeypatch
) -> None:
    old_cid = "emergency-attempt-1"
    sess = _seed_session(
        db,
        symbol="EWMK",
        quantity=60.0,
        le_extra={
            "operator_flatten_requested_utc": "2026-07-13T18:04:00+00:00",
            "realized_pnl_usd": 4.0,
            "exit_order_id": "exit-partial-1",
            "exit_client_order_id": old_cid,
            "pending_exit_reason": "operator_flatten",
            "pending_exit_quantity": 100.0,
            "pending_exit_submitted_at_utc": "2026-07-13T18:04:01+00:00",
            "emergency_exit_authority": {
                "reason": "operator_flatten",
                "requested_reasons": ["operator_flatten"],
                "client_order_id": old_cid,
                "order_id": "exit-partial-1",
                "phase": "submitted",
                "created_at_utc": "2026-07-13T18:04:00+00:00",
                "attempt_no": 1,
                "submitted_quantity": 100.0,
                "applied_filled_size": 40.0,
                "identity_contract": "alpaca_close_v1",
                "order_request": {
                    "account_scope": "alpaca:paper",
                    "product_id": "EWMK",
                    "side": "sell",
                    "base_size": "100",
                    "client_order_id": old_cid,
                    "position_intent": "sell_to_close",
                    "order_type": "market",
                    "time_in_force": "day",
                    "extended_hours": False,
                    "limit_price": None,
                },
            },
        },
    )
    adapter = _ScriptedAlpaca(positions=[60.0, 60.0, 60.0, 60.0])
    adapter.orders["exit-partial-1"] = _order(
        oid="exit-partial-1",
        cid=old_cid,
        symbol="EWMK",
        side="sell",
        status="cancelled",
        filled=40.0,
        avg=10.10,
        qty=100.0,
    )
    adapter.market_results.append(
        lambda _owner, cid: {
            "ok": True,
            "order_id": "exit-successor-2",
            "client_order_id": cid,
        }
    )
    adapter.orders["exit-successor-2"] = lambda owner, _oid: _order(
        oid="exit-successor-2",
        cid=owner.market_calls[0]["client_order_id"],
        symbol="EWMK",
        side="sell",
        status="open",
        filled=0.0,
        avg=None,
        qty=60.0,
    )
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)

    first = _run_tick(db, sess, adapter)
    first_le = sess.risk_snapshot_json["momentum_live_execution"]
    successor_cid = first_le["emergency_exit_authority"]["client_order_id"]

    assert first["flattened"] is False
    assert first_le["position"]["quantity"] == pytest.approx(60.0)
    assert first_le["realized_pnl_usd"] == pytest.approx(4.0)  # 40 was not applied twice
    assert first_le["emergency_exit_authority"]["attempt_no"] == 2
    assert first_le["emergency_exit_authority"]["phase"] == "prepared"
    assert successor_cid != old_cid
    assert first_le["emergency_exit_authority"]["terminal_attempts"][0][
        "applied_filled_size"
    ] == 40.0
    assert adapter.market_calls == []

    second = _run_tick(db, sess, adapter)
    second_le = sess.risk_snapshot_json["momentum_live_execution"]

    assert second["flattened"] is False
    assert len(adapter.market_calls) == 1
    assert adapter.market_calls[0]["client_order_id"] == successor_cid
    assert float(adapter.market_calls[0]["base_size"]) == pytest.approx(60.0)
    assert second_le["emergency_exit_authority"]["attempt_no"] == 2
    assert second_le["emergency_exit_authority"]["phase"] == "submitted"


@pytest.mark.parametrize(
    ("signed", "side_long", "expected", "error"),
    [
        (5.0, True, 5.0, None),
        (-5.0, False, 5.0, None),
        (-5.0, True, None, "opposite_sign_exposure"),
        (5.0, False, None, "opposite_sign_exposure"),
        (None, True, None, "broker_position_unknown"),
    ],
)
def test_signed_broker_quantity_never_hides_opposite_or_unknown_exposure(
    signed: float | None,
    side_long: bool,
    expected: float | None,
    error: str | None,
) -> None:
    qty, observed_error = lr._normalize_emergency_broker_quantity(
        signed,
        side_long=side_long,
    )
    assert qty == expected
    assert observed_error == error


def test_unknown_cost_basis_is_quarantined_and_never_fabricated_as_zero(
    monkeypatch,
) -> None:
    sess = _direct_session("EBAS")
    le = {
        "position": {
            "product_id": "EBAS",
            "quantity": 60.0,
            "avg_entry_price": None,
        }
    }
    sess.risk_snapshot_json = {"momentum_live_execution": le}
    monkeypatch.setattr(lr, "_emit", lambda *a, **k: None)

    lr._record_emergency_unpriced_fill(
        SimpleNamespace(),
        sess,
        le=le,
        authority={
            "attempt_no": 1,
            "client_order_id": "basis-unknown-cid",
            "order_id": "basis-unknown-order",
        },
        filled_quantity=20.0,
        fill_price=9.90,
        remaining_quantity=40.0,
        reason="operator_flatten",
        note="missing_entry_cost_basis",
    )

    pending = le["emergency_exit_accounting_pending"]
    assert pending["status"] == "pending_cost_basis"
    assert pending["unpriced_quantity"] == pytest.approx(20.0)
    assert pending["legs"][0]["fill_price"] == pytest.approx(9.90)
    assert pending["legs"][0]["note"] == "missing_entry_cost_basis"
    assert le["position"]["quantity"] == pytest.approx(40.0)
    assert le["position"]["emergency_accounting_basis_unknown"] is True
    assert le["position"].get("avg_entry_price") is None
