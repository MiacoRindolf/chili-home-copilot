"""Shared alert-broadcast registry for the live-trading WebSocket.

Previously this registry + broadcaster lived in ``app/routers/trading.py``.
That forced ``app/services/trading/alerts.py`` to reach back up into the
routers layer to push events to connected WebSocket clients — a
services→routers circular import that made lazy imports mandatory and
made test harnesses require the FastAPI app to be loaded just to
validate alert flow.

The registry is now a service module. Routers import from here (normal
direction), services push here (normal direction), and the lazy import
in ``alerts.py`` can go back to a top-level import.

Thread-safety:
  * The set of WebSockets is protected by a ``threading.Lock`` because
    the scheduler / alert code runs from sync threads while the
    WebSocket handler runs in an asyncio loop.
  * Broadcast itself is async; the sync wrapper schedules a task on the
    running loop or runs a short-lived loop if none is available.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # The WebSocket type is only used for registry typing; avoiding a
    # runtime import keeps this module importable in non-FastAPI contexts
    # (brain worker, pure unit tests).
    from fastapi import WebSocket


# Module-level registry. Mutated from both the WebSocket handler (adding
# on connect, removing on disconnect) and the broadcaster (pruning
# stale clients).
_live_clients: set[Any] = set()
_live_clients_lock = threading.Lock()


def register_client(client: "WebSocket") -> None:
    """Add a connected WebSocket to the broadcast set."""
    with _live_clients_lock:
        _live_clients.add(client)


def unregister_client(client: "WebSocket") -> None:
    """Remove a WebSocket from the broadcast set (safe if not present)."""
    with _live_clients_lock:
        _live_clients.discard(client)


def client_count() -> int:
    with _live_clients_lock:
        return len(_live_clients)


async def broadcast_trading_alert(alert_data: dict[str, Any]) -> None:
    """Push an alert to every connected live-trading WebSocket client.

    Pruning of stale (closed / errored) clients happens inline so a
    long-running process doesn't accumulate dead sockets.
    """
    msg = json.dumps({"type": "alert", **alert_data})
    with _live_clients_lock:
        clients = list(_live_clients)
    stale: list[Any] = []
    for ws_c in clients:
        try:
            await ws_c.send_text(msg)
        except Exception:
            stale.append(ws_c)
    if stale:
        with _live_clients_lock:
            for ws_c in stale:
                _live_clients.discard(ws_c)


def broadcast_alert_sync(alert_data: dict[str, Any]) -> None:
    """Sync-side wrapper so scheduler / alert code can call from sync
    context without knowing whether an event loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        loop.create_task(broadcast_trading_alert(alert_data))
    else:
        try:
            asyncio.run(broadcast_trading_alert(alert_data))
        except RuntimeError:
            # No loop + another loop is closing: drop the broadcast
            # silently. This matches the previous in-router behavior.
            pass


__all__ = [
    "broadcast_alert_sync",
    "broadcast_trading_alert",
    "client_count",
    "register_client",
    "unregister_client",
]
