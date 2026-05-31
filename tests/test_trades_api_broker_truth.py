from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.trading import Trade, TradingPosition


def _coinbase_position_with_owner(db: Session, user_id: int | None) -> tuple[Trade, Trade]:
    pos = TradingPosition(
        user_id=user_id,
        broker_source="coinbase",
        account_type="spot",
        ticker="ACX-USD",
        direction="long",
        asset_kind="crypto",
        current_quantity=7641.8,
        current_avg_price=0.04282061,
        state="open",
        last_observed_at=datetime.utcnow(),
        last_state_transition_at=datetime.utcnow(),
    )
    db.add(pos)
    db.flush()

    owner = Trade(
        user_id=user_id,
        ticker="ACX-USD",
        direction="long",
        entry_price=0.04466266,
        quantity=3822.0,
        status="open",
        broker_source="coinbase",
        broker_order_id="owner-order",
        broker_status="filled",
        position_id=pos.id,
        entry_date=datetime.utcnow() - timedelta(minutes=20),
        filled_at=datetime.utcnow() - timedelta(minutes=20),
    )
    db.add(owner)
    db.flush()
    pos.current_envelope_id = owner.id

    duplicate = Trade(
        user_id=user_id,
        ticker="ACX-USD",
        direction="long",
        entry_price=0.04466266,
        quantity=3822.0,
        status="open",
        broker_source="coinbase",
        broker_order_id="duplicate-order",
        broker_status="filled",
        position_id=pos.id,
        entry_date=datetime.utcnow() - timedelta(minutes=10),
        filled_at=datetime.utcnow() - timedelta(minutes=10),
    )
    db.add(duplicate)
    db.commit()
    return owner, duplicate


def test_broker_display_metrics_do_not_borrow_other_open_envelope(db: Session) -> None:
    from app.services.trading.broker_position_truth import (
        broker_position_display_metrics,
        broker_stale_open_trade_snapshot,
    )

    owner, duplicate = _coinbase_position_with_owner(db, user_id=None)

    owner_metrics = broker_position_display_metrics(db, owner)
    duplicate_metrics = broker_position_display_metrics(db, duplicate)
    duplicate_stale = broker_stale_open_trade_snapshot(db, duplicate)

    assert owner_metrics["entry_price"] == 0.04282061
    assert owner_metrics["quantity"] == 7641.8
    assert duplicate_metrics is None
    assert duplicate_stale["reason"] == "position_identity_owned_by_other_envelope"
    assert duplicate_stale["position_envelope_id"] == owner.id


def test_trades_api_open_rows_use_broker_truth_and_filter_duplicate_owner(
    paired_client,
    db: Session,
) -> None:
    client, user = paired_client
    owner, duplicate = _coinbase_position_with_owner(db, user_id=user.id)

    response = client.get("/api/trading/trades?status=open")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    ids = {row["id"] for row in payload["trades"]}
    assert owner.id in ids
    assert duplicate.id not in ids
    assert payload["suppressed_stale_count"] == 1

    row = next(row for row in payload["trades"] if row["id"] == owner.id)
    assert row["entry_price"] == 0.04282061
    assert row["quantity"] == 7641.8
    assert row["local_entry_price"] == 0.04466266
    assert row["local_quantity"] == 3822.0
    assert row["broker_truth_current_envelope_id"] == owner.id


def test_trades_api_envelope_flag_open_rows_preserve_broker_truth(
    paired_client,
    db: Session,
    monkeypatch,
) -> None:
    client, user = paired_client
    owner, duplicate = _coinbase_position_with_owner(db, user_id=user.id)
    monkeypatch.setattr(
        "app.routers.trading_sub.trades._phase5af_trades_api_use_envelopes_enabled",
        lambda: True,
    )

    response = client.get("/api/trading/trades?status=open")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    ids = {row["id"] for row in payload["trades"]}
    assert owner.id in ids
    assert duplicate.id not in ids
    assert payload["suppressed_stale_count"] == 1

    row = next(row for row in payload["trades"] if row["id"] == owner.id)
    assert row["entry_price"] == 0.04282061
    assert row["quantity"] == 7641.8
    assert row["local_entry_price"] == 0.04466266
    assert row["local_quantity"] == 3822.0
    assert row["broker_truth_current_envelope_id"] == owner.id
    assert row["broker_truth_metrics_source"] == "broker_position_identity"
