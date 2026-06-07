"""A stuck exit (insufficient balance + broker-confirmed-zero) reconciles, not loops."""
from __future__ import annotations

import app.services.trading.momentum_neural.live_runner as lr
from app.services import coinbase_service


def test_confirmed_zero_when_wallet_zero(monkeypatch):
    monkeypatch.setattr(coinbase_service, "get_accounts_raw", lambda: [
        {"currency": "USD", "available_balance": {"value": "100"}, "hold": {"value": "0"}},
        {"currency": "GWEI", "available_balance": {"value": "0"}, "hold": {"value": "0"}},
    ])
    assert lr._broker_balance_confirms_zero("GWEI-USD") is True


def test_not_zero_when_wallet_has_balance(monkeypatch):
    monkeypatch.setattr(coinbase_service, "get_accounts_raw", lambda: [
        {"currency": "GWEI", "available_balance": {"value": "1613"}, "hold": {"value": "0"}},
    ])
    assert lr._broker_balance_confirms_zero("GWEI-USD") is False


def test_absent_wallet_is_zero(monkeypatch):
    # successful fetch (USD wallet present) but no GWEI wallet -> confirmed zero
    monkeypatch.setattr(coinbase_service, "get_accounts_raw", lambda: [
        {"currency": "USD", "available_balance": {"value": "100"}, "hold": {"value": "0"}},
    ])
    assert lr._broker_balance_confirms_zero("GWEI-USD") is True


def test_failed_fetch_never_confirms_zero(monkeypatch):
    # disconnected / fetch failed -> UNKNOWN -> never reconcile (no false-close)
    monkeypatch.setattr(coinbase_service, "get_accounts_raw", lambda: [])
    assert lr._broker_balance_confirms_zero("GWEI-USD") is False


class _FakeProductClient:
    def __init__(self, base_min, quote_min, price):
        self._p = {"base_min_size": str(base_min), "quote_min_size": str(quote_min), "price": str(price)}

    def get_product(self, _product_id):
        return dict(self._p)


def test_unsellable_dust_confirms_zero(monkeypatch):
    # The CTSI wedge: 3.65 units * $0.0245 = $0.09 notional < $1 quote_min — an
    # unsellable dust residual that must confirm zero so the exit reconciles instead
    # of looping forever on 'Insufficient balance'.
    monkeypatch.setattr(coinbase_service, "get_accounts_raw", lambda: [
        {"currency": "CTSI", "available_balance": {"value": "3.64667982"}, "hold": {"value": "0"}},
    ])
    monkeypatch.setattr(coinbase_service, "get_coinbase_rest_client",
                        lambda: _FakeProductClient(base_min=0.1, quote_min=1, price=0.0245))
    assert lr._broker_balance_confirms_zero("CTSI-USD") is True


def test_real_sellable_position_not_dust(monkeypatch):
    # A genuine, sellable position (notional >> quote_min) must NOT confirm zero.
    monkeypatch.setattr(coinbase_service, "get_accounts_raw", lambda: [
        {"currency": "SOL", "available_balance": {"value": "5"}, "hold": {"value": "0"}},
    ])
    monkeypatch.setattr(coinbase_service, "get_coinbase_rest_client",
                        lambda: _FakeProductClient(base_min=0.01, quote_min=1, price=150.0))
    assert lr._broker_balance_confirms_zero("SOL-USD") is False


def test_below_base_min_size_is_dust(monkeypatch):
    # Below the venue's base_min_size (even if priced) -> unsellable -> zero.
    monkeypatch.setattr(coinbase_service, "get_accounts_raw", lambda: [
        {"currency": "BTC", "available_balance": {"value": "0.0000004"}, "hold": {"value": "0"}},
    ])
    monkeypatch.setattr(coinbase_service, "get_coinbase_rest_client",
                        lambda: _FakeProductClient(base_min=0.000001, quote_min=1, price=68000.0))
    assert lr._broker_balance_confirms_zero("BTC-USD") is True


def test_dust_check_fails_open_when_product_unavailable(monkeypatch):
    # If the product/min lookup fails, do NOT false-reconcile a non-zero balance.
    monkeypatch.setattr(coinbase_service, "get_accounts_raw", lambda: [
        {"currency": "SOL", "available_balance": {"value": "5"}, "hold": {"value": "0"}},
    ])
    monkeypatch.setattr(coinbase_service, "get_coinbase_rest_client", lambda: None)
    assert lr._broker_balance_confirms_zero("SOL-USD") is False
