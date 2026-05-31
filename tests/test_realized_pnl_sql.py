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
    assert sql.count("->> 'asset_class'") == 2
    assert "'option_contract'" in sql
    assert "'robinhood_options'" in sql


def test_live_contract_multiplier_sql_honors_snapshot_multiplier() -> None:
    sql = _compact(trade_contract_multiplier_sql("t"))

    assert "->> 'option_contract_multiplier'" in sql
    assert "->> 'contract_multiplier'" in sql
    assert "BTRIM(COALESCE(" in sql
    assert "[eE][+-]?[0-9]+" in sql
    assert "ELSE FALSE END" in sql


def test_paper_contract_multiplier_sql_honors_signal_asset_kind() -> None:
    sql = _compact(paper_trade_contract_multiplier_sql("pt"))

    assert "pt.signal_json" in sql
    assert sql.count("->> 'asset_kind'") == 3
    assert sql.count("->> 'asset_class'") == 3
    assert "'option_contract'" in sql
    assert "'robinhood_options'" in sql


def test_paper_contract_multiplier_sql_honors_paper_meta_multiplier() -> None:
    sql = _compact(paper_trade_contract_multiplier_sql("pt"))

    assert "-> '_paper_meta'" in sql
    assert "-> '_paper_meta') ? 'option_meta'" in sql
    assert "-> '_paper_meta') ->> 'options_path'" in sql
    assert "->> 'option_contract_multiplier'" in sql
    assert "->> 'contract_multiplier'" in sql
    assert sql.count("->> 'option_contract_multiplier'") >= 3
    assert "ELSE FALSE END" in sql
    assert "::numeric = 100.0" in sql
