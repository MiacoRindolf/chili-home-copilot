"""Top-level orchestrator for the Context Brain.

Single entry point: :func:`assemble_context`. The chat hot path
calls this once per turn. It runs the full pipeline:

    intent_router → retrievers (parallel) → scorer → budget → composer

…and returns an ``AssembledContext`` ready to feed into ``openai_client.chat()``.

Designed to be resilient: any exception in the pipeline triggers a
fallback path that returns a minimal AssembledContext built from
the raw user message only — chat never breaks because the brain
errored.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from sqlalchemy.orm import Session

from . import budget as budget_mod
from . import composer as composer_mod
from . import intent_router
from . import retrievers as retrievers_mod
from . import runtime_state as runtime_state_mod
from . import scorer as scorer_mod
from .types import AssembledContext, ContextCandidate, IntentClassification

logger = logging.getLogger(__name__)


# Match the existing chat_service.gather_context_parallel() worker count so
# we don't suddenly explode parallelism (Postgres pool is only 25/50).
_MAX_WORKERS = 4


def _retriever_safe(
    src_id: str,
    fn,
    message: str,
    db: Session,
    user_id: Optional[int],
    project_id: Optional[int],
    trace_id: str,
    conversation_id: Optional[int],
) -> list[ContextCandidate]:
    """Wrapper that ensures one bad retriever can't tank the others."""
    try:
        # chat_history takes an extra kwarg; everything else ignores it
        if src_id == "chat_history":
            return fn(
                message,
                db=db,
                user_id=user_id,
                project_id=project_id,
                trace_id=trace_id,
                conversation_id=conversation_id,
            )
        return fn(message, db=db, user_id=user_id, project_id=project_id, trace_id=trace_id)
    except Exception as e:
        logger.debug("[context_brain.assembly] retriever %s raised: %s", src_id, e)
        return []


def assemble_context(
    user_message: str,
    *,
    db: Session,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    chat_message_id: Optional[int] = None,
    trace_id: str = "context_brain",
) -> AssembledContext:
    """Run the full Context Brain pipeline. Always returns an
    AssembledContext (even on failure — falls back to bare prompt).
    """
    t0 = time.monotonic()

    # Defensive: read runtime state for budget. Default to 8K if anything
    # blows up loading it.
    try:
        state = runtime_state_mod.get_state(db)
        budget_tokens = int(state.token_budget_per_request)
        strategy_version = int(state.learned_strategy_version)
        if state.mode == "paused":
            return _bare_fallback(user_message, "context_brain paused")
    except Exception as e:
        logger.warning("[context_brain.assembly] runtime_state read failed: %s", e)
        budget_tokens = 8000
        strategy_version = 1

    # 1. Intent classification (heuristic, no LLM)
    try:
        intent = intent_router.classify_intent(user_message, db=db, user_id=user_id)
    except Exception as e:
        logger.warning("[context_brain.assembly] classify_intent failed: %s", e)
        intent = IntentClassification(intent="casual", confidence=0.5, signals=["error"])

    selected_sources = intent_router.retrievers_for(intent.intent)

    # 2. Parallel retrieval
    candidates: list[ContextCandidate] = []
    try:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            futures = {}
            for src_id in selected_sources:
                fn = retrievers_mod.REGISTRY.get(src_id)
                if fn is None:
                    continue
                fut = ex.submit(
                    _retriever_safe,
                    src_id, fn, user_message, db, user_id, project_id, trace_id,
                    conversation_id,
                )
                futures[fut] = src_id
            for fut in as_completed(futures, timeout=20):
                try:
                    result = fut.result(timeout=2)
                except Exception as e:
                    logger.debug(
                        "[context_brain.assembly] retriever %s timed out: %s",
                        futures[fut], e,
                    )
                    continue
                candidates.extend(result)
    except Exception as e:
        logger.warning("[context_brain.assembly] parallel retrieval failed: %s", e)
        # We still try to compose with whatever we got

    # 3. Score candidates
    try:
        candidates = scorer_mod.score_candidates(candidates, intent, db=db)
    except Exception as e:
        logger.warning("[context_brain.assembly] scoring failed: %s", e)

    # 4. Apply token budget
    try:
        candidates = budget_mod.apply_budget(candidates, budget_tokens=budget_tokens)
    except Exception as e:
        logger.warning("[context_brain.assembly] budgeting failed: %s", e)

    total_selected = budget_mod.total_selected_tokens(candidates)

    # 5. Compose final prompt
    try:
        prompt_text = composer_mod.compose_prompt(user_message, intent, candidates)
    except Exception as e:
        logger.warning("[context_brain.assembly] compose failed: %s", e)
        prompt_text = user_message

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # 6. Persist assembly log (best-effort)
    assembly_id = composer_mod.write_assembly_log(
        db,
        user_message=user_message,
        intent=intent,
        candidates=candidates,
        total_tokens=total_selected,
        budget_token_cap=budget_tokens,
        strategy_version=strategy_version,
        elapsed_ms=elapsed_ms,
        chat_message_id=chat_message_id,
        user_id=user_id,
    )

    logger.info(
        "[context_brain.assembly] intent=%s conf=%.2f cands=%d sel=%d tokens=%d/%d (%.0f%%) "
        "elapsed=%dms assembly_id=%s",
        intent.intent, intent.confidence,
        len(candidates),
        sum(1 for c in candidates if c.selected),
        total_selected, budget_tokens,
        100.0 * total_selected / max(1, budget_tokens),
        elapsed_ms, assembly_id,
    )

    return AssembledContext(
        prompt_text=prompt_text,
        intent=intent,
        candidates=candidates,
        total_tokens=total_selected,
        budget_token_cap=budget_tokens,
        strategy_version=strategy_version,
        assembly_id=assembly_id,
        elapsed_ms=elapsed_ms,
    )


def _bare_fallback(user_message: str, reason: str) -> AssembledContext:
    """Minimal context that just echoes the user message — chat must
    work even when the brain decides to bail."""
    return AssembledContext(
        prompt_text=user_message,
        intent=IntentClassification(intent="casual", confidence=0.0, signals=[reason]),
        candidates=[],
        total_tokens=0,
        budget_token_cap=0,
        strategy_version=0,
    )
