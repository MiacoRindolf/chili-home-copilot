from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import alpaca_reconcile as ar
from app.services.trading.momentum_neural import live_runner
from app.services.trading.momentum_neural.adaptive_risk_runtime_contract import (
    build_adaptive_risk_reservation_claim,
)
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    SUBMITTED,
    acquire_action_claim,
    read_action_claim,
    update_action_claim_phase,
)
from tests.test_adaptive_alpaca_lifecycle import (
    _live_session as _adaptive_live_session,
    _reservation_request as _adaptive_reservation_request,
)
from tests.test_alpaca_account_risk_reservations import TEST_ALPACA_ACCOUNT_ID


class _Fresh:
    def __init__(self, age: float = 0.05) -> None:
        self.age = age

    def age_seconds(self) -> float:
        return self.age


def _order(
    *,
    oid: str,
    cid: str,
    symbol: str,
    side: str,
    status: str,
    qty: float,
    filled: float,
    order_type: str = "limit",
    time_in_force: str | None = None,
    extended_hours: bool | None = None,
    position_intent: str | None = None,
    limit_price: float | None = None,
):
    return SimpleNamespace(
        order_id=oid,
        client_order_id=cid,
        product_id=symbol,
        side=side,
        status=status,
        order_type=order_type,
        filled_size=filled,
        average_filled_price=5.0 if filled else None,
        raw={
            "qty": str(qty),
            "time_in_force": time_in_force,
            "extended_hours": extended_hours,
            "position_intent": position_intent,
            "limit_price": limit_price,
        },
    )


def _close_request(*, symbol: str, cid: str, side: str, qty: float) -> dict:
    return {
        "product_id": symbol,
        "side": side,
        "base_size": str(float(qty)),
        "client_order_id": cid,
        "position_intent": "sell_to_close" if side == "sell" else "buy_to_close",
        "order_type": "market",
        "time_in_force": "day",
        "extended_hours": False,
        "limit_price": None,
        "market_session": "regular",
    }


class _Adapter:
    def __init__(self, entry_order, *, signed_qty: float | None) -> None:
        self.entry_order = entry_order
        self.signed_qty = signed_qty
        self.orders_by_cid = {entry_order.client_order_id: entry_order} if entry_order else {}
        self.orders_by_oid = {entry_order.order_id: entry_order} if entry_order else {}
        self.market_calls: list[dict] = []
        self.limit_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self.competing_open_orders: list[object] = []
        self.position_avg_entry_price = 5.0
        self.bbo = SimpleNamespace(
            product_id=(entry_order.product_id if entry_order else "TEST"),
            bid=4.99,
            ask=5.01,
            mid=5.0,
            freshness=_Fresh(),
        )

    def get_order(self, oid):
        return self.orders_by_oid.get(str(oid)), None

    def get_order_by_client_order_id_truth(self, cid):
        order = self.orders_by_cid.get(str(cid))
        return {"readable": True, "found": order is not None, "order": order}

    def get_order_by_client_order_id(self, cid):
        return self.orders_by_cid.get(str(cid)), None

    def get_position_quantity(self, _symbol):
        return self.signed_qty

    def list_positions(self):
        if self.signed_qty is None or abs(float(self.signed_qty)) <= 1e-9:
            return [], None
        return ([{
            "product_id": self.entry_order.product_id,
            "qty": float(self.signed_qty),
            "avg_entry_price": self.position_avg_entry_price,
        }], None)

    def list_open_orders(self, **_kwargs):
        return list(self.competing_open_orders), None

    def cancel_order(self, oid):
        self.cancel_calls.append(str(oid))
        order = self.orders_by_oid[str(oid)]
        order.status = "canceled"
        return {"ok": True}

    def get_execution_bbo(self, symbol, *, max_age_seconds):
        assert max_age_seconds == 2.0
        self.bbo.product_id = symbol
        return self.bbo, self.bbo.freshness

    def _accept(self, kwargs, *, order_type):
        oid = f"close-{len(self.market_calls) + len(self.limit_calls)}"
        order = _order(
            oid=oid,
            cid=kwargs["client_order_id"],
            symbol=kwargs["product_id"],
            side=kwargs["side"],
            status="accepted",
            qty=float(kwargs["base_size"]),
            filled=0.0,
            order_type=order_type,
            time_in_force=str(kwargs.get("time_in_force") or "day"),
            extended_hours=bool(kwargs.get("extended_hours", False)),
            position_intent=str(kwargs.get("position_intent") or ""),
            limit_price=(
                float(kwargs["limit_price"])
                if kwargs.get("limit_price") is not None
                else None
            ),
        )
        self.orders_by_cid[order.client_order_id] = order
        self.orders_by_oid[order.order_id] = order
        return {"ok": True, "order_id": oid, "status": "accepted"}

    def place_market_order(self, **kwargs):
        self.market_calls.append(dict(kwargs))
        return self._accept(kwargs, order_type="market")

    def place_limit_order_gtc(self, **kwargs):
        self.limit_calls.append(dict(kwargs))
        return self._accept(kwargs, order_type="limit")


