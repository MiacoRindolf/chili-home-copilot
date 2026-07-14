from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    acquire_action_claim,
    activate_deadman_replacement_containment,
    advance_owner_transport,
    advance_owner_transport_committed,
    certify_deadman_handoff_reprotected,
    lease_deadman_handoff_replacement,
    lease_deadman_handoff_replacement_committed,
    lease_owner_transport,
    prepare_deadman_close_handoff,
    prepare_deadman_replacement_containment,
    read_action_claim,
    read_action_claim_committed,
    reconcile_deadman_replacement_successor,
    resolve_owner_transport_terminal,
    retire_deadman_handoff_for_fractional_day_close,
    retire_deadman_handoff_reprotected,
)

from tests.test_momentum_emergency_exit_recovery import (
    TEST_ALPACA_ACCOUNT_ID,
    _order,
    _seed_session,
)

from app.services.trading.momentum_neural import live_runner as lr


def _request(
    *,
    symbol: str,
    cid: str,
    qty: float,
    kind: str,
) -> dict:
    common = {
        "account_scope": "alpaca:paper",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        "product_id": symbol,
        "side": "sell",
        "base_size": str(float(qty)),
        "client_order_id": cid,
        "position_intent": "sell_to_close",
        "extended_hours": False,
    }
    if kind == "deadman":
        return {
            **common,
            "order_type": "stop",
            "time_in_force": "gtc",
            "stop_price": 7.5,
        }
    return {
        **common,
        "order_type": "market",
        "time_in_force": "day",
        "limit_price": None,
    }


def _seed_owner(db, *, symbol: str, quantity: float):
    sess = _seed_session(db, symbol=symbol, quantity=quantity)
    claim_token = f"owner-{uuid.uuid4().hex}"
    snapshot = dict(sess.risk_snapshot_json or {})
    snapshot["alpaca_symbol_claim_token"] = claim_token
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.commit()
    acquired = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=claim_token,
        owner_session_id=int(sess.id),
        client_order_id=f"entry-{uuid.uuid4().hex[:12]}",
        metadata={
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            "order_request": {
                "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
                "product_id": symbol,
                "side": "buy",
                "base_size": "10.0",
            },
        },
        account_scope="alpaca:paper",
    )
    assert acquired["ok"] is True
    db.commit()
    context = {
        "symbol": symbol,
        "claim_token": claim_token,
        "owner_session_id": int(sess.id),
        "account_scope": "alpaca:paper",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
    }
    return sess, context


def _install_terminal_deadman(
    db,
    *,
    context: dict,
    initial_qty: float,
    filled: float,
    remaining: float,
):
    symbol = context["symbol"]
    deadman_cid = f"dm-{uuid.uuid4().hex[:12]}"
    deadman_oid = f"dm-oid-{uuid.uuid4().hex[:10]}"
    lease_token = f"dm-worker-{uuid.uuid4().hex}"
    deadman_request = _request(
        symbol=symbol,
        cid=deadman_cid,
        qty=initial_qty,
        kind="deadman",
    )
    leased = lease_owner_transport(
        db,
        **context,
        transport_kind="deadman",
        client_order_id=deadman_cid,
        order_request=deadman_request,
        lease_token=lease_token,
    )
    assert leased["ok"] is True
    assert advance_owner_transport(
        db,
        **context,
        client_order_id=deadman_cid,
        lease_token=lease_token,
        phase="submitted",
        broker_order_id=deadman_oid,
    )
    successor_cid = f"close-{uuid.uuid4().hex[:12]}"
    successor = _request(
        symbol=symbol,
        cid=successor_cid,
        qty=initial_qty,
        kind="exit",
    )
    handoff_token = f"handoff-{uuid.uuid4().hex}"
    prepared = prepare_deadman_close_handoff(
        db,
        **context,
        handoff_token=handoff_token,
        deadman_client_order_id=deadman_cid,
        deadman_broker_order_id=deadman_oid,
        deadman_order_request=deadman_request,
        successor_transport_kind="emergency_exit",
        successor_intent=successor,
        reason="focused_test_close",
    )
    assert prepared["ok"] is True
    assert resolve_owner_transport_terminal(
        db,
        **context,
        client_order_id=deadman_cid,
        broker_order_id=deadman_oid,
        broker_order_status="canceled",
        filled_size=filled,
        remaining_quantity=remaining,
    )
    db.commit()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    current = dict(claim["metadata"]["owner_transport"])
    handoff = dict(claim["metadata"]["deadman_close_handoff"])
    return current, handoff


def _write_live_state(db, sess, le: dict) -> None:
    snapshot = dict(sess.risk_snapshot_json or {})
    snapshot["momentum_live_execution"] = le
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.commit()
    db.refresh(sess)


def _deadman_watermark(current: dict) -> dict:
    return {
        "identity_contract": "alpaca_deadman_applied_fill_v1",
        "order_id": current["broker_order_id"],
        "client_order_id": current["client_order_id"],
        "owner_transport": {
            "client_order_id": current["client_order_id"],
            "order_request": dict(current["order_request"]),
        },
        "applied_filled_size": current["filled_size"],
        "broker_remaining_quantity": current["remaining_quantity"],
    }


