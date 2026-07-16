"""Massive provenance survives the operational price-bus bridge."""
from __future__ import annotations

from app.services.massive_client import QuoteSnapshot, TradeSnapshot
from app.services.trading import price_bus as pb


def test_massive_quote_keeps_clocks_and_does_not_create_fake_trade_candle(monkeypatch):
    now = 2_000.0
    monkeypatch.setattr(pb.time, "time", lambda: now)
    bus = pb.PriceBus()
    bus.bridge_massive_ws()

    bus._massive_tick_cb(
        "TEST",
        QuoteSnapshot(
            price=2.0, bid=1.99, ask=2.01, timestamp=now - 0.2,
            provider_event_at=now - 0.3, received_at=now - 0.2,
            available_at=now - 0.1, sequence=10,
        ),
    )

    quote = bus.get_quote("TEST")
    assert quote is not None
    assert quote.provider_event_at == now - 0.3
    assert quote.provider_sequence == 10
    assert quote.event_kind == "quote"
    assert bus.get_current_candle("TEST") is None


def test_massive_trade_uses_provider_event_time_for_candle(monkeypatch):
    now = 1_700_000_100.0
    monkeypatch.setattr(pb.time, "time", lambda: now)
    bus = pb.PriceBus()
    bus.bridge_massive_ws()

    bus._massive_tick_cb(
        "TEST",
        TradeSnapshot(
            price=2.5, size=100, timestamp=now - 0.1,
            provider_event_at=1_700_000_098.5, received_at=now - 0.1,
            available_at=now - 0.05, sequence=11,
        ),
    )

    candle = bus.get_current_candle("TEST")
    assert candle is not None
    assert candle.bucket_start == (
        1_700_000_098.5 // bus._candle_interval
    ) * bus._candle_interval


def test_delayed_massive_frame_cannot_enter_operational_bus(monkeypatch):
    now = 2_000.0
    monkeypatch.setattr(pb.time, "time", lambda: now)
    bus = pb.PriceBus()
    bus.bridge_massive_ws()

    bus._massive_tick_cb(
        "DELAY",
        QuoteSnapshot(
            price=2.0, bid=1.99, ask=2.01, timestamp=now,
            provider_event_at=now - 900.0, received_at=now,
            available_at=now, sequence=12,
        ),
    )
    assert bus.get_quote("DELAY") is None
