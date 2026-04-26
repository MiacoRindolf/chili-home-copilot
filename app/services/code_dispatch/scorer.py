"""Tier scorer — picks which LLM tier to use for a given task.

Tiers (see CHILI_DISPATCH_AUTONOMOUS_DEV_PLAN.md §4.1):
  1 — local Ollama (chili-coder:current → qwen2.5-coder:7b fallback)
  2 — Groq free tier (llama-3.3-70b-versatile)
  3 — OpenAI gpt-4o-mini
  4 — OpenAI gpt-4o or Anthropic claude-opus-4.6 (premium)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .miner import Candidate


@dataclass
class TierChoice:
    tier: int
    reason: str
    complexity_score: float


def task_complexity_score(c: Candidate) -> float:
    """0.0 (trivial) to 1.0 (architectural)."""
    score = 0.0

    # Diff size component (clamped).
    score += min(c.estimated_diff_loc / 1500.0, 0.4)

    # File count component.
    score += min(len(c.intended_files) / 10.0, 0.2)

    # Frozen-scope adjacency: if the planner-supplied files live near
    # trading code, complexity goes up.
    sensitive_prefixes = (
        "app/services/trading",
        "app/trading_brain",
        "app/migrations",
        "scripts/start-",
    )
    if any(any(p.startswith(pref) for pref in sensitive_prefixes) for p in c.intended_files):
        score += 0.2

    # Prior failures imply this task is hard for cheaper tiers.
    score += min(c.prior_failure_count * 0.15, 0.3)

    return min(score, 1.0)


def choose_tier(c: Candidate, *, complexity: Optional[float] = None) -> TierChoice:
    """Map complexity → tier.

    On constrained-VRAM hardware (e.g. RTX 2070, 8 GB) we keep tier-1 narrow:
    only the cheapest, most isolated edits stay on the 3B local model. Anything
    above 0.20 complexity goes straight to tier 2 (Groq free tier, 70B class)
    rather than burning cycles on tier-1 retries. The retry-bumping logic still
    applies but the floor is higher.
    """
    if c.force_tier is not None:
        return TierChoice(tier=int(c.force_tier), reason="forced_by_operator", complexity_score=complexity or 0.0)

    cx = complexity if complexity is not None else task_complexity_score(c)

    base_tier = 1
    if cx >= 0.70:
        base_tier = 4
    elif cx >= 0.45:
        base_tier = 3
    elif cx >= 0.20:
        base_tier = 2

    tier = min(4, base_tier + min(c.prior_failure_count, 3))
    return TierChoice(tier=tier, reason=f"complexity={cx:.2f}+retries={c.prior_failure_count}", complexity_score=cx)
