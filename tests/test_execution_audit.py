from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models import ScanPattern, Trade, User
from app.services.trading.execution_audit import (
    _attach_order_state_projection_payload,
    _execution_event_has_real_fill,
    _execution_event_realized_slippage_bps,
    _order_state_projection_payload,
    aggregate_execution_events_for_pattern,
    apply_execution_event_to_trade,
    normalize_coinbase_order_event,
    normalize_robinhood_order_event,
    record_execution_event,
)
from app.services.trading.venue.order_state_machine import OrderState


def test_normalize_robinhood_partial_fill_event():
    trade = SimpleNamespace(ticker="AAPL", broker_order_id="rh-1", tca_reference_entry_price=100.0)
    event = normalize_robinhood_order_event(
        order={
            "id": "rh-1",
            "state": "partially_filled",
            "quantity": "10",
            "cumulative_quantity": "4",
            "average_price": "100.5",
            "created_at": "2026-04-10T12:00:00Z",
            "updated_at": "2026-04-10T12:00:03Z",
        },
        trade=trade,
    )
    assert event["event_type"] == "partial_fill"
    assert event["requested_quantity"] == 10.0
    assert event["cumulative_filled_quantity"] == 4.0
    assert event["realized_slippage_bps"] == 50.0


def test_order_state_projection_payload_serializes_state_enums():
    result = SimpleNamespace(
        wrote=True,
        reason="ok",
        from_state=None,
        to_state=OrderState.ACK,
        order_id="order-1",
        client_order_id="client-1",
    )

    out = _order_state_projection_payload(result)

    assert out == {
        "wrote": True,
        "reason": "ok",
        "from_state": None,
        "to_state": "ack",
        "order_id": "order-1",
        "client_order_id": "client-1",
    }


def test_attach_order_state_projection_payload_preserves_existing_payload():
    event = SimpleNamespace(payload_json={"broker": {"status": "open"}})

    _attach_order_state_projection_payload(
        event,
        {
            "wrote": False,
            "reason": "unknown_broker_status",
            "to_state": OrderState.ACK,
        },
    )

    assert event.payload_json["broker"] == {"status": "open"}
    assert event.payload_json["order_state_projection"] == {
        "wrote": False,
        "reason": "unknown_broker_status",
        "to_state": "ack",
    }


def test_normalize_robinhood_option_event_uses_option_family():
    trade = SimpleNamespace(
        ticker="SPY",
        broker_order_id="opt-rh-1",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        tca_reference_entry_price=1.25,
    )

    event = normalize_robinhood_order_event(
        order={
            "id": "opt-rh-1",
            "state": "filled",
            "quantity": "1",
            "cumulative_quantity": "1",
            "average_price": "1.30",
        },
        trade=trade,
    )

    assert event["venue"] == "robinhood"
    assert event["broker_source"] == "robinhood"
    assert event["execution_family"] == "robinhood_options"
    assert event["average_fill_price"] == 1.30


def test_normalize_robinhood_sparse_option_asset_class_uses_option_family():
    trade = SimpleNamespace(
        ticker="SPY",
        broker_order_id="opt-rh-asset-class",
        asset_kind=None,
        tags=None,
        indicator_snapshot={"asset_class": "options"},
        tca_reference_entry_price=1.25,
    )

    event = normalize_robinhood_order_event(
        order={
            "id": "opt-rh-asset-class",
            "state": "filled",
            "quantity": "1",
            "cumulative_quantity": "1",
            "average_price": "1.30",
        },
        trade=trade,
    )

    assert event["execution_family"] == "robinhood_options"


def test_normalize_robinhood_sparse_option_multiplier_uses_option_family():
    trade = SimpleNamespace(
        ticker="SPY",
        broker_order_id="opt-rh-multiplier",
        asset_kind=None,
        tags=None,
        indicator_snapshot={"breakout_alert": {"contract_multiplier": 100.0}},
        tca_reference_entry_price=1.25,
    )

    event = normalize_robinhood_order_event(
        order={
            "id": "opt-rh-multiplier",
            "state": "filled",
            "quantity": "1",
            "cumulative_quantity": "1",
            "average_price": "1.30",
        },
        trade=trade,
    )

    assert event["execution_family"] == "robinhood_options"


def test_normalize_coinbase_fill_event():
    trade = SimpleNamespace(ticker="BTC-USD", broker_order_id="cb-1", tca_reference_entry_price=50000.0)
    event = normalize_coinbase_order_event(
        order={
            "order_id": "cb-1",
            "client_order_id": "cid-1",
            "status": "FILLED",
            "product_id": "BTC-USD",
            "base_size": "0.25",
            "filled_size": "0.25",
            "average_filled_price": "50100",
            "created_time": "2026-04-10T12:00:00Z",
            "last_fill_time": "2026-04-10T12:00:02Z",
        },
        trade=trade,
    )
    assert event["event_type"] == "fill"
    assert event["client_order_id"] == "cid-1"
    assert event["requested_quantity"] == 0.25
    assert event["cumulative_filled_quantity"] == 0.25


