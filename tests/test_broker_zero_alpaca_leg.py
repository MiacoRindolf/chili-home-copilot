"""``_broker_position_confirms_zero`` must work on the lane we actually trade.

The check's own docstring records the 2026-06-11 incident where it was Coinbase-only and
a Robinhood phantom looped 8 flatten retries into LIVE_ERROR while the broker was flat.
That fix added Robinhood and stopped there — so for three months every ``alpaca_spot``
session fell through to ``return False`` and the broker-zero reconcile could never
confirm flat on the equity lane.

Measured cost (ANPA session 19771, 2026-09-04): a burst-window exit decided at 08:50:40Z
never placed an order; with no way to confirm the broker flat, the session held
``live_entered`` for 4h29m after the broker went flat at 13:32:24Z, and the resulting
ghost FAIL-CLOSED two lane launches mid-RTH on ``active_sessions: 1``. It had to be
settled by hand from a direct Alpaca read.

The safety property is as load-bearing as the fix: an unreadable broker must NEVER read
as flat. ``AlpacaSpotAdapter.get_position_quantity`` returns 0.0 only on an explicit HTTP
404 and None on any transport failure, and these tests pin that both ways.

DB-free: the adapter is stubbed; no network, no database.
"""
from __future__ import annotations

import types

import pytest

import app.services.trading.momentum_neural.live_runner as lr


class _StubAdapter:
    def __init__(self, value, *, raises=False):
        self._value = value
        self._raises = raises
        self.calls = []

    def get_position_quantity(self, symbol):
        self.calls.append(symbol)
        if self._raises:
            raise RuntimeError("transport blew up")
        return self._value


def _patch_adapter(monkeypatch, stub):
    import app.services.trading.venue.alpaca_spot as venue

    monkeypatch.setattr(venue, "AlpacaSpotAdapter", lambda: stub)
    return stub


def _sess(family="alpaca_spot", symbol="ANPA"):
    return types.SimpleNamespace(id=19771, symbol=symbol, execution_family=family)


@pytest.mark.parametrize("family", ["alpaca_spot", "alpaca_short"])
def test_explicit_zero_confirms_flat_on_both_alpaca_families(monkeypatch, family):
    stub = _patch_adapter(monkeypatch, _StubAdapter(0.0))
    assert lr._broker_position_confirms_zero(_sess(family)) is True
    assert stub.calls == ["ANPA"]


def test_short_side_negative_quantity_is_not_flat(monkeypatch):
    """alpaca_short reports a negative quantity — abs() is why the check is correct."""
    _patch_adapter(monkeypatch, _StubAdapter(-120.0))
    assert lr._broker_position_confirms_zero(_sess("alpaca_short")) is False


def test_held_quantity_is_not_flat(monkeypatch):
    _patch_adapter(monkeypatch, _StubAdapter(49.0))
    assert lr._broker_position_confirms_zero(_sess()) is False


def test_unreadable_broker_is_never_flat(monkeypatch):
    """None means 'we could not read', which must never close a position.

    This is the property that keeps an API outage from being mistaken for a successful
    exit — the same fail-safe the Coinbase and Robinhood legs already carry.
    """
    _patch_adapter(monkeypatch, _StubAdapter(None))
    assert lr._broker_position_confirms_zero(_sess()) is False


def test_raising_adapter_is_never_flat(monkeypatch):
    _patch_adapter(monkeypatch, _StubAdapter(None, raises=True))
    assert lr._broker_position_confirms_zero(_sess()) is False


def test_import_failure_is_never_flat(monkeypatch):
    """Even a broken import must degrade to the safe retry path, not to 'flat'."""
    import builtins

    real_import = builtins.__import__

    def _boom(name, *a, **k):
        if "alpaca_spot" in name:
            raise ImportError("no venue module")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert lr._broker_position_confirms_zero(_sess()) is False


def test_unknown_family_still_returns_false(monkeypatch):
    """The fall-through default is unchanged — this fix is additive."""
    _patch_adapter(monkeypatch, _StubAdapter(0.0))
    assert lr._broker_position_confirms_zero(_sess("some_future_venue")) is False


def test_alpaca_is_actually_reachable_from_the_family_set():
    """Guards the wiring itself: if ALPACA_EXECUTION_FAMILIES ever stops containing the
    lane's family, this check silently reverts to the three-month blind spot."""
    from app.services.trading.momentum_neural.live_runner import ALPACA_EXECUTION_FAMILIES

    assert "alpaca_spot" in ALPACA_EXECUTION_FAMILIES
    assert "alpaca_short" in ALPACA_EXECUTION_FAMILIES
