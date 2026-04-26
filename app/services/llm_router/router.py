"""Tier-aware LLM routing.

Tiers:
  1 — Ollama local (chili-coder:current → qwen2.5-coder:14b-instruct-q8_0 fallback)
  2 — Groq free tier
  3 — OpenAI gpt-4o-mini
  4 — OpenAI gpt-4o or Anthropic claude-opus-4.6

Behavior:
  - Each call is wrapped in log_call().
  - Failures cascade to the next tier up to 4.
  - Weak responses (refusal/short reply) are detected and trigger escalation.
  - Cost-per-1k-token estimates are baked in for budget tracking.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from .log import log_call

logger = logging.getLogger(__name__)

# Cost per 1k tokens (input/output blended approximation for budget rough-cut).
_COST_PER_1K = {
    1: 0.0,        # local ollama
    2: 0.0,        # groq free tier
    3: 0.0002,     # gpt-4o-mini
    4: 0.005,      # gpt-4o / opus-4.6
}


@dataclass
class RouteResult:
    completion: str
    provider: str
    model: str
    tier_used: int
    success: bool
    weak_response: bool
    latency_ms: int
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    log_row_id: Optional[int]


def route_chat(
    *,
    purpose: str,
    user_prompt: str,
    system_prompt: Optional[str] = None,
    starting_tier: int = 1,
    max_tier: int = 4,
    cycle_id: Optional[int] = None,
    trace_id: Optional[str] = None,
) -> RouteResult:
    trace_id = trace_id or str(uuid.uuid4())
    last_failure: Optional[str] = None

    for tier in range(starting_tier, max_tier + 1):
        provider, model = _provider_for_tier(tier)
        t0 = time.monotonic()
        try:
            completion, tokens_in, tokens_out = _call_provider(
                provider=provider,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            weak = _is_weak_response(completion)
            success = bool(completion) and not weak
            cost = _estimate_cost(tier, tokens_in, tokens_out)
            row_id = log_call(
                trace_id=trace_id,
                cycle_id=cycle_id,
                provider=provider,
                model=model,
                tier=tier,
                purpose=purpose,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                completion=completion,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                cost_usd=cost,
                success=success,
                weak_response=weak,
                failure_kind=("weak" if weak else None) if not success else None,
                distillable=False,  # set by mark_validation_outcome later
            )
            if success:
                return RouteResult(
                    completion=completion,
                    provider=provider,
                    model=model,
                    tier_used=tier,
                    success=True,
                    weak_response=False,
                    latency_ms=latency_ms,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    log_row_id=row_id,
                )
            last_failure = "weak" if weak else "empty"
            continue
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            log_call(
                trace_id=trace_id,
                cycle_id=cycle_id,
                provider=provider,
                model=model,
                tier=tier,
                purpose=purpose,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                completion=None,
                tokens_in=None,
                tokens_out=None,
                latency_ms=latency_ms,
                cost_usd=0.0,
                success=False,
                weak_response=False,
                failure_kind=type(e).__name__,
            )
            last_failure = type(e).__name__
            continue

    return RouteResult(
        completion="",
        provider="none",
        model="none",
        tier_used=max_tier,
        success=False,
        weak_response=False,
        latency_ms=0,
        tokens_in=None,
        tokens_out=None,
        log_row_id=None,
    )


def _provider_for_tier(tier: int) -> tuple[str, str]:
    if tier == 1:
        # chili-coder:current is the tag the promotion gate atomically swaps
        # when distillation produces a winner. Until D.5 ships, point this env
        # at the base coder directly: e.g. qwen2.5-coder:14b-instruct-q8_0.
        # 8 GB VRAM (RTX 2070) friendly default: 3B-Q8 fits with 8k context
        # and leaves room for QLoRA fine-tuning. Bump to 14B-Q8 if you have
        # a 24 GB+ card.
        default = os.environ.get(
            "CHILI_LOCAL_CODER_BASE", "qwen2.5-coder:3b-instruct-q8_0"
        )
        return ("ollama", os.environ.get("CHILI_LOCAL_CODER_MODEL", default))
    if tier == 2:
        return ("groq", os.environ.get("CHILI_TIER2_MODEL", "llama-3.3-70b-versatile"))
    if tier == 3:
        return ("openai", os.environ.get("CHILI_TIER3_MODEL", "gpt-4o-mini"))
    return ("openai", os.environ.get("CHILI_TIER4_MODEL", "gpt-4o"))


def _call_provider(
    *,
    provider: str,
    model: str,
    system_prompt: Optional[str],
    user_prompt: str,
) -> tuple[str, Optional[int], Optional[int]]:
    """Returns (completion, tokens_in, tokens_out). Raise on transport error."""
    if provider == "ollama":
        from .ollama_client import chat as ollama_chat

        return ollama_chat(model=model, system=system_prompt, user=user_prompt)
    # For groq/openai, defer to the existing app.openai_client which already
    # has the credentials and rate-limit handling.
    from ...openai_client import chat_with_provider  # type: ignore

    return chat_with_provider(provider=provider, model=model, system=system_prompt, user=user_prompt)


def _is_weak_response(text: Optional[str]) -> bool:
    if not text:
        return True
    stripped = text.strip()
    if len(stripped) < 24:
        return True
    refusal_markers = (
        "i cannot",
        "i can't",
        "i'm unable",
        "as an ai",
        "i do not",
    )
    low = stripped.lower()
    return any(m in low[:120] for m in refusal_markers)


def _estimate_cost(tier: int, tokens_in: Optional[int], tokens_out: Optional[int]) -> float:
    rate = _COST_PER_1K.get(tier, 0.0)
    total = (tokens_in or 0) + (tokens_out or 0)
    return round((total / 1000.0) * rate, 6)
