"""Integration-style test for AutoTrader v1 paper path (DB + orchestrator)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app import models
from app.models.trading import AutoTraderRun, BreakoutAlert, PaperTrade
from app.services.trading import auto_trader as at_mod


def test_run_auto_trader_tick_paper_creates_paper_trade(db, monkeypatch):
    u = models.User(name="at_int_u")
    db.add(u)
    db.flush()

    alert = BreakoutAlert(
        ticker="ATST",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.5,
        price_at_alert=40.0,
        entry_price=40.0,
        stop_loss=38.0,
        target_price=45.0,
        user_id=u.id,
        scan_pattern_id=None,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    cfg = SimpleNamespace(
        chili_autotrader_enabled=True,
        chili_autotrader_live_enabled=False,
        chili_autotrader_user_id=u.id,
        brain_default_user_id=u.id,
        chili_autotrader_llm_revalidation_enabled=False,
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=12.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_max_concurrent=5,
        chili_autotrader_per_trade_notional_usd=300.0,
        chili_autotrader_synergy_enabled=False,
        chili_autotrader_synergy_scale_notional_usd=150.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
    )
    monkeypatch.setattr(at_mod, "settings", cfg)
    monkeypatch.setattr(
        at_mod,
        "effective_autotrader_runtime",
        lambda _db: {
            "tick_allowed": True,
            "paused": False,
            "live_orders_effective": False,
            "live_orders_env": False,
            "desk_live_override": False,
            "monitor_entries_allowed": True,
            "payload": {},
        },
    )

    with patch.object(at_mod, "_current_price", return_value=40.0):
        with patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            return_value=(True, "ok"),
        ):
            out = at_mod.run_auto_trader_tick(db)

    assert out.get("ok") is True
    assert out.get("placed", 0) >= 1
    run = db.query(AutoTraderRun).filter(AutoTraderRun.breakout_alert_id == alert.id).first()
    assert run is not None
    assert run.decision == "placed"
    pt = (
        db.query(PaperTrade)
        .filter(PaperTrade.user_id == u.id, PaperTrade.ticker == "ATST")
        .first()
    )
    assert pt is not None
    sj = pt.signal_json or {}
    assert sj.get("auto_trader_v1") is True


def test_run_auto_trader_tick_matches_null_user_alerts(db, monkeypatch):
    """Production imminent alerts are written with user_id=NULL (system-scope).
    The entry tick must still process them when an autotrader user is configured."""
    u = models.User(name="at_int_null_u")
    db.add(u)
    db.flush()

    alert = BreakoutAlert(
        ticker="NULLU",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.6,
        price_at_alert=50.0,
        entry_price=50.0,
        stop_loss=47.0,
        target_price=60.0,
        user_id=None,  # system-scope, matches production
        scan_pattern_id=None,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    cfg = SimpleNamespace(
        chili_autotrader_enabled=True,
        chili_autotrader_live_enabled=False,
        chili_autotrader_user_id=u.id,
        brain_default_user_id=u.id,
        chili_autotrader_llm_revalidation_enabled=False,
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=12.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_max_concurrent=5,
        chili_autotrader_per_trade_notional_usd=300.0,
        chili_autotrader_synergy_enabled=False,
        chili_autotrader_synergy_scale_notional_usd=150.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
    )
    monkeypatch.setattr(at_mod, "settings", cfg)
    monkeypatch.setattr(
        at_mod,
        "effective_autotrader_runtime",
        lambda _db: {
            "tick_allowed": True,
            "paused": False,
            "live_orders_effective": False,
            "live_orders_env": False,
            "desk_live_override": False,
            "monitor_entries_allowed": True,
            "payload": {},
        },
    )

    with patch.object(at_mod, "_current_price", return_value=50.0):
        with patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            return_value=(True, "ok"),
        ):
            out = at_mod.run_auto_trader_tick(db)

    assert out.get("ok") is True
    # The null-user alert must have been picked up (either placed or audited).
    run = db.query(AutoTraderRun).filter(AutoTraderRun.breakout_alert_id == alert.id).first()
    assert run is not None, "null-user alert was not processed"
