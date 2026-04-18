"""API tests for Autopilot pattern desk + autotrader desk PATCH."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.trading import BrainRuntimeMode, BreakoutAlert, Trade
from app.services.trading.autotrader_desk import AUTOTRADER_DESK_SLICE


def test_autotrader_desk_guest_403(client) -> None:
    r = client.get("/api/trading/autotrader/desk")
    assert r.status_code == 403


def test_autotrader_desk_paired_get_patch(paired_client, db: Session) -> None:
    c, user = paired_client
    r = c.get("/api/trading/autotrader/desk")
    assert r.status_code == 200
    js = r.json()
    assert js.get("ok") is True
    assert "autotrader" in js
    assert "trades" in js and isinstance(js["trades"], list)

    r2 = c.patch(
        "/api/trading/autotrader/desk",
        json={"paused": True},
    )
    assert r2.status_code == 200
    row = db.query(BrainRuntimeMode).filter(BrainRuntimeMode.slice_name == AUTOTRADER_DESK_SLICE).first()
    assert row is not None
    assert row.mode == "paused"

    r3 = c.patch("/api/trading/autotrader/desk", json={"paused": False, "live_orders": True})
    assert r3.status_code == 200
    db.refresh(row)
    assert row.mode == "active"
    assert row.payload_json.get("live_orders") is True


def test_autotrader_desk_lists_pattern_trade(paired_client, db: Session) -> None:
    c, user = paired_client
    ba = BreakoutAlert(
        ticker="DESK1",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.5,
        price_at_alert=10.0,
        user_id=user.id,
    )
    db.add(ba)
    db.flush()
    t = Trade(
        user_id=user.id,
        ticker="DESK1",
        direction="long",
        entry_price=10.0,
        quantity=1.0,
        status="open",
        stop_loss=9.0,
        take_profit=12.0,
        related_alert_id=ba.id,
    )
    db.add(t)
    db.commit()

    r = c.get("/api/trading/autotrader/desk")
    assert r.status_code == 200
    payload = r.json()
    tickers = [x["ticker"] for x in payload.get("trades", [])]
    assert "DESK1" in tickers

    row = next(x for x in payload["trades"] if x["ticker"] == "DESK1")
    for fld in (
        "entry_price",
        "entry_date",
        "current_price",
        "unrealized_pnl_usd",
        "unrealized_pnl_pct",
        "quote_source",
        "asset_type",
        "overrides",
        "opened_today_et",
        "controls_supported",
        "close_supported",
    ):
        assert fld in row, f"missing enrichment field: {fld}"
    assert row["quote_source"] in ("robinhood", "market_data", "unavailable")
    assert row["asset_type"] == "stock"
    # No-adopt model: every linked row is managed; no explicit adopt toggle.
    assert row["controls_supported"] is True
    assert "can_adopt" not in row


def test_autotrader_desk_lists_plan_level_trade(paired_client, db: Session) -> None:
    c, user = paired_client
    t = Trade(
        user_id=user.id,
        ticker="PLAN1",
        direction="long",
        entry_price=10.0,
        quantity=2.0,
        status="open",
        stop_loss=9.25,
        take_profit=11.5,
        broker_source="robinhood",
        tags="robinhood-sync",
    )
    db.add(t)
    db.commit()

    r = c.get("/api/trading/autotrader/desk")
    assert r.status_code == 200
    payload = r.json()
    tickers = [x["ticker"] for x in payload.get("trades", [])]
    assert "PLAN1" in tickers

    row = next(x for x in payload["trades"] if x["ticker"] == "PLAN1")
    assert row["monitor_scope"] == "plan_levels"
    assert row["scan_pattern_id"] is None
    assert row["related_alert_id"] is None
