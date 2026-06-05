"""The per-entry portfolio drawdown gate must measure the LIVE book, not the
paper-shadow book. Regression for the unit-mismatch that divided the paper
book's simulated exploration losses by live capital and chronically blocked
live entries.
"""
from unittest.mock import MagicMock

import app.services.trading.portfolio_optimizer as po
import app.services.trading.portfolio_risk as pr


def _mock_db(open_count: int = 0) -> MagicMock:
    db = MagicMock()
    # check_portfolio_drawdown does db.query(Trade).filter(...).filter(...).count()
    db.query.return_value.filter.return_value.filter.return_value.count.return_value = open_count
    db.query.return_value.filter.return_value.count.return_value = open_count
    return db


def test_gate_uses_live_book_and_ignores_paper(monkeypatch):
    # Live book healthy (+$250); the paper book's simulated losses are irrelevant
    # because the gate no longer queries PaperTrade at all.
    monkeypatch.setattr(pr, "_compute_unrealized_pnl", lambda db, uid: -50.0)
    monkeypatch.setattr(pr, "_monthly_total_pnl", lambda db, uid: 300.0)
    r = po.check_portfolio_drawdown(_mock_db(open_count=3), user_id=1, capital=10_000.0)
    assert r["unrealized_pnl"] == -50.0
    assert r["closed_30d_pnl"] == 300.0
    assert r["total_pnl"] == 250.0
    assert r["dd_pct"] == 2.5          # +2.5% on live capital — healthy
    assert r["breached"] is False
    assert r["open_positions"] == 3


def test_gate_still_trips_on_real_live_drawdown(monkeypatch):
    # Safety preserved: a genuine LIVE drawdown beyond the limit still trips.
    monkeypatch.setattr(pr, "_compute_unrealized_pnl", lambda db, uid: -1000.0)
    monkeypatch.setattr(pr, "_monthly_total_pnl", lambda db, uid: -2000.0)
    r = po.check_portfolio_drawdown(_mock_db(open_count=1), user_id=1, capital=10_000.0)
    assert r["total_pnl"] == -3000.0
    assert r["dd_pct"] == -30.0        # -30% < -15% limit
    assert r["breached"] is True
    assert r["reason"] == "drawdown_breached"


def test_gate_invalid_capital_fails_closed(monkeypatch):
    monkeypatch.setattr(pr, "_compute_unrealized_pnl", lambda db, uid: 0.0)
    monkeypatch.setattr(pr, "_monthly_total_pnl", lambda db, uid: 0.0)
    r = po.check_portfolio_drawdown(_mock_db(), user_id=1, capital=0.0)
    assert r["reason"] == "invalid_capital"
    assert r["breached"] is True
