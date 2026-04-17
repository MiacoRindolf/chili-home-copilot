"""LLM revalidation gate for AutoTrader v1 — strict JSON, fail-closed."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ...models.trading import BreakoutAlert
from ..llm_caller import call_llm
from .auto_trader_rules import alert_confidence_from_score, projected_profit_pct

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "auto_trader_revalidation.txt"


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _strip_code_fence(s: str) -> str:
    cleaned = (s or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    return cleaned.strip()


def parse_revalidation_response(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    cleaned = _strip_code_fence(raw)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("[autotrader_llm] JSON parse failed len=%d", len(cleaned))
        return None
    if not isinstance(obj, dict):
        return None
    if "viable" not in obj:
        return None
    return obj


def run_revalidation_llm(
    alert: BreakoutAlert,
    *,
    current_price: float,
    ohlcv_summary: str | None = None,
    pattern_name: str | None = None,
    trace_id: str = "autotrader-revalidation",
) -> tuple[bool, dict[str, Any]]:
    """Return (viable, snapshot) where snapshot includes raw keys or error."""
    system = _load_system_prompt()
    ppp = projected_profit_pct(alert.entry_price, alert.target_price)
    user_payload = {
        "ticker": alert.ticker,
        "pattern_name": pattern_name or "",
        "entry_price": alert.entry_price,
        "stop_loss": alert.stop_loss,
        "take_profit": alert.target_price,
        "current_price": current_price,
        "projected_profit_pct": ppp,
        "confidence_from_score": alert_confidence_from_score(alert),
        "scorecard": (alert.indicator_snapshot or {}).get("imminent_scorecard"),
        "ohlcv_summary": ohlcv_summary or "",
    }
    messages = [{"role": "user", "content": json.dumps(user_payload, default=str)}]
    raw = call_llm(
        messages,
        max_tokens=256,
        trace_id=trace_id,
        cacheable=False,
        system_prompt=system,
    )
    parsed = parse_revalidation_response(raw)
    if parsed is None:
        return False, {"error": "parse_failed", "raw_preview": (raw or "")[:500]}

    viable = bool(parsed.get("viable"))
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    reason = str(parsed.get("reason", ""))[:500]
    snap = {"viable": viable, "confidence": conf, "reason": reason, "raw": parsed}
    return viable, snap