def _variant(db) -> MomentumStrategyVariant:
    variant = MomentumStrategyVariant(
        family="detached_claim_test",
        variant_key=f"detached-{uuid.uuid4().hex}",
        label="detached claim test",
        params_json={},
    )
    db.add(variant)
    db.flush()
    return variant


def _terminal_owner(db, symbol: str) -> TradingAutomationSession:
    variant = _variant(db)
    session = TradingAutomationSession(
        user_id=None,
        venue="alpaca",
        execution_family="alpaca_spot",
        mode="live",
        symbol=symbol,
        variant_id=variant.id,
        state="live_cancelled",
        risk_snapshot_json={},
        correlation_id=f"detached-{uuid.uuid4().hex}",
    )
    db.add(session)
    db.flush()
    return session


def _seed_entry_claim(
    db,
    *,
    symbol: str,
    side: str,
    qty: float,
    owner_session_id: int | None,
    status: str = "filled",
    filled: float | None = None,
):
    cid = f"entry-{symbol.lower()}-{uuid.uuid4().hex[:8]}"
    oid = f"oid-{symbol.lower()}-{uuid.uuid4().hex[:8]}"
    token = f"token-{uuid.uuid4().hex}"
    result = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=owner_session_id,
        client_order_id=cid,
        metadata={
            "order_role": "primary",
            "order_request": {
                "product_id": symbol,
                "side": side,
                "base_size": qty,
                "limit_price": 5.0,
            },
        },
        account_scope="alpaca:paper",
    )
    assert result["ok"]
    assert update_action_claim_phase(
        db,
        symbol=symbol,
        claim_token=token,
        phase=SUBMITTED,
        client_order_id=cid,
        broker_order_id=oid,
        account_scope="alpaca:paper",
    )
    db.commit()
    return _order(
        oid=oid,
        cid=cid,
        symbol=symbol,
        side=side,
        status=status,
        qty=qty,
        filled=qty if filled is None else filled,
    )


def _seed_complete_adaptive_entry_claim(
    db,
    monkeypatch,
    *,
    symbol: str,
    status: str,
    filled: int,
):
    # Root-owned targeted cleanup now includes the adaptive ledgers and the dependent
    # append-only Alpaca fill/cycle tables. Do not issue a fixture-local TRUNCATE: CASCADE
    # would correctly hit the append-only trigger and make suite order affect the result.
    monkeypatch.setattr(
        ar.settings,
        "chili_alpaca_expected_account_id",
        TEST_ALPACA_ACCOUNT_ID,
        raising=False,
    )
    sess = _adaptive_live_session(db, symbol=symbol)
    cid = f"chili-ar-{uuid.uuid4().hex[:12]}"
    request = _adaptive_reservation_request(symbol=symbol, cid=cid)
    le = dict(sess.risk_snapshot_json[live_runner.KEY_LIVE_EXEC])
    ensured = live_runner._ensure_adaptive_alpaca_reservation(
        sess,
        le,
        request_payload=request.to_payload(),
    )
    if ensured["ok"] is not True:
        rejected_packet = (
            db.query(AdaptiveRiskDecisionPacket)
            .filter(
                AdaptiveRiskDecisionPacket.reservation_request_sha256
                == request.request_sha256
            )
            .one_or_none()
        )
        raise AssertionError(
            {
                "ensured": ensured,
                "decision_packet": (
                    dict(rejected_packet.decision_packet_json)
                    if rejected_packet is not None
                    else None
                ),
            }
        )
    packet_row = db.get(
        AdaptiveRiskDecisionPacket,
        ensured["decision"].decision_packet_sha256,
    )
    assert packet_row is not None
    packet = dict(packet_row.decision_packet_json)
    reservation_claim = build_adaptive_risk_reservation_claim(
        packet,
        claim_id=cid,
    ).to_payload()
    planned = int(ensured["decision"].quantity_shares)
    assert 0 <= filled <= planned
    oid = f"adaptive-entry-{uuid.uuid4().hex[:10]}"
    token = f"adaptive-entry-token-{uuid.uuid4().hex}"
    order_request = {
        "product_id": symbol,
        "side": "buy",
        "base_size": planned,
        "limit_price": request.entry_limit_price,
        "client_order_id": cid,
        "position_intent": "buy_to_open",
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": False,
        "asset_class": "us_equity",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
    }
    metadata = {
        "order_role": "primary",
        "order_request": order_request,
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        "adaptive_risk_decision_packet": packet,
        "adaptive_risk_reservation_claim": reservation_claim,
        "adaptive_risk_reservation_request": request.to_payload(),
        "adaptive_risk_lifecycle_binding": dict(ensured["binding"]),
    }
    acquired = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=int(sess.id),
        client_order_id=cid,
        metadata=metadata,
        account_scope="alpaca:paper",
    )
    assert acquired["ok"]
    assert update_action_claim_phase(
        db,
        symbol=symbol,
        claim_token=token,
        phase=SUBMITTED,
        client_order_id=cid,
        broker_order_id=oid,
        account_scope="alpaca:paper",
    )
    sess.state = "live_cancelled"
    db.commit()
    order = _order(
        oid=oid,
        cid=cid,
        symbol=symbol,
        side="buy",
        status=status,
        qty=planned,
        filled=filled,
        limit_price=request.entry_limit_price,
        time_in_force="day",
        extended_hours=False,
        position_intent="buy_to_open",
    )
    return {
        "session": sess,
        "request": request,
        "order": order,
        "planned": planned,
        "reservation_id": ensured["decision"].reservation_id,
        "store": ensured["store"],
    }


