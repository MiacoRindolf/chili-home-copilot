"""Decompose a user query into N tiny focused sub-questions.

Two-pass design:

  1. **Heuristic gate** — most queries don't need decomposition. Short
     casual messages, single-clause asks, "yes"/"no" answers all pass
     through unsplit. Cheap regex/length heuristics decide.
  2. **LLM decomposer** — when the gate trips, ask Ollama (cheap, free,
     local) to break the query into 2-N tiny sub-questions, each
     answerable in 1-2 paragraphs. Returns a JSON array.

This module makes ZERO premium-LLM calls. All work is local Ollama.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from . import ollama_client
from .purpose_policy import default_primary_local
from .tree_types import (
    CHUNK_KIND_CODE,
    CHUNK_KIND_FACT,
    CHUNK_KIND_GENERAL,
    CHUNK_KIND_REASONING,
    ChunkPlan,
    DecompositionPlan,
)

logger = logging.getLogger(__name__)


# Heuristic indicators that a query is COMPLEX enough to benefit from
# decomposition. Tuned conservatively — false positives waste an Ollama
# call but never produce a worse final answer; false negatives just mean
# we miss an opportunity.
_COMPLEXITY_INDICATORS = [
    # Multiple questions
    r"\?[^?]{15,}\?",
    # Compound asks with explicit conjunctions
    r"\b(also|and then|after that|additionally|moreover|furthermore)\b",
    # Step / list indicators
    r"\b(step\s*\d|first[,:].*then|finally)\b",
    # "Compare X with Y" / "How do X and Y differ"
    r"\b(compare|contrast|versus|vs\.?|differences? between|how (do|does) .* differ)\b",
    # Multi-part instructions
    r"\b(analyze|explain|describe|review).+\band\b.+\b(suggest|recommend|propose)\b",
    # Long enough that there's likely structure
    r"^.{300,}$",
]
_COMPLEXITY_RE = re.compile("|".join(f"({p})" for p in _COMPLEXITY_INDICATORS), re.IGNORECASE | re.DOTALL)


def _heuristic_should_decompose(query: str) -> bool:
    q = (query or "").strip()
    if len(q) < 60:
        return False
    if _COMPLEXITY_RE.search(q):
        return True
    # Multi-sentence queries are candidates if at least one sentence is
    # itself substantive (>40 chars). Avoids splitting "Hi! How are you?"
    sentences = [s.strip() for s in re.split(r"[.!?]+", q) if s.strip()]
    if len(sentences) >= 3 and any(len(s) > 40 for s in sentences):
        return True
    return False


_DECOMPOSE_SYSTEM_PROMPT = """You are a query decomposer for an autonomous research system.

Given a user query, decide whether splitting it into smaller focused sub-questions would yield a higher-quality final answer than answering it whole.

If decomposition is NOT useful (the query is simple, single-purpose, or conversational), return:
{"decompose": false}

If decomposition IS useful, return a JSON object:
{
  "decompose": true,
  "chunks": [
    {"query": "first sub-question, focused and answerable in 1-2 paragraphs", "kind": "fact|reasoning|code|general"},
    ...
  ]
}

Rules:
* Maximum 6 chunks. Fewer is better. Aim for 2-4.
* Each chunk must be answerable INDEPENDENTLY — no chunk should refer to another by index.
* "kind" hints at retrieval style:
  - "fact"     for factual lookups
  - "reasoning" for analysis or judgment calls
  - "code"     for code-specific questions (file names, APIs, implementations)
  - "general"  for everything else
* Chunks should COVER the original query when answered together.
* Do NOT add chunks that aren't supported by the original query.
* Respond with ONLY the JSON object — no prose, no markdown fences.
"""


_KIND_ALIASES = {
    "fact": CHUNK_KIND_FACT,
    "factual": CHUNK_KIND_FACT,
    "reasoning": CHUNK_KIND_REASONING,
    "analysis": CHUNK_KIND_REASONING,
    "code": CHUNK_KIND_CODE,
    "general": CHUNK_KIND_GENERAL,
}


def _normalize_kind(raw: str) -> str:
    return _KIND_ALIASES.get((raw or "").strip().lower(), CHUNK_KIND_GENERAL)


def _parse_decomposer_json(text: str) -> Optional[dict]:
    """Robust JSON parser — strips code fences and extracts the first
    {...} block when the model surrounds output with prose."""
    if not text:
        return None
    s = text.strip()
    # Strip ```json fences if present
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # Find the first balanced top-level JSON object
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                blob = s[start:i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    return None
    return None


def decompose(
    query: str,
    *,
    db: Optional[Session] = None,
    model: Optional[str] = None,
    max_chunks: int = 6,
    timeout_sec: float = 12.0,
) -> DecompositionPlan:
    """Returns a DecompositionPlan. Always non-empty: when decomposition
    is skipped or fails, the plan contains a single chunk = the original
    query (so the chunk_executor still runs the full query through Ollama
    and produces grounded raw context).
    """
    q = (query or "").strip()
    plan = DecompositionPlan(strategy="heuristic_passthrough")
    if not q:
        return plan

    # 1. Heuristic gate. Short / single-purpose queries skip the decomposer
    #    LLM call entirely.
    if not _heuristic_should_decompose(q):
        plan.chunks = [ChunkPlan(index=0, query=q, kind=CHUNK_KIND_GENERAL)]
        return plan

    # 2. LLM decomposer
    chosen_model = model or default_primary_local()
    res = ollama_client.chat(
        messages=[
            {"role": "system", "content": _DECOMPOSE_SYSTEM_PROMPT},
            {"role": "user", "content": q},
        ],
        model=chosen_model,
        temperature=0.1,
        timeout_sec=timeout_sec,
    )
    plan.decomposer_model = chosen_model
    plan.decompose_latency_ms = res.latency_ms

    if not res.ok or not res.text:
        # Ollama unreachable or model unloaded → fall back to passthrough
        plan.strategy = "ollama_unavailable_passthrough"
        plan.chunks = [ChunkPlan(index=0, query=q, kind=CHUNK_KIND_GENERAL)]
        return plan

    parsed = _parse_decomposer_json(res.text)
    if not parsed or not parsed.get("decompose"):
        # Decomposer voted no, or output unparseable
        plan.strategy = "decomposer_voted_no" if parsed else "decomposer_unparseable"
        plan.chunks = [ChunkPlan(index=0, query=q, kind=CHUNK_KIND_GENERAL)]
        return plan

    raw_chunks = parsed.get("chunks") or []
    if not isinstance(raw_chunks, list):
        plan.strategy = "decomposer_bad_shape"
        plan.chunks = [ChunkPlan(index=0, query=q, kind=CHUNK_KIND_GENERAL)]
        return plan

    out: list[ChunkPlan] = []
    for i, c in enumerate(raw_chunks[:max_chunks]):
        if not isinstance(c, dict):
            continue
        cq = str(c.get("query") or "").strip()
        if not cq or len(cq) < 8:
            continue
        out.append(ChunkPlan(
            index=i,
            query=cq,
            kind=_normalize_kind(str(c.get("kind") or CHUNK_KIND_GENERAL)),
        ))

    if not out:
        plan.strategy = "decomposer_empty"
        plan.chunks = [ChunkPlan(index=0, query=q, kind=CHUNK_KIND_GENERAL)]
        return plan

    plan.strategy = "llm_decompose"
    plan.chunks = out
    logger.info(
        "[context_brain.decomposer] decomposed into %d chunks (model=%s, %dms): %s",
        len(out), chosen_model, res.latency_ms,
        [c.kind for c in out],
    )
    return plan
