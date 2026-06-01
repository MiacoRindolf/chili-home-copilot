"""SQL snippets for realized P&L return normalization.

Live option ``Trade.pnl`` is recorded in dollars per contract
(``premium_delta * quantity * 100``). Any learner that divides by
``entry_price * quantity`` must therefore include the contract multiplier,
or option returns are overstated by 100x.
"""
from __future__ import annotations

OPTION_CONTRACT_MULTIPLIER_SQL = "100.0"
PRICE_DOMAIN_OPTION_PREMIUM_SQL = "'option_premium'"
OPTION_ASSET_CLASS_ALIASES_SQL = (
    "('option', 'options', 'option_contract', 'option_contracts', "
    "'options_contract', 'options_contracts', 'contract_option', "
    "'contract_options', 'equity_option', 'equity_options', "
    "'stock_option', 'stock_options', 'option_spread', "
    "'options_spread', 'option_spreads', 'options_spreads', "
    "'optionspread', 'optionspreads', 'robinhood_option', "
    "'robinhood_options')"
)


def _col(alias: str | None, name: str) -> str:
    return f"{alias}.{name}" if alias else name


def _json_truthy_sql(json_expr: str, key: str) -> str:
    return (
        f"LOWER(COALESCE({json_expr} ->> '{key}', 'false')) "
        "IN ('1', 'true', 'yes', 'on')"
    )


def _json_numeric_equals_sql(json_expr: str, key: str, expected: str) -> str:
    raw = f"BTRIM(COALESCE({json_expr} ->> '{key}', ''))"
    return (
        f"CASE WHEN {raw} ~ '^[+-]?([0-9]+(\\.[0-9]*)?|\\.[0-9]+)([eE][+-]?[0-9]+)?$' "
        f"THEN ({raw})::numeric = {expected} ELSE FALSE END"
    )


def _json_option_price_domain_sql(json_expr: str) -> str:
    price_domains = f"({json_expr} -> 'price_domains')"
    option_meta = f"({json_expr} -> 'option_meta')"
    entry_execution = f"({json_expr} -> 'entry_execution')"
    return (
        "("
        f"LOWER(COALESCE({price_domains} ->> 'entry_price', '')) = {PRICE_DOMAIN_OPTION_PREMIUM_SQL}"
        f" OR LOWER(COALESCE({price_domains} ->> 'exit_price', '')) = {PRICE_DOMAIN_OPTION_PREMIUM_SQL}"
        f" OR LOWER(COALESCE({price_domains} ->> 'limit_price', '')) = {PRICE_DOMAIN_OPTION_PREMIUM_SQL}"
        f" OR LOWER(COALESCE({json_expr} ->> 'option_price_domain', '')) = {PRICE_DOMAIN_OPTION_PREMIUM_SQL}"
        f" OR LOWER(COALESCE({option_meta} ->> 'price_domain', '')) = {PRICE_DOMAIN_OPTION_PREMIUM_SQL}"
        f" OR LOWER(COALESCE({entry_execution} ->> 'option_price_domain', '')) = {PRICE_DOMAIN_OPTION_PREMIUM_SQL}"
        ")"
    )


def _asset_alias_sql(expr: str) -> str:
    return (
        f"REPLACE(LOWER(COALESCE({expr}, '')), '-', '_') "
        f"IN {OPTION_ASSET_CLASS_ALIASES_SQL}"
    )


def _json_asset_alias_sql(json_expr: str, key: str) -> str:
    return _asset_alias_sql(f"{json_expr} ->> '{key}'")


def _partial_declared_sql(alias: str | None) -> str:
    return (
        f"(COALESCE({_col(alias, 'partial_taken')}, FALSE) "
        f"OR {_col(alias, 'partial_taken_qty')} IS NOT NULL "
        f"OR {_col(alias, 'partial_taken_price')} IS NOT NULL)"
    )


def _direction_is_short_sql(alias: str | None) -> str:
    return f"LOWER(COALESCE({_col(alias, 'direction')}, 'long')) = 'short'"


def _realized_return_fraction_sql(
    alias: str | None,
    *,
    contract_multiplier_sql: str,
    non_partial_quantity_sql: str,
) -> str:
    """Return partial-aware ``pnl / opening_notional`` SQL."""
    pnl = _col(alias, "pnl")
    entry = _col(alias, "entry_price")
    qty = _col(alias, "quantity")
    partial_qty = _col(alias, "partial_taken_qty")
    partial_price = _col(alias, "partial_taken_price")
    multiplier = f"({contract_multiplier_sql})"
    partial_pnl = (
        "CASE "
        f"WHEN {_direction_is_short_sql(alias)} "
        f"THEN ({entry} - {partial_price}) * {partial_qty} * {multiplier} "
        f"ELSE ({partial_price} - {entry}) * {partial_qty} * {multiplier} "
        "END"
    )
    return f"""
        CASE
          WHEN {pnl} IS NULL
            OR {entry} IS NULL
            OR {entry} <= 0
            OR {multiplier} IS NULL
            OR {multiplier} <= 0
          THEN NULL
          WHEN {_partial_declared_sql(alias)}
          THEN
            CASE
              WHEN {qty} IS NOT NULL
                AND {qty} > 0
                AND {partial_qty} IS NOT NULL
                AND {partial_qty} > 0
                AND {partial_price} IS NOT NULL
                AND {partial_price} > 0
                AND ({qty} + {partial_qty}) > 0
              THEN ({pnl} + ({partial_pnl})) / ({entry} * ({qty} + {partial_qty}) * {multiplier})
              ELSE NULL
            END
          WHEN ({non_partial_quantity_sql}) IS NOT NULL
            AND ({non_partial_quantity_sql}) > 0
          THEN {pnl} / ({entry} * ({non_partial_quantity_sql}) * {multiplier})
          ELSE NULL
        END
    """


