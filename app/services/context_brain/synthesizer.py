"""Final synthesizer — the ONLY paid LLM call in the tree pipeline.

Takes the compiler's output (a dense, deduplicated, locally-curated
context document) and the original user query, then asks gpt-5.5 to
produce the final user-facing answer.

Why this stays on premium for now:
  * gpt-5.5 has the highest factual grounding + reasoning quality
    available to us.
  * The compiler has already done the heavy lifting (decompose →
    parallel chunk → merge), so the synthesizer's input is short and
    dense — minimal premium-token spend.
  * Once the learning cycle has accumulated enough mechanical patterns
    for an intent, we promote those into deterministic templates and
    the synthesizer call goes away. Same graduation pattern as the
    trading and code brains.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .. import openai_client
from .purpose_policy import default_synthesizer

logger = logging.getLogger(__name__)


_SYNTHESIS_SYSTEM_PROMPT_BASE = """You are CHILI, the user's home copilot. You will be given:

  1. The user's message.
  2. A compiled context document prepared by a research assistant
     (already curated and deduplicated from multiple grounded sources).

Use the compiled context as your primary source of truth. When the
context says "not in provided context" for some aspect, acknowledge
that gap rather than inventing details.

Style: clear, direct, no filler. Short paragraphs over walls of text.
Format with markdown when it improves scanability (lists, code fences),
otherwise prose. Match the user's tone."""


def synthesize(
    user_query: str,
    compiled_context: str,
    *,
    user_name: str = "you",
    chat_history: Optional[list[dict]] = None,
    model: Optional[str] = None,
    trace_id: str = "context_synth",
) -> tuple[dict, int]:
    """Calls gpt-5.5 (or whichever model is set as the synthesizer).

    Returns (result_dict, latency_ms) where result_dict has the same
    shape as ``openai_client.chat()``: ``{"reply", "tokens_used", "model"}``.
    """
    # Build messages: optional brief recent history + the user message.
    msgs: list[dict] = []
    if chat_history:
        # Cap at last 6 to keep the synth call cheap; the compiled context
        # already includes the salient bits from longer history via the
        # F.1 retrievers.
        for m in chat_history[-6:]:
            role = (m.get("role") or "").strip() or "user"
            content = (m.get("content") or "").strip()
            if content:
                msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": user_query})

    system_prompt = _SYNTHESIS_SYSTEM_PROMPT_BASE
    system_prompt += f"\n\nYou are talking to: {user_name}."
    if compiled_context:
        system_prompt += (
            "\n\n<compiled_context>\n"
            + compiled_context.strip()
            + "\n</compiled_context>"
        )

    t0 = time.monotonic()
    try:
        result = openai_client.chat(
            messages=msgs,
            system_prompt=system_prompt,
            trace_id=trace_id,
            user_message=user_query,
        )
    except Exception as e:
        logger.exception("[context_brain.synthesizer] openai_client.chat() raised")
        latency_ms = int((time.monotonic() - t0) * 1000)
        return ({"reply": "", "tokens_used": 0, "model": "error"}, latency_ms)
    latency_ms = int((time.monotonic() - t0) * 1000)
    return result, latency_ms
