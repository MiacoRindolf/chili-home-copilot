"""SQL snippets for realized P&L return normalization.

Live option ``Trade.pnl`` is recorded in dollars per contract
(``premium_delta * quantity * 100``). Any learner that divides by
``entry_price * quantity`` must therefore include the contract multiplier,
or option returns are overstated by 100x.
"""
from __future__ import annotations

OPTION_CONTRACT_MULTIPLIER_SQL = "100.0"
OPTION_ASSET_CLASS_ALIASES_SQL = (
    "('option', 'options', 'option_contract', "
    "'robinhood_option', 'robinhood_options')"
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


def _asset_alias_sql(expr: str) -> str:
    return f"LOWER(COALESCE({expr}, '')) IN {OPTION_ASSET_CLASS_ALIASES_SQL}"


def _json_asset_alias_sql(json_expr: str, key: str) -> str:
    return _asset_alias_sql(f"{json_expr} ->> '{key}'")


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
            OR {_json_truthy_sql(snap, 'options_path')}
            OR {_json_numeric_equals_sql(snap, 'option_contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {_json_numeric_equals_sql(snap, 'contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {breakout} ? 'option_meta'
            OR {_json_asset_alias_sql(breakout, 'asset_kind')}
            OR {_json_asset_alias_sql(breakout, 'asset_type')}
            OR {_json_asset_alias_sql(breakout, 'asset_class')}
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
            OR {_json_truthy_sql(signal, 'options_path')}
            OR {_json_numeric_equals_sql(signal, 'option_contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {_json_numeric_equals_sql(signal, 'contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {paper_meta} ? 'option_meta'
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
            OR {_json_truthy_sql(breakout, 'options_path')}
            OR {_json_numeric_equals_sql(breakout, 'option_contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
            OR {_json_numeric_equals_sql(breakout, 'contract_multiplier', OPTION_CONTRACT_MULTIPLIER_SQL)}
          THEN {OPTION_CONTRACT_MULTIPLIER_SQL}
          ELSE 1.0
        END
    """


def trade_return_fraction_sql(alias: str | None = None) -> str:
    """Return ``pnl / notional`` for live trades, option-contract aware."""
    return (
        f"{_col(alias, 'pnl')} / "
        f"({_col(alias, 'entry_price')} * {_col(alias, 'quantity')} * "
        f"({trade_contract_multiplier_sql(alias)}))"
    )


def paper_trade_return_fraction_sql(alias: str | None = None) -> str:
    """Return ``pnl / notional`` for paper trades, option-contract aware."""
    return (
        f"{_col(alias, 'pnl')} / "
        f"({_col(alias, 'entry_price')} * {_col(alias, 'quantity')} * "
        f"({paper_trade_contract_multiplier_sql(alias)}))"
    )
