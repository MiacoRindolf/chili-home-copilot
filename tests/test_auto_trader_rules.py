"""Unit tests for AutoTrader v1 rule gate helpers."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.trading import BreakoutAlert
from app.services.trading.auto_trader_rules import (
    RuleGateContext,
    alert_confidence_from_score,
    projected_profit_pct,
    passes_rule_gate,
)


@pytest.fixture(autouse=True)
def _strategy_parameters_use_test_defaults(monkeypatch):
    """Keep these unit tests pure; adaptive parameters have DB coverage elsewhere."""
    from app.services.trading import strategy_parameter

    strategy_parameter.invalidate_cache()

    def _default_parameter(*_args, default=None, **_kwargs):
        return default

    monkeypatch.setattr(strategy_parameter, "get_parameter", _default_parameter)
    monkeypatch.setattr(strategy_parameter, "register_parameter", lambda *_args, **_kwargs: -1)
    yield
    strategy_parameter.invalidate_cache()


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
    # Required when chili_autotrader_rth_only is True: without this the gate
    # consults ``us_stock_extended_session_open`` (which is not patched here)
    # and short-circuits before the slippage check. MagicMock attrs default
    # to truthy MagicMock instances, so explicitly set to False.
    settings.chili_autotrader_allow_extended_hours = False
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


# VV — per-lane concurrency cap tests. The rule gate should bucket the
# alert into one of {equity, crypto, options} and consult the matching
# per-lane cap from RuleGateSettings (or strategy_parameter when
# available). Each lane's cap is independent: the equity lane filling
# up must NOT block crypto or options entries.

@patch("app.services.trading.pattern_imminent_alerts.us_stock_session_open", return_value=True)
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_per_lane_equity_blocks_only_equity(_mock_port, _mock_rth):
    """Equity lane at cap → equity alert blocked, crypto alert still passes."""
    db = MagicMock()
    settings = MagicMock()
    settings.chili_autotrader_rth_only = False
    settings.chili_autotrader_confidence_floor = 0.5
    settings.chili_autotrader_min_projected_profit_pct = 5.0
    settings.chili_autotrader_max_symbol_price_usd = 500.0
    settings.chili_autotrader_max_entry_slippage_pct = 5.0
    settings.chili_autotrader_daily_loss_cap_usd = 500.0
    settings.chili_autotrader_max_concurrent = 60  # global outer ceiling
    settings.chili_autotrader_max_concurrent_equity = 2
    settings.chili_autotrader_max_concurrent_crypto = 2
    settings.chili_autotrader_max_concurrent_options = 2
    settings.chili_autotrader_crypto_enabled = True
    settings.chili_autotrader_options_enabled = True
    settings.chili_autotrader_assumed_capital_usd = 100_000.0

    # Equity lane at its cap (2/2). Crypto lane has 0/2 open.
    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=2,  # global counter
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 2, "crypto": 0, "options": 0},
    )

    eq_alert = BreakoutAlert(
        ticker="EQX", asset_type="stock", alert_tier="pattern_imminent",
        score_at_alert=0.7, price_at_alert=10.0, entry_price=10.0,
        stop_loss=9.5, target_price=11.5, user_id=1,
    )
    ok, reason, snap = passes_rule_gate(
        db, eq_alert, settings=settings, ctx=ctx, for_new_entry=True
    )
    assert not ok
    assert reason == "max_concurrent_equity"
    assert snap.get("concurrency_lane") == "equity"

    # Same context — but a crypto alert. Should NOT be blocked by the
    # equity lane filling up.
    cr_alert = BreakoutAlert(
        ticker="BTC-USD", asset_type="crypto", alert_tier="pattern_imminent",
        score_at_alert=0.7, price_at_alert=50000.0, entry_price=50000.0,
        stop_loss=48000.0, target_price=55000.0, user_id=1,
    )
    crypto_ctx = RuleGateContext(
        current_price=50000.0,
        autotrader_open_count=2,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 2, "crypto": 0, "options": 0},
    )
    ok, reason, snap = passes_rule_gate(
        db, cr_alert, settings=settings, ctx=crypto_ctx, for_new_entry=True
    )
    # Crypto path bypasses RTH/price-cap; concurrency lane is 'crypto'
    # with 0/2 open → should pass the lane gate (may still pass or fail
    # downstream checks, but the reason MUST NOT be max_concurrent_*).
    assert snap.get("concurrency_lane") == "crypto"
    assert not (reason or "").startswith("max_concurrent")


@patch("app.services.trading.auto_trader_rules.resolve_effective_capital", return_value=(100_000.0, "test"))
@patch("app.services.trading.auto_trader_rules.resolve_brain_risk_context", return_value={"dial_value": 1.0})
@patch("app.services.trading.auto_trader_rules.resolve_effective_slippage_pct", return_value=(2.0, "test"))
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_options_skips_underlying_stop_target_validation(
    _mock_port,
    _mock_slippage,
    _mock_brain,
    _mock_capital,
):
    """Options substitutions carry underlying stop/target levels, while
    entry_price is the option premium.
    """
    from app.services.trading.strategy_parameter import invalidate_cache

    invalidate_cache()
    db = None
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=5.0,
        chili_autotrader_max_symbol_price_usd=50.0,
        chili_autotrader_max_entry_slippage_pct=2.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=2,
        chili_autotrader_max_concurrent_crypto=2,
        chili_autotrader_max_concurrent_options=2,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=True,
        chili_autotrader_options_min_underlying_reward_risk=1.0,
        chili_autotrader_options_min_option_reward_risk=1.0,
        chili_autotrader_options_min_expected_value_pct=0.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
    )

    ctx = RuleGateContext(
        current_price=113.25,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )
    alert = BreakoutAlert(
        ticker="A",
        asset_type="options",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=113.25,
        entry_price=5.40,
        stop_loss=110.0,
        target_price=130.0,
        user_id=1,
        indicator_snapshot={
            "option_meta": {
                "strike": 115.0,
                "expiration": "2026-06-18",
                "option_type": "call",
                "limit_price": 5.40,
                "quantity": 1,
            }
        },
    )

    ok, reason, snap = passes_rule_gate(
        db, alert, settings=settings, ctx=ctx, for_new_entry=True
    )

    assert ok
    assert reason == "ok"
    assert snap.get("options_path") is True
    assert snap.get("projected_profit_pct") is None
    assert snap.get("projected_profit_pct_source") == "options_entry_quality"
    assert snap["option_entry_quality"]["option_reward_risk"] > 1.0
    assert snap.get("stop_target_validation_skipped_reason") == "options_underlying_levels"


@patch("app.services.trading.auto_trader_rules.resolve_effective_capital", return_value=(100_000.0, "test"))
@patch("app.services.trading.auto_trader_rules.resolve_brain_risk_context", return_value={"dial_value": 1.0})
@patch("app.services.trading.auto_trader_rules.resolve_effective_slippage_pct", return_value=(2.0, "test"))
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_options_blocks_when_target_cannot_pay_premium(
    _mock_port,
    _mock_slippage,
    _mock_brain,
    _mock_capital,
):
    db = None
    settings = SimpleNamespace(
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=5.0,
        chili_autotrader_max_symbol_price_usd=50.0,
        chili_autotrader_max_entry_slippage_pct=2.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_daily_loss_cap_pct=0.0,
        chili_autotrader_max_concurrent=60,
        chili_autotrader_max_concurrent_equity=2,
        chili_autotrader_max_concurrent_crypto=2,
        chili_autotrader_max_concurrent_options=2,
        chili_autotrader_crypto_enabled=False,
        chili_autotrader_options_enabled=True,
        chili_autotrader_options_min_underlying_reward_risk=1.0,
        chili_autotrader_options_min_option_reward_risk=1.0,
        chili_autotrader_options_min_expected_value_pct=0.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
        chili_autotrader_broker_equity_cache_enabled=False,
    )
    ctx = RuleGateContext(
        current_price=113.25,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
    )
    alert = BreakoutAlert(
        ticker="A",
        asset_type="options",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=113.25,
        entry_price=5.40,
        stop_loss=110.0,
        target_price=119.0,
        user_id=1,
        indicator_snapshot={
            "option_meta": {
                "strike": 115.0,
                "expiration": "2026-06-18",
                "option_type": "call",
                "limit_price": 5.40,
                "quantity": 1,
            }
        },
    )

    ok, reason, snap = passes_rule_gate(
        db, alert, settings=settings, ctx=ctx, for_new_entry=True
    )

    assert not ok
    assert reason == "options_entry_quality:option_target_profit_non_positive"
    assert snap.get("projected_profit_pct") is None
    assert snap["option_entry_quality"]["option_profit_at_target"] < 0.0


@patch("app.services.trading.pattern_imminent_alerts.us_stock_session_open", return_value=True)
@patch("app.services.trading.portfolio_risk.check_new_trade_allowed", return_value=(True, "ok"))
def test_passes_rule_gate_global_outer_ceiling_still_enforced(_mock_port, _mock_rth):
    """Even if the equity lane has headroom, the global outer ceiling on
    the SUM of all open trades still fires. Belt-and-braces."""
    db = MagicMock()
    settings = MagicMock()
    settings.chili_autotrader_rth_only = False
    settings.chili_autotrader_confidence_floor = 0.5
    settings.chili_autotrader_min_projected_profit_pct = 5.0
    settings.chili_autotrader_max_symbol_price_usd = 500.0
    settings.chili_autotrader_max_entry_slippage_pct = 5.0
    settings.chili_autotrader_daily_loss_cap_usd = 500.0
    settings.chili_autotrader_max_concurrent = 5  # global ceiling
    settings.chili_autotrader_max_concurrent_equity = 100
    settings.chili_autotrader_max_concurrent_crypto = 100
    settings.chili_autotrader_max_concurrent_options = 100
    settings.chili_autotrader_assumed_capital_usd = 100_000.0

    ctx = RuleGateContext(
        current_price=10.0,
        autotrader_open_count=5,  # at global ceiling
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 5, "crypto": 0, "options": 0},
    )

    alert = BreakoutAlert(
        ticker="EQY", asset_type="stock", alert_tier="pattern_imminent",
        score_at_alert=0.7, price_at_alert=10.0, entry_price=10.0,
        stop_loss=9.5, target_price=11.5, user_id=1,
    )
    ok, reason, _ = passes_rule_gate(
        db, alert, settings=settings, ctx=ctx, for_new_entry=True
    )
    assert not ok
    # Equity lane is at 5/100 (headroom), but global is at 5/5 (full).
    # The equity lane check fires first only when lane_open >= lane_cap,
    # which is NOT the case here (5 < 100). The global ceiling fires
    # next.
    assert reason == "max_concurrent_global"
