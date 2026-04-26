"""CHILI Context Brain (Phase F) — TurboQuant-style adaptive context.

Third domain brain in CHILI. Owns the LLM context-assembly pipeline:

    intent_router  →  retrievers (parallel)  →  scorer  →  budget
                                                              │
                                                              ▼
                                  feedback ←  composer  ←  distiller

Each chat turn flows through this pipeline. Every assembly is logged
to ``context_assembly_log``, every retrieved candidate to
``context_candidate_log``, and the resulting LLM response's outcome
to ``context_outcome_log``. A 6-hour learning loop reads those rows
and updates ``learned_context_weights`` so the brain gets *better*
at picking which sources matter for which intents over time.

The architecture mirrors the trading and code brains: deterministic
gates first, LLM-assisted distillation only when needed, mechanical
graduation as the long-term goal.

Design choices (validated 2026-04-26 with operator):
  * Global learning (single weight set across users) for fast convergence
  * gpt-5-nano as v1 distiller (cheap; replace with local Qwen later)
  * 8K-token default budget per assembly
  * Heuristic intent classification v1 (no LLM); learned classifier later
  * Always-on (no feature flag) but with safe fallback to legacy code
    path on any exception
"""
from __future__ import annotations

__all__ = [
    "types",
    "runtime_state",
    "intent_router",
    "scorer",
    "budget",
    "composer",
    "assembly",
]
