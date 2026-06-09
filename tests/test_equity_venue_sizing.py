"""Phase E4: venue-aware equity-relative sizing (RH equity for stocks, Coinbase for crypto)."""
from __future__ import annotations

import app.services.trading.momentum_neural.risk_policy as rp
from app.services import broker_service, coinbase_service


def _patch(monkeypatch):
    monkeypatch.setattr(broker_service, "get_portfolio", lambda: {"equity": 50000.0, "buying_power": 50000.0})
    monkeypatch.setattr(coinbase_service, "get_portfolio", lambda: {"equity": 2000.0})


def test_account_equity_is_venue_aware(monkeypatch):
    _patch(monkeypatch)
    assert rp._account_equity_usd("robinhood_spot") == 50000.0  # RH equity for stocks
    assert rp._account_equity_usd("coinbase_spot") == 2000.0    # Coinbase for crypto
    assert rp._account_equity_usd(None) == 2000.0               # default coinbase


def test_caps_scale_to_the_right_venue_equity(monkeypatch):
    _patch(monkeypatch)
    # notional fraction default 0.15
    assert rp.equity_relative_notional_cap(500.0, "robinhood_spot") == 7500.0   # 0.15 * 50000
    assert rp.equity_relative_notional_cap(500.0, "coinbase_spot") == 300.0     # 0.15 * 2000
    # loss fraction default 0.01
    assert rp.equity_relative_loss_cap(50.0, "robinhood_spot") == 500.0         # 0.01 * 50000
    assert rp.equity_relative_loss_cap(50.0, "coinbase_spot") == 20.0           # 0.01 * 2000


def test_falls_back_to_fixed_when_equity_unavailable(monkeypatch):
    monkeypatch.setattr(broker_service, "get_portfolio", lambda: {})
    assert rp.equity_relative_notional_cap(500.0, "robinhood_spot") == 500.0  # fixed fallback


def test_sizing_basis_uses_buying_power(monkeypatch):
    # Default: the sizing basis is BUYING POWER (margin-inclusive), not settled equity.
    monkeypatch.setattr(broker_service, "get_portfolio", lambda: {"equity": 10000.0, "buying_power": 21000.0})
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_size_use_buying_power", True, raising=False)
    assert rp._account_equity_usd("robinhood_spot") == 21000.0  # buying power utilized
    # Opt-out -> settled equity only.
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_size_use_buying_power", False, raising=False)
    assert rp._account_equity_usd("robinhood_spot") == 10000.0
    # Buying power missing -> fall back to equity even when the flag is on.
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_size_use_buying_power", True, raising=False)
    monkeypatch.setattr(broker_service, "get_portfolio", lambda: {"equity": 10000.0})
    assert rp._account_equity_usd("robinhood_spot") == 10000.0
