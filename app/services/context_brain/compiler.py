"""Compile resolved chunk responses into ONE dense context document.

Two compilation modes:

  * **Pass-through** (single chunk, no decomposition): just pack the
    chunk's response as-is. No extra LLM call.
  * **Re-analyze** (multiple chunks): hand the chunk responses to Ollama
    and ask it to merge them into a single coherent context document
    that the premium synthesizer can use directly. Strips redundancy,
    notes gaps, organizes by topic.

The compiler does NOT answer the user — its output is consumed by the
synthesizer. Think of it as a research-assistant's notes, not the final
report.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import ollama_client
from .purpose_policy import default_primary_local
from .tree_types import ChunkResponse

logger = logging.getLogger(__name__)


_COMPILER_SYSTEM_PROMPT = """You are a context compiler.

You will be given:
  * The original user query.
  * Several focused sub-question/answer pairs (each was answered by a
    local model with access to grounded context).

Your job is to merge those pairs into a SINGLE dense context document
that another model will use to answer the original query well.

Rules:
* Organize the content logically (topical headings if helpful).
* DEDUPLICATE — if two chunks say the same thing, say it once.
* PRESERVE specifics — file paths, function names, numbers, dates,
  quotes — exactly as they appear in chunk answers.
* When chunks disagree or one says "Not in provided context", note that
  briefly.
* Do NOT answer the original query. Just compile context for someone
  else to answer it.
* Output plain text. No fences, no JSON, no chunk labels.
* Aim for 200-600 words. Less if the chunks were brief.
"""


def compile_chunks(
    user_query: str,
    chunk_responses: list[ChunkResponse],
    *,
    model: Optional[str] = None,
    timeout_sec: float = 25.0,
) -> tuple[str, int]:
    """Returns (compiled_context_text, latency_ms).

    Falls back to deterministic concatenation when Ollama is unreachable
    OR when there's only one chunk (no point in invoking the compiler
    LLM just to format a single response).
    """
    if not chunk_responses:
        return "", 0

    successful = [c for c in chunk_responses if c.success and c.selected_response]
    if not successful:
        return "", 0

    # Single-chunk fast path: skip compiler LLM entirely
    if len(successful) == 1:
        return successful[0].selected_response.strip(), 0

    # Build the input for the compiler
    sections: list[str] = [f"Original query: {user_query.strip()}", "", "Chunks:"]
    for c in chunk_responses:
        if not c.success or not c.selected_response:
            sections.append(
                f"\n[chunk {c.plan.index} ({c.plan.kind}) — {c.plan.query!r}]\n"
                f"(no answer: {c.error or 'failed'})"
            )
            continue
        flagged = ""
        if c.similarity_score is not None and c.similarity_score < 0.4:
            flagged = (
                f" (note: cross-examined, models disagreed "
                f"sim={c.similarity_score:.2f})"
            )
        sections.append(
            f"\n[chunk {c.plan.index} ({c.plan.kind}) — {c.plan.query!r}]"
            f"{flagged}\n{c.selected_response.strip()}"
        )
    user_block = "\n".join(sections)

    chosen_model = model or default_primary_local()
    res = ollama_client.chat(
        messages=[
            {"role": "system", "content": _COMPILER_SYSTEM_PROMPT},
            {"role": "user", "content": user_block},
        ],
        model=chosen_model,
        temperature=0.2,
        timeout_sec=timeout_sec,
    )

    if not res.ok or not res.text:
        # Ollama failed — fall back to deterministic concat so the
        # synthesizer still gets useful raw material.
        logger.info(
            "[context_brain.compiler] LLM compile failed (%s); "
            "falling back to plain concat",
            res.error,
        )
        plain = "\n\n".join(
            f"### {c.plan.query}\n{c.selected_response.strip()}"
            for c in successful
        )
        return plain, res.latency_ms

    return res.text.strip(), res.latency_ms
