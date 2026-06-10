"""Broker-connectivity alarm: sustained configured-but-disconnected must alert loudly
(the RH token died silently for ~7 weeks — post-mortem 2026-06-10)."""
from __future__ import annotations

from unittest.mock import patch

import app.services.trading.broker_connectivity_watch as bw


def _statuses(rh_connected: bool, cb_connected: bool = True):
    return {
        "robinhood": {"configured": True, "connected": rh_connected},
        "coinbase": {"configured": True, "connected": cb_connected},
    }


def test_sustained_disconnect_alerts_once_then_resolves(monkeypatch):
    bw.reset_for_tests()
    broadcasts = []
    monkeypatch.setattr(bw, "_broadcast", lambda p: broadcasts.append(p))
    monkeypatch.setattr(bw, "get_all_broker_statuses", lambda: _statuses(rh_connected=False))
    monkeypatch.setattr(bw, "_alarm_threshold_seconds", lambda: 900.0)  # 15 min

    out1 = bw.run_broker_connectivity_watch(now=1_000.0)        # first sighting: track only
    assert out1["alerted"] == [] and out1["down"]
    out2 = bw.run_broker_connectivity_watch(now=1_000.0 + 600)  # 10 min: below threshold
    assert out2["alerted"] == []
    out3 = bw.run_broker_connectivity_watch(now=1_000.0 + 1000) # 16.6 min: ALERT
    assert out3["alerted"] == ["robinhood"]
    assert any(b.get("severity") == "critical" and b.get("broker") == "robinhood" for b in broadcasts)
    out4 = bw.run_broker_connectivity_watch(now=1_000.0 + 2000) # still down: no re-spam
    assert out4["alerted"] == []

    # reconnect -> resolved broadcast + state cleared
    monkeypatch.setattr(bw, "get_all_broker_statuses", lambda: _statuses(rh_connected=True))
    out5 = bw.run_broker_connectivity_watch(now=1_000.0 + 3000)
    assert out5["resolved"] == ["robinhood"]
    assert any(b.get("severity") == "resolved" for b in broadcasts)


def test_unconfigured_or_connected_brokers_never_alert(monkeypatch):
    bw.reset_for_tests()
    broadcasts = []
    monkeypatch.setattr(bw, "_broadcast", lambda p: broadcasts.append(p))
    monkeypatch.setattr(bw, "get_all_broker_statuses", lambda: {
        "robinhood": {"configured": False, "connected": False},   # not configured: ignore
        "coinbase": {"configured": True, "connected": True},      # healthy
    })
    out = bw.run_broker_connectivity_watch(now=5_000.0)
    assert out["alerted"] == [] and out["down"] == [] and broadcasts == []
    out2 = bw.run_broker_connectivity_watch(now=5_000.0 + 99_999)
    assert out2["alerted"] == [] and broadcasts == []
