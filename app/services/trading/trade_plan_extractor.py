"""LLM-based trade plan extractor.

Given a pattern's conditions, current indicators, and alert levels, asks the
LLM to produce a structured monitoring plan with entry validation,
invalidation conditions, monitoring signals, and key levels.
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

    return plan


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