def trade_contract_multiplier_sql(alias: str | None = None) -> str:
    """Return a PostgreSQL expression for a live trade contract multiplier."""
    asset_kind = _col(alias, "asset_kind")
    tags = _col(alias, "tags")
    snap = f"COALESCE({_col(alias, 'indicator_snapshot')}, '{{}}'::jsonb)"
    breakout = f"({snap} -> 'breakout_alert')"
    return f"""
        CASE
          WHEN {_asset_alias_sql(asset_kind)}
            OR LOWER(COALESCE({tags}, '')) LIKE '%option%'
            OR {snap} ? 'option_meta'
            OR {_json_asset_alias_sql(snap, 'asset_kind')}
            OR {_json_asset_alias_sql(snap, 'asset_type')}
            OR {_json_asset_alias_sql(snap, 'asset_class')}
            OR {_json_option_price_domain_sql(snap)}
            OR {_json_truthy_sql(snap, 'options_path')}
            OR {_json_numeric_equals_sql(snap, 'option_contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {_json_numeric_equals_sql(snap, 'contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {breakout} ? 'option_meta'
            OR {_json_asset_alias_sql(breakout, 'asset_kind')}
            OR {_json_asset_alias_sql(breakout, 'asset_type')}
            OR {_json_asset_alias_sql(breakout, 'asset_class')}
            OR {_json_option_price_domain_sql(breakout)}
            OR {_json_truthy_sql(breakout, 'options_path')}
            OR {_json_numeric_equals_sql(breakout, 'option_contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {_json_numeric_equals_sql(breakout, 'contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
          THEN {OPTION_CONTRACT_MULTIPLIER_SQL}
          ELSE 1.0
        END
    """


def paper_trade_contract_multiplier_sql(alias: str | None = None) -> str:
    """Return a PostgreSQL expression for a paper trade contract multiplier."""
    signal = f"COALESCE({_col(alias, 'signal_json')}, '{{}}'::jsonb)"
    breakout = f"({signal} -> 'breakout_alert')"
    paper_meta = f"({signal} -> '_paper_meta')"
    return f"""
        CASE
          WHEN {signal} ? 'option_meta'
            OR {_json_asset_alias_sql(signal, 'asset_kind')}
            OR {_json_asset_alias_sql(signal, 'asset_type')}
            OR {_json_asset_alias_sql(signal, 'asset_class')}
            OR {_json_option_price_domain_sql(signal)}
            OR {_json_truthy_sql(signal, 'options_path')}
            OR {_json_numeric_equals_sql(signal, 'option_contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {_json_numeric_equals_sql(signal, 'contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {paper_meta} ? 'option_meta'
            OR {_json_option_price_domain_sql(paper_meta)}
            OR {_json_truthy_sql(paper_meta, 'options_path')}
            OR {_json_asset_alias_sql(paper_meta, 'asset_kind')}
            OR {_json_asset_alias_sql(paper_meta, 'asset_type')}
            OR {_json_asset_alias_sql(paper_meta, 'asset_class')}
            OR {_json_numeric_equals_sql(paper_meta, 'option_contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {_json_numeric_equals_sql(paper_meta, 'contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {breakout} ? 'option_meta'
            OR {_json_asset_alias_sql(breakout, 'asset_kind')}
            OR {_json_asset_alias_sql(breakout, 'asset_type')}
            OR {_json_asset_alias_sql(breakout, 'asset_class')}
            OR {_json_option_price_domain_sql(breakout)}
            OR {_json_truthy_sql(breakout, 'options_path')}
            OR {_json_numeric_equals_sql(breakout, 'option_contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {_json_numeric_equals_sql(breakout, 'contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
          THEN {OPTION_CONTRACT_MULTIPLIER_SQL}
          ELSE 1.0
        END
    """


def trade_return_fraction_sql(alias: str | None = None) -> str:
    """Return ``pnl / opening_notional`` for live trades.

    Mirrors ``return_math.realized_return_pct``: partial-close rows rebuild
    opening quantity from ``quantity + partial_taken_qty`` and fail closed
    when partial evidence is incomplete. Non-partial live rows prefer broker
    filled quantity when present.
    """
    filled_or_quantity = (
        f"CASE WHEN {_col(alias, 'filled_quantity')} IS NOT NULL "
        f"AND {_col(alias, 'filled_quantity')} > 0 "
        f"THEN {_col(alias, 'filled_quantity')} ELSE {_col(alias, 'quantity')} END"
    )
    return _realized_return_fraction_sql(
        alias,
        contract_multiplier_sql=trade_contract_multiplier_sql(alias),
        non_partial_quantity_sql=filled_or_quantity,
    )


def paper_trade_return_fraction_sql(alias: str | None = None) -> str:
    """Return ``pnl / opening_notional`` for paper trades."""
    return _realized_return_fraction_sql(
        alias,
        contract_multiplier_sql=paper_trade_contract_multiplier_sql(alias),
        non_partial_quantity_sql=_col(alias, "quantity"),
    )
