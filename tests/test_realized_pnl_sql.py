from __future__ import annotations

from app.services.trading.realized_pnl_sql import (
    PAPER_DYNAMIC_PATTERN_EV_EXCLUDED_EXIT_REASONS,
    paper_dynamic_pattern_ev_exit_filter_sql,
    paper_trade_contract_multiplier_sql,
    paper_trade_return_fraction_sql,
    trade_contract_multiplier_sql,
    trade_return_fraction_sql,
)


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def test_live_contract_multiplier_sql_honors_snapshot_asset_kind() -> None:
    sql = _compact(trade_contract_multiplier_sql("t"))

    assert "t.indicator_snapshot" in sql
    assert sql.count("->> 'asset_kind'") == 2
    assert sql.count("->> 'asset_class'") == 2
    assert "'option_contract'" in sql
    assert "'option_contracts'" in sql
    assert "'contract_options'" in sql
    assert "'robinhood_options'" in sql
    assert "REPLACE(LOWER(COALESCE(" in sql


def test_live_contract_multiplier_sql_honors_snapshot_multiplier() -> None:
    sql = _compact(trade_contract_multiplier_sql("t"))

    assert "->> 'option_contract_multiplier'" in sql
    assert "->> 'contract_multiplier'" in sql
    assert "BTRIM(COALESCE(" in sql
    assert "[eE][+-]?[0-9]+" in sql
    assert "ELSE FALSE END" in sql


def test_live_contract_multiplier_sql_honors_price_domain_identity() -> None:
    sql = _compact(trade_contract_multiplier_sql("t"))

    assert "-> 'price_domains'" in sql
    assert "->> 'entry_price'" in sql
    assert "->> 'exit_price'" in sql
    assert "->> 'limit_price'" in sql
    assert "->> 'option_price_domain'" in sql
    assert "->> 'price_domain'" in sql
    assert "'option_premium'" in sql


def test_paper_contract_multiplier_sql_honors_signal_asset_kind() -> None:
    sql = _compact(paper_trade_contract_multiplier_sql("pt"))

    assert "pt.signal_json" in sql
    assert sql.count("->> 'asset_kind'") == 3
    assert sql.count("->> 'asset_class'") == 3
    assert "'option_contract'" in sql
    assert "'equity_options'" in sql
    assert "'robinhood_options'" in sql
    assert "REPLACE(LOWER(COALESCE(" in sql


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


def test_paper_contract_multiplier_sql_honors_price_domain_identity() -> None:
    sql = _compact(paper_trade_contract_multiplier_sql("pt"))

    assert "-> 'price_domains'" in sql
    assert "-> '_paper_meta'" in sql
    assert sql.count("->> 'entry_price'") >= 3
    assert sql.count("->> 'option_price_domain'") >= 3
    assert "'option_premium'" in sql


def test_live_return_fraction_sql_is_partial_and_fill_aware() -> None:
    sql = _compact(trade_return_fraction_sql("t"))

    assert "t.partial_taken" in sql
    assert "t.partial_taken_qty" in sql
    assert "t.partial_taken_price" in sql
    assert "t.quantity + t.partial_taken_qty" in sql
    assert "t.filled_quantity" in sql
    assert "CASE WHEN t.filled_quantity IS NOT NULL AND t.filled_quantity > 0" in sql
    assert "LOWER(COALESCE(t.direction, 'long')) = 'short'" in sql
    assert "ELSE NULL" in sql


def test_paper_return_fraction_sql_is_partial_aware_without_filled_quantity() -> None:
    sql = _compact(paper_trade_return_fraction_sql("pt"))

    assert "pt.partial_taken" in sql
    assert "pt.partial_taken_qty" in sql
    assert "pt.partial_taken_price" in sql
    assert "pt.quantity + pt.partial_taken_qty" in sql
    assert "pt.filled_quantity" not in sql
    assert "LOWER(COALESCE(pt.direction, 'long')) = 'short'" in sql
    assert "ELSE NULL" in sql


def test_paper_dynamic_pattern_ev_filter_excludes_shadow_janitor_only() -> None:
    sql = _compact(paper_dynamic_pattern_ev_exit_filter_sql("pt"))

    assert PAPER_DYNAMIC_PATTERN_EV_EXCLUDED_EXIT_REASONS == frozenset({
        "shadow_capacity_janitor",
    })
    assert "LOWER(COALESCE(pt.exit_reason, '')) NOT IN" in sql
    assert "'shadow_capacity_janitor'" in sql
    assert "exit_engine_time_decay" not in sql
    assert "'expired'" not in sql


def test_return_fraction_sql_rejects_nonfinite_numeric_inputs() -> None:
    live_sql = _compact(trade_return_fraction_sql("t"))
    paper_sql = _compact(paper_trade_return_fraction_sql("pt"))
    nonfinite_guard = "::text NOT IN ('NaN', 'Infinity', '-Infinity')"

    for column in (
        "t.pnl",
        "t.entry_price",
        "t.quantity",
        "t.partial_taken_qty",
        "t.partial_taken_price",
    ):
        assert f"({column}){nonfinite_guard}" in live_sql
    assert f"t.quantity + t.partial_taken_qty)){nonfinite_guard}" in live_sql
    assert live_sql.count(nonfinite_guard) >= 7

    for column in (
        "pt.pnl",
        "pt.entry_price",
        "pt.quantity",
        "pt.partial_taken_qty",
        "pt.partial_taken_price",
    ):
        assert f"({column}){nonfinite_guard}" in paper_sql
    assert f"pt.quantity + pt.partial_taken_qty)){nonfinite_guard}" in paper_sql
    assert paper_sql.count(nonfinite_guard) >= 7


def test_return_fraction_sql_rejects_nonfinite_computed_outputs() -> None:
    live_sql = _compact(trade_return_fraction_sql("t"))
    paper_sql = _compact(paper_trade_return_fraction_sql("pt"))
    nonfinite_guard = "::text NOT IN ('NaN', 'Infinity', '-Infinity')"

    assert f"t.entry_price * (t.quantity + t.partial_taken_qty) * (" in live_sql
    assert f"t.pnl / (t.entry_price * (CASE WHEN t.filled_quantity" in live_sql
    assert live_sql.count("CASE WHEN") >= 4

    assert f"pt.entry_price * (pt.quantity + pt.partial_taken_qty) * (" in paper_sql
    assert f"pt.pnl / (pt.entry_price * (pt.quantity) * (" in paper_sql
    assert paper_sql.count("CASE WHEN") >= 3

    assert live_sql.count(nonfinite_guard) >= 10
    assert paper_sql.count(nonfinite_guard) >= 10