def test_normalize_coinbase_payload_json_stringifies_sdk_objects():
    class SdkOnlyObject:
        def __str__(self) -> str:
            return "sdk-order-configuration"

    event = normalize_coinbase_order_event(
        order={
            "order_id": "cb-open-1",
            "status": "OPEN",
            "product_id": "THQ-USD",
            "order_configuration": SdkOnlyObject(),
        },
        trade=SimpleNamespace(
            ticker="THQ-USD",
            broker_order_id="cb-open-1",
            tca_reference_entry_price=0.02063,
        ),
    )

    assert event["payload_json"]["order_configuration"] == "sdk-order-configuration"


def test_normalize_coinbase_open_zero_fill_has_no_slippage():
    event = normalize_coinbase_order_event(
        order={
            "order_id": "cb-open-zero-fill",
            "status": "OPEN",
            "product_id": "THQ-USD",
            "base_size": "100",
            "filled_size": "0",
            "average_filled_price": "0",
        },
        trade=SimpleNamespace(
            ticker="THQ-USD",
            broker_order_id="cb-open-zero-fill",
            tca_reference_entry_price=0.02063,
        ),
    )

    assert event["event_type"] == "ack"
    assert event["cumulative_filled_quantity"] == 0.0
    assert event["average_fill_price"] is None
    assert event["realized_slippage_bps"] is None


def test_record_execution_event_sanitizes_zero_average_fill_price(monkeypatch):
    class FakeExecutionEvent(SimpleNamespace):
        pass

    captured = []
    fake_db = SimpleNamespace(
        add=lambda row: captured.append(row),
        flush=lambda: None,
    )
    monkeypatch.setattr(
        "app.models.trading.TradingExecutionEvent",
        FakeExecutionEvent,
    )
    monkeypatch.setattr(
        "app.services.trading.execution_audit._resolve_position_id_for_event",
        lambda *a, **k: None,
    )

    event = record_execution_event(
        fake_db,
        user_id=None,
        ticker="THQ-USD",
        broker_source="coinbase",
        order_id="cb-open-zero-fill",
        event_type="ack",
        status="open",
        requested_quantity=100.0,
        cumulative_filled_quantity=0.0,
        average_fill_price=0.0,
        reference_price=0.02063,
        realized_slippage_bps=-10000.0,
        payload_json={"status": "OPEN"},
        apply_to_trade=False,
    )

    assert captured == [event]
    assert event.average_fill_price is None
    assert event.realized_slippage_bps is None


def test_record_execution_event_keeps_real_partial_fill_slippage(monkeypatch):
    class FakeExecutionEvent(SimpleNamespace):
        pass

    captured = []
    fake_db = SimpleNamespace(
        add=lambda row: captured.append(row),
        flush=lambda: None,
    )
    monkeypatch.setattr(
        "app.models.trading.TradingExecutionEvent",
        FakeExecutionEvent,
    )
    monkeypatch.setattr(
        "app.services.trading.execution_audit._resolve_position_id_for_event",
        lambda *a, **k: None,
    )

    event = record_execution_event(
        fake_db,
        user_id=None,
        ticker="BTC-USD",
        broker_source="coinbase",
        order_id="cb-partial-fill",
        event_type="partial_fill",
        status="partially_filled",
        requested_quantity=1.0,
        cumulative_filled_quantity=0.25,
        last_fill_quantity=0.25,
        average_fill_price=50100.0,
        reference_price=50000.0,
        realized_slippage_bps=20.0,
        payload_json={"status": "PARTIALLY_FILLED"},
        apply_to_trade=False,
    )

    assert captured == [event]
    assert event.average_fill_price == 50100.0
    assert event.realized_slippage_bps == 20.0


def test_execution_event_reader_ignores_zero_fill_slippage_noise():
    event = SimpleNamespace(
        event_type="status",
        status="open",
        cumulative_filled_quantity=0.0,
        last_fill_quantity=None,
        average_fill_price=0.0,
        realized_slippage_bps=-10000.0,
    )

    assert _execution_event_has_real_fill(event) is False
    assert _execution_event_realized_slippage_bps(event) is None


