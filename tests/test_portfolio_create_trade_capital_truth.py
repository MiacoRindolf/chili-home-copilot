from __future__ import annotations

import pytest

from app.services.trading import portfolio


class _FakeDb:
    def __init__(self) -> None:
        self.added = None
        self.committed = False

    def add(self, obj) -> None:
        self.added = obj

    def commit(self) -> None:
        self.committed = True

    def refresh(self, obj) -> None:
        obj.id = 1


def test_manual_trade_journal_does_not_call_entry_risk_gate(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise AssertionError("manual trade journal must not size risk")

    monkeypatch.setattr(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        _boom,
    )
    db = _FakeDb()

    trade = portfolio.create_trade(
        db,
        user_id=11,
        ticker="AAPL",
        direction="long",
        entry_price=150.0,
        quantity=1,
        indicator_snapshot="{}",
    )

    assert trade.id == 1
    assert trade.management_scope == "manual"
    assert db.committed is True


def test_entry_risk_opt_in_requires_proven_capital() -> None:
    with pytest.raises(ValueError, match="risk_capital_unavailable"):
        portfolio.create_trade(
            _FakeDb(),
            user_id=11,
            ticker="AAPL",
            direction="long",
            entry_price=150.0,
            quantity=1,
            indicator_snapshot="{}",
            enforce_entry_risk=True,
        )


def test_entry_risk_opt_in_uses_proven_capital(monkeypatch) -> None:
    db = _FakeDb()
    calls = []

    def _fake_gate(db_arg, user_id_arg, ticker_arg, *, capital):
        calls.append((db_arg, user_id_arg, ticker_arg, capital))
        return True, "ok"

    monkeypatch.setattr(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        _fake_gate,
    )

    trade = portfolio.create_trade(
        db,
        user_id=11,
        ticker="AAPL",
        direction="long",
        entry_price=150.0,
        quantity=1,
        indicator_snapshot="{}",
        enforce_entry_risk=True,
        risk_capital=12_345.67,
    )

    assert trade.id == 1
    assert calls == [(db, 11, "AAPL", 12_345.67)]
