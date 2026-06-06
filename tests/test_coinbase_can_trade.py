"""Sell-scope preflight: coinbase_service.can_trade() gates live on TRADE permission.

A connected but view-only / buy-only Coinbase key would let live ENTRIES through
but block EXITS ("403 Missing Required Scopes" on sell). can_trade() verifies the
key's TRADE permission via Coinbase's own get_api_key_permissions (no order),
fail-closed.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    from app.services import coinbase_service as cs
    cs._can_trade_cache.update({"value": None, "ts": 0.0})
    yield
    cs._can_trade_cache.update({"value": None, "ts": 0.0})


class _Client:
    def __init__(self, perms):
        self._perms = perms

    def get_api_key_permissions(self):
        if isinstance(self._perms, Exception):
            raise self._perms
        return self._perms


def _wire(monkeypatch, *, client, connected=True):
    from app.services import coinbase_service as cs
    monkeypatch.setattr(cs, "_get_client", lambda: client)
    monkeypatch.setattr(cs, "is_connected", lambda: connected)


def test_can_trade_true(monkeypatch) -> None:
    from app.services import coinbase_service as cs
    _wire(monkeypatch, client=_Client({"can_view": True, "can_trade": True, "can_transfer": True}))
    assert cs.can_trade() is True


def test_can_trade_false_view_only(monkeypatch) -> None:
    from app.services import coinbase_service as cs
    _wire(monkeypatch, client=_Client({"can_view": True, "can_trade": False}))
    assert cs.can_trade() is False


def test_can_trade_false_when_not_connected(monkeypatch) -> None:
    from app.services import coinbase_service as cs
    _wire(monkeypatch, client=None, connected=False)
    assert cs.can_trade() is False


def test_can_trade_fail_closed_on_error(monkeypatch) -> None:
    from app.services import coinbase_service as cs
    _wire(monkeypatch, client=_Client(RuntimeError("boom")))
    # No prior positive verification -> fail-closed.
    assert cs.can_trade() is False


def test_can_trade_uses_recent_positive_cache_on_error(monkeypatch) -> None:
    import time as _t
    from app.services import coinbase_service as cs
    # Seed a recent positive verification.
    cs._can_trade_cache.update({"value": True, "ts": _t.time()})
    _wire(monkeypatch, client=_Client(RuntimeError("transient")))
    # Recent success -> tolerate the transient error.
    assert cs.can_trade() is True
