"""Per-symbol execution-family routing (crypto->Coinbase, equity->Robinhood)."""
from __future__ import annotations

from app.services.trading.execution_family_registry import (
    EXECUTION_FAMILY_COINBASE_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
    resolve_execution_family_for_symbol,
    venue_for_execution_family,
)


def test_crypto_routes_to_coinbase():
    assert resolve_execution_family_for_symbol("KAIO-USD") == EXECUTION_FAMILY_COINBASE_SPOT
    assert resolve_execution_family_for_symbol("BTC-USD") == EXECUTION_FAMILY_COINBASE_SPOT
    assert resolve_execution_family_for_symbol("ETH-USD") == EXECUTION_FAMILY_COINBASE_SPOT


def test_equity_routes_to_robinhood():
    assert resolve_execution_family_for_symbol("ARKK") == EXECUTION_FAMILY_ROBINHOOD_SPOT
    assert resolve_execution_family_for_symbol("AAPL") == EXECUTION_FAMILY_ROBINHOOD_SPOT
    assert resolve_execution_family_for_symbol("CLSK") == EXECUTION_FAMILY_ROBINHOOD_SPOT


def test_venue_for_family():
    assert venue_for_execution_family("coinbase_spot") == "coinbase"
    assert venue_for_execution_family("robinhood_spot") == "robinhood"
    assert venue_for_execution_family("") == "coinbase"  # default
