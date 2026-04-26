"""Token budget enforcer.

Greedy knapsack: walk candidates in score order, take each one whose
inclusion would keep the running token total under the cap AND keep
its source's allocation under that source's per-source cap.

We do this in two passes:
  1. Greedy by ``relevance_score`` (scored already)
  2. If still under budget by 30%+, second pass picks lower-scored
     candidates that fit (avoids leaving capacity on the table)

Per-source caps come from ``DEFAULT_SOURCE_CAPS`` in types.py. They
exist to prevent any single retriever from monopolizing the prompt
even when its candidates score very highly.
"""
from __future__ import annotations

import logging
from typing import Optional

from .types import DEFAULT_SOURCE_CAPS, ContextCandidate

logger = logging.getLogger(__name__)


def apply_budget(
    candidates: list[ContextCandidate],
    *,
    budget_tokens: int,
    source_caps: Optional[dict[str, float]] = None,
) -> list[ContextCandidate]:
    """Mutates each candidate's ``selected`` flag in place. Returns the
    same list (still sorted by relevance) so callers can pass through.
    """
    caps = dict(source_caps or DEFAULT_SOURCE_CAPS)

    # Per-source token allocation already used
    used_per_source: dict[str, int] = {}
    total_used = 0

    # Pass 1: greedy by relevance score
    for c in candidates:
        if c.tokens_estimated <= 0:
            continue
        if total_used + c.tokens_estimated > budget_tokens:
            continue
        cap_frac = caps.get(c.source_id, 1.0)
        cap_tokens = int(budget_tokens * cap_frac)
        if used_per_source.get(c.source_id, 0) + c.tokens_estimated > cap_tokens:
            continue
        c.selected = True
        used_per_source[c.source_id] = used_per_source.get(c.source_id, 0) + c.tokens_estimated
        total_used += c.tokens_estimated

    # Pass 2: if we have meaningful slack, try lower-scored candidates that fit
    slack = budget_tokens - total_used
    if slack > budget_tokens * 0.30:
        for c in candidates:
            if c.selected:
                continue
            if c.tokens_estimated <= 0 or c.tokens_estimated > slack:
                continue
            cap_frac = caps.get(c.source_id, 1.0)
            cap_tokens = int(budget_tokens * cap_frac)
            if used_per_source.get(c.source_id, 0) + c.tokens_estimated > cap_tokens:
                continue
            c.selected = True
            used_per_source[c.source_id] = used_per_source.get(c.source_id, 0) + c.tokens_estimated
            total_used += c.tokens_estimated
            slack = budget_tokens - total_used
            if slack <= 0:
                break

    logger.debug(
        "[context_brain.budget] %d/%d candidates selected, %d/%d tokens used (%.1f%%)",
        sum(1 for c in candidates if c.selected),
        len(candidates),
        total_used,
        budget_tokens,
        100.0 * total_used / max(1, budget_tokens),
    )
    return candidates


def total_selected_tokens(candidates: list[ContextCandidate]) -> int:
    return sum(c.tokens_estimated for c in candidates if c.selected)
