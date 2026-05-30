from __future__ import annotations

from app.services.trading.realized_pnl_sql import (
    paper_trade_contract_multiplier_sql,
    trade_contract_multiplier_sql,
)


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def test_live_contract_multiplier_sql_honors_snapshot_asset_kind() -> None:
    sql = _compact(trade_contract_multiplier_sql("t"))

    assert "t.indicator_snapshot" in sql
    assert sql.count("->> 'asset_kind'") == 2


def test_paper_contract_multiplier_sql_honors_signal_asset_kind() -> None:
    sql = _compact(paper_trade_contract_multiplier_sql("pt"))

    assert "pt.signal_json" in sql
    assert sql.count("->> 'asset_kind'") == 2