@pytest.mark.parametrize(
    "symbol,entry_side,signed_qty,close_side,intent,missing_owner",
    [
        ("HNDLONG", "buy", 10.0, "sell", "sell_to_close", False),
    ],
)
def test_terminal_owner_handoffs_exact_paper_long_once(
    db,
    monkeypatch,
    symbol,
    entry_side,
    signed_qty,
    close_side,
    intent,
    missing_owner,
):
    monkeypatch.setattr(ar, "market_session_now", lambda _symbol: "regular")
    owner = None if missing_owner else _terminal_owner(db, symbol)
    order = _seed_entry_claim(
        db,
        symbol=symbol,
        side=entry_side,
        qty=abs(signed_qty),
        owner_session_id=(None if owner is None else owner.id),
    )
    adapter = _Adapter(order, signed_qty=signed_qty)

    first = ar._sweep_detached_entry_claims(db, adapter)
    assert first["detached_entry_claims_handed_off"] == 1
    assert first["detached_entry_closes_submitted"] == 1
    calls = adapter.market_calls
    assert len(calls) == 1
    assert calls[0]["side"] == close_side
    assert calls[0]["position_intent"] == intent

    readable, claim = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and claim is not None
    assert claim["action"] == "orphan_flatten"
    assert claim["phase"] == "submitted"
    assert claim["owner_session_id"] is None
    assert claim["metadata"]["entry_handoff_proof"]["entry_broker_order_id"] == order.order_id

    second = ar._sweep_active_orphan_claims(db, adapter)
    assert second["claims_still_pending"] == 1
    assert len(adapter.market_calls) == 1


@pytest.mark.parametrize("case", ["quantity_mismatch", "average_mismatch", "open_order"])
def test_detached_entry_handoff_requires_exact_uncontested_single_lot(
    db,
    case,
):
    symbol = f"STRICT{uuid.uuid4().hex[:5].upper()}"
    owner = _terminal_owner(db, symbol)
    order = _seed_entry_claim(
        db,
        symbol=symbol,
        side="buy",
        qty=10.0,
        owner_session_id=owner.id,
    )
    adapter = _Adapter(
        order,
        signed_qty=(12.0 if case == "quantity_mismatch" else 10.0),
    )
    if case == "average_mismatch":
        adapter.position_avg_entry_price = 5.25
    if case == "open_order":
        adapter.competing_open_orders = [
            SimpleNamespace(order_id="manual-open", product_id=symbol)
        ]

    result = ar._sweep_detached_entry_claims(db, adapter)

    assert result["detached_entry_claims_handed_off"] == 0
    assert result["detached_entry_claims_quarantined"] == 1
    assert adapter.market_calls == []
    assert adapter.limit_calls == []
    assert adapter.cancel_calls == []
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    assert claim["action"] == "entry"


@pytest.mark.parametrize(
    "side,signed_qty,expected_ok",
    [("sell", 5.0, True), ("buy", -5.0, False)],
)
def test_premarket_handoff_uses_fresh_extended_limit_and_no_quote_blocks(
    monkeypatch,
    side,
    signed_qty,
    expected_ok,
):
    monkeypatch.setattr(ar, "market_session_now", lambda _symbol: "premarket")
    entry_side = "buy" if side == "sell" else "sell"
    adapter = _Adapter(
        _order(
            oid="entry",
            cid="entry-cid",
            symbol="PREMKT",
            side=entry_side,
            status="filled",
            qty=5.0,
            filled=5.0,
        ),
        signed_qty=signed_qty,
    )
    result = ar._place_alpaca_equity_close(
        adapter,
        symbol="PREMKT",
        close_side=side,
        quantity=5.0,
        client_order_id="close-cid",
    )
    if not expected_ok:
        assert result["pre_place_blocked"] is True
        assert result["execution_quarantined"] is True
        assert result["transport_attempted"] is False
        assert adapter.market_calls == []
        assert adapter.limit_calls == []
        return
    assert result["ok"] is True
    assert adapter.market_calls == []
    assert len(adapter.limit_calls) == 1
    assert adapter.limit_calls[0]["extended_hours"] is True
    assert adapter.limit_calls[0]["position_intent"] == (
        "sell_to_close" if side == "sell" else "buy_to_close"
    )

    adapter.bbo = None
    blocked = ar._place_alpaca_equity_close(
        adapter,
        symbol="PREMKT",
        close_side=side,
        quantity=5.0,
        client_order_id="close-cid-2",
    )
    assert blocked["pre_place_blocked"] is True
    assert len(adapter.limit_calls) == 1


