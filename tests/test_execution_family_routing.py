"""Per-symbol execution-family routing (crypto->Coinbase, equity->Robinhood)."""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.execution_family_registry import (
    EXECUTION_FAMILY_ALPACA_SPOT,
    EXECUTION_FAMILY_COINBASE_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
    ExecutionFamilyRoutingError,
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


def _configure_alpaca_paper_route(monkeypatch, *, ready: bool) -> None:
    monkeypatch.setattr(
        settings, "chili_momentum_equity_execution_via_alpaca_paper", True
    )
    monkeypatch.setattr(settings, "chili_alpaca_enabled", ready)
    monkeypatch.setattr(settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(settings, "chili_alpaca_api_key", "paper-key" if ready else "")
    monkeypatch.setattr(
        settings, "chili_alpaca_api_secret", "paper-secret" if ready else ""
    )
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        "00000000-0000-0000-0000-000000000001" if ready else "",
    )


def test_selected_alpaca_paper_equity_route_never_falls_back_to_robinhood(
    monkeypatch,
):
    _configure_alpaca_paper_route(monkeypatch, ready=False)

    with pytest.raises(
        ExecutionFamilyRoutingError, match="alpaca_paper_equity_route_not_ready"
    ):
        resolve_execution_family_for_symbol("AAPL", mode="live")


def test_selected_alpaca_paper_equity_route_requires_paper_posture(monkeypatch):
    _configure_alpaca_paper_route(monkeypatch, ready=True)
    monkeypatch.setattr(settings, "chili_alpaca_paper", False)

    with pytest.raises(
        ExecutionFamilyRoutingError, match="alpaca_paper_equity_route_not_ready"
    ):
        resolve_execution_family_for_symbol("AAPL", mode="live")


def test_selected_ready_alpaca_paper_equity_route_is_exact(monkeypatch):
    _configure_alpaca_paper_route(monkeypatch, ready=True)

    assert (
        resolve_execution_family_for_symbol("AAPL", mode="live")
        == EXECUTION_FAMILY_ALPACA_SPOT
    )


def test_db_only_paper_mode_retains_simulator_fallback_without_selected_route(
    monkeypatch,
):
    monkeypatch.setattr(
        settings, "chili_momentum_equity_execution_via_alpaca_paper", False
    )
    monkeypatch.setattr(settings, "chili_alpaca_enabled", True)
    monkeypatch.setattr(settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(settings, "chili_alpaca_api_key", "paper-key")
    monkeypatch.setattr(settings, "chili_alpaca_api_secret", "paper-secret")
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", "")

    assert (
        resolve_execution_family_for_symbol("AAPL", mode="paper")
        == EXECUTION_FAMILY_ROBINHOOD_SPOT
    )


@pytest.fixture
def crypto_paper_route(monkeypatch):
    _configure_alpaca_paper_route(monkeypatch, ready=True)
    monkeypatch.setattr(settings, "chili_momentum_equity_execution_via_alpaca_paper", False)
    monkeypatch.setattr(settings, "chili_momentum_crypto_execution_via_alpaca_paper", True)


@pytest.mark.parametrize("symbol", ["BTC-USD", "00", "BTC/USD", "ETH/BTC", "DOGE/USDC"])
@pytest.mark.parametrize("outcome", ["listed", "unlisted", "error"])
def test_crypto_paper_route_never_substitutes_live_family(monkeypatch, crypto_paper_route, symbol, outcome):
    from app.services.trading.venue import alpaca_spot
    calls = []
    def listing(value):
        calls.append(value)
        if outcome == "error":
            raise RuntimeError("listing unavailable")
        return outcome == "listed"
    monkeypatch.setattr(alpaca_spot, "alpaca_lists_symbol", listing)
    if outcome == "listed":
        assert resolve_execution_family_for_symbol(symbol) == EXECUTION_FAMILY_ALPACA_SPOT
    else:
        with pytest.raises(ExecutionFamilyRoutingError, match="alpaca_paper_crypto_listing_unavailable"):
            resolve_execution_family_for_symbol(symbol)
    assert calls == [symbol]


@pytest.mark.parametrize("field,value", [
    ("chili_alpaca_enabled", False), ("chili_alpaca_paper", False),
    ("chili_alpaca_api_key", ""), ("chili_alpaca_api_secret", ""),
    ("chili_alpaca_expected_account_id", ""),
])
def test_crypto_paper_missing_posture_refuses_before_listing(monkeypatch, crypto_paper_route, field, value):
    from app.services.trading.venue import alpaca_spot
    monkeypatch.setattr(settings, field, value)
    def forbidden(_):
        pytest.fail("unready PAPER route must not read the broker")
    monkeypatch.setattr(alpaca_spot, "alpaca_lists_symbol", forbidden)
    with pytest.raises(ExecutionFamilyRoutingError, match="alpaca_paper_crypto_route_not_ready"):
        resolve_execution_family_for_symbol("BTC/USD")


def test_crypto_paper_classifier_failure_cannot_choose_live_family(monkeypatch, crypto_paper_route):
    from app.services.trading.venue import robinhood_spot
    def unavailable(_):
        raise RuntimeError("classifier unavailable")
    monkeypatch.setattr(robinhood_spot, "_is_crypto_product", unavailable)
    with pytest.raises(ExecutionFamilyRoutingError, match="alpaca_paper_crypto_route_resolution_failed"):
        resolve_execution_family_for_symbol("BTC-USD")


def test_crypto_latch_does_not_reclassify_dashed_equity(monkeypatch, crypto_paper_route):
    from app.services.trading.venue import alpaca_spot
    def forbidden(_):
        pytest.fail("equity must not be probed as crypto")
    monkeypatch.setattr(alpaca_spot, "alpaca_lists_symbol", forbidden)
    assert resolve_execution_family_for_symbol("ODD-W") == EXECUTION_FAMILY_ROBINHOOD_SPOT


def test_crypto_paper_routing_refusal_is_not_auto_arm_readiness(monkeypatch, crypto_paper_route):
    from app.services.trading.momentum_neural import auto_arm
    from app.services.trading.venue import alpaca_spot
    monkeypatch.setattr(alpaca_spot, "alpaca_lists_symbol", lambda _: False)
    cache = {"coinbase_spot": True, "alpaca_spot": True}
    assert auto_arm._readiness_reads_coinbase("BTC/USD") == (False, "family_unresolved")
    assert auto_arm._venue_broker_ready_for("BTC/USD", cache) is False
    assert cache == {"coinbase_spot": True, "alpaca_spot": True}


@pytest.mark.parametrize("symbol", ["BTC/USD", "ETH/BTC", "DOGE/USDC"])
def test_native_crypto_symbol_cannot_bypass_auto_arm_crypto_posture(crypto_paper_route, symbol):
    from app.services.trading.momentum_neural import auto_arm
    assert auto_arm._is_coinbase_tradeable_symbol(symbol) is True
    assert auto_arm._coinbase_spot_paper_posture_refused(symbol, "coinbase_spot") is True
