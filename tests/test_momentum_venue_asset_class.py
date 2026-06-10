"""Asset-class venue validation — an EQUITY may route to ANY equity venue (robinhood_spot OR
alpaca_spot, the same-name A/B), while a cross-asset-class request (equity via the crypto
venue, or vice versa) is still blocked. A pre-flight dry-run caught the old exact-match check
blocking alpaca_spot for equities. (docs/DESIGN/ALPACA_LANE.md)"""

from __future__ import annotations

from app.services.trading.execution_family_registry import asset_class_of_execution_family


def test_asset_class_mapping():
    assert asset_class_of_execution_family("coinbase_spot") == "crypto"
    assert asset_class_of_execution_family("robinhood_spot") == "equity"
    assert asset_class_of_execution_family("alpaca_spot") == "equity"
    assert asset_class_of_execution_family("robinhood_agentic_mcp") == "equity"
    assert asset_class_of_execution_family("ALPACA_SPOT") == "equity"  # normalized
    assert asset_class_of_execution_family("multi_venue_arbitrage") == "other"


def test_alpaca_and_robinhood_share_equity_asset_class():
    # The A/B enabler: an equity that default-resolves to robinhood_spot may ALSO be requested
    # via alpaca_spot (same asset class) -> the venue check must NOT block it.
    assert asset_class_of_execution_family("alpaca_spot") == asset_class_of_execution_family("robinhood_spot")


def test_cross_asset_class_still_differs():
    # The safety case the check must still BLOCK: an equity venue vs the crypto venue.
    assert asset_class_of_execution_family("alpaca_spot") != asset_class_of_execution_family("coinbase_spot")
    assert asset_class_of_execution_family("robinhood_spot") != asset_class_of_execution_family("coinbase_spot")
