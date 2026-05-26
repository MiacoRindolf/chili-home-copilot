from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_portfolio_close_option_uses_contract_multiplier_and_preserves_meta() -> None:
    from app.services.trading.portfolio import close_trade

    option_meta = {
        "breakout_alert": {
            "asset_type": "options",
            "option_meta": {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
            },
        }
    }
    trade = SimpleNamespace(
        id=6601,
        user_id=None,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        exit_price=None,
        quantity=2,
        status="open",
        pnl=None,
        exit_reason=None,
        notes="",
        indicator_snapshot=option_meta,
        tca_reference_exit_price=None,
        tca_exit_slippage_bps=None,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = trade

    with patch(
        "app.services.trading.tca_service.resolve_exit_reference_price",
        side_effect=AssertionError("option manual close must not use underlying quote for TCA"),
    ), patch(
        "app.services.trading.portfolio.get_indicator_snapshot",
        side_effect=AssertionError("option manual close must preserve option metadata"),
    ), patch(
        "app.services.trading.brain_work.execution_hooks.on_live_trade_closed",
        return_value=None,
    ):
        out = close_trade(db, trade_id=6601, user_id=None, exit_price=1.45)

    assert out is trade
    assert trade.status == "closed"
    assert trade.pnl == 40.0
    assert trade.indicator_snapshot is option_meta
    assert trade.tca_reference_exit_price is None
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(trade)
