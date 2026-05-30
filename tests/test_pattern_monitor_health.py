from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from app.services.trading.market_data import compute_indicators
from app.services.trading.pattern_condition_monitor import evaluate_pattern_health
from app.services.trading.pattern_position_monitor import (
    _effective_monitor_health_score,
    _trade_pnl_pct,
)


def test_pattern_health_omits_missing_inputs_from_ratio() -> None:
    rules = {
        "conditions": [
            {"indicator": "volume_ratio", "op": "<", "value": 0.8},
            {"indicator": "gap_pct", "op": ">", "value": 2.0},
        ],
    }

    health = evaluate_pattern_health(rules, {"volume_ratio": 0.5})
    snap = health.to_dict()

    assert health.health_score == 1.0
    assert snap["conditions_total"] == 2
    assert snap["conditions_evaluable"] == 1
    assert snap["conditions_met"] == 1
    assert snap["missing_indicators"] == ["gap_pct"]


def test_effective_health_uses_plan_vitals_when_static_inputs_absent() -> None:
    rules = {
        "conditions": [
            {"indicator": "volume_ratio", "op": "<", "value": 0.8},
            {"indicator": "gap_pct", "op": ">", "value": 2.0},
        ],
    }
    condition_health = evaluate_pattern_health(rules, {})

    class PlanHealth:
        plan_health_score = 1.0

    class Vitals:
        composite_health = 0.62

    score, source = _effective_monitor_health_score(
        condition_health=condition_health,
        plan_health=PlanHealth(),
        vitals=Vitals(),
    )

    assert score == 0.62
    assert source == "live_plan_vitals_min"


def test_effective_health_prefers_live_thesis_over_entry_filter_retention() -> None:
    rules = {
        "conditions": [
            {"indicator": "gap_pct", "op": ">", "value": 2.0},
        ],
    }
    condition_health = evaluate_pattern_health(rules, {"gap_pct": 0.1})
    assert condition_health.health_score == 0.0

    class PlanHealth:
        plan_health_score = 1.0

    class Vitals:
        composite_health = 0.74

    score, source = _effective_monitor_health_score(
        condition_health=condition_health,
        plan_health=PlanHealth(),
        vitals=Vitals(),
    )

    assert score == 0.74
    assert source == "live_plan_vitals_min"


def test_option_pattern_monitor_pnl_uses_premium_not_underlying() -> None:
    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
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
        pnl_pct, source = _trade_pnl_pct(trade, current_price=729.0)

    assert pnl_pct == pytest.approx(16.0)
    assert source == "robinhood_options"


def test_option_pattern_monitor_pnl_does_not_fallback_to_underlying() -> None:
    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
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
        return_value={"price": None, "source": "robinhood_options_unavailable"},
    ):
        pnl_pct, source = _trade_pnl_pct(trade, current_price=729.0)

    assert pnl_pct is None
    assert source == "option_premium_unavailable"


@pytest.mark.parametrize("bad_price", [True, float("nan"), float("inf"), 0, -1, "bad"])
def test_option_pattern_monitor_pnl_rejects_bad_premium_quote(bad_price) -> None:
    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        indicator_snapshot={"asset_kind": "option"},
    )

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": bad_price, "source": "robinhood_options"},
    ):
        pnl_pct, source = _trade_pnl_pct(trade, current_price=729.0)

    assert pnl_pct is None
    assert source == "option_premium_unavailable"


@pytest.mark.parametrize("bad_entry", [True, float("nan"), float("inf"), 0, -1, "bad"])
def test_option_pattern_monitor_pnl_rejects_bad_entry_price(bad_entry) -> None:
    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        entry_price=bad_entry,
        indicator_snapshot={"asset_kind": "option"},
    )

    with patch(
        "app.services.trading.broker_quotes.broker_quote_for_trade",
        return_value={"price": 1.45, "source": "robinhood_options"},
    ):
        pnl_pct, source = _trade_pnl_pct(trade, current_price=729.0)

    assert pnl_pct is None
    assert source == "entry_unavailable"


@pytest.mark.parametrize("bad_price", [True, float("nan"), float("inf"), 0, -1, "bad"])
def test_stock_pattern_monitor_pnl_rejects_bad_current_price(bad_price) -> None:
    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        entry_price=100.0,
        indicator_snapshot={},
    )

    pnl_pct, source = _trade_pnl_pct(trade, current_price=bad_price)

    assert pnl_pct is None
    assert source == "price_unavailable"


def test_compute_indicators_exposes_pattern_canonical_inputs() -> None:
    dates = pd.date_range("2026-01-01", periods=30, freq="D")
    df = pd.DataFrame(
        {
            "Open": [100 + i for i in range(30)],
            "High": [101 + i for i in range(30)],
            "Low": [99 + i for i in range(30)],
            "Close": [100.5 + i for i in range(30)],
            "Volume": [1000 + i * 10 for i in range(30)],
        },
        index=dates,
    )

    out = compute_indicators(
        "TEST",
        indicators=["volume_ratio", "gap_pct"],
        preloaded_df=df,
    )

    assert out["volume_ratio"][-1]["value"] > 0
    assert out["gap_pct"][-1]["value"] is not None
