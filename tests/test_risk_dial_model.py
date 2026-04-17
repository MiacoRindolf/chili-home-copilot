"""Phase I: pure unit tests for :mod:`app.services.trading.risk_dial_model`.

No database; fast (<1s total).
"""
from __future__ import annotations

import math

import pytest

from app.services.trading.risk_dial_model import (
    RiskDialConfig,
    RiskDialInput,
    compute_dial,
    compute_dial_id,
)


def _default_config(**overrides) -> RiskDialConfig:
    base = dict(
        default_risk_on=1.0,
        default_cautious=0.7,
        default_risk_off=0.3,
        drawdown_floor=0.5,
        drawdown_trigger_pct=10.0,
        ceiling=1.5,
    )
    base.update(overrides)
    return RiskDialConfig(**base)


class TestRegimeDefaults:
    def test_risk_on_uses_configured_default(self):
        cfg = _default_config()
        out = compute_dial(RiskDialInput(regime="risk_on"), config=cfg)
        assert out.dial_value == pytest.approx(1.0)
        assert out.regime == "risk_on"
        assert out.regime_default == pytest.approx(1.0)

    def test_cautious_uses_configured_default(self):
        cfg = _default_config()
        out = compute_dial(RiskDialInput(regime="cautious"), config=cfg)
        assert out.dial_value == pytest.approx(0.7)

    def test_risk_off_uses_configured_default(self):
        cfg = _default_config()
        out = compute_dial(RiskDialInput(regime="risk_off"), config=cfg)
        assert out.dial_value == pytest.approx(0.3)

    def test_unknown_regime_falls_back_to_cautious(self):
        cfg = _default_config()
        out = compute_dial(RiskDialInput(regime="bananas"), config=cfg)
        assert out.regime is None
        assert out.dial_value == pytest.approx(0.7)

    def test_none_regime_falls_back_to_cautious(self):
        cfg = _default_config()
        out = compute_dial(RiskDialInput(regime=None), config=cfg)
        assert out.regime is None
        assert out.dial_value == pytest.approx(0.7)


class TestDrawdownScaler:
    def test_zero_drawdown_no_scale(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(regime="risk_on", drawdown_pct=0.0), config=cfg,
        )
        assert out.drawdown_multiplier == pytest.approx(1.0)
        assert out.dial_value == pytest.approx(1.0)

    def test_full_trigger_drawdown_hits_floor(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(regime="risk_on", drawdown_pct=10.0), config=cfg,
        )
        assert out.drawdown_multiplier == pytest.approx(0.5)
        assert out.dial_value == pytest.approx(0.5)

    def test_beyond_trigger_clamps_to_floor(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(regime="risk_on", drawdown_pct=25.0), config=cfg,
        )
        assert out.drawdown_multiplier == pytest.approx(0.5)

    def test_half_trigger_linear_interpolation(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(regime="risk_on", drawdown_pct=5.0), config=cfg,
        )
        assert out.drawdown_multiplier == pytest.approx(0.75)
        assert out.dial_value == pytest.approx(0.75)

    def test_negative_drawdown_treated_as_zero(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(regime="risk_on", drawdown_pct=-5.0), config=cfg,
        )
        assert out.drawdown_multiplier == pytest.approx(1.0)

    def test_drawdown_applies_to_cautious_and_risk_off(self):
        cfg = _default_config()
        out_c = compute_dial(
            RiskDialInput(regime="cautious", drawdown_pct=10.0), config=cfg,
        )
        out_r = compute_dial(
            RiskDialInput(regime="risk_off", drawdown_pct=10.0), config=cfg,
        )
        assert out_c.dial_value == pytest.approx(0.7 * 0.5)
        assert out_r.dial_value == pytest.approx(0.3 * 0.5)


class TestOverrideBehaviour:
    def test_override_under_ceiling_is_applied(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(
                regime="risk_on",
                user_override_multiplier=1.2,
            ),
            config=cfg,
        )
        assert out.override_multiplier == pytest.approx(1.2)
        assert out.dial_value == pytest.approx(1.2)
        assert out.override_rejected is False
        assert out.capped_at_ceiling is False

    def test_override_above_ceiling_is_rejected(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(
                regime="risk_on",
                user_override_multiplier=2.0,
            ),
            config=cfg,
        )
        assert out.override_rejected is True
        assert out.override_multiplier == pytest.approx(1.0)
        assert out.dial_value == pytest.approx(1.0)

    def test_negative_override_is_rejected(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(
                regime="cautious",
                user_override_multiplier=-0.5,
            ),
            config=cfg,
        )
        assert out.override_rejected is True
        assert out.override_multiplier == pytest.approx(1.0)
        assert out.dial_value == pytest.approx(0.7)

    def test_override_at_exactly_ceiling_is_allowed(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(
                regime="risk_on",
                user_override_multiplier=1.5,
            ),
            config=cfg,
        )
        assert out.override_rejected is False
        assert out.dial_value == pytest.approx(1.5)


class TestClamping:
    def test_dial_never_exceeds_ceiling(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(regime="risk_on", user_override_multiplier=1.0),
            config=cfg,
        )
        assert 0.0 <= out.dial_value <= cfg.ceiling

    def test_dial_never_goes_below_zero(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(regime="risk_off", drawdown_pct=100.0),
            config=cfg,
        )
        assert out.dial_value >= 0.0


class TestDeterminism:
    def test_same_input_produces_same_output(self):
        cfg = _default_config()
        inp = RiskDialInput(
            regime="cautious",
            drawdown_pct=5.0,
            user_override_multiplier=1.1,
            user_id=42,
        )
        out_a = compute_dial(inp, config=cfg)
        out_b = compute_dial(inp, config=cfg)
        assert out_a.dial_value == out_b.dial_value
        assert out_a.reasoning == out_b.reasoning

    def test_dial_id_is_deterministic(self):
        cfg = _default_config()
        a = compute_dial_id(user_id=42, regime="cautious", config=cfg)
        b = compute_dial_id(user_id=42, regime="cautious", config=cfg)
        assert a == b
        assert len(a) == 32

    def test_dial_id_differs_on_regime(self):
        cfg = _default_config()
        a = compute_dial_id(user_id=42, regime="cautious", config=cfg)
        b = compute_dial_id(user_id=42, regime="risk_on", config=cfg)
        assert a != b

    def test_dial_id_global_vs_user(self):
        cfg = _default_config()
        a = compute_dial_id(user_id=None, regime="cautious", config=cfg)
        b = compute_dial_id(user_id=1, regime="cautious", config=cfg)
        assert a != b


class TestReasoningPayload:
    def test_reasoning_carries_all_components(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(
                regime="risk_on",
                drawdown_pct=5.0,
                user_override_multiplier=1.1,
            ),
            config=cfg,
        )
        for k in (
            "regime",
            "regime_default",
            "drawdown_pct",
            "drawdown_multiplier",
            "override_multiplier",
            "override_rejected",
            "ceiling",
            "capped_at_ceiling",
            "raw_unclamped",
        ):
            assert k in out.reasoning, f"missing {k}"

    def test_reasoning_numbers_are_real(self):
        cfg = _default_config()
        out = compute_dial(
            RiskDialInput(regime="cautious", drawdown_pct=3.0),
            config=cfg,
        )
        for k, v in out.reasoning.items():
            if isinstance(v, float):
                assert not math.isnan(v)
                assert not math.isinf(v)
