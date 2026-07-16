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
