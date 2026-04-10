"""Trading Brain Assistant: LLM chat grounded in trading DB and worker state."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from ...schemas.trading import TradingBrainAssistantChatResponse, TradingBrainRecommendation
from ..llm_caller import call_llm
from .brain_assistant_context import build_snapshot

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 12
MAX_TOKENS = 1600

SYSTEM_PROMPT = """You are the CHILI Trading Brain Assistant.

You are not an explain-only narrator. You are a trading copilot grounded in the live CHILI trading snapshot below.
Your job is to analyze, recommend, decide, and adapt within policy-bound execution lanes while staying honest about what is gated.

RULES:
- Answer only from the provided snapshot and conversation. Do not invent metrics or pattern names.
- Use the market thesis, automation focus, opportunity-board state, patterns, and readiness data when present.
- You may provide actionable recommendations such as buy, sell, hold, reduce, or wait when the snapshot supports them.
- Keep execution truth honest. Recommendations are allowed; live execution remains policy-, broker-, readiness-, and governance-gated unless the snapshot says otherwise.
- Do not make guarantees. If context is missing, list it in missing_context instead of using generic compliance filler.
- If the user asks to "search" or "find" patterns, use the patterns_summary (it may already be filtered by keyword if they asked for a ticker/term).

OUTPUT FORMAT:
Return ONLY valid JSON with this shape:
{
  "reply": "human-readable markdown summary",
  "recommendations": [
    {
      "action": "buy|sell|hold|reduce|wait",
      "symbol": "ticker or market",
      "market": "optional venue/market",
      "thesis": "short thesis",
      "rationale": ["bullet 1", "bullet 2"],
      "entry": "entry zone or trigger",
      "invalidation": "stop or invalidation",
      "exit_logic": "take profit / exit logic",
      "timeframe": "intraday|swing|...",
      "confidence": 0.0,
      "risk_note": "risk-aware note",
      "sizing_guidance": "size guidance when context exists",
      "execution_readiness": {"status": "ready|gated|missing_context", "reason": "..."},
      "what_would_change": "what would change the recommendation",
      "missing_context": ["optional missing items"],
      "source_of_truth_provider": "provider when relevant",
      "source_of_truth_exchange": "exchange when relevant"
    }
  ],
  "missing_context": ["optional missing items"]
}
"""


def _extract_json_payload(reply: str) -> dict[str, Any] | None:
    text = (reply or "").strip()
    if not text:
        return None
    candidates = [text]
    fenced = re.findall(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text, flags=re.I)
    candidates.extend(fenced)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def _normalize_recommendations(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rationale = item.get("rationale")
        if isinstance(rationale, str):
            rationale = [rationale]
        if not isinstance(rationale, list):
            rationale = []
        payload = {
            "action": str(item.get("action") or "wait").strip().lower(),
            "symbol": item.get("symbol"),
            "market": item.get("market"),
            "thesis": str(item.get("thesis") or "").strip(),
            "rationale": [str(x).strip() for x in rationale if str(x).strip()],
            "entry": item.get("entry"),
            "invalidation": item.get("invalidation"),
            "exit_logic": item.get("exit_logic"),
            "timeframe": item.get("timeframe"),
            "confidence": item.get("confidence"),
            "risk_note": item.get("risk_note"),
            "sizing_guidance": item.get("sizing_guidance"),
            "execution_readiness": item.get("execution_readiness") if isinstance(item.get("execution_readiness"), dict) else None,
            "what_would_change": item.get("what_would_change"),
            "missing_context": item.get("missing_context") if isinstance(item.get("missing_context"), list) else [],
            "source_of_truth_provider": item.get("source_of_truth_provider"),
            "source_of_truth_exchange": item.get("source_of_truth_exchange"),
        }
        if not payload["thesis"]:
            continue
        try:
            out.append(TradingBrainRecommendation(**payload).model_dump())
        except Exception:
            logger.debug("[brain_assistant] invalid recommendation payload skipped", exc_info=True)
    return out


def _response_from_reply(reply: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = _extract_json_payload(reply)
    if payload is None:
        return TradingBrainAssistantChatResponse(
            ok=True,
            reply=reply.strip(),
            recommendations=[],
            missing_context=[],
            snapshot_at=snapshot.get("snapshot_at"),
            execution_readiness=(snapshot.get("automation_focus") or {}).get("operator_readiness"),
            data_fidelity=(snapshot.get("automation_focus") or {}).get("focus_session", {}).get("data_fidelity"),
        ).model_dump()

    normalized = TradingBrainAssistantChatResponse(
        ok=True,
        reply=str(payload.get("reply") or "").strip(),
        recommendations=[
            TradingBrainRecommendation(**row) for row in _normalize_recommendations(payload.get("recommendations"))
        ],
        missing_context=[str(x).strip() for x in payload.get("missing_context", []) if str(x).strip()],
        snapshot_at=snapshot.get("snapshot_at"),
        execution_readiness=(snapshot.get("automation_focus") or {}).get("operator_readiness"),
        data_fidelity=(snapshot.get("automation_focus") or {}).get("focus_session", {}).get("data_fidelity"),
    )
    return normalized.model_dump()


def _build_messages(
    snapshot: dict[str, Any],
    conversation: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Build full message list: system (with snapshot JSON) + conversation (capped)."""
    snapshot_blob = json.dumps(snapshot, indent=2)
    system_content = (
        SYSTEM_PROMPT
        + "\n\n--- Snapshot (JSON) ---\n"
        + snapshot_blob
        + "\n--- End snapshot ---"
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    # Cap conversation history to avoid token overflow
    for m in conversation[-MAX_HISTORY_TURNS:]:
        role = m.get("role")
        content = m.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    return messages


def chat(
    db: Session,
    user_id: int | None,
    messages: list[dict[str, str]],
    include_pattern_search: bool = True,
    refresh: bool = False,
) -> dict[str, Any]:
    """
    Process a Trading Brain Assistant chat turn.

    Args:
        db: DB session
        user_id: Current user (for snapshot and insights)
        messages: Conversation so far [{role, content}]; last message should be user
        include_pattern_search: If True, pass last user message to snapshot for keyword filter
        refresh: If True, bypass snapshot cache

    Returns:
        Backward-compatible response with ``ok`` / ``reply`` plus structured recommendation fields.
    """
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content"):
            last_user = m["content"]
            break

    snapshot = build_snapshot(
        db,
        user_id,
        user_message=last_user if include_pattern_search else None,
        use_cache=not refresh,
    )

    conversation = [{"role": m.get("role", ""), "content": m.get("content", "")} for m in messages]
    full_messages = _build_messages(snapshot, conversation)

    reply = call_llm(
        full_messages,
        max_tokens=MAX_TOKENS,
        trace_id="trading-brain-assistant",
    )

    if not reply:
        from ...openai_client import is_configured
        if not is_configured():
            return {
                "ok": False,
                "reply": "",
                "error": "LLM is not configured. Set your API key in settings to chat with the Trading Brain Assistant.",
                "recommendations": [],
                "missing_context": [],
                "snapshot_at": snapshot.get("snapshot_at"),
            }
        return {
            "ok": False,
            "reply": "",
            "error": "The assistant could not generate a reply. Try again or refresh the page.",
            "recommendations": [],
            "missing_context": [],
            "snapshot_at": snapshot.get("snapshot_at"),
        }

    return _response_from_reply(reply, snapshot)
