"""LLM provider labeling, token accounting, and rough cost estimates.

The estimates are intentionally conservative operator telemetry. Billing is
still the provider's source of truth, but this module gives CHILI enough local
signal to see hot paths and enforce a daily paid-model budget when requested.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    service_tier: str | None = None


@dataclass(frozen=True)
class ModelPricing:
    input_per_million: Decimal
    cached_input_per_million: Decimal
    output_per_million: Decimal


_ZERO = Decimal("0")


_OPENAI_PRICING: tuple[tuple[str, ModelPricing], ...] = (
    (
        "gpt-5.5",
        ModelPricing(
            input_per_million=Decimal("5.00"),
            cached_input_per_million=Decimal("0.50"),
            output_per_million=Decimal("30.00"),
        ),
    ),
    (
        "gpt-5.4-mini",
        ModelPricing(
            input_per_million=Decimal("0.75"),
            cached_input_per_million=Decimal("0.075"),
            output_per_million=Decimal("4.50"),
        ),
    ),
    (
        "gpt-5.4-nano",
        ModelPricing(
            input_per_million=Decimal("0.20"),
            cached_input_per_million=Decimal("0.020"),
            output_per_million=Decimal("1.25"),
        ),
    ),
    (
        "gpt-4o-mini",
        ModelPricing(
            input_per_million=Decimal("0.15"),
            cached_input_per_million=Decimal("0.075"),
            output_per_million=Decimal("0.60"),
        ),
    ),
    (
        "gpt-4o",
        ModelPricing(
            input_per_million=Decimal("2.50"),
            cached_input_per_million=Decimal("1.25"),
            output_per_million=Decimal("10.00"),
        ),
    ),
)


def provider_from_base_url(base_url: str | None) -> str:
    """Resolve provider from the actual endpoint, not the config slot name."""
    u = (base_url or "").strip().lower()
    if "api.openai.com" in u or "openai.azure.com" in u:
        return "openai"
    if "groq.com" in u:
        return "groq"
    if "generativelanguage.googleapis.com" in u:
        return "gemini"
    if "localhost" in u or "127.0.0.1" in u or ":11434" in u:
        return "ollama"
    return "other"


def pricing_for_model(model: str | None) -> ModelPricing | None:
    m = (model or "").strip().lower()
    for prefix, pricing in _OPENAI_PRICING:
        if m.startswith(prefix):
            return pricing
    return None


def _usage_detail_int(details: Any, key: str) -> int:
    if details is None:
        return 0
    value = None
    if isinstance(details, dict):
        value = details.get(key)
    else:
        value = getattr(details, key, None)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def usage_from_response(response: Any) -> LLMUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return LLMUsage(service_tier=getattr(response, "service_tier", None))

    def _field(name: str) -> int:
        try:
            return int(getattr(usage, name, 0) or 0)
        except (TypeError, ValueError):
            return 0

    prompt_tokens = _field("prompt_tokens")
    completion_tokens = _field("completion_tokens")
    total_tokens = _field("total_tokens") or (prompt_tokens + completion_tokens)
    cached_tokens = _usage_detail_int(
        getattr(usage, "prompt_tokens_details", None),
        "cached_tokens",
    )
    reasoning_tokens = _usage_detail_int(
        getattr(usage, "completion_tokens_details", None),
        "reasoning_tokens",
    )
    return LLMUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        service_tier=getattr(response, "service_tier", None),
    )


def estimate_cost_usd(
    *,
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
) -> float:
    """Estimate provider cost in USD.

    Only OpenAI paid-model pricing is estimated here. Free-tier Groq, local
    Ollama, and non-OpenAI-compatible unknowns return 0.0.
    """
    if provider != "openai":
        return 0.0
    pricing = pricing_for_model(model)
    if pricing is None:
        return 0.0
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    cached = min(prompt, max(0, int(cached_tokens or 0)))
    uncached = max(0, prompt - cached)
    cost = (
        (Decimal(uncached) * pricing.input_per_million)
        + (Decimal(cached) * pricing.cached_input_per_million)
        + (Decimal(completion) * pricing.output_per_million)
    ) / Decimal(1_000_000)
    return float(cost.quantize(Decimal("0.000001")))


def approximate_tokens(text: str | None) -> int:
    """Cheap token approximation for streaming paths with no usage object."""
    if not text:
        return 0
    return max(1, len(text) // 4)
