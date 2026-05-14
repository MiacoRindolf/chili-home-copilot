from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.models.trading import ScanPattern, Trade
from app.services.trading.pattern_regime_ledger import build_ledger


def test_regime_ledger_excludes_closed_trades_without_exit_price(db):
    pattern = ScanPattern(
        name="ledger-quality-pattern",
        rules_json={},
        timeframe="1d",
    )
    db.add(pattern)
    db.flush()

    now = datetime.utcnow()
    db.add_all(
        [
            Trade(
                ticker="ABC",
                direction="long",
                entry_price=10.0,
                exit_price=None,
                quantity=2.0,
                entry_date=now - timedelta(days=2),
                exit_date=now - timedelta(days=1),
                status="closed",
                scan_pattern_id=pattern.id,
                pnl=None,
            ),
            Trade(
                ticker="ABC",
                direction="long",
                entry_price=10.0,
                exit_price=11.0,
                quantity=2.0,
                entry_date=now - timedelta(days=2),
                exit_date=now - timedelta(days=1),
                status="closed",
                scan_pattern_id=pattern.id,
                pnl=None,
            ),
        ]
    )
    db.commit()

    result = build_ledger(db, window_days=30)
    cells = db.execute(
        text(
            """
            SELECT regime_dimension, n_trades, n_wins, mean_pnl_pct, sum_pnl
            FROM trading_pattern_regime_performance_daily
            WHERE ledger_run_id = :run_id AND pattern_id = :pattern_id
            ORDER BY regime_dimension
            """
        ),
        {"run_id": result["run_id"], "pattern_id": pattern.id},
    ).mappings().all()

    assert cells
    assert {row["n_trades"] for row in cells} == {1}
    assert {row["n_wins"] for row in cells} == {1}
    for row in cells:
        assert row["mean_pnl_pct"] == pytest.approx(10.0)
        assert row["sum_pnl"] == pytest.approx(2.0)
