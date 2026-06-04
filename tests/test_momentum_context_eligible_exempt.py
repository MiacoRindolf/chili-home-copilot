"""Unit tests for the trade-eligible exemption on the stock momentum-context gate.

The gate's gap/relative-volume requirement is a momentum-surge proxy. It must NOT
drop the mean-reversion setups of patterns that have already cleared
certification + promotion (they face the expected-edge gate downstream). These
tests pin that exemption and prove it is flag-gated and does not affect
non-eligible rows.
"""
from app.models.trading import BreakoutAlert
from app.services.trading.auto_trader_rules import (
    RuleGateContext,
    RuleGateSettings,
    _stock_momentum_context_gate,
)


def _weak_stock_alert():
    """A stock alert with sub-floor gap/volume (a quiet mean-reversion setup)."""
    return BreakoutAlert(
        ticker="MEANREV",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.7,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.5,
        target_price=12.0,
        user_id=1,
        indicator_snapshot={"flat_indicators": {"gap_pct": 0.16, "vol_ratio": 0.54}},
    )


def _settings(*, exempt_eligible: bool):
    # Gate enabled, queue saturated-threshold at 1.0, surge floors 5%/2x.
    return RuleGateSettings(
        stock_momentum_context_gate_enabled=True,
        stock_momentum_context_exempt_eligible=exempt_eligible,
        stock_momentum_context_min_queue_pressure=1.0,
        stock_momentum_context_min_gap_pct=5.0,
        stock_momentum_context_min_volume_ratio=2.0,
    )


def _full_queue_ctx():
    return RuleGateContext(
        current_price=10.0,
        autotrader_open_count=0,
        realized_loss_today_usd=0.0,
        autotrader_open_count_by_lane={"equity": 0, "crypto": 0, "options": 0},
        candidate_queue_pressure=1.0,  # saturated → gate would otherwise activate
    )


def test_trade_eligible_pattern_is_exempt():
    alert = _weak_stock_alert()
    setattr(alert, "_chili_pattern_trade_eligible", True)
    ok, reason, snap = _stock_momentum_context_gate(
        alert, settings_snapshot=_settings(exempt_eligible=True), ctx=_full_queue_ctx()
    )
    assert ok is True
    assert reason is None
    assert snap["inactive_reason"] == "pattern_trade_eligible_exempt"
    assert snap["active"] is False  # the surge proxy never ran


def test_non_eligible_row_still_gated():
    # No eligibility flag (a shadow / exploration row) → the proxy still applies.
    alert = _weak_stock_alert()
    ok, reason, snap = _stock_momentum_context_gate(
        alert, settings_snapshot=_settings(exempt_eligible=True), ctx=_full_queue_ctx()
    )
    assert ok is False
    assert reason == "stock_momentum_context_below_floor"
    assert snap["active"] is True


def test_exemption_is_flag_gated():
    # Eligible, but the exemption is switched OFF → legacy behavior (still gated).
    alert = _weak_stock_alert()
    setattr(alert, "_chili_pattern_trade_eligible", True)
    ok, reason, snap = _stock_momentum_context_gate(
        alert, settings_snapshot=_settings(exempt_eligible=False), ctx=_full_queue_ctx()
    )
    assert ok is False
    assert reason == "stock_momentum_context_below_floor"
