"""Unit tests for AutoTrader v1 rule gate helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.models.trading import BreakoutAlert
from app.services.trading.auto_trader_rules import (
    RuleGateContext,
    alert_confidence_from_score,
    projected_profit_pct,
    passes_rule_gate,
)


def test_projected_profit_pct():
    assert projected_profit_pct(100.0, 112.0) == 12.0
    assert projected_profit_pct(None, 112.0) is None
    assert projected_profit_pct(0, 112.0) is None


def test_alert_confidence_from_score():
    a = BreakoutAlert(
        ticker="X",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.4,
        price_at_alert=10.0,
    )
    assert abs(alert_confidence_from_score(a) - min(0.95, 0.55 + 0.2)) < 1e-6


def test_passes_rule_gate_confidence_fail():
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="AAA",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.0,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=11.5,
        user_id=1,
    )
    ctx = RuleGateContext(current_price=10.0, autotrader_open_count=0, realized_loss_today_usd=0.0)
    settings = MagicMock()
    settings.chili_autotrader_rth_only = False
    settings.chili_autotrader_confidence_floor = 0.99
    settings.chili_autotrader_min_projected_profit_pct = 5.0
    settings.chili_autotrader_max_symbol_price_usd = 500.0
    settings.chili_autotrader_max_entry_slippage_pct = 5.0
    settings.chili_autotrader_daily_loss_cap_usd = 500.0
    settings.chili_autotrader_max_concurrent = 5

    ok, reason, _ = passes_rule_gate(db, alert, settings=settings, ctx=ctx, for_new_entry=True)
    assert not ok
    assert reason == "confidence_below_floor"


@patch("app.services.trading.pattern_imminent_alerts.us_stock_session_open", return_value=True)
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_slippage_fail(_mock_port, _mock_rth):
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="AAA",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.5,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        user_id=1,
    )
    ctx = RuleGateContext(current_price=12.0, autotrader_open_count=0, realized_loss_today_usd=0.0)
    settings = MagicMock()
    settings.chili_autotrader_rth_only = True
    settings.chili_autotrader_confidence_floor = 0.5
    settings.chili_autotrader_min_projected_profit_pct = 5.0
    settings.chili_autotrader_max_symbol_price_usd = 500.0
    settings.chili_autotrader_max_entry_slippage_pct = 1.0
    settings.chili_autotrader_daily_loss_cap_usd = 500.0
    settings.chili_autotrader_max_concurrent = 5
    settings.chili_autotrader_assumed_capital_usd = 100_000.0

    ok, reason, _ = passes_rule_gate(db, alert, settings=settings, ctx=ctx, for_new_entry=True)
    assert not ok
    assert reason == "missed_entry_slippage"


@patch("app.services.trading.pattern_imminent_alerts.us_stock_session_open", return_value=True)
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_projected_profit_fail(_mock_port, _mock_rth):
    db = MagicMock()
    alert = BreakoutAlert(
        ticker="AAA",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.5,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=10.5,
        user_id=1,
    )
    ctx = RuleGateContext(current_price=10.0, autotrader_open_count=0, realized_loss_today_usd=0.0)
    settings = MagicMock()
    settings.chili_autotrader_rth_only = False
    settings.chili_autotrader_confidence_floor = 0.5
    settings.chili_autotrader_min_projected_profit_pct = 12.0
    settings.chili_autotrader_max_symbol_price_usd = 500.0
    settings.chili_autotrader_max_entry_slippage_pct = 5.0
    settings.chili_autotrader_daily_loss_cap_usd = 500.0
    settings.chili_autotrader_max_concurrent = 5
    settings.chili_autotrader_assumed_capital_usd = 100_000.0

    ok, reason, _ = passes_rule_gate(db, alert, settings=settings, ctx=ctx, for_new_entry=True)
    assert not ok
    assert reason == "projected_profit_below_min"
