from __future__ import annotations

from app.services.trading.pattern_regime_ledger import (
    BACKTEST_REGIME_DIMENSIONS,
    REGIME_DIMENSIONS,
    _VALID_TRADE_RETURN_SQL,
)


def test_live_regime_dimensions_use_contract_aware_realized_returns() -> None:
    assert REGIME_DIMENSIONS
    for sql in REGIME_DIMENSIONS.values():
        assert "t.pnl AS pnl" in sql
        assert "t.pnl /" in sql
        assert "asset_kind" in sql
        assert "t.pnl IS NOT NULL" in sql
        assert "t.quantity > 0" in sql
        assert _VALID_TRADE_RETURN_SQL in sql
        assert "t.filled_quantity" in sql
        assert "t.partial_taken_qty" in sql
        assert "t.exit_price - t.entry_price" not in sql
        assert "t.entry_price - t.exit_price" not in sql


def test_backtest_regime_dimensions_do_not_default_missing_returns_to_zero() -> None:
    assert BACKTEST_REGIME_DIMENSIONS
    for sql in BACKTEST_REGIME_DIMENSIONS.values():
        assert "pt.outcome_return_pct AS pnl" in sql
        assert "pt.outcome_return_pct AS ret_pct" in sql
        assert "pt.outcome_return_pct IS NOT NULL" in sql
        assert "COALESCE(pt.outcome_return_pct" not in sql
