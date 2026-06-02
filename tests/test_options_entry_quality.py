from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.options.entry_quality import (
    evaluate_long_option_entry,
    resolve_option_entry_thresholds,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        chili_autotrader_options_min_underlying_reward_risk=1.0,
        chili_autotrader_options_min_option_reward_risk=1.0,
        chili_autotrader_options_min_expected_value_pct=0.0,
    )


def test_option_entry_quality_penalizes_bid_ask_spread_before_acceptance() -> None:
    decision = evaluate_long_option_entry(
        None,
        alert=SimpleNamespace(entry_price=3.40, target_price=112.0, stop_loss=99.0),
        option_meta={
            "underlying": "XYZ",
            "strike": 105.0,
            "expiration": "2026-06-19",
            "option_type": "call",
            "limit_price": 3.40,
            "quantity": 1,
            "quote_snapshot": {"bid": 2.80, "ask": 3.40},
        },
        current_underlying_price=100.0,
        confidence=0.9,
        settings=_settings(),
    )

    assert decision.accepted is False
    assert decision.reason == "option_reward_risk_after_cost_below_min"
    assert decision.snapshot["execution_cost_model"] == "entry_spread_penalty_v1"
    assert decision.snapshot["liquidity_cost_per_share"] == pytest.approx(0.60)
    assert decision.snapshot["option_reward_risk"] > 1.0
    assert decision.snapshot["option_reward_risk_after_cost"] < 1.0
    assert decision.snapshot["expected_value_after_cost_pct_of_premium"] < (
        decision.snapshot["expected_value_pct_of_premium"]
    )


def test_option_entry_quality_penalizes_zero_bid_liquidity() -> None:
    decision = evaluate_long_option_entry(
        None,
        alert=SimpleNamespace(entry_price=3.40, target_price=112.0, stop_loss=99.0),
        option_meta={
            "underlying": "XYZ",
            "strike": 105.0,
            "expiration": "2026-06-19",
            "option_type": "call",
            "limit_price": 3.40,
            "quantity": 1,
            "quote_snapshot": {"bid": 0.0, "ask": 3.40},
        },
        current_underlying_price=100.0,
        confidence=0.9,
        settings=_settings(),
    )

    assert decision.accepted is False
    assert decision.reason == "option_reward_risk_after_cost_below_min"
    assert decision.snapshot["entry_bid"] == 0.0
    assert decision.snapshot["entry_ask"] == 3.4
    assert decision.snapshot["liquidity_cost_per_share"] == pytest.approx(3.40)
    assert decision.snapshot["option_reward_risk"] > 1.0
    assert decision.snapshot["option_reward_risk_after_cost"] < 1.0


def test_option_entry_quality_rejects_missing_quote_snapshot_spread() -> None:
    decision = evaluate_long_option_entry(
        None,
        alert=SimpleNamespace(entry_price=3.40, target_price=112.0, stop_loss=99.0),
        option_meta={
            "underlying": "XYZ",
            "strike": 105.0,
            "expiration": "2026-06-19",
            "option_type": "call",
            "limit_price": 3.40,
            "quantity": 1,
        },
        current_underlying_price=100.0,
        confidence=0.9,
        settings=_settings(),
    )

    assert decision.accepted is False
    assert decision.reason == "missing_option_quote_spread"
    assert decision.snapshot["entry_bid"] is None
    assert decision.snapshot["entry_ask"] is None
    assert "execution_cost_model" not in decision.snapshot


def test_option_entry_quality_rejects_mark_only_quote_snapshot_spread() -> None:
    decision = evaluate_long_option_entry(
        None,
        alert=SimpleNamespace(entry_price=3.40, target_price=112.0, stop_loss=99.0),
        option_meta={
            "underlying": "XYZ",
            "strike": 105.0,
            "expiration": "2026-06-19",
            "option_type": "call",
            "limit_price": 3.40,
            "quantity": 1,
            "quote_snapshot": {"mark": 3.20, "last": 3.30},
        },
        current_underlying_price=100.0,
        confidence=0.9,
        settings=_settings(),
    )

    assert decision.accepted is False
    assert decision.reason == "missing_option_quote_spread"
    assert decision.snapshot["entry_bid"] is None
    assert decision.snapshot["entry_ask"] is None


def test_option_entry_quality_rejects_crossed_quote_snapshot() -> None:
    decision = evaluate_long_option_entry(
        None,
        alert=SimpleNamespace(entry_price=3.40, target_price=112.0, stop_loss=99.0),
        option_meta={
            "underlying": "XYZ",
            "strike": 105.0,
            "expiration": "2026-06-19",
            "option_type": "call",
            "limit_price": 3.40,
            "quantity": 1,
            "quote_snapshot": {"bid": 3.45, "ask": 3.40},
        },
        current_underlying_price=100.0,
        confidence=0.9,
        settings=_settings(),
    )

    assert decision.accepted is False
    assert decision.reason == "crossed_option_quote_snapshot"


def test_option_entry_quality_rejects_boolean_price_domains() -> None:
    decision = evaluate_long_option_entry(
        None,
        alert=SimpleNamespace(entry_price=3.40, target_price=True, stop_loss=99.0),
        option_meta={
            "underlying": "XYZ",
            "strike": 105.0,
            "expiration": "2026-06-19",
            "option_type": "call",
            "limit_price": 3.40,
            "quantity": 1,
        },
        current_underlying_price=100.0,
        confidence=0.9,
        settings=_settings(),
    )

    assert decision.accepted is False
    assert decision.reason == "missing_underlying_target"
    assert decision.snapshot["underlying_target"] is None


@pytest.mark.parametrize(
    "confidence",
    [None, True, float("nan"), float("inf"), -0.01, 1.01, 70.0],
)
def test_option_entry_quality_rejects_invalid_confidence(confidence) -> None:
    decision = evaluate_long_option_entry(
        None,
        alert=SimpleNamespace(entry_price=3.40, target_price=112.0, stop_loss=99.0),
        option_meta={
            "underlying": "XYZ",
            "strike": 105.0,
            "expiration": "2026-06-19",
            "option_type": "call",
            "limit_price": 3.40,
            "quantity": 1,
        },
        current_underlying_price=100.0,
        confidence=confidence,
        settings=_settings(),
    )

    assert decision.accepted is False
    assert decision.reason == "invalid_confidence_probability"
    assert "confidence_probability" not in decision.snapshot


def test_option_entry_thresholds_ignore_bad_settings_and_clamp_learned_values(monkeypatch) -> None:
    from app.services.trading.options import entry_quality

    monkeypatch.setattr(entry_quality.strategy_parameter, "register_parameter", lambda *_a, **_k: None)

    learned = {
        "entry_min_underlying_reward_risk": float("nan"),
        "entry_min_option_reward_risk": 999.0,
        "entry_min_expected_value_pct": -999.0,
    }
    monkeypatch.setattr(
        entry_quality.strategy_parameter,
        "get_parameter",
        lambda _db, strategy_family, parameter_key, default=None: learned[parameter_key],
    )

    thresholds = resolve_option_entry_thresholds(
        object(),
        settings=SimpleNamespace(
            chili_autotrader_options_min_underlying_reward_risk=True,
            chili_autotrader_options_min_option_reward_risk=float("inf"),
            chili_autotrader_options_min_expected_value_pct=float("nan"),
        ),
    )

    assert thresholds.min_underlying_reward_risk == 1.0
    assert thresholds.min_option_reward_risk == 10.0
    assert thresholds.min_expected_value_pct == -100.0
