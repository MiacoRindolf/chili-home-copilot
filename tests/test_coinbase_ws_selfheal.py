"""Coinbase WS self-heal — on_close must arm exactly one reconnect loop that
re-subscribes the previous product set."""
from __future__ import annotations

import time

from app.services.trading.venue.coinbase_spot import CoinbaseWebSocketSeam as CoinbaseWs


def test_on_close_triggers_reconnect_and_resubscribes(monkeypatch):
    ws = CoinbaseWs()
    ws.enabled = True
    ws._running = True
    ws._subscribed = {"BTC-USD", "ETH-USD", "DOGE-USD"}
    calls = []

    def _fake_start(product_ids=None):
        ws._running = True
        calls.append(sorted(product_ids or []))
        return {"ok": True}

    monkeypatch.setattr(ws, "start", _fake_start)
    monkeypatch.setattr("time.sleep", lambda s: None)  # fast-forward backoff
    ws._on_close()
    for _ in range(100):  # wait for the daemon thread
        if calls:
            break
        time.sleep(0.02)
    assert calls and calls[0] == ["BTC-USD", "DOGE-USD", "ETH-USD"]
    assert ws._running is True
    assert ws._reconnecting is False


def test_on_close_noop_when_disabled():
    ws = CoinbaseWs()
    ws.enabled = False
    ws._running = True
    ws._on_close()
    assert not getattr(ws, "_reconnecting", False)