@pytest.mark.parametrize(
    "status,filled,close_side,signed_qty,expected_rotation",
    [
        ("rejected", 0.0, "sell", 9.0, True),
        ("expired", 0.0, "buy", -9.0, False),
        ("canceled", 4.0, "sell", 5.0, True),
        ("canceled", 3.0, "buy", -6.0, False),
    ],
)
def test_terminal_close_residual_rotates_deterministically_and_retries_once(
    db,
    monkeypatch,
    status,
    filled,
    close_side,
    signed_qty,
    expected_rotation,
):
    monkeypatch.setattr(ar, "market_session_now", lambda _symbol: "regular")
    symbol = f"RES{uuid.uuid4().hex[:5].upper()}"
    cid = f"orphrec-{symbol}-first"
    token = f"orphan-first-{uuid.uuid4().hex}"
    oid = f"close-first-{uuid.uuid4().hex[:8]}"
    result = acquire_action_claim(
        db,
        symbol=symbol,
        action="orphan_flatten",
        claim_token=token,
        owner_session_id=None,
        client_order_id=cid,
        metadata={
            "terminal_entry_handoff": True,
            "entry_handoff_proof": {
                "proof_version": "durable_entry_claim_handoff_v1",
                "entry_claim_token": "entry-root",
                "entry_client_order_id": f"entry-{symbol}",
                "entry_broker_order_id": f"entry-oid-{symbol}",
                "entry_account_scope": "alpaca:paper",
                "entry_side": "buy" if close_side == "sell" else "sell",
                "entry_filled_size": 9.0,
                "entry_average_filled_price": 5.0,
                "broker_position_qty": 9.0 if close_side == "sell" else -9.0,
                "broker_position_avg_entry_price": 5.0,
                "no_competing_open_orders": True,
            },
            "close_side": close_side,
            "close_attempt_no": 1,
            "close_attempt_history": [],
            "qty": 9.0,
            "close_request": _close_request(
                symbol=symbol,
                cid=cid,
                side=close_side,
                qty=9.0,
            ),
        },
        account_scope="alpaca:paper",
    )
    assert result["ok"]
    assert update_action_claim_phase(
        db,
        symbol=symbol,
        claim_token=token,
        phase=SUBMITTED,
        client_order_id=cid,
        broker_order_id=oid,
        account_scope="alpaca:paper",
    )
    db.commit()
    close_order = _order(
        oid=oid,
        cid=cid,
        symbol=symbol,
        side=close_side,
        status=status,
        qty=9.0,
        filled=filled,
        order_type="market",
        time_in_force="day",
        extended_hours=False,
        position_intent=(
            "sell_to_close" if close_side == "sell" else "buy_to_close"
        ),
    )
    adapter = _Adapter(close_order, signed_qty=signed_qty)

    first = ar._sweep_active_orphan_claims(db, adapter)
    if not expected_rotation:
        assert first["claims_residual_rotated"] == 0
        assert first["claims_quarantined"] == 1
        assert adapter.market_calls == []
        readable, unchanged = read_action_claim(
            db,
            symbol=symbol,
            account_scope="alpaca:paper",
        )
        assert readable and unchanged is not None
        assert unchanged["claim_token"] == token
        assert unchanged["client_order_id"] == cid
        return
    assert first["claims_residual_rotated"] == 1
    assert len(adapter.market_calls) == 1
    readable, rotated = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and rotated is not None
    assert rotated["claim_token"] != token
    assert rotated["client_order_id"] != cid
    assert rotated["metadata"]["close_attempt_no"] == 2
    assert rotated["metadata"]["close_attempt_history"][-1]["status"] == status
    deterministic_cid = rotated["client_order_id"]

    second = ar._sweep_active_orphan_claims(db, adapter)
    assert second["claims_still_pending"] == 1
    assert len(adapter.market_calls) == 1
    readable, same = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and same is not None
    assert same["client_order_id"] == deterministic_cid


@pytest.mark.parametrize("status,filled", [("canceled", 0.0), ("filled", 10.0)])
def test_terminal_entry_with_broker_flat_resolves_without_close_post(
    db,
    monkeypatch,
    status,
    filled,
):
    monkeypatch.setattr(ar, "market_session_now", lambda _symbol: "regular")
    symbol = f"FLAT{uuid.uuid4().hex[:4].upper()}"
    owner = _terminal_owner(db, symbol)
    order = _seed_entry_claim(
        db,
        symbol=symbol,
        side="buy",
        qty=10.0,
        owner_session_id=owner.id,
        status=status,
        filled=filled,
    )
    adapter = _Adapter(order, signed_qty=0.0)

    result = ar._sweep_detached_entry_claims(db, adapter)
    assert result["detached_entry_claims_resolved"] == 1
    assert adapter.market_calls == []
    readable, claim = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and claim is not None
    assert claim["phase"] == "resolved"


