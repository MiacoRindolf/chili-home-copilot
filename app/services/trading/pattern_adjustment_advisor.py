"""LLM-based stop/target adjustment advisor for pattern-linked positions.

Given a pattern's condition health, current position P&L, and live market
state, asks the LLM whether to tighten stop, loosen target, hold, or exit.
Safety rails prevent widening stops or lowering targets.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are CHILI's pattern-position management engine.  You monitor open
positions that were entered based on a specific learned pattern.  Your job
is to decide whether the stop-loss or take-profit should be ADJUSTED based
on whether the pattern's conditions are still holding AND whether the
dynamic trade plan conditions are intact.

You receive TWO types of health data:
- PATTERN HEALTH: static rule conditions from the original pattern
- TRADE PLAN STATUS: dynamic conditions including invalidation triggers,
  monitoring signal changes, and key level breaches

RULES (never violate):
1. You may TIGHTEN a stop (move it closer to current price) but NEVER
   widen it beyond the original pattern stop.
2. You may LOOSEN a target (move it further from current price) but NEVER
   lower it below the current price.
3. When health is high and improving, recommend HOLD or loosen the target.
4. When health is degrading, recommend tightening the stop.
5. When health is critically low (<30%) and the position is at a loss,
   recommend EXIT_NOW.
6. CRITICAL INVALIDATION from the trade plan = strong signal for EXIT_NOW
   or aggressive stop tightening.
7. WARNING invalidation = signal to tighten stop conservatively.
8. Monitoring signal WORSENED = factor into tighter management.
9. Monitoring signal RESOLVED = positive, factor into hold/loosen.
10. Always provide a brief reasoning the user can understand.
11. Respond ONLY with valid JSON matching the schema below.

Response schema:
{
  "action": "tighten_stop" | "loosen_target" | "hold" | "exit_now",
  "new_stop": <float or null>,
  "new_target": <float or null>,
  "confidence": <float 0-1>,
  "reasoning": "<1-2 sentence explanation>"
}
"""


@dataclass
class AdjustmentRecommendation:
    action: str  # tighten_stop | loosen_target | hold | exit_now
    new_stop: float | None = None
    new_target: float | None = None
    confidence: float = 0.0
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "new_stop": self.new_stop,
            "new_target": self.new_target,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


def _build_user_prompt(
    *,
    ticker: str,
    pattern_name: str,
    pattern_description: str,
    health_summary: str,
    health_score: float,
    health_delta: float | None,
    current_price: float,
    entry_price: float,
    current_stop: float | None,
    current_target: float | None,
    pattern_stop: float | None,
    pattern_target: float | None,
    pnl_pct: float | None,
    trade_plan_health: Any = None,
) -> str:
    delta_str = f"{health_delta:+.0%}" if health_delta is not None else "N/A"
    pnl_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "N/A"

    parts = [f"""\
Ticker: {ticker}
Pattern: "{pattern_name}"
Description: {pattern_description}

Current price: ${current_price:.4f}
Entry price: ${entry_price:.4f}
P&L: {pnl_str}

Current stop: ${current_stop or 0:.4f}
Current target: ${current_target or 0:.4f}
Original pattern stop: ${pattern_stop or 0:.4f}
Original pattern target: ${pattern_target or 0:.4f}

Pattern health: {health_score:.0%} (delta: {delta_str})
{health_summary}"""]

    if trade_plan_health is not None:
        tph = trade_plan_health
        parts.append("\n--- TRADE PLAN STATUS ---")
        parts.append(f"Entry validated: {'YES' if tph.entry_validated else 'NO'}")
        parts.append(f"Plan health: {tph.plan_health_score:.0%}")

        if tph.invalidations_triggered:
            parts.append("INVALIDATION CONDITIONS TRIGGERED:")
            for inv in tph.invalidations_triggered:
                parts.append(f"  [{inv.get('severity', 'warning').upper()}] {inv.get('desc', '')}")

        if tph.caution_signals_changed:
            parts.append("MONITORING SIGNAL CHANGES:")
            for sig in tph.caution_signals_changed:
                parts.append(f"  {sig.get('desc', '')}: {sig.get('direction', 'changed')}")

        if tph.levels_breached:
            parts.append("KEY LEVELS BREACHED:")
            for lb in tph.levels_breached:
                parts.append(f"  {lb.get('level', '')}: ${lb.get('value', 0):.2f}")

        parts.append(tph.human_summary)

    parts.append("\nBased on ALL of the above (pattern health AND trade plan status), "
                 "what adjustment (if any) should be made?")
    return "\n".join(parts)


