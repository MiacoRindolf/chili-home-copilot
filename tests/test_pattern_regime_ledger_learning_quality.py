from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.models.trading import ScanPattern, Trade
from app.services.trading.pattern_regime_ledger import (
    _add_backtest_regime_row,
    _add_live_regime_row,
    build_ledger,
)


def test_live_regime_row_accumulator_skips_invalid_realized_returns():
    groups = {}

    assert not _add_live_regime_row(
        groups,
        "ticker_regime",
        SimpleNamespace(
            pid=1,
            regime_label="trend",
            ret_pct=None,
            pnl=-15.0,
            hold_days=1.0,
        ),
    )
    assert groups == {}
    assert not _add_live_regime_row(
        groups,
        "ticker_regime",
        SimpleNamespace(
            pid=1,
            regime_label="trend",
            ret_pct=True,
            pnl=1.0,
            hold_days=1.0,
        ),
    )
    assert groups == {}

    assert _add_live_regime_row(
        groups,
        "ticker_regime",
        SimpleNamespace(
            pid=1,
            regime_label="trend",
            ret_pct=16.0,
            pnl=True,
            hold_days=True,
        ),
    )

    group = groups[("ticker_regime", 1, "trend")]
    assert group.rets_pct == pytest.approx([16.0])
    assert group.pnls == []
    assert group.holds_days == []


def test_backtest_regime_row_accumulator_skips_invalid_returns():
    groups = {}

    assert not _add_backtest_regime_row(
        groups,
        "ticker_regime",
        SimpleNamespace(
            pid=1,
            regime_label="trend",
            ret_pct=float("nan"),
            pnl=0.0,
            hold_days=3.0,
        ),
    )
    assert groups == {}
    assert not _add_backtest_regime_row(
        groups,
        "ticker_regime",
        SimpleNamespace(
            pid=1,
            regime_label="trend",
            ret_pct=False,
            pnl=0.0,
            hold_days=3.0,
        ),
    )
    assert groups == {}

    assert _add_backtest_regime_row(
        groups,
        "ticker_regime",
        SimpleNamespace(
            pid=1,
            regime_label="trend",
            ret_pct=-2.5,
            pnl=-2.5,
            hold_days=3.0,
        ),
    )

    group = groups[("ticker_regime", 1, "trend")]
    assert group.rets_pct == pytest.approx([-2.5])
    assert group.pnls == pytest.approx([-2.5])
    assert group.holds_days == pytest.approx([3.0])


def test_regime_ledger_excludes_closed_trades_without_computable_realized_return(db):
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
                exit_price=11.0,
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
                exit_price=None,
                quantity=2.0,
                entry_date=now - timedelta(days=2),
                exit_date=now - timedelta(days=1),
                status="closed",
                scan_pattern_id=pattern.id,
                pnl=2.0,
            ),
            Trade(
                ticker="ABC",
                direction="long",
                entry_price=1.25,
                exit_price=1.10,
                quantity=1.0,
                entry_date=now - timedelta(days=2),
                exit_date=now - timedelta(days=1),
                status="closed",
                scan_pattern_id=pattern.id,
                pnl=-15.0,
                asset_kind="option",
                partial_taken=True,
                partial_taken_qty=1.0,
                partial_taken_price=None,
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


def test_regime_ledger_option_realized_pnl_uses_contract_multiplier(db):
    pattern = ScanPattern(
        name="ledger-option-pattern",
        rules_json={},
        timeframe="1d",
    )
    db.add(pattern)
    db.flush()

    now = datetime.utcnow()
    db.add(
        Trade(
            ticker="XYZ",
            direction="long",
            entry_price=1.25,
            exit_price=716.0,
            quantity=2.0,
            entry_date=now - timedelta(days=2),
            exit_date=now - timedelta(days=1),
            status="closed",
            scan_pattern_id=pattern.id,
            pnl=40.0,
            asset_kind="option",
            indicator_snapshot={
                "asset_type": "options",
                "option_meta": {
                    "underlying": "XYZ",
                    "symbol": "XYZ260619C00100000",
                    "strike": 100.0,
                    "right": "call",
                    "expiration": "2026-06-19",
                },
            },
        )
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
        assert row["mean_pnl_pct"] == pytest.approx(16.0)
        assert row["sum_pnl"] == pytest.approx(40.0)