def test_execution_event_reader_keeps_real_partial_fill_slippage():
    event = SimpleNamespace(
        event_type="partial_fill",
        status="partially_filled",
        cumulative_filled_quantity=0.25,
        last_fill_quantity=0.25,
        average_fill_price=50100.0,
        realized_slippage_bps=20.0,
    )

    assert _execution_event_has_real_fill(event) is True
    assert _execution_event_realized_slippage_bps(event) == 20.0


def test_apply_execution_event_to_trade_partial_fill_updates_fill_state():
    trade = SimpleNamespace(
        quantity=10.0,
        filled_quantity=None,
        remaining_quantity=None,
        submitted_at=None,
        acknowledged_at=None,
        first_fill_at=None,
        last_fill_at=None,
        filled_at=None,
        avg_fill_price=None,
        entry_price=100.0,
        status="working",
        broker_status="queued",
        last_broker_sync=None,
    )
    event = SimpleNamespace(
        requested_quantity=10.0,
        cumulative_filled_quantity=4.0,
        average_fill_price=100.5,
        submitted_at=datetime.utcnow(),
        acknowledged_at=datetime.utcnow(),
        first_fill_at=datetime.utcnow(),
        last_fill_at=None,
        event_at=datetime.utcnow(),
        status="partially_filled",
    )
    apply_execution_event_to_trade(trade, event)
    assert trade.status == "working"
    assert trade.filled_quantity == 4.0
    assert trade.remaining_quantity == 6.0
    assert trade.avg_fill_price == 100.5


def test_aggregate_execution_events_partial_fill_then_cancel(db):
    user = User(name="Exec Audit")
    db.add(user)
    db.flush()
    pattern = ScanPattern(
        name="Audit Pattern",
        rules_json={},
        origin="web_discovered",
        asset_class="equity",
        timeframe="1d",
        user_id=user.id,
    )
    db.add(pattern)
    db.flush()
    trade = Trade(
        user_id=user.id,
        ticker="AAPL",
        direction="long",
        entry_price=100.0,
        quantity=10.0,
        status="working",
        broker_source="robinhood",
        broker_order_id="rh-agg-1",
        scan_pattern_id=pattern.id,
        strategy_proposal_id=None,
        tca_reference_entry_price=100.0,
        entry_date=datetime.utcnow() - timedelta(days=1),
    )
    db.add(trade)
    db.flush()

    record_execution_event(
        db,
        user_id=user.id,
        ticker="AAPL",
        trade=trade,
        scan_pattern_id=pattern.id,
        broker_source="robinhood",
        order_id="rh-agg-1",
        event_type="submitted",
        status="queued",
        requested_quantity=10.0,
        submitted_at=datetime.utcnow() - timedelta(minutes=3),
        acknowledged_at=datetime.utcnow() - timedelta(minutes=3),
        reference_price=100.0,
        payload_json={"step": "submitted"},
    )
    record_execution_event(
        db,
        user_id=user.id,
        ticker="AAPL",
        trade=trade,
        scan_pattern_id=pattern.id,
        broker_source="robinhood",
        order_id="rh-agg-1",
        event_type="partial_fill",
        status="partially_filled",
        requested_quantity=10.0,
        cumulative_filled_quantity=4.0,
        last_fill_quantity=4.0,
        average_fill_price=100.5,
        submitted_at=datetime.utcnow() - timedelta(minutes=3),
        acknowledged_at=datetime.utcnow() - timedelta(minutes=3),
        first_fill_at=datetime.utcnow() - timedelta(minutes=2),
        event_at=datetime.utcnow() - timedelta(minutes=2),
        reference_price=100.0,
        realized_slippage_bps=50.0,
        payload_json={"step": "partial"},
    )
    record_execution_event(
        db,
        user_id=user.id,
        ticker="AAPL",
        trade=trade,
        scan_pattern_id=pattern.id,
        broker_source="robinhood",
        order_id="rh-agg-1",
        event_type="cancel",
        status="cancelled",
        requested_quantity=10.0,
        cumulative_filled_quantity=4.0,
        submitted_at=datetime.utcnow() - timedelta(minutes=3),
        acknowledged_at=datetime.utcnow() - timedelta(minutes=1),
        event_at=datetime.utcnow() - timedelta(minutes=1),
        reference_price=100.0,
        payload_json={"step": "cancel"},
    )
    db.commit()

    stats = aggregate_execution_events_for_pattern(
        db,
        scan_pattern_id=pattern.id,
        user_id=user.id,
        window_days=30,
    )
    assert stats["n_orders"] == 1
    assert stats["n_filled"] == 1
    assert stats["n_partial"] == 1
    assert stats["n_miss"] == 0
    assert stats["fill_rate"] == 1.0
    assert stats["partial_fill_rate"] == 1.0
    assert stats["provider_truth_mode"] == "broker_event_audited"
