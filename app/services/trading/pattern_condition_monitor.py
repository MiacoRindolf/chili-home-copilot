"""Pattern condition health evaluator.

Evaluates a ScanPattern's rules_json conditions against live indicator
values and returns a structured health assessment.  Also evaluates
LLM-generated trade plans (invalidation conditions, monitoring signals,
key levels).  Pure logic — no LLM, no DB writes.
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


# ── Trade Plan evaluation ────────────────────────────────────────────────

_OP_MAP = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: abs(a - b) < 1e-9 if isinstance(a, float) else a == b,
}


@dataclass
class TradePlanHealth:
    entry_validated: bool = False
    invalidations_triggered: list[dict] = field(default_factory=list)
    caution_signals_changed: list[dict] = field(default_factory=list)
    levels_breached: list[dict] = field(default_factory=list)
    plan_health_score: float = 1.0
    human_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_validated": self.entry_validated,
            "invalidations_triggered": self.invalidations_triggered,
            "caution_signals_changed": self.caution_signals_changed,
            "levels_breached": self.levels_breached,
            "plan_health_score": round(self.plan_health_score, 3),
        }

    @property
    def has_critical_invalidation(self) -> bool:
        return any(i.get("severity") == "critical" for i in self.invalidations_triggered)

    @property
    def has_any_invalidation(self) -> bool:
        return len(self.invalidations_triggered) > 0


def evaluate_trade_plan(
    trade_plan: dict | None,
    indicators: dict[str, Any],
    current_price: float,
) -> TradePlanHealth:
    """Evaluate a structured trade plan against live indicators.

    Parameters
    ----------
    trade_plan : the JSON plan stored on BreakoutAlert.trade_plan
    indicators : flat dict of indicator key -> current value
    current_price : live price
    """
    if not trade_plan:
        return TradePlanHealth(entry_validated=True, human_summary="No trade plan defined.")

    result = TradePlanHealth()
    summary_parts: list[str] = []

    # 1. Entry validation.
    ev = trade_plan.get("entry_validation", {})
    reclaim = ev.get("required_reclaim")
    if reclaim is not None:
        result.entry_validated = current_price >= reclaim
        status = "VALIDATED" if result.entry_validated else "NOT YET"
        summary_parts.append(f"Entry reclaim ${reclaim:.2f}: {status} (price ${current_price:.2f})")
    else:
        result.entry_validated = True

    # 2. Invalidation conditions.
    penalty = 0.0
    for cond in trade_plan.get("invalidation_conditions", []):
        triggered = _eval_plan_condition(cond, indicators, current_price)
        if triggered:
            result.invalidations_triggered.append({
                "desc": cond.get("desc", ""),
                "indicator": cond.get("indicator", ""),
                "severity": cond.get("severity", "warning"),
            })
            sev = cond.get("severity", "warning")
            penalty += 0.3 if sev == "critical" else 0.15
            summary_parts.append(f"  INVALIDATION [{sev.upper()}]: {cond.get('desc', '')}")

    # 3. Monitoring signals — check if baseline has changed.
    for sig in trade_plan.get("monitoring_signals", []):
        change = _eval_signal_change(sig, indicators, current_price)
        if change:
            result.caution_signals_changed.append(change)
            direction = change.get("direction", "changed")
            if direction == "worsened":
                penalty += 0.1
            elif direction == "resolved":
                penalty -= 0.05
            summary_parts.append(f"  Signal {sig.get('desc', '')}: {direction}")

    # 4. Key levels.
    levels = trade_plan.get("key_levels", {})
    for level_name in ("early_warning", "near_resistance"):
        lv = levels.get(level_name)
        if lv is None:
            continue
        if level_name == "early_warning" and current_price < lv:
            result.levels_breached.append({"level": level_name, "value": lv, "price": current_price})
            penalty += 0.1
            summary_parts.append(f"  LEVEL BREACH: {level_name} ${lv:.2f} (price ${current_price:.2f})")

    result.plan_health_score = max(0.0, min(1.0, 1.0 - penalty))

    if not summary_parts:
        summary_parts.append("Trade plan: all conditions nominal.")
    result.human_summary = "\n".join(summary_parts)

    return result


def _eval_plan_condition(cond: dict, indicators: dict[str, Any], current_price: float) -> bool:
    """Evaluate a single invalidation condition. Returns True if TRIGGERED (bad)."""
    ind_key = cond.get("indicator", "")
    op_str = cond.get("op", "")
    ref_key = cond.get("ref")
    value = cond.get("value")

    actual = indicators.get(ind_key)
    if actual is None:
        if ind_key == "price":
            actual = current_price
        else:
            return False

    if ref_key:
        compare_to = indicators.get(ref_key)
        if compare_to is None and ref_key == "price":
            compare_to = current_price
        if compare_to is None:
            return False
    elif value is not None:
        compare_to = value
    else:
        return False

    op_fn = _OP_MAP.get(op_str)
    if not op_fn:
        return False

    try:
        return op_fn(float(actual), float(compare_to))
    except (TypeError, ValueError):
        return False


def _eval_signal_change(sig: dict, indicators: dict[str, Any], current_price: float) -> dict | None:
    """Check if a monitoring signal has changed from its baseline.

    Returns a change dict with 'direction' = 'worsened' | 'resolved' | None.
    """
    watch = sig.get("watch", "")
    baseline = sig.get("baseline", "")
    ind_key = sig.get("indicator", "")

    if watch == "direction":
        actual = indicators.get(ind_key)
        if actual is None:
            return None
        prev_key = f"{ind_key}_5d_direction"
        direction_val = indicators.get(prev_key, indicators.get(f"{ind_key}_direction"))
        if direction_val is None:
            return None
        current_dir = "falling" if str(direction_val).lower() in ("falling", "down", "negative") else "rising"
        if baseline == "falling" and current_dir == "rising":
            return {"desc": sig.get("desc", ""), "indicator": ind_key, "direction": "resolved"}
        elif baseline == "rising" and current_dir == "falling":
            return {"desc": sig.get("desc", ""), "indicator": ind_key, "direction": "worsened"}
        return None

    if watch == "level":
        level = sig.get("level")
        if level is None:
            return None
        if baseline == "below" and current_price >= level:
            return {"desc": sig.get("desc", ""), "indicator": ind_key, "direction": "resolved"}
        elif baseline == "above" and current_price < level:
            return {"desc": sig.get("desc", ""), "indicator": ind_key, "direction": "worsened"}
        return None

    return None
