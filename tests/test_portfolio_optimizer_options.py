from __future__ import annotations

import pytest

from app.models.trading import Trade
from app.services.trading import portfolio_risk


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)


class _FakeDb:
    def __init__(self, *result_sets):
        self._result_sets = list(result_sets) or [[]]
        self._idx = 0

    def query(self, *_args, **_kwargs):
        rows = self._result_sets[min(self._idx, len(self._result_sets) - 1)]
        self._idx += 1
        return _FakeQuery(rows)


def _open_trade() -> Trade:
    return Trade(
        ticker="SPY",
        direction="long",
        entry_price=100.0,
        quantity=1.0,
        status="open",
    )


def test_portfolio_drawdown_uses_live_book_helpers(monkeypatch) -> None:
    from app.services.trading.portfolio_optimizer import check_portfolio_drawdown

    monkeypatch.setattr(
        portfolio_risk,
        "_compute_unrealized_pnl",
        lambda db, user_id: -250.0,
    )
    monkeypatch.setattr(
        portfolio_risk,
        "_monthly_total_pnl",
        lambda db, user_id: 50.0,
    )

    out = check_portfolio_drawdown(
        _FakeDb([_open_trade()]),
        user_id=None,
        capital=10_000.0,
        max_dd_pct=15.0,
    )

    assert out["ok"] is True
    assert out["breached"] is False
    assert out["reason"] is None
    assert out["unrealized_pnl"] == pytest.approx(-250.0)
    assert out["closed_30d_pnl"] == pytest.approx(50.0)
    assert out["total_pnl"] == pytest.approx(-200.0)
    assert out["dd_pct"] == pytest.approx(-2.0)
    assert out["open_positions"] == 1
    assert out["valuation_complete"] is True
    assert out["valuation_missing_count"] == 0


def test_portfolio_drawdown_blocks_real_live_book_breach(monkeypatch) -> None:
    from app.services.trading.portfolio_optimizer import check_portfolio_drawdown

    monkeypatch.setattr(
        portfolio_risk,
        "_compute_unrealized_pnl",
        lambda db, user_id: -500.0,
    )
    monkeypatch.setattr(
        portfolio_risk,
        "_monthly_total_pnl",
        lambda db, user_id: -1_600.0,
    )

    out = check_portfolio_drawdown(
        _FakeDb([_open_trade()]),
        user_id=None,
        capital=10_000.0,
        max_dd_pct=15.0,
    )

    assert out["ok"] is False
    assert out["breached"] is True
    assert out["reason"] == "drawdown_breached"
    assert out["total_pnl"] == pytest.approx(-2_100.0)
    assert out["dd_pct"] == pytest.approx(-21.0)
    assert out["valuation_complete"] is True


def test_portfolio_drawdown_blocks_invalid_capital(monkeypatch) -> None:
    from app.services.trading.portfolio_optimizer import check_portfolio_drawdown

    monkeypatch.setattr(
        portfolio_risk,
        "_compute_unrealized_pnl",
        lambda db, user_id: -100.0,
    )
    monkeypatch.setattr(
        portfolio_risk,
        "_monthly_total_pnl",
        lambda db, user_id: -50.0,
    )

    out = check_portfolio_drawdown(
        _FakeDb([]),
        user_id=None,
        capital=None,
        max_dd_pct=15.0,
    )

    assert out["ok"] is False
    assert out["breached"] is True
    assert out["reason"] == "invalid_capital"
    assert out["valuation_complete"] is True
    assert out["valuation_missing_count"] == 0
    assert out["open_positions"] == 0
    assert out["total_pnl"] == pytest.approx(-150.0)
    assert out["dd_pct"] == pytest.approx(0.0)
