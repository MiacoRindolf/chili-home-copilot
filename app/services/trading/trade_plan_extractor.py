"""Trade plan extraction: rich mechanical planner + optional LLM fallback.

Mechanical path derives invalidations and monitoring from pattern conditions.
``extract_trade_plan`` is used only when the mechanical plan is too sparse (complex patterns).
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are CHILI's trade-plan extraction engine.  Given a pattern alert with
entry/stop/target levels and the current market indicators, produce a
STRUCTURED MONITORING PLAN that a mechanical system can evaluate every few
minutes.

The plan must be a JSON object with these sections:

{
  "entry_validation": {
    "required_reclaim": <float — price level that must be reclaimed for the entry to be valid>,
    "method": "<string — e.g. 'buy_stop_above_entry', 'limit_at_entry', 'market_on_reclaim'>"
  },
  "invalidation_conditions": [
    {
      "desc": "<plain-English description>",
      "indicator": "<indicator key like sma_20, obv, rsi_14, ema_50, vwap, macd_hist>",
      "op": "<comparison: '>', '<', '>=', '<=', '=='>",
      "ref": "<optional: another indicator key or 'price' for cross-comparison>",
      "value": <optional: numeric threshold if not using ref>,
      "severity": "<'warning' or 'critical'>"
    }
  ],
  "monitoring_signals": [
    {
      "desc": "<plain-English description>",
      "indicator": "<indicator key>",
      "watch": "<'direction' or 'level' or 'cross'>",
      "baseline": "<current status: 'falling', 'rising', 'below', 'above', etc.>",
      "level": <optional float for level-based signals>
    }
  ],
  "trajectory_conditions": [
    {
      "desc": "<plain-English>",
      "indicator": "<indicator key>",
      "watch": "direction",
      "baseline": "<'rising' or 'falling' expected for healthy long setup>"
    }
  ],
  "key_levels": {
    "entry": <float>,
    "stop": <float>,
    "target": <float>,
    "early_warning": <float or null — a level that if broken signals the thesis is weakening>,
    "near_resistance": <float or null — nearest overhead resistance>,
    "vwap": <float or null>
  },
  "position_rules": {
    "max_risk_pct": <float — recommended max portfolio risk percentage>,
    "sizing_note": "<brief sizing guidance>"
  }
}

RULES:
1. Every invalidation condition MUST use real indicator keys the system can evaluate.
   Valid keys: price, sma_20, sma_50, ema_20, ema_50, ema_100, ema_200, rsi_14,
   macd_hist, macd, macd_signal, adx, atr, obv, mfi, vwap, bb_pct, volume_ratio,
   stochastic_k, dist_to_resistance_pct, high_watermark.
2. For ref-based conditions (comparing two indicators), use the "ref" field.
   For absolute threshold conditions, use the "value" field.
3. Include 2-5 invalidation conditions, ordered by severity (critical first).
4. Include 2-4 monitoring signals — things the user should watch.
5. Respond ONLY with valid JSON. No markdown, no explanation.
"""


def extract_trade_plan(
    *,
    ticker: str,
    pattern_name: str,
    pattern_description: str,
    pattern_conditions: list[dict],
    entry_price: float,
    stop_loss: float,
    target_price: float,
    current_price: float,
    indicators: dict[str, Any],
) -> dict | None:
    """Call LLM to extract a structured trade monitoring plan.

    Returns the parsed plan dict, or None on failure.
    """
    from ..llm_caller import call_llm

    ind_summary = _format_indicators(indicators)
    cond_summary = _format_conditions(pattern_conditions)

    user_msg = f"""\
Ticker: {ticker}
Pattern: "{pattern_name}"
Description: {pattern_description}

Pattern conditions:
{cond_summary}

Alert levels:
  Entry: ${entry_price:.4f}
  Stop: ${stop_loss:.4f}
  Target: ${target_price:.4f}

Current price: ${current_price:.4f}
Price vs entry: {"above" if current_price >= entry_price else "below"} entry by \
{abs(current_price - entry_price) / entry_price * 100:.1f}%

Current indicators:
{ind_summary}

Based on the pattern conditions, the current indicator state, and the alert levels, \
produce the structured monitoring plan JSON.
"""

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    try:
        raw = call_llm(messages=messages, max_tokens=600, trace_id=f"trade-plan-{ticker}")
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        plan = json.loads(raw)
    except Exception as e:
        logger.warning("[trade_plan] LLM extraction failed for %s: %s", ticker, e)
        return None

    plan = _validate_plan(plan, entry_price=entry_price, stop_loss=stop_loss, target_price=target_price)
    return plan


