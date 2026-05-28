from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.options.entry_quality import evaluate_long_option_entry


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
