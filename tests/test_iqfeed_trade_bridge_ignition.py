"""Bridge-side ignition wiring — payload shape, queue drain, NOTIFY emit.

Pure-process tests: no IQFeed socket, no DB (the NOTIFY connection is stubbed).
The v3 authority envelope is untouched by design; these tests pin the SEPARATE
channel + minimal payload contract the live_runner_loop consumer validates.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import scripts.iqfeed_trade_bridge as bridge
from scripts.iqfeed_ignition_detector import IgnitionConfig, IgnitionDetector

T0 = datetime(2026, 7, 13, 11, 57, 0, tzinfo=timezone.utc)


def _drain_all():
    bridge._drain_ignition_payloads()


def _burst(detector_feed, symbol="PLSM"):
    """Feed a PLSM-shaped burst through the given per-print feed callable."""
    for i in range(40 * 60):
        at = T0 + timedelta(seconds=i / 40.0)
        elapsed = (at - T0).total_seconds()
        detector_feed(symbol, at, 4.30 * (1.0 + 0.0035 * elapsed), 500.0)


def test_observe_ignition_print_queues_contract_payload(monkeypatch):
    monkeypatch.setattr(bridge, "IGNITION_ENABLED", True)
    monkeypatch.setattr(bridge, "_ignition_detector", IgnitionDetector(IgnitionConfig()))
    _drain_all()

    _burst(
        lambda sym, at, px, sz: bridge._observe_ignition_print(
            sym, at, px, sz, connection_generation=3
        )
    )

    payloads = bridge._drain_ignition_payloads()
    assert len(payloads) == 1
    data = json.loads(payloads[0])
    assert data == {
        "schema": "chili.iqfeed-ignition-nominate.v1",
        "symbol": "PLSM",
        "source": "ignition_tick",
        "fired_at": data["fired_at"],
        "last_price": data["last_price"],
        "pct_change_60s": data["pct_change_60s"],
        "dollar_vol_60s": data["dollar_vol_60s"],
        "prints_10s": data["prints_10s"],
        "bridge_run_id": bridge.BRIDGE_RUN_ID,
        "connection_generation": 3,
    }
    parsed = datetime.fromisoformat(data["fired_at"])
    assert parsed.tzinfo is not None
    assert data["last_price"] > 0
    assert data["pct_change_60s"] >= 0.05
    assert data["dollar_vol_60s"] >= 150_000.0
    assert isinstance(data["prints_10s"], int)


def test_observe_ignition_print_disabled_is_inert(monkeypatch):
    monkeypatch.setattr(bridge, "IGNITION_ENABLED", False)
    monkeypatch.setattr(bridge, "_ignition_detector", IgnitionDetector(IgnitionConfig()))
    _drain_all()

    _burst(
        lambda sym, at, px, sz: bridge._observe_ignition_print(
            sym, at, px, sz, connection_generation=1
        )
    )

    assert bridge._drain_ignition_payloads() == []


def test_observe_ignition_print_never_raises_into_parse_path(monkeypatch):
    monkeypatch.setattr(bridge, "IGNITION_ENABLED", True)

    class _Broken:
        def on_print(self, *_a, **_k):
            raise RuntimeError("detector blew up")

    monkeypatch.setattr(bridge, "_ignition_detector", _Broken())
    _drain_all()

    # Must swallow — the authority parse path can never be poisoned.
    bridge._observe_ignition_print("PLSM", T0, 5.0, 100.0, connection_generation=1)
    assert bridge._drain_ignition_payloads() == []


def test_emit_ignition_notifications_sends_each_payload_on_channel(monkeypatch):
    monkeypatch.setattr(bridge, "IGNITION_ENABLED", True)
    monkeypatch.setattr(bridge, "_ignition_detector", IgnitionDetector(IgnitionConfig()))
    _drain_all()

    _burst(
        lambda sym, at, px, sz: bridge._observe_ignition_print(
            sym, at, px, sz, connection_generation=2
        )
    )

    executed = []

    class _Conn:
        def execute(self, statement, params):
            executed.append((str(statement), params))

    emitted = bridge._emit_ignition_notifications(_Conn())

    assert emitted == 1
    assert len(executed) == 1
    statement, params = executed[0]
    assert "pg_notify" in statement
    assert params["channel"] == "momentum_iqfeed_ignition"
    assert json.loads(params["payload"])["symbol"] == "PLSM"
    # Queue drained: a second emit sends nothing.
    assert bridge._emit_ignition_notifications(_Conn()) == 0
