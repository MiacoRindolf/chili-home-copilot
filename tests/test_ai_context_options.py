from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch


def test_open_trade_context_option_uses_contract_premium_domain() -> None:
    from app.services.trading.ai_context import _format_open_trade_context_line

    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        quantity=2,
        entry_price=1.25,
        entry_date=datetime(2026, 5, 26),
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
    )

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": 1.45, "source": "robinhood_options"},
    ):
        line = _format_open_trade_context_line(trade)

    assert "[OPTIONS]" in line
    assert "2 contract(s)" in line
    assert "@ $1.2500 premium" in line
    assert "current premium $1.4500" in line
    assert "P&L +$40.00 (+16.0%)" in line
    assert "contract_multiplier=100" in line
    assert "quote_source=robinhood_options" in line
    assert "SPY 2026-06-19 strike=729.0 call" in line
    assert "shares" not in line.lower()


def test_open_trade_context_stock_preserves_existing_shape() -> None:
    from app.services.trading.ai_context import _format_open_trade_context_line

    trade = SimpleNamespace(
        ticker="AAPL",
        direction="long",
        quantity=3,
        entry_price=195.0,
        entry_date=datetime(2026, 5, 26),
        indicator_snapshot={},
    )

    line = _format_open_trade_context_line(trade)

    assert line == "  - LONG 3x @ $195.0 (entered 2026-05-26)"