def _validate_plan(
    plan: dict,
    *,
    entry_price: float,
    stop_loss: float,
    target_price: float,
) -> dict:
    """Ensure required keys exist and levels are sane."""
    if "entry_validation" not in plan:
        plan["entry_validation"] = {"required_reclaim": entry_price, "method": "buy_stop_above_entry"}

    if "invalidation_conditions" not in plan:
        plan["invalidation_conditions"] = []

    if "monitoring_signals" not in plan:
        plan["monitoring_signals"] = []

    levels = plan.get("key_levels", {})
    levels.setdefault("entry", entry_price)
    levels.setdefault("stop", stop_loss)
    levels.setdefault("target", target_price)
    plan["key_levels"] = levels

    if "position_rules" not in plan:
        risk_pct = abs(entry_price - stop_loss) / entry_price * 100 if entry_price else 0
        plan["position_rules"] = {
            "max_risk_pct": min(2.0, max(0.5, round(1.0 if risk_pct < 10 else 0.5, 1))),
            "sizing_note": "wide stop" if risk_pct > 15 else "normal stop width",
        }
    plan.setdefault("trajectory_conditions", [])

    return plan


def _mechanical_severity(ind: str) -> str:
    """Map indicator family to invalidation severity (complex patterns)."""
    price_level = {"price", "ema_50", "ema_100", "ema_200", "sma_50", "vwap", "high_watermark"}
    trend = {"ema_20", "sma_20", "macd", "macd_signal"}
    momentum = {"rsi_14", "macd_hist", "stochastic_k", "mfi", "bb_pct", "bb_pct_b"}
    volume = {"volume_ratio", "obv", "adx", "atr"}
    if ind in price_level:
        return "critical"
    if ind in trend:
        return "critical"
    if ind in momentum:
        return "warning"
    if ind in volume:
        return "info"
    return "warning"


