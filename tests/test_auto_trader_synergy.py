"""Tests for AutoTrader synergy / scale-in planning."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.models.trading import Trade
from app.services.trading.auto_trader_synergy import maybe_scale_in


def test_maybe_scale_in_disabled():
    db = MagicMock()
    settings = MagicMock()
    settings.chili_autotrader_synergy_enabled = False
    assert (
        maybe_scale_in(
            db,
            user_id=1,
            ticker="AAA",
            new_scan_pattern_id=2,
            new_stop=9.0,
            new_target=50.0,
            current_price=10.0,
            settings=settings,
        )
        is None
    )


@patch("app.services.trading.auto_trader_synergy.find_open_autotrader_trade")
def test_maybe_scale_in_computes_weighted_entry(mock_find):
    db = MagicMock()
    t = Trade(
        user_id=1,
        ticker="AAA",
        direction="long",
        entry_price=10.0,
        quantity=30.0,
        status="open",
        stop_loss=9.0,
        take_profit=12.0,
        scan_pattern_id=1,
        auto_trader_version="v1",
        scale_in_count=0,
    )
    mock_find.return_value = t

    settings = MagicMock()
    settings.chili_autotrader_synergy_enabled = True
    settings.chili_autotrader_per_trade_notional_usd = 300.0
    settings.chili_autotrader_synergy_scale_notional_usd = 150.0

    plan = maybe_scale_in(
        db,
        user_id=1,
        ticker="AAA",
        new_scan_pattern_id=2,
        new_stop=9.5,
        new_target=13.0,
        current_price=11.0,
        settings=settings,
    )
    assert plan is not None
    assert plan.new_stop == 9.0
    assert plan.new_target == 13.0
    add_q = 150.0 / 11.0
    expected_avg = (10.0 * 30.0 + 11.0 * add_q) / (30.0 + add_q)
    assert abs(plan.new_avg_entry - expected_avg) < 1e-6
