from __future__ import annotations

from app.services.trading.pattern_regime_ledger import REGIME_DIMENSIONS


def test_live_regime_dimensions_use_contract_aware_realized_returns() -> None:
    assert REGIME_DIMENSIONS
    for sql in REGIME_DIMENSIONS.values():
        assert "t.pnl AS pnl" in sql
        assert "t.pnl /" in sql
        assert "asset_kind" in sql
        assert "t.pnl IS NOT NULL" in sql
        assert "t.quantity > 0" in sql
        assert "t.exit_price - t.entry_price" not in sql
        assert "t.entry_price - t.exit_price" not in sql
