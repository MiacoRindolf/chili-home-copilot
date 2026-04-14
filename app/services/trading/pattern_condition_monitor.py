"""Pattern condition health evaluator.

Evaluates a ScanPattern's rules_json conditions against live indicator
values and returns a structured health assessment.  Pure logic — no LLM,
no DB writes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Human-readable descriptions for common indicator keys.
_INDICATOR_DESCRIPTIONS: dict[str, str] = {
    "dist_to_resistance_pct": "price distance to resistance (how close to the ceiling)",
    "price": "current price",
    "macd_hist": "MACD histogram (momentum direction)",
    "macd": "MACD line",
    "macd_signal": "MACD signal line",
    "adx": "ADX trend strength",
    "vcp_count": "volatility contraction count (coiling)",
    "rsi_14": "RSI (momentum oscillator)",
    "rsi": "RSI (momentum oscillator)",
    "volume_ratio": "relative volume vs average",
    "bb_pct": "Bollinger Band %B position",
    "stochastic_k": "Stochastic %K",
    "ema_20": "20-period EMA",
    "ema_50": "50-period EMA (intermediate trend)",
    "ema_100": "100-period EMA",
    "ema_200": "200-period EMA (long-term trend)",
    "sma_20": "20-period SMA",
    "sma_50": "50-period SMA",
    "atr": "Average True Range (volatility)",
    "obv": "On-Balance Volume (accumulation/distribution)",
    "mfi": "Money Flow Index",
    "regime_composite": "market regime classification",
}


@dataclass
class ConditionResult:
    indicator: str
    op: str
    threshold: Any
    actual_value: Any
    met: bool
    human_desc: str = ""


@dataclass
class ConditionHealth:
    conditions: list[ConditionResult] = field(default_factory=list)
    health_score: float = 0.0
    health_delta: float | None = None
    critical_failures: list[ConditionResult] = field(default_factory=list)
    human_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "health_score": round(self.health_score, 3),
            "health_delta": round(self.health_delta, 3) if self.health_delta is not None else None,
            "conditions_met": sum(1 for c in self.conditions if c.met),
            "conditions_total": len(self.conditions),
            "critical_failures": [
                {"indicator": c.indicator, "desc": c.human_desc} for c in self.critical_failures
            ],
            "conditions": [
                {
                    "indicator": c.indicator,
                    "op": c.op,
                    "threshold": c.threshold,
                    "actual": c.actual_value,
                    "met": c.met,
                    "desc": c.human_desc,
                }
                for c in self.conditions
            ],
        }


def _describe_condition(ind: str, op: str, threshold: Any, actual: Any, met: bool) -> str:
    """One-line plain-English description of a condition's status."""
    base = _INDICATOR_DESCRIPTIONS.get(ind, ind)
    if isinstance(threshold, str):
        ref_desc = _INDICATOR_DESCRIPTIONS.get(threshold, threshold)
        thresh_str = ref_desc
    else:
        thresh_str = str(threshold)

    if actual is None:
        return f"{base}: no data available"

    actual_fmt = f"{actual:.4f}" if isinstance(actual, float) else str(actual)
    status = "MET" if met else "FAILED"
    return f"{base} {op} {thresh_str} → actual {actual_fmt} [{status}]"


def _eval_single(cond: dict, indicators: dict[str, Any]) -> ConditionResult:
    """Evaluate one condition dict against flat indicator values."""
    from .pattern_engine import _eval_condition

    ind_key = cond.get("indicator", "")
    op = cond.get("op", "")
    raw_threshold = cond.get("value") if "value" in cond else cond.get("ref", "?")

    actual = indicators.get(ind_key)
    # For ref-based conditions, resolve the reference value for display.
    if "ref" in cond and cond["ref"]:
        display_threshold = cond["ref"]
    else:
        display_threshold = raw_threshold

    met = _eval_condition(cond, indicators)

    desc = _describe_condition(ind_key, op, display_threshold, actual, met)
    return ConditionResult(
        indicator=ind_key,
        op=op,
        threshold=display_threshold,
        actual_value=actual,
        met=met,
        human_desc=desc,
    )


def evaluate_pattern_health(
    rules_json: dict | str | None,
    indicators: dict[str, Any],
    *,
    previous_health: float | None = None,
    alert_conditions_met: set[str] | None = None,
) -> ConditionHealth:
    """Evaluate all conditions from a pattern's rules_json against live indicators.

    Parameters
    ----------
    rules_json : pattern's rules_json (dict or JSON string)
    indicators : flat dict of indicator key → current value
    previous_health : health_score from the last check (for delta)
    alert_conditions_met : set of indicator keys that were met when the
        alert originally fired; used to flag critical failures
    """
    if rules_json is None:
        return ConditionHealth(human_summary="No pattern conditions defined.")

    if isinstance(rules_json, str):
        try:
            rules_json = json.loads(rules_json)
        except Exception:
            return ConditionHealth(human_summary="Could not parse pattern conditions.")

    conditions_list = (rules_json or {}).get("conditions", [])
    if not conditions_list:
        return ConditionHealth(human_summary="Pattern has no conditions.")

    results: list[ConditionResult] = []
    for cond in conditions_list:
        results.append(_eval_single(cond, indicators))

    met_count = sum(1 for r in results if r.met)
    total = len(results)
    health = met_count / total if total > 0 else 0.0
    delta = health - previous_health if previous_health is not None else None

    critical: list[ConditionResult] = []
    if alert_conditions_met:
        for r in results:
            if not r.met and r.indicator in alert_conditions_met:
                critical.append(r)

    summary_parts = [f"Pattern health: {met_count}/{total} conditions met ({health:.0%})"]
    if delta is not None:
        direction = "improving" if delta > 0 else "degrading" if delta < 0 else "stable"
        summary_parts.append(f"Trend: {direction} (delta {delta:+.0%})")
    if critical:
        names = ", ".join(c.indicator for c in critical)
        summary_parts.append(f"Critical failures (were met at alert): {names}")
    for r in results:
        summary_parts.append(f"  {'✓' if r.met else '✗'} {r.human_desc}")

    return ConditionHealth(
        conditions=results,
        health_score=health,
        health_delta=delta,
        critical_failures=critical,
        human_summary="\n".join(summary_parts),
    )
