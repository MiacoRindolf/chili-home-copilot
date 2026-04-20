"""Tests for the service-layer alert-broadcast registry (Phase 4 item #7).

Previously lived in ``app.routers.trading`` and forced
``app.services.trading.alerts`` to do a lazy services→routers import.
The registry is now in services; these tests verify the contract and
the backwards-compat re-exports from the router module.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.services.trading import alert_broadcast as ab


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty client set."""
    with ab._live_clients_lock:
        ab._live_clients.clear()
    yield
    with ab._live_clients_lock:
        ab._live_clients.clear()


def test_register_and_unregister_client():
    client = MagicMock()
    assert ab.client_count() == 0

    ab.register_client(client)
    assert ab.client_count() == 1

    ab.unregister_client(client)
    assert ab.client_count() == 0


def test_unregister_unknown_client_is_safe():
    """discard-not-remove semantics — unregistering a non-present client
    must not raise (called in finally blocks on disconnect)."""
    ab.unregister_client(MagicMock())
    assert ab.client_count() == 0


def test_broadcast_sends_json_message_to_all_clients():
    async def _run():
        clients = []
        for _ in range(3):
            c = MagicMock()
            # send_text is async in real WebSockets
            async def _send_text(msg, _c=c):
                _c._last_msg = msg
            c.send_text = _send_text
            ab.register_client(c)
            clients.append(c)

        await ab.broadcast_trading_alert(
            {"ticker": "AAPL", "alert_type": "pattern", "price": 150.0}
        )

        import json
        for c in clients:
            assert hasattr(c, "_last_msg")
            payload = json.loads(c._last_msg)
            assert payload["type"] == "alert"
            assert payload["ticker"] == "AAPL"
            assert payload["alert_type"] == "pattern"
            assert payload["price"] == 150.0

    asyncio.run(_run())


def test_broadcast_prunes_stale_clients():
    """A client whose ``send_text`` raises is considered stale and must
    be removed from the registry — otherwise the set grows unboundedly."""
    async def _run():
        good = MagicMock()
        async def _send_good(msg):
            good._last = msg
        good.send_text = _send_good

        bad = MagicMock()
        async def _send_bad(msg):
            raise ConnectionError("closed")
        bad.send_text = _send_bad

        ab.register_client(good)
        ab.register_client(bad)
        assert ab.client_count() == 2

        await ab.broadcast_trading_alert({"ticker": "X"})

        # Stale (bad) client pruned; good client still present.
        assert ab.client_count() == 1

    asyncio.run(_run())


def test_broadcast_alert_sync_outside_loop_dispatches():
    """Called from a sync thread with no running loop — must start a
    short-lived loop, broadcast, and return cleanly."""
    client = MagicMock()
    calls = []
    async def _send_text(msg):
        calls.append(msg)
    client.send_text = _send_text
    ab.register_client(client)

    ab.broadcast_alert_sync({"ticker": "SYNC1", "alert_type": "imminent"})

    # Short-lived loop finished by the time broadcast_alert_sync returned.
    assert len(calls) == 1
    import json
    assert json.loads(calls[0])["ticker"] == "SYNC1"


def test_router_module_re_exports_for_backwards_compat():
    """External imports like ``from app.routers.trading import broadcast_trading_alert``
    must still work — we moved the source of truth, not the public name."""
    import app.routers.trading as rt

    assert rt._broadcast_alert_sync is ab.broadcast_alert_sync
    assert rt.broadcast_trading_alert is ab.broadcast_trading_alert