def test_adaptive_partial_fill_handoff_tracks_open_remainder_and_flat_atomically(
    db,
    monkeypatch,
):
    monkeypatch.setattr(ar, "market_session_now", lambda _symbol: "regular")
    symbol = f"A{uuid.uuid4().hex[:3].upper()}"
    seeded = _seed_complete_adaptive_entry_claim(
        db,
        monkeypatch,
        symbol=symbol,
        status="canceled",
        filled=1,
    )
    planned = int(seeded["planned"])
    filled = max(1, planned // 2)
    seeded["order"].filled_size = float(filled)
    adapter = _Adapter(seeded["order"], signed_qty=float(filled))

    handed = ar._sweep_detached_entry_claims(db, adapter)

    assert handed["detached_entry_claims_handed_off"] == 1
    assert handed["detached_entry_closes_submitted"] == 1
    state = seeded["store"].read_state(seeded["reservation_id"])
    assert state.state == "filled"
    assert state.cumulative_filled_quantity_shares == filled
    assert state.open_quantity_shares == filled
    assert float(state.pending_structural_risk_usd) == 0.0
    readable, close_claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and close_claim is not None
    assert close_claim["action"] == "orphan_flatten"
    binding = close_claim["metadata"]["adaptive_risk_lifecycle_binding"]
    assert binding["cumulative_filled_quantity_shares"] == filled
    assert binding["open_quantity_shares"] == filled

    close_order = adapter.orders_by_oid[close_claim["broker_order_id"]]
    close_order.status = "filled"
    close_order.filled_size = float(filled)
    close_order.raw["alpaca_status"] = "filled"
    adapter.signed_qty = 0.0
    closed = ar._sweep_active_orphan_claims(db, adapter)

    # The fake adapter deliberately has no authoritative account-activity
    # reader. Broker-flat/order status alone cannot self-certify fill economics,
    # so the exact claim and owned risk stay retained for a later same-CID read.
    assert closed["claims_recovered"] == 0
    assert closed["claims_still_pending"] == 1
    durable = seeded["store"].read_state(seeded["reservation_id"])
    assert durable.state == "filled"
    assert durable.open_quantity_shares == filled
    assert durable.opportunity_status == "consumed"
    readable, retained = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and retained is not None
    assert retained["phase"] != "resolved"

    replayed = ar._sweep_active_orphan_claims(db, adapter)
    assert replayed["claims_still_pending"] == 1
    assert seeded["store"].read_state(seeded["reservation_id"]).state == "filled"


def test_adaptive_terminal_zero_fill_releases_opportunity_idempotently(
    db,
    monkeypatch,
):
    symbol = f"Z{uuid.uuid4().hex[:3].upper()}"
    seeded = _seed_complete_adaptive_entry_claim(
        db,
        monkeypatch,
        symbol=symbol,
        status="canceled",
        filled=0,
    )
    adapter = _Adapter(seeded["order"], signed_qty=0.0)

    first = ar._sweep_detached_entry_claims(db, adapter)
    second = ar._sweep_detached_entry_claims(db, adapter)

    assert first["detached_entry_claims_resolved"] == 1
    assert second["detached_entry_claims_resolved"] == 0
    state = seeded["store"].read_state(seeded["reservation_id"])
    assert state.state == "released"
    assert state.opportunity_status == "available"
    assert state.cumulative_filled_quantity_shares == 0
    assert adapter.market_calls == []
    assert adapter.cancel_calls == []


def test_partial_adaptive_marker_quarantines_before_any_broker_mutation(
    db,
):
    symbol = f"APM{uuid.uuid4().hex[:4].upper()}"
    owner = _terminal_owner(db, symbol)
    order = _seed_entry_claim(
        db,
        symbol=symbol,
        side="buy",
        qty=10.0,
        owner_session_id=owner.id,
        status="open",
        filled=0.0,
    )
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    metadata = dict(claim["metadata"])
    metadata["adaptive_risk_reservation_request"] = {"partial": True}
    db.execute(
        text(
            "UPDATE broker_symbol_action_claims "
            "SET metadata_json = CAST(:metadata AS jsonb) "
            "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
        ),
        {"metadata": json.dumps(metadata), "symbol": symbol},
    )
    db.commit()
    adapter = _Adapter(order, signed_qty=0.0)

    result = ar._sweep_detached_entry_claims(db, adapter)

    assert result["detached_adaptive_lifecycle_quarantined"] == 1
    assert adapter.cancel_calls == []
    assert adapter.market_calls == []
    assert adapter.limit_calls == []


@pytest.mark.parametrize("mismatch", ["connection_generation", "account_identity"])
def test_complete_adaptive_claim_identity_mismatch_is_broker_dark(
    db,
    monkeypatch,
    mismatch,
):
    symbol = f"I{uuid.uuid4().hex[:3].upper()}"
    seeded = _seed_complete_adaptive_entry_claim(
        db,
        monkeypatch,
        symbol=symbol,
        status="open",
        filled=0,
    )
    if mismatch == "connection_generation":
        readable, claim = read_action_claim(
            db,
            symbol=symbol,
            account_scope="alpaca:paper",
        )
        assert readable and claim is not None
        metadata = dict(claim["metadata"])
        binding = dict(metadata["adaptive_risk_lifecycle_binding"])
        binding["connection_generation"] = "alpaca-arm:wrong-generation"
        metadata["adaptive_risk_lifecycle_binding"] = binding
        db.execute(
            text(
                "UPDATE broker_symbol_action_claims "
                "SET metadata_json = CAST(:metadata AS jsonb) "
                "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
            ),
            {"metadata": json.dumps(metadata), "symbol": symbol},
        )
        db.commit()
    else:
        monkeypatch.setattr(
            ar.settings,
            "chili_alpaca_expected_account_id",
            "different-paper-account",
            raising=False,
        )
    adapter = _Adapter(seeded["order"], signed_qty=0.0)

    result = ar._sweep_detached_entry_claims(db, adapter)

    assert result["detached_adaptive_lifecycle_quarantined"] == 1
    assert adapter.cancel_calls == []
    assert adapter.market_calls == []
    assert adapter.limit_calls == []


def test_adaptive_replaced_entry_waits_for_exact_successor_lineage(
    db,
    monkeypatch,
):
    symbol = f"R{uuid.uuid4().hex[:3].upper()}"
    seeded = _seed_complete_adaptive_entry_claim(
        db,
        monkeypatch,
        symbol=symbol,
        status="pending",
        filled=0,
    )
    seeded["order"].raw["alpaca_status"] = "replaced"
    seeded["order"].raw["replaced_by"] = f"successor-{uuid.uuid4().hex[:8]}"
    adapter = _Adapter(seeded["order"], signed_qty=0.0)

    result = ar._sweep_detached_entry_claims(db, adapter)

    assert result["detached_replacement_lineage_unresolved"] == 1
    assert result["detached_entry_claims_pending"] == 1
    assert adapter.cancel_calls == []
    assert adapter.market_calls == []
    state = seeded["store"].read_state(seeded["reservation_id"])
    assert state.state == "reserved"


def _seed_orphan_claim(
    db,
    *,
    symbol: str,
    cid: str,
    token: str,
    side: str,
    qty: float,
    attempt_no: int = 1,
    submitted_oid: str | None = None,
    frozen: bool = False,
):
    entry_side = "buy" if side == "sell" else "sell"
    signed_entry_qty = qty if entry_side == "buy" else -qty
    metadata = {
        "terminal_entry_handoff": True,
        "entry_handoff_proof": {
            "proof_version": "durable_entry_claim_handoff_v1",
            "entry_claim_token": "entry-root",
            "entry_client_order_id": f"entry-{symbol}",
            "entry_broker_order_id": f"entry-oid-{symbol}",
            "entry_account_scope": "alpaca:paper",
            "entry_side": entry_side,
            "entry_filled_size": qty,
            "entry_average_filled_price": 5.0,
            "broker_position_qty": signed_entry_qty,
            "broker_position_avg_entry_price": 5.0,
            "no_competing_open_orders": True,
        },
        "close_side": side,
        "close_attempt_no": attempt_no,
        "close_attempt_history": [],
        "qty": qty,
    }
    if frozen or submitted_oid is not None:
        metadata["close_request"] = _close_request(
            symbol=symbol,
            cid=cid,
            side=side,
            qty=qty,
        )
    acquired = acquire_action_claim(
        db,
        symbol=symbol,
        action="orphan_flatten",
        claim_token=token,
        owner_session_id=None,
        client_order_id=cid,
        metadata=metadata,
        account_scope="alpaca:paper",
    )
    assert acquired["ok"]
    if submitted_oid is not None:
        assert update_action_claim_phase(
            db,
            symbol=symbol,
            claim_token=token,
            phase=SUBMITTED,
            client_order_id=cid,
            broker_order_id=submitted_oid,
            account_scope="alpaca:paper",
        )
    db.commit()


def _expire_claim_lease(db, symbol: str) -> None:
    db.execute(
        text(
            "UPDATE broker_symbol_action_claims "
            "SET lease_expires_at = NOW() - interval '1 second' "
            "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
        ),
        {"symbol": symbol},
    )
    db.commit()


def test_close_request_is_durable_before_timeout_and_restart_recovers_once(db, monkeypatch):
    monkeypatch.setattr(ar, "market_session_now", lambda _symbol: "regular")
    symbol = f"TIME{uuid.uuid4().hex[:4].upper()}"
    cid = f"orphrec-{symbol}-timeout"
    token = f"orphan-timeout-{uuid.uuid4().hex}"
    _seed_orphan_claim(
        db,
        symbol=symbol,
        cid=cid,
        token=token,
        side="sell",
        qty=6.0,
    )
    readable, claim = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and claim is not None
    adapter = _Adapter(None, signed_qty=6.0)

    def _accepted_then_timeout(**kwargs):
        adapter.market_calls.append(dict(kwargs))
        adapter._accept(kwargs, order_type="market")
        return {"ok": False, "error": "TimeoutError"}

    adapter.place_market_order = _accepted_then_timeout
    submitted = ar._submit_handoff_close(adapter, claim)
    assert submitted["recovered"] is True
    assert len(adapter.market_calls) == 1
    readable, durable = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and durable is not None
    assert durable["phase"] == "submitted"
    assert durable["metadata"]["close_request"]["base_size"] == "6.0"

    restarted = ar._sweep_active_orphan_claims(db, adapter)
    assert restarted["claims_still_pending"] == 1
    assert len(adapter.market_calls) == 1


@pytest.mark.parametrize("status", ["canceled", "expired"])
def test_terminal_handoff_close_and_broker_flat_resolves_exact_claim(db, status):
    symbol = f"ZFLAT{uuid.uuid4().hex[:3].upper()}"
    cid = f"orphrec-{symbol}-flat"
    token = f"orphan-flat-{uuid.uuid4().hex}"
    oid = f"close-flat-{uuid.uuid4().hex[:8]}"
    _seed_orphan_claim(
        db,
        symbol=symbol,
        cid=cid,
        token=token,
        side="sell",
        qty=4.0,
        submitted_oid=oid,
    )
    order = _order(
        oid=oid,
        cid=cid,
        symbol=symbol,
        side="sell",
        status=status,
        qty=4.0,
        filled=0.0,
        order_type="market",
        time_in_force="day",
        extended_hours=False,
        position_intent="sell_to_close",
    )
    adapter = _Adapter(order, signed_qty=0.0)

    result = ar._sweep_active_orphan_claims(db, adapter)
    assert result["claims_still_pending"] == 0
    readable, resolved = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and resolved is not None
    assert resolved["phase"] == "resolved"
    assert adapter.market_calls == []


def test_residual_retry_authority_continues_after_eight_attempts(db, monkeypatch):
    monkeypatch.setattr(ar, "market_session_now", lambda _symbol: "regular")
    symbol = f"NOCAP{uuid.uuid4().hex[:3].upper()}"
    cid = f"orphrec-{symbol}-nine"
    token = f"orphan-nine-{uuid.uuid4().hex}"
    oid = f"close-nine-{uuid.uuid4().hex[:8]}"
    _seed_orphan_claim(
        db,
        symbol=symbol,
        cid=cid,
        token=token,
        side="sell",
        qty=4.0,
        attempt_no=9,
        submitted_oid=oid,
    )
    order = _order(
        oid=oid,
        cid=cid,
        symbol=symbol,
        side="sell",
        status="rejected",
        qty=4.0,
        filled=0.0,
        order_type="market",
        time_in_force="day",
        extended_hours=False,
        position_intent="sell_to_close",
    )
    adapter = _Adapter(order, signed_qty=4.0)

    result = ar._sweep_active_orphan_claims(db, adapter)
    assert result["claims_residual_rotated"] == 1
    assert len(adapter.market_calls) == 1
    readable, successor = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and successor is not None
    assert successor["metadata"]["close_attempt_no"] == 10
    assert successor["metadata"]["residual_retry_authority_exhausted"] is False
    assert len(successor["metadata"]["close_attempt_history"]) <= 8


@pytest.mark.parametrize("raw_qty", [None, "3.0"])
def test_missing_or_wrong_broker_qty_cannot_adopt_or_rotate_claim(db, raw_qty):
    symbol = f"BADQ{uuid.uuid4().hex[:4].upper()}"
    cid = f"orphrec-{symbol}-qty"
    token = f"orphan-qty-{uuid.uuid4().hex}"
    oid = f"close-qty-{uuid.uuid4().hex[:8]}"
    _seed_orphan_claim(
        db,
        symbol=symbol,
        cid=cid,
        token=token,
        side="sell",
        qty=4.0,
        submitted_oid=oid,
    )
    order = _order(
        oid=oid,
        cid=cid,
        symbol=symbol,
        side="sell",
        status="rejected",
        qty=4.0,
        filled=0.0,
        order_type="market",
        time_in_force="day",
        extended_hours=False,
        position_intent="sell_to_close",
    )
    order.raw["qty"] = raw_qty
    adapter = _Adapter(order, signed_qty=4.0)

    result = ar._sweep_active_orphan_claims(db, adapter)
    assert result["claims_residual_rotated"] == 0
    assert result["claims_still_pending"] == 1
    readable, unchanged = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and unchanged is not None
    assert unchanged["claim_token"] == token
    assert adapter.market_calls == []


def test_strict_absent_frozen_cid_never_mints_a_successor(db, monkeypatch):
    monkeypatch.setattr(ar, "market_session_now", lambda _symbol: "regular")
    symbol = f"ABS{uuid.uuid4().hex[:5].upper()}"
    cid = f"orphrec-{symbol}-old"
    token = f"orphan-absent-{uuid.uuid4().hex}"
    _seed_orphan_claim(
        db,
        symbol=symbol,
        cid=cid,
        token=token,
        side="sell",
        qty=5.0,
        frozen=True,
    )
    _expire_claim_lease(db, symbol)
    adapter = _Adapter(None, signed_qty=5.0)

    result = ar._sweep_active_orphan_claims(db, adapter)
    assert result["claims_residual_rotated"] == 0
    assert result["claims_still_pending"] == 1
    assert adapter.market_calls == []
    readable, retained = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and retained is not None
    assert retained["claim_token"] == token
    assert retained["client_order_id"] == cid


def test_unknown_frozen_cid_truth_authorizes_zero_posts(db, monkeypatch):
    monkeypatch.setattr(ar, "market_session_now", lambda _symbol: "regular")
    symbol = f"UNK{uuid.uuid4().hex[:5].upper()}"
    cid = f"orphrec-{symbol}-unknown"
    token = f"orphan-unknown-{uuid.uuid4().hex}"
    _seed_orphan_claim(
        db,
        symbol=symbol,
        cid=cid,
        token=token,
        side="sell",
        qty=5.0,
        frozen=True,
    )
    _expire_claim_lease(db, symbol)
    adapter = _Adapter(None, signed_qty=5.0)
    adapter.get_order_by_client_order_id_truth = lambda _cid: {
        "readable": False,
        "found": False,
        "order": None,
    }

    result = ar._sweep_active_orphan_claims(db, adapter)
    assert result["claims_residual_rotated"] == 0
    assert result["claims_still_pending"] == 1
    assert adapter.market_calls == []
    readable, unchanged = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and unchanged is not None
    assert unchanged["client_order_id"] == cid


def test_old_frozen_cid_found_by_strict_truth_prevents_rotation(db, monkeypatch):
    monkeypatch.setattr(ar, "market_session_now", lambda _symbol: "regular")
    symbol = f"LATE{uuid.uuid4().hex[:4].upper()}"
    cid = f"orphrec-{symbol}-late"
    token = f"orphan-late-{uuid.uuid4().hex}"
    _seed_orphan_claim(
        db,
        symbol=symbol,
        cid=cid,
        token=token,
        side="sell",
        qty=5.0,
        frozen=True,
    )
    _expire_claim_lease(db, symbol)
    late = _order(
        oid=f"late-{uuid.uuid4().hex[:8]}",
        cid=cid,
        symbol=symbol,
        side="sell",
        status="accepted",
        qty=5.0,
        filled=0.0,
        order_type="market",
        time_in_force="day",
        extended_hours=False,
        position_intent="sell_to_close",
    )
    adapter = _Adapter(late, signed_qty=5.0)
    adapter.get_order_by_client_order_id = lambda _cid: (None, None)

    result = ar._sweep_active_orphan_claims(db, adapter)
    assert result["claims_recovered"] == 1
    assert result["claims_residual_rotated"] == 0
    assert adapter.market_calls == []
    readable, same = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and same is not None
    assert same["client_order_id"] == cid


def test_old_generic_unclaimed_orphan_claim_has_zero_broker_authority(db):
    symbol = f"OLDGEN{uuid.uuid4().hex[:5].upper()}"
    token = f"orphan-old-generic-{uuid.uuid4().hex}"
    cid = f"orphrec-{symbol}-old-generic"
    acquired = acquire_action_claim(
        db,
        symbol=symbol,
        action="orphan_flatten",
        claim_token=token,
        owner_session_id=None,
        client_order_id=cid,
        metadata={
            "qty": 100.0,
            "close_side": "sell",
            "position_intent": "sell_to_close",
            "historical_position_inference": True,
        },
        account_scope="alpaca:paper",
    )
    assert acquired["ok"]
    db.commit()

    class _NoBrokerAuthority:
        def __getattr__(self, name):
            pytest.fail(f"unsafe generic claim attempted broker call: {name}")

    result = ar._sweep_active_orphan_claims(db, _NoBrokerAuthority())

    assert result["unsafe_unclaimed_orphan_claims_quarantined"] == 1
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    assert claim["claim_token"] == token
    assert claim["phase"] == "claimed"


def test_generic_sweep_never_rotates_runner_close_only_claim_into_manual_floor(db):
    symbol = f"LCAP{uuid.uuid4().hex[:5].upper()}"
    cid = f"chili-lco-{uuid.uuid4().hex[:16]}"
    token = f"legacy-close-{uuid.uuid4().hex}"
    oid = f"legacy-close-oid-{uuid.uuid4().hex[:8]}"
    acquired = acquire_action_claim(
        db,
        symbol=symbol,
        action="orphan_flatten",
        claim_token=token,
        owner_session_id=123,
        client_order_id=cid,
        metadata={
            "runner_emergency_close_only": True,
            "close_side": "sell",
            "position_intent": "sell_to_close",
            "max_close_qty": 10.0,
            "broker_position_qty_at_recertification": 100.0,
            "close_request": _close_request(
                symbol=symbol,
                cid=cid,
                side="sell",
                qty=10.0,
            ),
        },
        account_scope="alpaca:paper",
    )
    assert acquired["ok"]
    assert update_action_claim_phase(
        db,
        symbol=symbol,
        claim_token=token,
        phase=SUBMITTED,
        client_order_id=cid,
        broker_order_id=oid,
        account_scope="alpaca:paper",
    )
    db.commit()
    partial = _order(
        oid=oid,
        cid=cid,
        symbol=symbol,
        side="sell",
        status="canceled",
        qty=10.0,
        filled=5.0,
        order_type="market",
        time_in_force="day",
        extended_hours=False,
        position_intent="sell_to_close",
    )
    adapter = _Adapter(partial, signed_qty=95.0)

    result = ar._sweep_active_orphan_claims(db, adapter)

    assert result["runner_emergency_close_only_claims_skipped"] == 1
    assert result["claims_residual_rotated"] == 0
    assert adapter.market_calls == []
    assert adapter.limit_calls == []
    readable, same = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and same is not None
    assert same["claim_token"] == token
    assert same["client_order_id"] == cid
