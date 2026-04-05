"""Market regime detection and conditioning for pattern DSL.

Provides regime indicators that can be referenced in ScanPattern conditions:
  - regime_spy_direction: "bullish" | "bearish" | "flat"
  - regime_vix: "low" | "normal" | "elevated" | "extreme"
  - regime_composite: "risk_on" | "cautious" | "risk_off"
  - regime_numeric: 1 (risk_on) | 0 (cautious) | -1 (risk_off)
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_regime_indicators() -> dict[str, Any]:
    """Return current regime as a flat indicator dict for pattern evaluation."""
    from .market_data import get_market_regime

    regime = get_market_regime()
    return {
        "regime_spy_direction": regime.get("spy_direction", "flat"),
        "regime_spy_momentum_5d": regime.get("spy_momentum_5d", 0.0),
        "regime_vix": regime.get("vix_regime", "unknown"),
        "regime_vix_value": regime.get("vix"),
        "regime_composite": regime.get("regime", "cautious"),
        "regime_numeric": regime.get("regime_numeric", 0),
    }


def inject_regime_into_indicators(indicators: dict[str, Any]) -> dict[str, Any]:
    """Merge regime indicators into an existing indicator dict for pattern evaluation."""
    try:
        regime = get_regime_indicators()
        indicators.update(regime)
    except Exception as e:
        logger.debug("[regime] Failed to inject regime: %s", e)
    return indicators


def regime_condition_matches(condition: dict[str, Any], indicators: dict[str, Any]) -> bool | None:
    """Evaluate a regime-based DSL condition.

    Returns True/False if the condition is a regime condition, None if not.
    Supports conditions like:
      {"indicator": "regime_composite", "op": "==", "value": "risk_on"}
      {"indicator": "regime_vix", "op": "!=", "value": "extreme"}
      {"indicator": "regime_numeric", "op": ">=", "value": 0}
    """
    ind = condition.get("indicator", "")
    if not ind.startswith("regime_"):
        return None

    actual = indicators.get(ind)
    if actual is None:
        return None

    op = condition.get("op", "==")
    value = condition.get("value")

    if isinstance(actual, (int, float)) and isinstance(value, (int, float)):
        if op == ">":
            return actual > value
        elif op == ">=":
            return actual >= value
        elif op == "<":
            return actual < value
        elif op == "<=":
            return actual <= value
        elif op == "==":
            return actual == value
        elif op == "!=":
            return actual != value
    else:
        actual_str = str(actual).strip().lower()
        value_str = str(value).strip().lower()
        if op == "==":
            return actual_str == value_str
        elif op == "!=":
            return actual_str != value_str
        elif op == "in":
            return actual_str in [str(v).strip().lower() for v in (value if isinstance(value, list) else [value])]

    return None


REGIME_CONDITION_TEMPLATES = [
    {"indicator": "regime_composite", "op": "==", "value": "risk_on"},
    {"indicator": "regime_composite", "op": "!=", "value": "risk_off"},
    {"indicator": "regime_vix", "op": "!=", "value": "extreme"},
    {"indicator": "regime_numeric", "op": ">=", "value": 0},
]
