"""Tests for the venue factory (Phase 4 item #8).

The factory is the single entry point callers use to resolve a
:class:`VenueAdapter`. Before it existed, auto_trader, bracket_writer_g2,
and stuck_order_watchdog each instantiated adapters inline with slightly
different normalization rules.
"""
from __future__ import annotations

from unittest.mock import patch

from app.services.trading.venue import factory
from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter
from app.services.trading.venue.protocol import VenueAdapter
from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter


def test_adapters_declare_venue_adapter_protocol():
    """Every adapter class must be a subclass of the protocol so static
    type checkers can validate the surface at import time."""
    assert issubclass(RobinhoodSpotAdapter, VenueAdapter)
    assert issubclass(CoinbaseSpotAdapter, VenueAdapter)


def test_get_adapter_returns_robinhood_for_robinhood_source():
    adapter = factory.get_adapter("robinhood")
    assert isinstance(adapter, RobinhoodSpotAdapter)


def test_get_adapter_returns_coinbase_for_coinbase_source():
    adapter = factory.get_adapter("coinbase")
    assert isinstance(adapter, CoinbaseSpotAdapter)


def test_get_adapter_normalizes_case_and_whitespace():
    """Callers pass broker_source from DB rows that may have inconsistent
    case. Centralizing normalization in the factory means every call site
    agrees on what 'robinhood' means."""
    assert isinstance(factory.get_adapter("ROBINHOOD"), RobinhoodSpotAdapter)
    assert isinstance(factory.get_adapter("  robinhood  "), RobinhoodSpotAdapter)
    assert isinstance(factory.get_adapter("Coinbase"), CoinbaseSpotAdapter)


def test_get_adapter_coinbase_spot_alias():
    """``coinbase_spot`` is a legacy alias some call sites used before
    normalization existed. The factory must still accept it."""
    assert isinstance(factory.get_adapter("coinbase_spot"), CoinbaseSpotAdapter)


def test_get_adapter_unknown_source_returns_none():
    assert factory.get_adapter("alpaca") is None
    assert factory.get_adapter("") is None
    assert factory.get_adapter(None) is None


def test_is_supported_mirrors_get_adapter():
    assert factory.is_supported("robinhood") is True
    assert factory.is_supported("COINBASE") is True
    assert factory.is_supported("alpaca") is False
    assert factory.is_supported(None) is False


def test_supported_broker_sources_exposes_canonical_set():
    """Downstream code (e.g. reconciler filters) uses this set — it must
    contain only the canonical names, not the aliases."""
    assert factory.SUPPORTED_BROKER_SOURCES == frozenset({"robinhood", "coinbase"})


def test_get_adapter_on_builder_exception_returns_none(monkeypatch, caplog):
    """If an adapter constructor raises (broken broker SDK, missing
    config), the factory must log and return None — NOT crash the
    caller's loop."""
    import logging

    def _boom():
        raise RuntimeError("simulated adapter init failure")

    monkeypatch.setitem(factory._BUILDERS, "robinhood", _boom)
    caplog.set_level(logging.WARNING, logger="app.services.trading.venue.factory")

    result = factory.get_adapter("robinhood")
    assert result is None
    assert any(
        "adapter build failed" in r.getMessage() for r in caplog.records
    )