class _DeadmanLifecycleAdapter:
    """Strict deadman adapter double with one exact CID-visible broker order."""

    def __init__(
        self,
        *,
        lifecycle: str,
        broker_position: float,
        filled: float = 0.0,
        average_fill: float | None = None,
        submit_indeterminate: bool = False,
    ) -> None:
        self.lifecycle = lifecycle
        self.broker_position = broker_position
        self.filled = filled
        self.average_fill = average_fill
        self.submit_indeterminate = submit_indeterminate
        self.place_calls: list[dict[str, Any]] = []
        self.order = None
        self.orders_by_cid: dict[str, Any] = {}
        self.orders_by_oid: dict[str, Any] = {}
        self.cancel_calls: list[str] = []

    def get_account_snapshot(self) -> dict[str, Any]:
        return {
            "ok": True,
            "paper": True,
            "account_id": TEST_ALPACA_ACCOUNT_ID,
        }

    def get_position_quantity(self, _product_id: str) -> float:
        return self.broker_position

    def place_deadman_stop(self, **kwargs: Any) -> dict[str, Any]:
        self.place_calls.append(dict(kwargs))
        cid = str(kwargs["client_order_id"])
        oid = f"deadman-{self.lifecycle}-{len(self.place_calls)}"
        self.order = _order(
            oid=oid,
            cid=cid,
            symbol=str(kwargs["product_id"]),
            side="sell",
            status="pending",
            filled=self.filled,
            avg=self.average_fill,
            qty=float(kwargs["base_size"]),
            order_type="stop",
            time_in_force="gtc",
            extended_hours=False,
            position_intent="sell_to_close",
            raw_overrides={
                "alpaca_status": self.lifecycle,
                "stop_price": float(kwargs["stop_price"]),
            },
        )
        self.orders_by_cid[cid] = self.order
        self.orders_by_oid[oid] = self.order
        if self.submit_indeterminate:
            return {
                "ok": False,
                "error": "ReadTimeout after submit",
                "submit_outcome": "indeterminate",
                "client_order_id": cid,
            }
        return {
            "ok": True,
            "order_id": oid,
            "status": self.lifecycle,
            "client_order_id": cid,
        }

    def get_order_by_client_order_id_truth(
        self,
        client_order_id: str,
    ) -> dict[str, Any]:
        order = self.orders_by_cid.get(client_order_id)
        if order is None and (
            self.order is not None
            and self.order.client_order_id == client_order_id
        ):
            order = self.order
        if order is not None:
            return {"readable": True, "found": True, "order": order}
        return {"readable": True, "found": False, "order": None}

    def get_order_truth(self, order_id: str) -> dict[str, Any]:
        order = self.orders_by_oid.get(order_id)
        return {
            "readable": True,
            "found": order is not None,
            "order": order,
        }

    def cancel_order_by_id(self, order_id: str) -> bool:
        self.cancel_calls.append(order_id)
        return True


def _ensure_initial_deadman(db, sess, adapter: _DeadmanLifecycleAdapter):
    le = dict(sess.risk_snapshot_json["momentum_live_execution"])
    pos = dict(le["position"])
    result = lr._ensure_alpaca_deadman_stop(
        db,
        sess,
        adapter,
        le=le,
        product_id=sess.symbol,
        quantity=float(pos["quantity"]),
        avg_entry_price=float(pos["avg_entry_price"]),
        software_stop_price=float(pos["stop_price"]),
    )
    db.commit()
    db.refresh(sess)
    return result


def _claim_deadman_generation(
    db,
    *,
    context: dict,
    session_id: int,
    generation: int,
    quantity: float,
    broker_order_id: str,
    terminal_fill: float | None = None,
    terminal_remaining: float | None = None,
) -> tuple[dict[str, Any], str]:
    cid = f"chili_dm_{session_id}_{generation}_focused"
    request = _request(
        symbol=context["symbol"],
        cid=cid,
        qty=quantity,
        kind="deadman",
    )
    lease_token = f"generation-{generation}-{uuid.uuid4().hex}"
    leased = lease_owner_transport(
        db,
        **context,
        transport_kind="deadman",
        client_order_id=cid,
        order_request=request,
        lease_token=lease_token,
    )
    assert leased["ok"] is True, leased
    assert advance_owner_transport(
        db,
        **context,
        client_order_id=cid,
        lease_token=lease_token,
        phase="submitted",
        broker_order_id=broker_order_id,
    )
    if terminal_fill is not None:
        assert terminal_remaining is not None
        assert resolve_owner_transport_terminal(
            db,
            **context,
            client_order_id=cid,
            broker_order_id=broker_order_id,
            broker_order_status="canceled",
            filled_size=terminal_fill,
            remaining_quantity=terminal_remaining,
        )
    db.commit()
    readable, claim = read_action_claim(
        db,
        symbol=context["symbol"],
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    return dict(claim["metadata"]["owner_transport"]), cid


def _install_truth_order(
    adapter: _DeadmanLifecycleAdapter,
    *,
    transport: dict[str, Any],
    order_id: str,
    status: str,
    lifecycle: str,
    filled: float,
    average_fill: float | None,
) -> Any:
    request = dict(transport["order_request"])
    order = _order(
        oid=order_id,
        cid=str(transport["client_order_id"]),
        symbol=str(request["product_id"]),
        side="sell",
        status=status,
        filled=filled,
        avg=average_fill,
        qty=float(request["base_size"]),
        order_type="stop",
        time_in_force="gtc",
        extended_hours=False,
        position_intent="sell_to_close",
        raw_overrides={
            "alpaca_status": lifecycle,
            "stop_price": float(request["stop_price"]),
        },
    )
    adapter.orders_by_cid[order.client_order_id] = order
    adapter.orders_by_oid[order.order_id] = order
    adapter.order = order
    return order


@pytest.fixture
def _deadman_settings(monkeypatch):
    monkeypatch.setattr(settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        TEST_ALPACA_ACCOUNT_ID,
        raising=False,
    )
    monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)


