from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.venue import alpaca_spot
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter


class _Client:
    def __init__(self, account) -> None:
        self.account = account

    def get_account(self):
        return self.account


def _account(**overrides):
    values = {
        "id": "acct-safety-flags",
        "equity": "100000",
        "last_equity": "100000",
        "buying_power": "400000",
        "cash": "100000",
        "status": "ACTIVE",
        "shorting_enabled": True,
        "multiplier": "4",
        "account_blocked": False,
        "trading_blocked": False,
        "transfers_blocked": False,
        "trade_suspended_by_user": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_account_snapshot_exposes_exact_operational_safety_booleans(
    monkeypatch,
) -> None:
    account = _account(trading_blocked=True)
    monkeypatch.setattr(alpaca_spot, "_trading_client", lambda: _Client(account))

    snapshot = AlpacaSpotAdapter().get_account_snapshot()

    assert snapshot["ok"] is True
    assert snapshot["account_blocked"] is False
    assert snapshot["trading_blocked"] is True
    assert snapshot["transfers_blocked"] is False
    assert snapshot["trade_suspended_by_user"] is False
    assert all(
        isinstance(snapshot[name], bool)
        for name in (
            "account_blocked",
            "trading_blocked",
            "transfers_blocked",
            "trade_suspended_by_user",
        )
    )


def test_account_snapshot_does_not_truthiness_coerce_unreadable_safety_flags(
    monkeypatch,
) -> None:
    account = _account(
        account_blocked="false",
        trading_blocked=0,
        transfers_blocked="false",
        trade_suspended_by_user=None,
    )
    monkeypatch.setattr(alpaca_spot, "_trading_client", lambda: _Client(account))

    snapshot = AlpacaSpotAdapter().get_account_snapshot()

    assert snapshot["ok"] is True
    assert snapshot["account_blocked"] is None
    assert snapshot["trading_blocked"] is None
    assert snapshot["transfers_blocked"] is None
    assert snapshot["trade_suspended_by_user"] is None
