"""Trading Brain Assistant: LLM chat grounded in trading DB and worker state."""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from ..llm_caller import call_llm
from .brain_assistant_context import build_snapshot

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 12
MAX_TOKENS = 1600

SYSTEM_PROMPT = """You are the CHILI Trading Brain Assistant. You help the user understand and steer the trading *brain*: what it is doing, learning, has learned, and plans next. You have access to a live snapshot of the brain's state below.

RULES:
- Answer only from the provided snapshot and conversation. Do not invent metrics or pattern names.
- Explain internal CHILI state: worker status, backtest queue, patterns, recent activity, insights. Suggest evolution directions (e.g. which patterns to boost, what to explore next) using the data.
- Do NOT give personalized investment advice, price targets, or recommendations to buy/sell. If asked, redirect to "I can only describe what the trading brain is doing and learning."
- Be concise. Use bullet points when listing patterns or queue stats. If the user asks to "search" or "find" patterns, use the patterns_summary (it may already be filtered by keyword if they asked for a ticker/term).
- Encourage collaboration: e.g. "You can boost pattern X from the Discovered Patterns section" or "I can only see what's in the snapshot; use Refresh if you need the latest."
"""


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
        { "ok": bool, "reply": str, "error"?: str }
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
            }
        return {
            "ok": False,
            "reply": "",
            "error": "The assistant could not generate a reply. Try again or refresh the page.",
        }

    return {"ok": True, "reply": reply.strip()}