@pytest.mark.parametrize(
    "raw_status",
    [
        "held",
        "calculated",
        "suspended",
        "pending_cancel",
        "pending_replace",
        "unknown",
        "",
    ],
)
def test_ambiguous_alpaca_stop_lifecycle_is_not_certifiably_active(raw_status):
    order = SimpleNamespace(status="pending", raw={"alpaca_status": raw_status})
    assert lr._alpaca_protective_order_is_certifiably_active(order) is False


@pytest.mark.parametrize(
    "raw_status",
    ["pending_cancel", "pending_replace", "unknown", ""],
)
def test_existing_initial_deadman_nonallowlisted_lifecycle_never_protects(
    db,
    _deadman_settings,
    raw_status,
):
    symbol = f"X{uuid.uuid4().hex[:5].upper()}"
    sess, context = _seed_owner(db, symbol=symbol, quantity=10.0)
    cid = f"dm-existing-{uuid.uuid4().hex[:10]}"
    oid = f"oid-existing-{uuid.uuid4().hex[:10]}"
    request = _request(symbol=symbol, cid=cid, qty=10.0, kind="deadman")
    lease_token = f"lease-{uuid.uuid4().hex}"
    leased = lease_owner_transport(
        db,
        **context,
        transport_kind="deadman",
        client_order_id=cid,
        order_request=request,
        lease_token=lease_token,
    )
    assert leased["ok"] is True
    assert advance_owner_transport(
        db,
        **context,
        client_order_id=cid,
        lease_token=lease_token,
        phase="submitted",
        broker_order_id=oid,
    )
    db.commit()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    transport = dict(claim["metadata"]["owner_transport"])
    le = dict(sess.risk_snapshot_json["momentum_live_execution"])
    le["deadman_stop"] = {
        "order_id": oid,
        "client_order_id": cid,
        "stop_price": 7.5,
        "qty": 10.0,
        "phase": "submitted",
        "owner_transport": transport,
    }
    _write_live_state(db, sess, le)
    adapter = _DeadmanLifecycleAdapter(
        lifecycle=raw_status,
        broker_position=10.0,
    )
    adapter.order = _order(
        oid=oid,
        cid=cid,
        symbol=symbol,
        side="sell",
        status="pending",
        filled=0.0,
        avg=None,
        qty=10.0,
        order_type="stop",
        time_in_force="gtc",
        extended_hours=False,
        position_intent="sell_to_close",
        raw_overrides={"alpaca_status": raw_status, "stop_price": 7.5},
    )

    result = _ensure_initial_deadman(db, sess, adapter)

    assert result.get("protected") is not True
    assert result["full_close_queued"] is True
    assert result["error"] == "deadman_active_certification_failed"
    assert adapter.place_calls == []


@pytest.mark.parametrize(
    "raw_status",
    ["accepted", "pending_new", "accepted_for_bidding", "stopped"],
)
def test_initial_deadman_requires_exact_active_lifecycle_before_protected(
    db,
    _deadman_settings,
    raw_status,
):
    sess, _context = _seed_owner(db, symbol=f"A{raw_status[:3].upper()}", quantity=10.0)
    adapter = _DeadmanLifecycleAdapter(
        lifecycle=raw_status,
        broker_position=10.0,
    )

    result = _ensure_initial_deadman(db, sess, adapter)

    assert result["ok"] is True
    assert result["protected"] is True
    assert len(adapter.place_calls) == 1


