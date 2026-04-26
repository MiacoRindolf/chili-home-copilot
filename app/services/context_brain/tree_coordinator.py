"""Top-level orchestrator for the F.10 tree pipeline.

Single public entry: :func:`run_tree`. The gateway calls this when
the resolved policy says ``routing_strategy='tree'``.

Stages, with all DB writes (decomposition_tree + decomposition_chunk
rows) happening at the END so a partial-failure tree doesn't leave
half-rows behind:

    decompose (Ollama)
      → execute_chunks (parallel Ollama, optional cross-exam)
      → compile (Ollama)
      → synthesize (gpt-5.5 — only paid call)

Failures degrade gracefully:
  * No chunks resolved → return raw user query (the gateway falls back
    to plain passthrough)
  * Compiler failure → plain concat of chunk responses
  * Synthesizer failure → return compiled context as the answer (tells
    the user we have the research but couldn't polish it)
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import chunk_executor as chunk_exec_mod
from . import compiler as compiler_mod
from . import decomposer as decomposer_mod
from . import synthesizer as synthesizer_mod
from .purpose_policy import default_synthesizer
from .tree_types import (
    DecompositionPlan,
    PurposePolicy,
    TreeOutcome,
)

logger = logging.getLogger(__name__)


def run_tree(
    user_query: str,
    *,
    db: Session,
    policy: PurposePolicy,
    assembled_context: str = "",
    chat_history: Optional[list[dict]] = None,
    user_name: str = "you",
    user_id: Optional[int] = None,
    trace_id: str = "context_tree",
) -> TreeOutcome:
    """Run the full tree pipeline. Returns a TreeOutcome with final_text
    plus per-stage metrics. Database rows are persisted (best-effort)
    just before return so the operator UI can inspect after the fact.
    """
    t0 = time.monotonic()
    outcome = TreeOutcome(final_text="")

    # ── Stage 1: Decompose ──────────────────────────────────────────────
    plan = decomposer_mod.decompose(
        user_query,
        db=db,
        model=policy.primary_local_model,
        max_chunks=policy.max_chunks,
        timeout_sec=12.0,
    )
    outcome.decomposition_strategy = plan.strategy
    outcome.decompose_latency_ms = plan.decompose_latency_ms
    outcome.ollama_calls_count += 1 if plan.strategy.startswith("llm_") or plan.strategy.endswith("_voted_no") else 0
    if plan.strategy == "ollama_unavailable_passthrough":
        # Decomposer's Ollama probe failed → degrade to passthrough single-chunk
        # but keep the pipeline going (chunk_executor will also fail gracefully)
        logger.info("[context_brain.tree] decomposer Ollama unavailable; using single-chunk fallback")

    # ── Stage 2: Execute chunks in parallel ─────────────────────────────
    t_chunks = time.monotonic()
    chunk_responses = chunk_exec_mod.execute_chunks(
        plan.chunks,
        assembled_context=assembled_context,
        policy=policy,
    )
    outcome.chunk_latency_ms = int((time.monotonic() - t_chunks) * 1000)
    outcome.chunks = chunk_responses
    for c in chunk_responses:
        if c.success:
            outcome.ollama_calls_count += 1
            outcome.ollama_total_tokens += int(c.primary_tokens_out or 0)
            if c.secondary_response:
                outcome.ollama_calls_count += 1
                outcome.ollama_total_tokens += int(c.secondary_tokens_out or 0)

    successful_chunks = [c for c in chunk_responses if c.success and c.selected_response]
    if not successful_chunks:
        outcome.success = False
        outcome.error = "no_chunks_resolved"
        outcome.total_latency_ms = int((time.monotonic() - t0) * 1000)
        outcome.final_text = ""
        _persist_tree_to_db(db, outcome, plan, user_query)
        return outcome

    # ── Stage 3: Compile chunks into one dense context document ────────
    compiled_text, compile_ms = compiler_mod.compile_chunks(
        user_query, chunk_responses,
        model=policy.primary_local_model,
        timeout_sec=25.0,
    )
    outcome.compiled_context = compiled_text
    outcome.compile_latency_ms = compile_ms
    if compile_ms > 0:
        outcome.ollama_calls_count += 1
        outcome.ollama_total_tokens += len(compiled_text) // 4

    # ── Stage 4: Premium synthesis (the ONLY paid call) ────────────────
    if policy.use_premium_synthesis:
        outcome.synthesizer_model = policy.synthesizer_model or default_synthesizer()
        outcome.used_synthesis = True
        synth_result, synth_ms = synthesizer_mod.synthesize(
            user_query=user_query,
            compiled_context=compiled_text,
            user_name=user_name,
            chat_history=chat_history,
            model=outcome.synthesizer_model,
            trace_id=trace_id,
        )
        outcome.synthesize_latency_ms = synth_ms
        outcome.premium_calls_count += 1
        outcome.premium_total_tokens += int(synth_result.get("tokens_used") or 0)
        outcome.final_text = (synth_result.get("reply") or "").strip()
        # If the synthesizer returned the empty error sentinel, fall back
        # to the compiled context so the user gets SOMETHING actionable.
        if not outcome.final_text or synth_result.get("model") == "error":
            outcome.final_text = compiled_text
            outcome.success = True
            outcome.error = "synthesizer_empty_used_compiled"
        else:
            outcome.success = True
    else:
        # Policy says skip the paid synth — return the compiled context
        # as the answer. (Useful for back-office / non-user-facing
        # purposes where Ollama-only is fine.)
        outcome.final_text = compiled_text
        outcome.success = True

    outcome.total_latency_ms = int((time.monotonic() - t0) * 1000)
    _persist_tree_to_db(db, outcome, plan, user_query)
    return outcome


def _persist_tree_to_db(
    db: Session,
    outcome: TreeOutcome,
    plan: DecompositionPlan,
    user_query: str,
) -> None:
    """Best-effort write of tree + chunk rows. NEVER raises so a DB
    write failure can't fail the chat turn."""
    try:
        # Caller (gateway) wires gateway_log_id onto outcome AFTER we
        # return — so we accept a None here and link later if needed.
        # But if the caller already wired one (not the case in current
        # code path), respect it.
        tree_row = db.execute(text(
            "INSERT INTO decomposition_tree "
            "(gateway_log_id, parent_query, chunk_count, chunks_resolved, "
            " chunks_failed, decomposition_strategy, decomposer_model, "
            " compiled_context_tokens) "
            "VALUES (:glid, :pq, :cc, :cr, :cf, :ds, :dm, :ctt) "
            "RETURNING id"
        ), {
            "glid": outcome.gateway_log_id,
            "pq": (user_query or "")[:8000],
            "cc": len(outcome.chunks),
            "cr": sum(1 for c in outcome.chunks if c.success),
            "cf": sum(1 for c in outcome.chunks if not c.success),
            "ds": plan.strategy,
            "dm": plan.decomposer_model,
            "ctt": len(outcome.compiled_context) // 4,
        }).fetchone()
        if not tree_row:
            db.commit()
            return
        outcome.tree_id = int(tree_row[0])

        for c in outcome.chunks:
            db.execute(text(
                "INSERT INTO decomposition_chunk "
                "(tree_id, chunk_index, chunk_query, chunk_kind, "
                " primary_model, primary_response, primary_tokens_out, primary_latency_ms, "
                " secondary_model, secondary_response, secondary_tokens_out, secondary_latency_ms, "
                " similarity_score, selected_response, selection_reason, "
                " is_high_stakes, success, error_message) "
                "VALUES (:tid, :i, :q, :k, :pm, :pr, :pt, :pl, "
                "        :sm, :sr, :st, :sl, :sim, :sel, :selr, "
                "        :hs, :ok, :err)"
            ), {
                "tid": outcome.tree_id,
                "i": int(c.plan.index),
                "q": (c.plan.query or "")[:4000],
                "k": c.plan.kind,
                "pm": c.primary_model,
                "pr": (c.primary_response or "")[:8000],
                "pt": int(c.primary_tokens_out or 0),
                "pl": int(c.primary_latency_ms or 0),
                "sm": c.secondary_model,
                "sr": (c.secondary_response or "")[:8000] if c.secondary_response else None,
                "st": int(c.secondary_tokens_out or 0),
                "sl": int(c.secondary_latency_ms or 0),
                "sim": c.similarity_score,
                "sel": (c.selected_response or "")[:8000],
                "selr": c.selection_reason,
                "hs": bool(c.is_high_stakes),
                "ok": bool(c.success),
                "err": (c.error or "")[:500] if c.error else None,
            })
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[context_brain.tree] persist failed: %s", e)
