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


def test_portfolio_summary_option_uses_premium_mark_and_contract_multiplier() -> None:
    from app.services.trading.portfolio import get_portfolio_summary

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
        id=6602,
        user_id=1,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2,
        status="open",
        indicator_snapshot=option_meta,
        broker_source="robinhood",
        exit_date=None,
        pnl=None,
    )

    open_q = MagicMock()
    open_q.filter.return_value.all.return_value = [trade]
    closed_q = MagicMock()
    closed_q.filter.return_value.order_by.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [open_q, closed_q]

    with patch(
        "app.services.trading.portfolio.fetch_quote",
        side_effect=AssertionError("option summary must not use underlying quote"),
    ), patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": 1.45, "source": "robinhood_options"},
    ), patch(
        "app.services.trading.portfolio.get_trade_stats",
        return_value={
            "total_trades": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "best_trade": 0,
            "worst_trade": 0,
            "max_drawdown": 0,
        },
    ):
        summary = get_portfolio_summary(db, user_id=1)

    row = summary["positions"][0]
    assert row["asset_type"] == "options"
    assert row["contract_multiplier"] == 100.0
    assert row["current_price"] == 1.45
    assert row["unrealized_pnl"] == 40.0
    assert row["unrealized_pct"] == 16.0
    assert summary["total_invested"] == 250.0
    assert summary["total_current"] == 290.0
    assert summary["unrealized_pnl"] == 40.0


def test_portfolio_summary_short_option_rollup_uses_signed_pnl() -> None:
    from app.services.trading.portfolio import get_portfolio_summary

    trade = SimpleNamespace(
        id=6603,
        user_id=1,
        ticker="SPY",
        direction="short",
        entry_price=1.45,
        quantity=2,
        status="open",
        indicator_snapshot={
            "breakout_alert": {
                "asset_type": "options",
                "option_meta": {
                    "underlying": "SPY",
                    "expiration": "2026-06-19",
                    "strike": 729.0,
                    "option_type": "call",
                },
            }
        },
        broker_source="robinhood",
        exit_date=None,
        pnl=None,
    )

    open_q = MagicMock()
    open_q.filter.return_value.all.return_value = [trade]
    closed_q = MagicMock()
    closed_q.filter.return_value.order_by.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [open_q, closed_q]

    with patch(
        "app.services.trading.portfolio.fetch_quote",
        side_effect=AssertionError("option summary must not use underlying quote"),
    ), patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": 1.25, "source": "robinhood_options"},
    ), patch(
        "app.services.trading.portfolio.get_trade_stats",
        return_value={},
    ):
        summary = get_portfolio_summary(db, user_id=1)

    row = summary["positions"][0]
    assert row["asset_type"] == "options"
    assert row["unrealized_pnl"] == 40.0
    assert row["unrealized_pct"] == 13.79
    assert summary["total_invested"] == 290.0
    assert summary["total_current"] == 250.0
    assert summary["unrealized_pnl"] == 40.0
    assert summary["total_pnl"] == 40.0