def get_adjustment(
    *,
    ticker: str,
    pattern_name: str,
    pattern_description: str,
    health_summary: str,
    health_score: float,
    health_delta: float | None,
    current_price: float,
    entry_price: float,
    current_stop: float | None,
    current_target: float | None,
    pattern_stop: float | None,
    pattern_target: float | None,
    pnl_pct: float | None,
    trade_plan_health: Any = None,
) -> AdjustmentRecommendation:
    """Call LLM and return a validated adjustment recommendation."""
    from ..llm_caller import call_llm

    user_msg = _build_user_prompt(
        ticker=ticker,
        pattern_name=pattern_name,
        pattern_description=pattern_description,
        health_summary=health_summary,
        health_score=health_score,
        health_delta=health_delta,
        current_price=current_price,
        entry_price=entry_price,
        current_stop=current_stop,
        current_target=current_target,
        pattern_stop=pattern_stop,
        pattern_target=pattern_target,
        pnl_pct=pnl_pct,
        trade_plan_health=trade_plan_health,
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    try:
        raw = call_llm(
            messages=messages,
            max_tokens=300,
            trace_id=f"pattern-adjust-{ticker}",
        )
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
    except Exception as e:
        logger.warning("[pattern_adjust] LLM call or parse failed for %s: %s", ticker, e)
        return AdjustmentRecommendation(action="hold", confidence=0.0, reasoning="LLM unavailable")

    rec = AdjustmentRecommendation(
        action=parsed.get("action", "hold"),
        new_stop=parsed.get("new_stop"),
        new_target=parsed.get("new_target"),
        confidence=float(parsed.get("confidence", 0)),
        reasoning=parsed.get("reasoning", ""),
    )

    # --- Safety rails ---
    rec = _apply_safety_rails(
        rec,
        current_price=current_price,
        current_stop=current_stop,
        current_target=current_target,
        pattern_stop=pattern_stop,
    )
    return rec


def _apply_safety_rails(
    rec: AdjustmentRecommendation,
    *,
    current_price: float,
    current_stop: float | None,
    current_target: float | None,
    pattern_stop: float | None,
) -> AdjustmentRecommendation:
    """Enforce hard constraints on the recommendation."""
    valid_actions = {"tighten_stop", "loosen_target", "hold", "exit_now"}
    if rec.action not in valid_actions:
        rec.action = "hold"

    if rec.action == "tighten_stop" and rec.new_stop is not None:
        # Stop can only move CLOSER to current price (higher for longs).
        if current_stop and rec.new_stop < current_stop:
            rec.new_stop = current_stop
        # Never widen beyond original pattern stop.
        if pattern_stop and rec.new_stop < pattern_stop:
            rec.new_stop = pattern_stop
        # Stop must stay below current price.
        if rec.new_stop >= current_price:
            rec.new_stop = current_price * 0.98

    if rec.action == "loosen_target" and rec.new_target is not None:
        # Target can only move FURTHER from current price (higher for longs).
        if current_target and rec.new_target < current_target:
            rec.new_target = current_target
        # Target must stay above current price.
        if rec.new_target <= current_price:
            rec.new_target = current_price * 1.02

    return rec