def extract_trade_plan_mechanical(
    *,
    pattern_conditions: list[dict],
    entry_price: float,
    stop_loss: float,
    target_price: float,
    current_price: float,
    indicators: dict[str, Any],
) -> dict:
    """Derive a trade plan mechanically from pattern conditions — no LLM.

    Every condition that was met at alert time becomes an invalidation
    condition if it flips.  Top indicators become the monitoring signals.
    For complex patterns (5+ conditions), severity is tiered and extra
    levels (Fib-style cushion, resistance hint) are added.
    """
    invalidations: list[dict] = []
    monitoring_signals: list[dict] = []

    VALID_INDICATORS = {
        "price", "sma_20", "sma_50", "ema_20", "ema_50", "ema_100", "ema_200",
        "rsi_14", "macd_hist", "macd", "macd_signal", "adx", "atr", "obv",
        "mfi", "vwap", "bb_pct", "bb_pct_b", "volume_ratio", "stochastic_k",
        "dist_to_resistance_pct", "high_watermark",
    }

    n_cond = len(pattern_conditions or [])
    complex_pattern = n_cond >= 5

    # Normalize indicator aliases from flattened snapshots
    ind_alias = dict(indicators)
    if "bb_pct_b" not in ind_alias and ind_alias.get("bb_pct") is not None:
        ind_alias["bb_pct_b"] = ind_alias["bb_pct"]

    for cond in pattern_conditions:
        ind = cond.get("indicator", "")
        op = cond.get("op", "")
        val = cond.get("value")
        ref = cond.get("ref")

        if ind not in VALID_INDICATORS:
            continue

        # Invert the condition for invalidation:
        # if the pattern required `rsi_14 > 50`, invalidation is `rsi_14 < 50`
        inv_op = _invert_op(op)
        if inv_op:
            if complex_pattern:
                severity = _mechanical_severity(ind)
            else:
                severity = "critical" if ind in ("price", "ema_50", "sma_50") else "warning"
            inv_entry: dict[str, Any] = {
                "desc": f"{ind} no longer meets pattern condition ({ind} {op} {val or ref})",
                "indicator": ind,
                "op": inv_op,
                "severity": severity,
            }
            if ref:
                inv_entry["ref"] = ref
            elif val is not None:
                inv_entry["value"] = val
            invalidations.append(inv_entry)

        # Use top conditions as monitoring signals
        if len(monitoring_signals) < (6 if complex_pattern else 4):
            actual = ind_alias.get(ind)
            baseline = "unknown"
            if actual is not None and val is not None:
                try:
                    baseline = "above" if float(actual) >= float(val) else "below"
                except (TypeError, ValueError):
                    pass

            monitoring_signals.append({
                "desc": f"Watch {ind} ({op} {val or ref})",
                "indicator": ind,
                "watch": "level" if val is not None else "direction",
                "baseline": baseline,
                "level": val if isinstance(val, (int, float)) else None,
            })

    # Severity order: critical < warning < info
    _sev_rank = {"critical": 0, "warning": 1, "info": 2}

    def _inv_key(x: dict) -> tuple:
        return (_sev_rank.get(x.get("severity"), 1), x.get("indicator", ""))

    invalidations.sort(key=_inv_key)

    cap_inv = 8 if complex_pattern else 5
    invalidations = invalidations[:cap_inv]

    # Compute early warning + optional Fib-style cushion (long bias; still useful as distance)
    early_warning = None
    if stop_loss and entry_price:
        risk_width = abs(entry_price - stop_loss)
        if risk_width > 0 and entry_price > stop_loss:
            fib_cushion = entry_price - 0.382 * risk_width
            linear_cushion = stop_loss + (entry_price - stop_loss) * 0.3
            early_warning = round(max(fib_cushion, linear_cushion), 6)
        else:
            early_warning = round(stop_loss + (entry_price - stop_loss) * 0.3, 4)

    near_resistance = None
    if target_price and current_price and target_price > current_price:
        near_resistance = round(target_price, 6)
    else:
        dr = ind_alias.get("dist_to_resistance_pct")
        try:
            if dr is not None and current_price:
                near_resistance = round(float(current_price) * (1.0 + float(dr) / 100.0), 6)
        except (TypeError, ValueError):
            pass

    trajectory_conditions: list[dict[str, Any]] = []
    seen_traj: set[str] = set()
    for cond in pattern_conditions or []:
        ind = cond.get("indicator", "")
        if ind == "rsi_14" and "rsi_traj" not in seen_traj:
            trajectory_conditions.append({
                "desc": "RSI trajectory — falling from overbought is caution",
                "indicator": "rsi_14",
                "watch": "direction",
                "baseline": "rising",
            })
            seen_traj.add("rsi_traj")
        if ind in ("volume_ratio", "obv") and "obv_traj" not in seen_traj:
            trajectory_conditions.append({
                "desc": "OBV / flow trajectory vs price (distribution risk)",
                "indicator": "obv",
                "watch": "direction",
                "baseline": "rising",
            })
            seen_traj.add("obv_traj")

    try:
        vwap_v = ind_alias.get("vwap")
        if vwap_v and current_price:
            vv = float(vwap_v)
            cp = float(current_price)
            if vv > 0 and abs(cp - vv) / vv < 0.025:
                monitoring_signals.append({
                    "desc": "Price near VWAP — watch reclaim vs lost",
                    "indicator": "vwap",
                    "watch": "level",
                    "baseline": "above" if cp >= vv else "below",
                    "level": vv,
                })
    except (TypeError, ValueError):
        pass

    risk_pct = abs(entry_price - stop_loss) / entry_price * 100 if entry_price and stop_loss else 0.0
    reward_pct = abs(target_price - entry_price) / entry_price * 100 if entry_price and target_price else 0.0
    rr = (reward_pct / risk_pct) if risk_pct > 0.01 else 0.0
    if complex_pattern and rr >= 2.0:
        sizing_note = f"mechanical plan — pattern R:R ~ {rr:.1f}:1; derived from {n_cond} conditions"
    elif complex_pattern:
        sizing_note = f"mechanical plan — {n_cond} conditions; moderate R:R"
    else:
        sizing_note = "mechanical plan — derived from pattern conditions"

    plan = {
        "entry_validation": {
            "required_reclaim": entry_price,
            "method": "buy_stop_above_entry",
        },
        "invalidation_conditions": invalidations,
        "monitoring_signals": monitoring_signals[: (8 if complex_pattern else 5)],
        "trajectory_conditions": trajectory_conditions,
        "key_levels": {
            "entry": entry_price,
            "stop": stop_loss,
            "target": target_price,
            "early_warning": early_warning,
            "near_resistance": near_resistance,
            "vwap": ind_alias.get("vwap"),
        },
        "position_rules": {
            "max_risk_pct": min(2.0, max(0.5, round(1.0 if risk_pct < 10 else 0.75, 2))),
            "sizing_note": sizing_note,
        },
    }
    return _validate_plan(plan, entry_price=entry_price, stop_loss=stop_loss, target_price=target_price)


def _invert_op(op: str) -> str | None:
    """Return the inverse comparison operator."""
    _map = {
        ">": "<=", ">=": "<",
        "<": ">=", "<=": ">",
        "==": "!=", "!=": "==",
    }
    return _map.get(op)


def _format_indicators(indicators: dict[str, Any]) -> str:
    lines = []
    for k, v in sorted(indicators.items()):
        if v is None:
            continue
        if isinstance(v, float):
            lines.append(f"  {k}: {v:.4f}")
        else:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines[:40])


def _format_conditions(conditions: list[dict]) -> str:
    lines = []
    for c in conditions:
        ind = c.get("indicator", "?")
        op = c.get("op", "?")
        val = c.get("value", c.get("ref", "?"))
        lines.append(f"  {ind} {op} {val}")
    return "\n".join(lines) if lines else "  (none)"
