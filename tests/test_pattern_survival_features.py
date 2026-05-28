from __future__ import annotations

import pytest

from app.services.trading.pattern_survival.features import _collect_realized_30d


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Session:
    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.params = {}

    def execute(self, stmt, params):
        self.sql = str(stmt)
        self.params = dict(params)
        return _Result(self.row)


def test_collect_realized_30d_filters_to_closed_valid_notional_trades() -> None:
    db = _Session((2, 0.5, 16.0, 8.0, -4.0))

    out = _collect_realized_30d(db, pattern_id=42)

    assert out["trades_30d"] == 2
    assert out["hit_rate_30d"] == pytest.approx(0.5)
    assert out["expectancy_30d_pct"] == pytest.approx(16.0)
    assert out["sharpe_30d"] == pytest.approx(2.0)
    assert out["max_drawdown_30d_pct"] == pytest.approx(-4.0)
    assert "status = 'closed'" in db.sql
    assert "pnl IS NOT NULL" in db.sql
    assert "entry_price > 0" in db.sql
    assert "quantity > 0" in db.sql
    assert "asset_kind" in db.sql
    assert db.params == {"p": 42}
