from __future__ import annotations

import pandas as pd

from app.services.trading.market_data import compute_indicators
from app.services.trading.pattern_condition_monitor import evaluate_pattern_health
from app.services.trading.pattern_position_monitor import _effective_monitor_health_score


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
