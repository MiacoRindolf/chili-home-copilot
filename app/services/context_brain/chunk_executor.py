"""Resolve chunks in parallel via Ollama.

Each chunk gets:
  1. Its own system prompt tuned to the chunk kind (fact/reasoning/code/general)
  2. The shared assembled-context prompt from the F.1 retrieval stage
  3. A primary local model call (always)
  4. An optional secondary local model call (when policy.cross_examine
     and the secondary model is actually pulled into Ollama)

Chunks run in parallel through ThreadPoolExecutor. Per-chunk timeout caps
runaway calls. Failures don't abort the tree — the compiler tolerates
partial chunk loss and either retries or notes the gap.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Optional

from . import ollama_client
from .purpose_policy import default_primary_local, default_secondary_local
from .tree_types import (
    CHUNK_KIND_CODE,
    CHUNK_KIND_FACT,
    CHUNK_KIND_REASONING,
    ChunkPlan,
    ChunkResponse,
    PurposePolicy,
)

logger = logging.getLogger(__name__)


# Per-kind system prompts. Kept short so the chunk model has room to
# answer without bumping into context-window limits on tiny local models.
_CHUNK_SYSTEM_PROMPTS = {
    CHUNK_KIND_FACT: (
        "You are a focused fact-finder. Answer the question using the "
        "context provided when relevant. State 'Not in provided context' "
        "if the answer isn't supported by what's given. Be concise — "
        "1-3 sentences. No filler."
    ),
    CHUNK_KIND_REASONING: (
        "You are a focused analytic helper. Reason step-by-step about "
        "the question, weighing trade-offs from the provided context. "
        "Give a clear conclusion. 2-4 short paragraphs maximum."
    ),
    CHUNK_KIND_CODE: (
        "You are a focused code helper. Answer the question about the "
        "code shown in context. When proposing a change, show only the "
        "relevant snippet. When explaining, point at exact file paths "
        "and function names. Brevity over polish."
    ),
}
_DEFAULT_CHUNK_SYSTEM = (
    "You are a focused researcher. Answer the question using the provided "
    "context. Be precise and brief. Do not invent details not present in "
    "the context."
)


def _system_prompt_for(kind: str) -> str:
    return _CHUNK_SYSTEM_PROMPTS.get(kind, _DEFAULT_CHUNK_SYSTEM)


def _similarity(a: str, b: str) -> float:
    """Cheap character-level similarity in [0, 1]. SequenceMatcher.ratio
    is plenty for "do these two model outputs say roughly the same thing"
    — we don't need semantic embedding here, just to flag radical
    disagreements (ratio < 0.4) for the disagreement resolver."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.strip(), b.strip()).ratio()


def _resolve_models(policy: PurposePolicy) -> tuple[str, Optional[str]]:
    primary = policy.primary_local_model or default_primary_local()
    secondary: Optional[str] = None
    if policy.cross_examine:
        sec = policy.secondary_local_model or default_secondary_local()
        # Only use the secondary if it's actually pulled into Ollama. The
        # has_model probe is cheap (one HTTP call) so we do it once per
        # chunk batch and cache via closure (per-call) — no global cache
        # to keep things simple.
        if sec and ollama_client.has_model(sec):
            secondary = sec
        else:
            logger.info(
                "[context_brain.chunk_executor] cross_examine requested but "
                "secondary model %r not pulled — running primary only",
                sec,
            )
    return primary, secondary


