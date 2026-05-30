"""API tests for Autopilot pattern desk + autotrader desk PATCH."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.trading import (
    BrainRuntimeMode,
    BreakoutAlert,
    PaperTrade,
    ScanPattern,
    Trade,
    TradingPosition,
)
from app.services.trading.autotrader_desk import AUTOTRADER_DESK_SLICE


def test_autotrader_desk_quote_helpers_reject_nonfinite_values() -> None:
    from app.services.trading.autotrader_desk import (
        _compute_unrealized,
        _safe_quote_float,
    )

    assert _safe_quote_float("Infinity") is None
    assert _safe_quote_float("-Infinity") is None
    assert _safe_quote_float("NaN") is None
    assert _safe_quote_float(True) is None
    assert _safe_quote_float(False) is None
    assert _safe_quote_float("1.45") == pytest.approx(1.45)
    assert _compute_unrealized(
        entry_price=1.25,
        current_price=float("inf"),
        quantity=2,
        direction="long",
        multiplier=100,
    ) == (None, None)
    assert _compute_unrealized(
        entry_price=1.25,
        current_price=True,
        quantity=2,
        direction="long",
        multiplier=100,
    ) == (None, None)


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


def test_autotrader_desk_option_trade_uses_premium_mark(
    paired_client,
    db: Session,
) -> None:
    c, user = paired_client
    t = Trade(
        user_id=user.id,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        stop_loss=0.80,
        take_profit=2.50,
        broker_source="robinhood",
        auto_trader_version="v1",
        indicator_snapshot={
            "breakout_alert": {
                "asset_type": "options",
                "option_meta": {
                    "underlying": "SPY",
                    "expiration": "2026-06-19",
                    "strike": 729.0,
                    "option_type": "call",
                },
            }
        },
    )
    db.add(t)
    db.commit()

    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "spy-729c"}
    fake_options.get_quote.return_value = {"mark_price": "1.45", "bid_price": "1.40", "ask_price": "1.50"}

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("option desk rows must not fetch underlying spot"),
    ), patch(
        "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
        return_value=fake_options,
    ):
        r = c.get("/api/trading/autotrader/desk")

    assert r.status_code == 200
    payload = r.json()
    row = next(x for x in payload["trades"] if x["ticker"] == "SPY")
    assert row["asset_type"] == "options"
    assert row["current_price"] == pytest.approx(1.45)
    assert row["quote_source"] == "robinhood_options"
    assert row["unrealized_pnl_usd"] == pytest.approx(40.0)
    assert row["unrealized_pnl_pct"] == pytest.approx(16.0)
    fake_options.find_contract.assert_called_once_with("SPY", "2026-06-19", 729.0, "call")
    fake_options.get_quote.assert_called_once_with("spy-729c")


def test_autotrader_desk_paper_option_uses_premium_mark(
    paired_client,
    db: Session,
) -> None:
    c, user = paired_client
    pat = ScanPattern(
        user_id=user.id,
        name="desk_paper_option_pattern",
        rules_json={"test": True},
        origin="unit",
        asset_class="stock",
        timeframe="1d",
        active=True,
        trade_count=0,
    )
    db.add(pat)
    db.flush()
    pt = PaperTrade(
        user_id=user.id,
        scan_pattern_id=pat.id,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json={
            "auto_trader_v1": True,
            "asset_type": "options",
            "option_meta": {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
            },
        },
    )
    db.add(pt)
    db.commit()

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("paper option desk rows must not fetch underlying spot"),
    ), patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": 1.45, "source": "robinhood_options"},
    ):
        r = c.get("/api/trading/autotrader/desk")

    assert r.status_code == 200
    payload = r.json()
    row = next(x for x in payload["paper_trades"] if x["ticker"] == "SPY")
    assert row["asset_type"] == "options"
    assert row["contract_multiplier"] == 100.0
    assert row["current_price"] == pytest.approx(1.45)
    assert row["quote_source"] == "robinhood_options"
    assert row["unrealized_pnl_usd"] == pytest.approx(40.0)
    assert row["unrealized_pnl_pct"] == pytest.approx(16.0)


def test_autotrader_desk_paper_option_rejects_boolean_mark(
    paired_client,
    db: Session,
) -> None:
    c, user = paired_client
    pat = ScanPattern(
        user_id=user.id,
        name="desk_paper_option_bad_quote_pattern",
        rules_json={"test": True},
        origin="unit",
        asset_class="stock",
        timeframe="1d",
        active=True,
        trade_count=0,
    )
    db.add(pat)
    db.flush()
    pt = PaperTrade(
        user_id=user.id,
        scan_pattern_id=pat.id,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        status="open",
        signal_json={
            "auto_trader_v1": True,
            "asset_type": "options",
            "option_meta": {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
            },
        },
    )
    db.add(pt)
    db.commit()

    with patch(
        "app.services.trading.market_data.fetch_quote",
        side_effect=AssertionError("paper option desk rows must not fetch underlying spot"),
    ), patch(
        "app.services.trading.paper_trading._paper_current_mark_price",
        return_value=True,
    ):
        r = c.get("/api/trading/autotrader/desk")

    assert r.status_code == 200
    payload = r.json()
    row = next(x for x in payload["paper_trades"] if x["ticker"] == "SPY")
    assert row["asset_type"] == "options"
    assert row["current_price"] is None
    assert row["quote_source"] == "option_premium_unavailable"
    assert row["unrealized_pnl_usd"] is None
    assert row["unrealized_pnl_pct"] is None


def test_autotrader_desk_suppresses_closed_broker_position(
    paired_client,
    db: Session,
) -> None:
    c, user = paired_client
    pos = TradingPosition(
        user_id=user.id,
        broker_source="robinhood",
        account_type="cash",
        ticker="GHOST",
        direction="long",
        current_quantity=0,
        current_avg_price=10.0,
        state="closed",
    )
    db.add(pos)
    db.flush()
    t = Trade(
        user_id=user.id,
        ticker="GHOST",
        direction="long",
        entry_price=10.0,
        quantity=1.0,
        status="open",
        stop_loss=9.0,
        take_profit=12.0,
        broker_source="robinhood",
        position_id=pos.id,
    )
    db.add(t)
    db.commit()

    r = c.get("/api/trading/autotrader/desk")
    assert r.status_code == 200
    payload = r.json()
    tickers = [x["ticker"] for x in payload.get("trades", [])]
    assert "GHOST" not in tickers
    suppressed = payload.get("suppressed_stale_trades") or []
    assert any(
        row["ticker"] == "GHOST" and row["reason"] == "position_identity_closed"
        for row in suppressed
    )


def test_autotrader_desk_uses_broker_position_identity_for_coinbase_metrics(
    paired_client,
    db: Session,
) -> None:
    c, user = paired_client
    t = Trade(
        user_id=user.id,
        ticker="ACX-USD",
        direction="long",
        entry_price=0.04466266,
        quantity=3822.0,
        status="open",
        broker_source="coinbase",
        asset_kind="crypto",
        tags="autotrader_v1",
        auto_trader_version="v1",
    )
    db.add(t)
    db.flush()
    pos = TradingPosition(
        user_id=user.id,
        broker_source="coinbase",
        account_type="spot",
        ticker="ACX-USD",
        direction="long",
        asset_kind="crypto",
        current_quantity=7641.8,
        current_avg_price=0.04282061,
        state="open",
        current_envelope_id=t.id,
    )
    db.add(pos)
    db.flush()
    t.position_id = pos.id
    db.commit()

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": 0.0426, "source": "coinbase"},
    ):
        r = c.get("/api/trading/autotrader/desk")

    assert r.status_code == 200
    payload = r.json()
    row = next(x for x in payload["trades"] if x["ticker"] == "ACX-USD")
    assert row["entry_price"] == pytest.approx(0.04282061)
    assert row["quantity"] == pytest.approx(7641.8)
    assert row["unrealized_pnl_usd"] == pytest.approx(-1.6859)
    assert row["unrealized_pnl_pct"] == pytest.approx(-0.5152)
    assert row["broker_truth_metrics_source"] == "broker_position_identity"
