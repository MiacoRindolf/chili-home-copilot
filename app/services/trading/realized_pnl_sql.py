"""SQL snippets for realized P&L return normalization.

Live option ``Trade.pnl`` is recorded in dollars per contract
(``premium_delta * quantity * 100``). Any learner that divides by
``entry_price * quantity`` must therefore include the contract multiplier,
or option returns are overstated by 100x.
"""
from __future__ import annotations

OPTION_CONTRACT_MULTIPLIER_SQL = "100.0"
PRICE_DOMAIN_OPTION_PREMIUM_SQL = "'option_premium'"
PAPER_DYNAMIC_PATTERN_EV_EXCLUDED_EXIT_REASONS = frozenset({
    "shadow_capacity_janitor",
})
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


def _finite_number_sql(expr: str) -> str:
    return (
        f"(({expr}) IS NOT NULL "
        f"AND ({expr})::text NOT IN ('NaN', 'Infinity', '-Infinity'))"
    )


def _positive_finite_number_sql(expr: str) -> str:
    return f"({_finite_number_sql(expr)} AND ({expr}) > 0)"


def paper_dynamic_pattern_ev_exit_filter_sql(alias: str | None = None) -> str:
    """Exclude paper-shadow cleanup rows from pattern EV queries."""
    exit_reason = _col(alias, "exit_reason")
    excluded = ", ".join(
        f"'{reason}'"
        for reason in sorted(PAPER_DYNAMIC_PATTERN_EV_EXCLUDED_EXIT_REASONS)
    )
    return f"LOWER(COALESCE({exit_reason}, '')) NOT IN ({excluded})"


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
    non_partial_qty = f"({non_partial_quantity_sql})"
    opening_qty = f"({qty} + {partial_qty})"
    partial_pnl = (
        "CASE "
        f"WHEN {_direction_is_short_sql(alias)} "
        f"THEN ({entry} - {partial_price}) * {partial_qty} * {multiplier} "
        f"ELSE ({partial_price} - {entry}) * {partial_qty} * {multiplier} "
        "END"
    )
    partial_denominator = f"({entry} * {opening_qty} * {multiplier})"
    partial_return = f"(({pnl} + ({partial_pnl})) / {partial_denominator})"
    non_partial_denominator = f"({entry} * {non_partial_qty} * {multiplier})"
    non_partial_return = f"({pnl} / {non_partial_denominator})"
    return f"""
        CASE
          WHEN NOT {_finite_number_sql(pnl)}
            OR NOT {_positive_finite_number_sql(entry)}
            OR NOT {_positive_finite_number_sql(multiplier)}
          THEN NULL
          WHEN {_partial_declared_sql(alias)}
          THEN
            CASE
              WHEN {_positive_finite_number_sql(qty)}
                AND {_positive_finite_number_sql(partial_qty)}
                AND {_positive_finite_number_sql(partial_price)}
                AND {_positive_finite_number_sql(opening_qty)}
                AND {_positive_finite_number_sql(partial_denominator)}
                AND {_finite_number_sql(partial_pnl)}
              THEN
                CASE WHEN {_finite_number_sql(partial_return)}
                  THEN {partial_return}
                  ELSE NULL
                END
              ELSE NULL
            END
          WHEN {_positive_finite_number_sql(non_partial_qty)}
            AND {_positive_finite_number_sql(non_partial_denominator)}
          THEN
            CASE WHEN {_finite_number_sql(non_partial_return)}
              THEN {non_partial_return}
              ELSE NULL
            END
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
