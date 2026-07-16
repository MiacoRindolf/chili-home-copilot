from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.trading.momentum_neural.paper_execution import (
    modeled_fill_leg_fee_usd,
    roundtrip_fee_usd,
)
from app.services.trading.momentum_neural.replay_mock_broker import (
    MockBrokerAdapter,
    RecordedQuote,
)


def test_fill_leg_fee_is_exactly_half_of_symmetric_roundtrip_model() -> None:
    kwargs = {
        "notional": 1_000.0,
        "fee_to_target_ratio": 0.08,
        "venue_rt_bps": 100.0,
    }

    assert modeled_fill_leg_fee_usd(**kwargs) == pytest.approx(
        roundtrip_fee_usd(**kwargs) / 2.0
    )


def test_replay_roundtrip_does_not_charge_roundtrip_fee_on_each_leg() -> None:
    broker = MockBrokerAdapter(
        slippage_bps=0.0,
        venue_rt_bps=100.0,
        freshness_mode="sim",
    )
    broker.set_clock(datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc))
    broker.set_quote("FEE", RecordedQuote(bid=9.99, ask=10.0, last=10.0))
    assert broker.place_market_order(
        product_id="FEE",
        side="buy",
        base_size="100",
        client_order_id="fee-entry",
    )["ok"] is True

    broker.set_quote("FEE", RecordedQuote(bid=11.0, ask=11.01, last=11.0))
    assert broker.place_market_order(
        product_id="FEE",
        side="sell",
        base_size="100",
        client_order_id="fee-exit",
    )["ok"] is True

    fills, _freshness = broker.get_fills(product_id="FEE", limit=10)
    assert [fill.fee for fill in fills] == pytest.approx([5.0, 5.5])
    assert sum(fill.fee for fill in fills) == pytest.approx(10.5)


def test_partial_fills_allocate_one_leg_fee_proportionally() -> None:
    broker = MockBrokerAdapter(
        slippage_bps=0.0,
        venue_rt_bps=100.0,
        resting_limit_fills=True,
        volume_cap_enabled=True,
        volume_participation_frac=0.25,
        freshness_mode="sim",
    )
    broker.set_clock(datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc))
    broker.set_quote("PART", RecordedQuote(bid=9.99, ask=10.0, last=10.0))
    broker.set_printed_volume("PART", 200.0)

    placed = broker.place_limit_order_gtc(
        product_id="PART",
        side="buy",
        base_size="100",
        limit_price="10.0",
        client_order_id="partial-entry",
    )
    assert placed["ok"] is True
    assert placed["status"] == "open"
    assert placed["raw"]["filled_size"] == pytest.approx(50.0)

    broker.set_printed_volume("PART", 200.0)
    fills, _freshness = broker.get_fills(product_id="PART", limit=10)
    assert [fill.size for fill in fills] == pytest.approx([50.0, 50.0])
    assert [fill.fee for fill in fills] == pytest.approx([2.5, 2.5])
    order, _freshness = broker.get_order(str(placed["order_id"]))
    assert order is not None
    assert order.status == "filled"
    assert order.raw["fee"] == pytest.approx(5.0)