def _execute_one_chunk(
    plan: ChunkPlan,
    *,
    assembled_context: str,
    primary_model: str,
    secondary_model: Optional[str],
    is_high_stakes: bool,
    timeout_sec: float,
) -> ChunkResponse:
    cr = ChunkResponse(plan=plan, is_high_stakes=is_high_stakes)
    sys_prompt = _system_prompt_for(plan.kind)
    user_block = (
        f"<context>\n{assembled_context}\n</context>\n\n"
        f"Question: {plan.query}"
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_block},
    ]

    # Primary call
    primary_res = ollama_client.chat(
        messages=messages,
        model=primary_model,
        temperature=0.2 if plan.kind == CHUNK_KIND_FACT else 0.4,
        timeout_sec=timeout_sec,
    )
    cr.primary_model = primary_res.model
    cr.primary_response = primary_res.text
    cr.primary_tokens_out = primary_res.tokens_out
    cr.primary_latency_ms = primary_res.latency_ms

    if not primary_res.ok:
        cr.success = False
        cr.error = primary_res.error
        cr.selected_response = ""
        return cr

    # Secondary call (cross-exam)
    if secondary_model:
        sec_res = ollama_client.chat(
            messages=messages,
            model=secondary_model,
            temperature=0.4,
            timeout_sec=timeout_sec,
        )
        cr.secondary_model = sec_res.model
        cr.secondary_response = sec_res.text if sec_res.ok else None
        cr.secondary_tokens_out = sec_res.tokens_out
        cr.secondary_latency_ms = sec_res.latency_ms

        if cr.secondary_response:
            cr.similarity_score = round(_similarity(cr.primary_response, cr.secondary_response), 4)
            # Disagreement resolution policy:
            #   * high agreement (>=0.6) → use primary (cheaper / longer history)
            #   * disagreement on high_stakes → flag for operator (we still
            #     surface primary but the audit row records the divergence)
            #   * disagreement on low_stakes → use the longer response
            if cr.similarity_score >= 0.6:
                cr.selected_response = cr.primary_response
                cr.selection_reason = "agreement"
            elif is_high_stakes:
                cr.selected_response = cr.primary_response
                cr.selection_reason = "primary_used_disagreement_flagged"
            else:
                if len(cr.secondary_response) > len(cr.primary_response):
                    cr.selected_response = cr.secondary_response
                    cr.selection_reason = "longer_response_picked"
                else:
                    cr.selected_response = cr.primary_response
                    cr.selection_reason = "primary_used_short_secondary"
        else:
            cr.selected_response = cr.primary_response
            cr.selection_reason = "secondary_failed"
    else:
        cr.selected_response = cr.primary_response
        cr.selection_reason = "primary_only"

    cr.success = True
    return cr


def execute_chunks(
    chunks: list[ChunkPlan],
    *,
    assembled_context: str,
    policy: PurposePolicy,
    max_workers: int = 4,
) -> list[ChunkResponse]:
    """Run all chunks in parallel. Returns one ChunkResponse per chunk,
    in input order (NOT completion order, so the compiler can keep the
    original chunk numbering for its prompt to the synthesizer).
    """
    if not chunks:
        return []
    primary_model, secondary_model = _resolve_models(policy)

    # Cap workers to avoid VRAM thrashing — Ollama is single-process and
    # serializes model loads when multiple models are needed concurrently.
    # 4 is a safe ceiling for 8GB VRAM with 3B-class models.
    workers = max(1, min(int(max_workers), len(chunks)))
    timeout = float(policy.chunk_timeout_sec or 30)

    results: list[Optional[ChunkResponse]] = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {}
        for plan in chunks:
            fut = ex.submit(
                _execute_one_chunk,
                plan,
                assembled_context=assembled_context,
                primary_model=primary_model,
                secondary_model=secondary_model,
                is_high_stakes=policy.high_stakes,
                timeout_sec=timeout,
            )
            futures[fut] = plan.index
        # Slightly more wall-clock than chunk timeout so we capture a
        # straggler if it just edges over the per-call deadline.
        wait_budget = timeout * 1.5
        for fut in as_completed(futures, timeout=wait_budget):
            try:
                cr = fut.result(timeout=2)
            except Exception as e:
                idx = futures[fut]
                results[idx] = ChunkResponse(
                    plan=chunks[idx],
                    success=False,
                    error=f"{type(e).__name__}: {e}",
                    selected_response="",
                    selection_reason="exception",
                    is_high_stakes=policy.high_stakes,
                )
                continue
            results[cr.plan.index] = cr

    # Fill any None slots with explicit failure rows so the compiler
    # gets a complete list (one entry per chunk plan).
    for i in range(len(chunks)):
        if results[i] is None:
            results[i] = ChunkResponse(
                plan=chunks[i],
                success=False,
                error="no_result_returned",
                selected_response="",
                selection_reason="missing",
                is_high_stakes=policy.high_stakes,
            )

    return [r for r in results if r is not None]
