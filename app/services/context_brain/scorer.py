"""Relevance scorer.

Combines four signals to produce a final ``relevance_score`` for each
``ContextCandidate``:

  * ``raw_score`` — what the retriever itself returned (cosine,
    confidence, freshness — semantics vary per source, normalized
    to a rough [0, 1] range)
  * ``learned_weight`` — global per (intent, source_id) multiplier from
    ``learned_context_weights``. Defaults to 1.0 when no row yet.
  * ``recency_decay`` — for memory/chat_history-style sources where
    fresher = better. Read from candidate.metadata['age_seconds'] when
    present.
  * ``intent_match`` — 1.0 if the source is in the intent's preferred
    retriever set (per intent_router.RETRIEVER_PLAN), else 0.6.

Final score is a simple weighted sum, then clamped to [0, 1]. The
exact coefficients are tunable but kept small/integer-friendly so
operators can sanity-check the math by hand. The learning loop
adjusts ``learned_weight`` over time — the *coefficients* stay fixed.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .intent_router import retrievers_for
from .types import ContextCandidate, IntentClassification

logger = logging.getLogger(__name__)


# Coefficients for the weighted sum. Tuned conservatively so a
# strong raw_score from a retriever still wins even with a low
# learned_weight in early days.
_W_RAW = 0.50
_W_LEARNED = 0.25
_W_INTENT = 0.15
_W_RECENCY = 0.10


def _load_global_weights(db: Session, intent: str) -> dict[str, float]:
    """Returns {source_id -> weight} for the given intent.

    Missing entries default to 1.0 in the caller. We over-fetch for the
    intent (one round-trip) rather than per-candidate.
    """
    try:
        rows = db.execute(
            text(
                "SELECT source_id, weight FROM learned_context_weights "
                "WHERE intent = :i"
            ),
            {"i": intent},
        ).fetchall()
    except Exception as e:
        logger.debug("[context_brain.scorer] weight load failed: %s", e)
        return {}
    return {str(r[0]): float(r[1] or 1.0) for r in rows or []}


def _recency_score(candidate: ContextCandidate) -> float:
    """Read candidate.metadata['age_seconds'] if present and convert
    to a 0..1 score where fresh=1.0, very old≈0.5.
    """
    age = candidate.metadata.get("age_seconds")
    if age is None:
        return 1.0
    try:
        age = float(age)
    except (TypeError, ValueError):
        return 1.0
    # Half-life ~1 hour; capped at 0.5 so old context isn't fully zeroed
    half_life = 3600.0
    score = 0.5 + 0.5 * (2 ** (-age / half_life))
    return max(0.5, min(1.0, score))


def score_candidates(
    candidates: list[ContextCandidate],
    intent: IntentClassification,
    *,
    db: Optional[Session] = None,
) -> list[ContextCandidate]:
    """Mutates each candidate with ``relevance_score`` and ``final_weight``.

    Returns the same list sorted by relevance_score descending. Stable
    sort so retriever-order is preserved when scores tie.
    """
    weights = _load_global_weights(db, intent.intent) if db is not None else {}
    preferred = retrievers_for(intent.intent)

    for c in candidates:
        learned = float(weights.get(c.source_id, 1.0))
        intent_match = 1.0 if c.source_id in preferred else 0.6
        recency = _recency_score(c)
        # Clamp inputs into [0, 1] before weighting
        raw = max(0.0, min(1.0, float(c.raw_score)))
        learned_norm = max(0.0, min(2.0, learned))  # learned can boost up to 2x
        # Map learned (0..2) onto (0..1) for the weighted sum
        learned_for_sum = learned_norm / 2.0

        score = (
            _W_RAW * raw
            + _W_LEARNED * learned_for_sum
            + _W_INTENT * intent_match
            + _W_RECENCY * recency
        )
        c.relevance_score = round(min(1.0, max(0.0, score)), 5)
        c.final_weight = round(learned_norm, 5)

    candidates.sort(key=lambda x: x.relevance_score, reverse=True)
    return candidates
