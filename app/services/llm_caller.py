"""Thin LLM wrapper for services that need LLM calls without importing chat_service.

Avoids circular dependencies: code_brain modules can import this instead of
chat_service, which itself imports code_brain at top level.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def call_llm(
    messages: list[dict[str, Any]],
    max_tokens: int = 800,
    trace_id: str = "llm-caller",
) -> str:
    """Call the configured LLM and return the reply text (or empty string on failure)."""
    from ..openai_client import chat as llm_chat, is_configured

    if not is_configured():
        logger.debug("[llm_caller] LLM not configured")
        return ""

    try:
        result = llm_chat(
            messages=messages,
            max_tokens=max_tokens,
            trace_id=trace_id,
        )
        reply = result.get("reply", "")
        return reply if isinstance(reply, str) else str(reply)
    except Exception as e:
        logger.warning("[llm_caller] LLM call failed: %s", e)
        return ""
