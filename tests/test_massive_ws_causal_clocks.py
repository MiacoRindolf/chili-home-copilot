"""Massive stock WebSocket event-time and local release-time provenance."""
from __future__ import annotations

import json

import pytest

from app.services import massive_client as mc


def test_quote_preserves_sip_clock_sequence_and_local_clocks(monkeypatch):
    seen = []
    monkeypatch.setattr(
        mc,
        "_fire_tick_listeners",
        lambda symbol, snap: seen.append((symbol, snap)),
    )
    monkeypatch.setattr(mc.time, "time", lambda: 1536036818.900)

    client = mc.MassiveWSClient()
    client._connection_generation = 7
    client._handle_messages(
        json.dumps([{
            "ev": "Q", "sym": "MSFT", "bx": 4, "bp": 114.125,
            "bs": 100, "ax": 7, "ap": 114.128, "as": 160,
            "c": 0, "i": [604], "t": 1536036818784,
            "q": 50385480, "z": 3,
        }]),
        received_at=1536036818.850,
    )

    symbol, snap = seen[0]
    assert symbol == "MSFT"
    assert snap.provider_timestamp_ms == 1536036818784
    assert snap.provider_event_at == pytest.approx(1536036818.784)
    assert snap.received_at == snap.timestamp == pytest.approx(1536036818.850)
    assert snap.available_at == pytest.approx(1536036818.900)
    assert snap.sequence == 50385480
    assert (snap.bid_exchange, snap.ask_exchange, snap.tape) == (4, 7, 3)
    assert snap.indicators == (604,)
    assert str(mc.uuid.UUID(snap.bridge_run_id)) == snap.bridge_run_id
    assert snap.connection_generation == 7


def test_trade_candles_use_sip_event_time_not_socket_receipt(monkeypatch):
    agg = mc.CandleAggregator(interval_seconds=60)
    monkeypatch.setattr(mc, "_candle_aggregators", {60: agg})
    monkeypatch.setattr(mc, "_fire_tick_listeners", lambda *_args: None)
    monkeypatch.setattr(mc.time, "time", lambda: 1700000100.5)

    mc.MassiveWSClient()._handle_messages(
        json.dumps([{
            "ev": "T", "sym": "TEST", "x": 4, "i": "trade-1",
            "z": 3, "p": 2.50, "s": 100, "ds": "100.25",
            "c": [12], "t": 1700000000123, "pt": 1700000000100,
            "q": 42, "trfi": 201, "trft": 1700000000110,
        }]),
        received_at=1700000100.0,
    )

    bar = agg._bars["TEST"]
    assert bar.bucket_start == 1699999980.0
    assert bar.bucket_start != 1700000100.0
    assert bar.volume == 100.0


def test_live_quote_rejects_locally_fresh_but_provider_delayed(monkeypatch):
    now = 2_000.0
    monkeypatch.setattr(mc.time, "time", lambda: now)
    with mc._ws_cache_lock:
        mc._ws_cache["DELAY"] = mc.QuoteSnapshot(
            price=2.0,
            bid=1.99,
            ask=2.01,
            timestamp=now,
            received_at=now,
            available_at=now,
            provider_event_at=now - 900.0,
            sequence=1,
        )
    assert mc.get_ws_quote("DELAY") is None


@pytest.mark.parametrize("bad_field", ["t", "q"])
def test_missing_exact_provider_provenance_is_not_published(monkeypatch, bad_field):
    event = {
        "ev": "Q", "sym": "BAD", "bp": 1.0, "ap": 1.1,
        "t": 1700000000000, "q": 1,
    }
    event.pop(bad_field)
    seen = []
    monkeypatch.setattr(mc, "_fire_tick_listeners", lambda *args: seen.append(args))
    mc.MassiveWSClient()._handle_messages(
        json.dumps([event]),
        received_at=1700000000.1,
    )
    assert seen == []


def test_late_or_duplicate_trade_cannot_reopen_closed_candle():
    agg = mc.CandleAggregator(interval_seconds=60)
    agg.on_trade(
        "TEST",
        mc.TradeSnapshot(
            price=2.0, size=10, timestamp=120.0,
            provider_event_at=120.0, sequence=2,
        ),
    )
    agg.on_trade(
        "TEST",
        mc.TradeSnapshot(
            price=99.0, size=10, timestamp=61.0,
            provider_event_at=61.0, sequence=1,
        ),
    )
    assert agg._bars["TEST"].bucket_start == 120.0
    assert agg._bars["TEST"].close == 2.0
