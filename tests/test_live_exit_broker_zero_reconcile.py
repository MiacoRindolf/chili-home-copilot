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
