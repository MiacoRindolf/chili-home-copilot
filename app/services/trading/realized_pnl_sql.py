"""SQL snippets for realized P&L return normalization.

Live option P&L is recorded in dollars per contract
(``premium_delta * quantity * 100``). Any learner that divides by
``entry_price * quantity`` must therefore include the contract multiplier,
or option returns are overstated by 100x.
"""
from __future__ import annotations

OPTION_CONTRACT_MULTIPLIER_SQL = "100.0"


def _col(alias: str | None, name: str) -> str:
    return f"{alias}.{name}" if alias else name


def _json_truthy_sql(json_expr: str, key: str) -> str:
    return (
        f"LOWER(COALESCE({json_expr} ->> '{key}', 'false')) "
        "IN ('1', 'true', 'yes', 'on')"
    )


def trade_contract_multiplier_sql(alias: str | None = None) -> str:
    """Return a PostgreSQL expression for a live-row contract multiplier."""
    asset_kind = _col(alias, "asset_kind")
    tags = _col(alias, "tags")
    snap = f"COALESCE({_col(alias, 'indicator_snapshot')}, '{{}}'::jsonb)"
    breakout = f"({snap} -> 'breakout_alert')"
    return f"""
        CASE
          WHEN LOWER(COALESCE({asset_kind}, '')) IN ('option', 'options')
            OR LOWER(COALESCE({tags}, '')) LIKE '%option%'
            OR {snap} ? 'option_meta'
            OR LOWER(COALESCE({snap} ->> 'asset_type', '')) IN ('option', 'options')
            OR {_json_truthy_sql(snap, 'options_path')}
            OR {breakout} ? 'option_meta'
            OR LOWER(COALESCE({breakout} ->> 'asset_type', ''))
               IN ('option', 'options')
            OR {_json_truthy_sql(breakout, 'options_path')}
          THEN {OPTION_CONTRACT_MULTIPLIER_SQL}
          ELSE 1.0
        END
    """


def paper_trade_contract_multiplier_sql(alias: str | None = None) -> str:
    """Return a PostgreSQL expression for a paper trade contract multiplier."""
    signal = f"COALESCE({_col(alias, 'signal_json')}, '{{}}'::jsonb)"
    breakout = f"({signal} -> 'breakout_alert')"
    return f"""
        CASE
          WHEN {signal} ? 'option_meta'
            OR LOWER(COALESCE({signal} ->> 'asset_type', '')) IN ('option', 'options')
            OR {_json_truthy_sql(signal, 'options_path')}
            OR {breakout} ? 'option_meta'
            OR LOWER(COALESCE({breakout} ->> 'asset_type', ''))
               IN ('option', 'options')
            OR {_json_truthy_sql(breakout, 'options_path')}
          THEN {OPTION_CONTRACT_MULTIPLIER_SQL}
          ELSE 1.0
        END
    """


def trade_return_fraction_sql(alias: str | None = None) -> str:
    """Return ``pnl / notional`` for live rows, option-contract aware."""
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