@pytest.mark.parametrize("raw_status", ["held", "suspended"])
@pytest.mark.parametrize("submit_indeterminate", [False, True])
def test_initial_or_recovered_nonexecutable_deadman_never_reports_protected(
    db,
    _deadman_settings,
    raw_status,
    submit_indeterminate,
):
    symbol = f"N{raw_status[:2].upper()}{int(submit_indeterminate)}"
    sess, context = _seed_owner(db, symbol=symbol, quantity=10.0)
    adapter = _DeadmanLifecycleAdapter(
        lifecycle=raw_status,
        broker_position=10.0,
        submit_indeterminate=submit_indeterminate,
    )

    result = _ensure_initial_deadman(db, sess, adapter)

    assert result.get("protected") is not True
    assert result["ambiguous_protection_requires_close"] is True
    le = sess.risk_snapshot_json["momentum_live_execution"]
    assert le["deadman_stop"]["order_id"] == adapter.order.order_id
    assert le["deadman_protection_reconcile_pending"]["reason"] == (
        f"deadman_{raw_status}_non_executable"
    )
    readable, claim = read_action_claim(
        db,
        symbol=context["symbol"],
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    assert claim["metadata"]["owner_transport"]["phase"] == "submitted"
    assert len(adapter.place_calls) == 1


@pytest.mark.parametrize("submit_indeterminate", [False, True])
def test_calculated_partial_accounts_once_but_retains_single_authority_until_cancel(
    db,
    _deadman_settings,
    submit_indeterminate,
):
    symbol = f"CAL{int(submit_indeterminate)}"
    sess, context = _seed_owner(db, symbol=symbol, quantity=10.0)
    adapter = _DeadmanLifecycleAdapter(
        lifecycle="calculated",
        broker_position=6.0,
        filled=4.0,
        average_fill=9.5,
        submit_indeterminate=submit_indeterminate,
    )

    first = _ensure_initial_deadman(db, sess, adapter)
    first_le = sess.risk_snapshot_json["momentum_live_execution"]
    first_pnl = float(first_le["realized_pnl_usd"])

    assert first.get("protected") is not True
    assert first["ambiguous_protection_requires_close"] is True
    assert first["calculated_fill_delta_accounted"] == pytest.approx(4.0)
    assert first_le["position"]["quantity"] == pytest.approx(6.0)
    assert first_le["deadman_stop"]["phase"] == "calculated_dormant"
    assert first_le["deadman_applied_fill_watermarks"][-1][
        "applied_filled_size"
    ] == pytest.approx(4.0)
    readable, claim = read_action_claim(
        db,
        symbol=context["symbol"],
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    assert claim["metadata"]["owner_transport"]["phase"] == "submitted"

    second = _ensure_initial_deadman(db, sess, adapter)
    second_le = sess.risk_snapshot_json["momentum_live_execution"]
    assert second["calculated_fill_delta_accounted"] == pytest.approx(0.0)
    assert float(second_le["realized_pnl_usd"]) == pytest.approx(first_pnl)
    assert second_le["position"]["quantity"] == pytest.approx(6.0)
    assert len(adapter.place_calls) == 1


@pytest.mark.parametrize(
    ("filled", "remaining"),
    [(0.0, 10.0), (4.0, 6.0)],
)
def test_resolved_terminal_replays_once_then_posts_one_fresh_generation(
    db,
    _deadman_settings,
    filled,
    remaining,
):
    symbol = f"R{int(filled)}{uuid.uuid4().hex[:4].upper()}"
    sess, context = _seed_owner(db, symbol=symbol, quantity=10.0)
    old_oid = f"terminal-{uuid.uuid4().hex[:8]}"
    old_transport, old_cid = _claim_deadman_generation(
        db,
        context=context,
        session_id=int(sess.id),
        generation=1,
        quantity=10.0,
        broker_order_id=old_oid,
        terminal_fill=filled,
        terminal_remaining=remaining,
    )
    adapter = _DeadmanLifecycleAdapter(
        lifecycle="accepted",
        broker_position=remaining,
    )
    _install_truth_order(
        adapter,
        transport=old_transport,
        order_id=old_oid,
        status="canceled",
        lifecycle="canceled",
        filled=filled,
        average_fill=(9.5 if filled > 0.0 else None),
    )

    result = _ensure_initial_deadman(db, sess, adapter)

    assert result["ok"] is True
    assert result["protected"] is True
    assert len(adapter.place_calls) == 1
    new_cid = str(adapter.place_calls[0]["client_order_id"])
    assert new_cid != old_cid
    assert f"chili_dm_{int(sess.id)}_2_" in new_cid
    le = sess.risk_snapshot_json["momentum_live_execution"]
    assert le["position"]["quantity"] == pytest.approx(remaining)
    markers = [
        row
        for row in le["deadman_applied_fill_watermarks"]
        if row["client_order_id"] == old_cid
    ]
    assert len(markers) == 1
    assert markers[0]["applied_filled_size"] == pytest.approx(filled)


@pytest.mark.parametrize("terminal_count", [2, 3, 21])
def test_all_terminal_predecessors_replay_oldest_first_before_active_child(
    db,
    _deadman_settings,
    terminal_count,
):
    start_qty = float(terminal_count + 5)
    symbol = f"C{terminal_count}{uuid.uuid4().hex[:3].upper()}"
    sess, context = _seed_owner(db, symbol=symbol, quantity=start_qty)
    adapter = _DeadmanLifecycleAdapter(
        lifecycle="accepted",
        broker_position=start_qty - terminal_count,
    )
    predecessor_cids: list[str] = []
    for index in range(1, terminal_count + 1):
        quantity = start_qty - (index - 1)
        remaining = quantity - 1.0
        oid = f"chain-{index}-{uuid.uuid4().hex[:6]}"
        transport, cid = _claim_deadman_generation(
            db,
            context=context,
            session_id=int(sess.id),
            generation=index,
            quantity=quantity,
            broker_order_id=oid,
            terminal_fill=1.0,
            terminal_remaining=remaining,
        )
        predecessor_cids.append(cid)
        _install_truth_order(
            adapter,
            transport=transport,
            order_id=oid,
            status="canceled",
            lifecycle="canceled",
            filled=1.0,
            average_fill=9.5,
        )
    active_qty = start_qty - terminal_count
    active_oid = f"active-{uuid.uuid4().hex[:8]}"
    active_transport, active_cid = _claim_deadman_generation(
        db,
        context=context,
        session_id=int(sess.id),
        generation=terminal_count + 1,
        quantity=active_qty,
        broker_order_id=active_oid,
    )
    _install_truth_order(
        adapter,
        transport=active_transport,
        order_id=active_oid,
        status="pending",
        lifecycle="accepted",
        filled=0.0,
        average_fill=None,
    )

    result = _ensure_initial_deadman(db, sess, adapter)

    assert result["ok"] is True
    assert result["protected"] is True
    assert result["order_id"] == active_oid
    assert adapter.place_calls == []
    le = sess.risk_snapshot_json["momentum_live_execution"]
    assert le["position"]["quantity"] == pytest.approx(active_qty)
    markers = le["deadman_applied_fill_watermarks"]
    assert [row["client_order_id"] for row in markers] == predecessor_cids
    first_pnl = float(le["realized_pnl_usd"])
    second = _ensure_initial_deadman(db, sess, adapter)
    second_le = sess.risk_snapshot_json["momentum_live_execution"]
    assert second["protected"] is True
    assert second_le["deadman_stop"]["client_order_id"] == active_cid
    assert float(second_le["realized_pnl_usd"]) == pytest.approx(first_pnl)

    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    metadata = claim["metadata"]
    assert len(metadata["protective_terminal_ledger"]) == terminal_count
    assert len(metadata["owner_transport_history"]) == min(20, terminal_count)
    assert metadata["deadman_generation_high_watermark"] == terminal_count + 1


def test_rth_entry_rejects_stale_premarket_extended_hours_generation_before_place(
    monkeypatch,
):
    calls: list[dict[str, Any]] = []
    reserve_calls: list[dict[str, Any]] = []
    sess = SimpleNamespace(
        id=77,
        user_id=42,
        symbol="RTHX",
        execution_family="alpaca_spot",
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            "alpaca_symbol_claim_token": "rth-token",
        },
    )
    monkeypatch.setattr(lr, "_confirmed_alpaca_arm_generation_reason", lambda _s: None)
    monkeypatch.setattr(
        lr,
        "reserve_alpaca_entry_risk_committed",
        lambda **kwargs: reserve_calls.append(dict(kwargs)) or {
            "ok": True,
            "created": True,
            "claim": {
                "symbol": "RTHX",
                "claim_token": "rth-token",
                "owner_session_id": 77,
                "account_scope": "alpaca:paper",
                "phase": "claimed",
                "client_order_id": kwargs["client_order_id"],
                "metadata": {
                    "entry_post_bind_token": kwargs["post_bind_token"],
                    "order_request": kwargs["order_request"],
                },
            },
        },
    )

    stale_kwargs = {
        "product_id": "RTHX",
        "side": "buy",
        "position_intent": "buy_to_open",
        "base_size": "5",
        "limit_price": "10.00",
        "time_in_force": "day",
        "extended_hours": True,
        "client_order_id": "stale-premarket-generation",
    }
    claim, _cid, early = lr._prepare_alpaca_place_claim(
        SimpleNamespace(),
        sess,
        dict(stale_kwargs),
        risk_stop_price=9.5,
        account_equity_usd=10_000.0,
    )
    assert claim is None
    assert early is None
    assert reserve_calls == []

    result = lr._governed_place(
        SimpleNamespace(),
        lambda **kwargs: calls.append(dict(kwargs)) or {"ok": True},
        sess=sess,
        **stale_kwargs,
    )

    assert result["pre_place_blocked"] is True
    assert result["error"] == "alpaca_entry_extended_hours_not_false"
    assert calls == []

    valid_kwargs = {
        **stale_kwargs,
        "client_order_id": "fresh-rth-generation",
        "extended_hours": False,
    }
    valid_claim, valid_cid, valid_early = lr._prepare_alpaca_place_claim(
        SimpleNamespace(),
        sess,
        valid_kwargs,
        risk_stop_price=9.5,
        account_equity_usd=10_000.0,
    )
    assert valid_early is None
    assert valid_claim is not None
    assert valid_cid == "fresh-rth-generation"
    assert len(reserve_calls) == 1
    assert reserve_calls[0]["order_request"]["extended_hours"] is False


def test_fractional_day_close_retirement_requires_exact_committed_predecessor(db):
    sess, context = _seed_owner(db, symbol="FDRM", quantity=0.5)
    current, handoff = _install_terminal_deadman(
        db,
        context=context,
        initial_qty=10.0,
        filled=9.5,
        remaining=0.5,
    )
    le = dict(sess.risk_snapshot_json["momentum_live_execution"])
    le["alpaca_fractional_day_close_required"] = {
        "identity_contract": "alpaca_fractional_day_close_v1",
        "product_id": context["symbol"],
        "broker_remainder_quantity": 0.5,
        "source_owner_transport": current,
    }
    _write_live_state(db, sess, le)

    assert not retire_deadman_handoff_for_fractional_day_close(
        db,
        **context,
        handoff_token=handoff["handoff_token"],
        broker_position_quantity=0.5,
    )
    db.rollback()

    le = dict(sess.risk_snapshot_json["momentum_live_execution"])
    le["deadman_applied_fill_watermarks"] = [_deadman_watermark(current)]
    _write_live_state(db, sess, le)
    assert retire_deadman_handoff_for_fractional_day_close(
        db,
        **context,
        handoff_token=handoff["handoff_token"],
        broker_position_quantity=0.5,
    )
    db.commit()

    readable, claim = read_action_claim(
        db,
        symbol=context["symbol"],
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    assert "deadman_close_handoff" not in claim["metadata"]
    retired = claim["metadata"]["deadman_close_handoff_history"][-1]
    assert retired["retirement_outcome"] == "fractional_remainder_day_close_required"
    assert retired["fractional_remainder_quantity"] == 0.5


def test_replacement_active_retains_lineage_until_exact_fill_watermark(db):
    sess, context = _seed_owner(db, symbol="RLIN", quantity=6.0)
    original, _handoff = _install_terminal_deadman(
        db,
        context=context,
        initial_qty=10.0,
        filled=4.0,
        remaining=6.0,
    )
    replacement_cid = f"dm-repl-{uuid.uuid4().hex[:10]}"
    replacement_request = _request(
        symbol=context["symbol"],
        cid=replacement_cid,
        qty=6.0,
        kind="deadman",
    )
    replacement_token = f"repl-worker-{uuid.uuid4().hex}"
    leased = lease_deadman_handoff_replacement(
        db,
        **context,
        client_order_id=replacement_cid,
        order_request=replacement_request,
        lease_token=replacement_token,
        broker_position_quantity=6.0,
        local_position_quantity=6.0,
    )
    assert leased["ok"] is True
    assert leased["handoff"]["protective_terminal_generations"][0][
        "client_order_id"
    ] == original["client_order_id"]
    assert advance_owner_transport(
        db,
        **context,
        client_order_id=replacement_cid,
        lease_token=replacement_token,
        phase="submitted",
        broker_order_id="repl-oid",
    )
    assert not certify_deadman_handoff_reprotected(
        db,
        **context,
        client_order_id=replacement_cid,
        broker_order_id="repl-oid",
        broker_order_status="accepted",
        broker_order_lifecycle="done_for_day",
    )
    assert certify_deadman_handoff_reprotected(
        db,
        **context,
        client_order_id=replacement_cid,
        broker_order_id="repl-oid",
        broker_order_status="accepted",
        broker_order_lifecycle="accepted",
    )
    db.commit()

    assert not retire_deadman_handoff_reprotected(
        db,
        **context,
        client_order_id=replacement_cid,
        broker_order_id="repl-oid",
        broker_order_status="accepted",
        broker_order_lifecycle="accepted",
    )
    db.rollback()
    le = dict(sess.risk_snapshot_json["momentum_live_execution"])
    le["deadman_applied_fill_watermarks"] = [_deadman_watermark(original)]
    _write_live_state(db, sess, le)
    assert retire_deadman_handoff_reprotected(
        db,
        **context,
        client_order_id=replacement_cid,
        broker_order_id="repl-oid",
        broker_order_status="accepted",
        broker_order_lifecycle="accepted",
    )
    db.commit()


def test_child_submit_survives_outer_rollback_with_predecessor_lineage(db):
    sess, context = _seed_owner(db, symbol="CRLN", quantity=10.0)
    original, _handoff = _install_terminal_deadman(
        db,
        context=context,
        initial_qty=10.0,
        filled=4.0,
        remaining=6.0,
    )

    # Simulate local accounting in the runner's still-open transaction.
    snapshot = dict(sess.risk_snapshot_json or {})
    le = dict(snapshot["momentum_live_execution"])
    le["position"] = {**dict(le["position"]), "quantity": 6.0}
    le["deadman_applied_fill_watermarks"] = [_deadman_watermark(original)]
    snapshot["momentum_live_execution"] = le
    sess.risk_snapshot_json = snapshot
    db.flush()

    replacement_cid = f"dm-child-{uuid.uuid4().hex[:10]}"
    replacement_request = _request(
        symbol=context["symbol"],
        cid=replacement_cid,
        qty=6.0,
        kind="deadman",
    )
    child_token = f"child-worker-{uuid.uuid4().hex}"
    child = lease_deadman_handoff_replacement_committed(
        **context,
        client_order_id=replacement_cid,
        order_request=replacement_request,
        lease_token=child_token,
        broker_position_quantity=6.0,
        local_position_quantity=6.0,
    )
    assert child["ok"] is True
    assert advance_owner_transport_committed(
        **context,
        client_order_id=replacement_cid,
        lease_token=child_token,
        phase="submitted",
        broker_order_id="child-oid",
    )

    # Crash/rollback: claim-side child remains, local accounting disappears.
    db.rollback()
    db.refresh(sess)
    rolled_back_le = sess.risk_snapshot_json["momentum_live_execution"]
    assert rolled_back_le["position"]["quantity"] == 10.0
    assert "deadman_applied_fill_watermarks" not in rolled_back_le
    readable, claim = read_action_claim_committed(
        symbol=context["symbol"],
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    current = claim["metadata"]["owner_transport"]
    handoff = claim["metadata"]["deadman_close_handoff"]
    assert current["client_order_id"] == replacement_cid
    assert current["phase"] == "submitted"
    assert handoff["protective_terminal_generations"][0]["client_order_id"] == original[
        "client_order_id"
    ]


def test_ack_loss_can_advance_only_same_fenced_generation(db):
    _sess, context = _seed_owner(db, symbol="ACKG", quantity=6.0)
    _original, _handoff = _install_terminal_deadman(
        db,
        context=context,
        initial_qty=10.0,
        filled=4.0,
        remaining=6.0,
    )
    cid = f"dm-ack-{uuid.uuid4().hex[:10]}"
    request = _request(
        symbol=context["symbol"],
        cid=cid,
        qty=6.0,
        kind="deadman",
    )
    lease_token = f"ack-worker-{uuid.uuid4().hex}"
    leased = lease_deadman_handoff_replacement(
        db,
        **context,
        client_order_id=cid,
        order_request=request,
        lease_token=lease_token,
        broker_position_quantity=6.0,
        local_position_quantity=6.0,
    )
    assert leased["ok"] is True
    assert advance_owner_transport(
        db,
        **context,
        client_order_id=cid,
        lease_token=lease_token,
        phase="submit_indeterminate",
        broker_order_id=None,
    )
    assert not advance_owner_transport(
        db,
        **context,
        client_order_id=cid,
        lease_token="stale-worker",
        phase="submitted",
        broker_order_id="ack-oid",
    )
    assert advance_owner_transport(
        db,
        **context,
        client_order_id=cid,
        lease_token=lease_token,
        phase="submitted",
        broker_order_id="ack-oid",
    )
    db.commit()


def test_replacement_containment_rejects_terminal_status_with_active_raw_lifecycle(
    db,
):
    _sess, context = _seed_owner(
        db,
        symbol=f"RL{uuid.uuid4().hex[:4].upper()}",
        quantity=10.0,
    )
    predecessor_cid = f"chili_dm_{context['owner_session_id']}_1_terminal_mismatch"
    predecessor_oid = f"old-{uuid.uuid4().hex[:10]}"
    predecessor_request = _request(
        symbol=context["symbol"],
        cid=predecessor_cid,
        qty=10.0,
        kind="deadman",
    )
    lease_token = f"old-worker-{uuid.uuid4().hex}"
    leased = lease_owner_transport(
        db,
        **context,
        transport_kind="deadman",
        client_order_id=predecessor_cid,
        order_request=predecessor_request,
        lease_token=lease_token,
    )
    assert leased["ok"] is True
    assert advance_owner_transport(
        db,
        **context,
        client_order_id=predecessor_cid,
        lease_token=lease_token,
        phase="submitted",
        broker_order_id=predecessor_oid,
    )
    successor_cid = f"successor-{uuid.uuid4().hex[:12]}"
    successor_oid = f"successor-oid-{uuid.uuid4().hex[:10]}"
    successor_request = _request(
        symbol=context["symbol"],
        cid=successor_cid,
        qty=10.0,
        kind="deadman",
    )
    close_intent = _request(
        symbol=context["symbol"],
        cid=f"close-{uuid.uuid4().hex[:12]}",
        qty=10.0,
        kind="exit",
    )
    prepared = prepare_deadman_replacement_containment(
        db,
        **context,
        predecessor_client_order_id=predecessor_cid,
        predecessor_broker_order_id=predecessor_oid,
        predecessor_order_request=predecessor_request,
        predecessor_reported_filled_size=0.0,
        successor_client_order_id=successor_cid,
        successor_broker_order_id=successor_oid,
        successor_order_request=successor_request,
        successor_broker_status="pending",
        successor_broker_lifecycle="pending_replace",
        successor_reported_filled_size=0.0,
        close_intent=close_intent,
    )
    assert prepared["ok"] is True

    rejected = activate_deadman_replacement_containment(
        db,
        **context,
        containment_id=prepared["containment"]["containment_id"],
        predecessor_broker_lifecycle="replaced",
        successor_broker_status="canceled",
        successor_broker_lifecycle="accepted",
        predecessor_reported_filled_size=0.0,
        successor_reported_filled_size=0.0,
        broker_remaining_quantity=10.0,
    )

    assert rejected == {
        "ok": False,
        "reason": "replacement_containment_terminal_truth_invalid",
    }
    readable, claim = read_action_claim(
        db,
        symbol=context["symbol"],
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    metadata = claim["metadata"]
    assert metadata["replacement_lineage_containment"]["state"] == "prepared"
    assert metadata["deadman_close_handoff"]["phase"] == (
        "replacement_lineage_containment_prepared"
    )
    assert "protective_attribution_quarantine_ledger" not in metadata


def test_active_partial_replacement_adopts_exact_open_remainder_with_quarantine(db):
    _sess, context = _seed_owner(
        db,
        symbol=f"RP{uuid.uuid4().hex[:4].upper()}",
        quantity=10.0,
    )
    predecessor_cid = f"chili_dm_{context['owner_session_id']}_1_partial"
    predecessor_oid = f"old-{uuid.uuid4().hex[:10]}"
    predecessor_request = _request(
        symbol=context["symbol"],
        cid=predecessor_cid,
        qty=10.0,
        kind="deadman",
    )
    lease_token = f"old-worker-{uuid.uuid4().hex}"
    leased = lease_owner_transport(
        db,
        **context,
        transport_kind="deadman",
        client_order_id=predecessor_cid,
        order_request=predecessor_request,
        lease_token=lease_token,
    )
    assert leased["ok"] is True
    assert advance_owner_transport(
        db,
        **context,
        client_order_id=predecessor_cid,
        lease_token=lease_token,
        phase="submitted",
        broker_order_id=predecessor_oid,
    )
    successor_cid = f"successor-{uuid.uuid4().hex[:12]}"
    successor_oid = f"successor-oid-{uuid.uuid4().hex[:10]}"
    successor_request = _request(
        symbol=context["symbol"],
        cid=successor_cid,
        qty=10.0,
        kind="deadman",
    )

    reconciled = reconcile_deadman_replacement_successor(
        db,
        **context,
        predecessor_client_order_id=predecessor_cid,
        predecessor_broker_order_id=predecessor_oid,
        predecessor_order_request=predecessor_request,
        predecessor_broker_lifecycle="replaced",
        predecessor_reported_filled_size=2.0,
        successor_client_order_id=successor_cid,
        successor_broker_order_id=successor_oid,
        successor_order_request=successor_request,
        successor_broker_status="open",
        successor_broker_lifecycle="partially_filled",
        successor_reported_filled_size=2.0,
        successor_average_filled_price=None,
        attributable_filled_size=0.0,
        attributable_fill_source=None,
        broker_remaining_quantity=8.0,
        successor_active=True,
        fill_attribution_quarantined=True,
    )

    assert reconciled["ok"] is True
    assert reconciled["transport"]["client_order_id"] == successor_cid
    assert reconciled["transport"]["broker_order_id"] == successor_oid
    readable, claim = read_action_claim(
        db,
        symbol=context["symbol"],
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    metadata = claim["metadata"]
    assert metadata["owner_transport"]["phase"] == "submitted"
    assert metadata["owner_transport_history"][-1]["broker_order_status"] == (
        "replaced"
    )
    assert metadata["owner_transport_history"][-1][
        "broker_order_lifecycle"
    ] == "replaced"
    assert metadata["protective_terminal_ledger"] == []
    quarantines = metadata["protective_attribution_quarantine_ledger"]
    assert len(quarantines) == 1
    assert quarantines[0]["fill_attribution_quarantined"] is True
    assert quarantines[0]["successor_applied_fill_baseline"] == 2.0
    assert quarantines[0]["broker_remaining_quantity"] == 8.0


def test_active_partial_replacement_advances_quarantined_baseline_without_pnl(
    db,
    _deadman_settings,
):
    sess, context = _seed_owner(
        db,
        symbol=f"RQ{uuid.uuid4().hex[:4].upper()}",
        quantity=10.0,
    )
    predecessor_cid = f"chili_dm_{int(sess.id)}_1_partial_drift"
    predecessor_oid = f"old-{uuid.uuid4().hex[:10]}"
    successor_cid = f"successor-{uuid.uuid4().hex[:10]}"
    successor_oid = f"successor-oid-{uuid.uuid4().hex[:10]}"
    predecessor_request = _request(
        symbol=context["symbol"],
        cid=predecessor_cid,
        qty=10.0,
        kind="deadman",
    )
    lease_token = f"old-worker-{uuid.uuid4().hex}"
    leased = lease_owner_transport(
        db,
        **context,
        transport_kind="deadman",
        client_order_id=predecessor_cid,
        order_request=predecessor_request,
        lease_token=lease_token,
    )
    assert leased["ok"] is True
    assert advance_owner_transport(
        db,
        **context,
        client_order_id=predecessor_cid,
        lease_token=lease_token,
        phase="submitted",
        broker_order_id=predecessor_oid,
    )
    db.commit()
    readable, claim = read_action_claim(
        db,
        symbol=context["symbol"],
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    predecessor_transport = dict(claim["metadata"]["owner_transport"])
    le = dict(sess.risk_snapshot_json["momentum_live_execution"])
    le["deadman_stop"] = {
        "order_id": predecessor_oid,
        "client_order_id": predecessor_cid,
        "stop_price": 7.5,
        "qty": 10.0,
        "phase": "submitted",
        "owner_transport": predecessor_transport,
    }
    _write_live_state(db, sess, le)

    adapter = _DeadmanLifecycleAdapter(
        lifecycle="partially_filled",
        broker_position=8.0,
    )
    predecessor = _order(
        oid=predecessor_oid,
        cid=predecessor_cid,
        symbol=context["symbol"],
        side="sell",
        status="pending",
        filled=0.0,
        avg=None,
        qty=10.0,
        order_type="stop",
        time_in_force="gtc",
        extended_hours=False,
        position_intent="sell_to_close",
        raw_overrides={
            "alpaca_status": "replaced",
            "replaced_by": successor_oid,
            "stop_price": 7.5,
        },
    )

    def _successor(cumulative: float):
        return _order(
            oid=successor_oid,
            cid=successor_cid,
            symbol=context["symbol"],
            side="sell",
            status="open",
            filled=cumulative,
            avg=None,
            qty=10.0,
            order_type="stop",
            time_in_force="gtc",
            extended_hours=False,
            position_intent="sell_to_close",
            raw_overrides={
                "alpaca_status": "partially_filled",
                "replaces": predecessor_oid,
                "stop_price": 7.5,
            },
        )

    successor = _successor(2.0)
    adapter.orders_by_cid.update({
        predecessor_cid: predecessor,
        successor_cid: successor,
    })
    adapter.orders_by_oid.update({
        predecessor_oid: predecessor,
        successor_oid: successor,
    })
    adapter.order = successor

    adopted = _ensure_initial_deadman(db, sess, adapter)
    assert adopted["protected"] is True
    db.refresh(sess)
    adopted_le = sess.risk_snapshot_json["momentum_live_execution"]
    assert adopted_le["position"]["quantity"] == 8.0
    initial_marker = [
        row
        for row in adopted_le["deadman_applied_fill_watermarks"]
        if row["client_order_id"] == successor_cid
    ]
    assert len(initial_marker) == 1
    assert initial_marker[0]["applied_filled_size"] == 2.0
    pnl_before = {
        key: value
        for key, value in adopted_le.items()
        if "pnl" in key.lower() or "realized" in key.lower()
    }

    successor = _successor(3.0)
    adapter.orders_by_cid[successor_cid] = successor
    adapter.orders_by_oid[successor_oid] = successor
    adapter.order = successor
    adapter.broker_position = 7.0
    le = dict(sess.risk_snapshot_json["momentum_live_execution"])
    pos = dict(le["position"])
    drift = lr._ensure_alpaca_deadman_stop(
        db,
        sess,
        adapter,
        le=le,
        product_id=sess.symbol,
        quantity=float(pos["quantity"]),
        avg_entry_price=float(pos["avg_entry_price"]),
        software_stop_price=float(pos["stop_price"]),
    )
    assert drift["protected"] is True
    assert sess.risk_snapshot_json["momentum_live_execution"]["position"][
        "quantity"
    ] == 7.0
    db.rollback()
    db.refresh(sess)
    assert sess.risk_snapshot_json["momentum_live_execution"]["position"][
        "quantity"
    ] == 8.0

    le = dict(sess.risk_snapshot_json["momentum_live_execution"])
    pos = dict(le["position"])
    replay = lr._ensure_alpaca_deadman_stop(
        db,
        sess,
        adapter,
        le=le,
        product_id=sess.symbol,
        quantity=float(pos["quantity"]),
        avg_entry_price=float(pos["avg_entry_price"]),
        software_stop_price=float(pos["stop_price"]),
    )
    assert replay.get("protected") is True, replay
    db.commit()
    db.refresh(sess)
    replay_le = sess.risk_snapshot_json["momentum_live_execution"]
    assert replay_le["position"]["quantity"] == 7.0
    replay_markers = [
        row
        for row in replay_le["deadman_applied_fill_watermarks"]
        if row["client_order_id"] == successor_cid
    ]
    assert len(replay_markers) == 1
    assert replay_markers[0]["applied_filled_size"] == 3.0
    assert replay_markers[0]["broker_remaining_quantity"] == 7.0
    assert {
        key: value
        for key, value in replay_le.items()
        if "pnl" in key.lower() or "realized" in key.lower()
    } == pnl_before

    exact_replay = _ensure_initial_deadman(db, sess, adapter)
    assert exact_replay["protected"] is True
    assert adapter.place_calls == []
    assert adapter.cancel_calls == []
    readable, claim = read_action_claim(
        db,
        symbol=context["symbol"],
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    quarantines = claim["metadata"][
        "protective_attribution_quarantine_ledger"
    ]
    assert len(quarantines) == 1
    assert quarantines[0]["successor_applied_fill_baseline"] == 3.0
    assert quarantines[0]["broker_remaining_quantity"] == 7.0
    assert len(quarantines[0]["successor_quarantined_fill_baselines"]) == 2
