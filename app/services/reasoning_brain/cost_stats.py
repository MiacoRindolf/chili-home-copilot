from __future__ import annotations

import copy
import json
import threading
from typing import Any

from ..llm_cost import approximate_tokens, estimate_cost_usd
from ..llm_material import stable_material_fingerprint

_REASONING_PURPOSES = (
    "reasoning_user_model",
    "reasoning_anticipate",
    "reasoning_proactive",
    "reasoning_evolve",
    "reasoning_web_research",
)
_REASONING_COUNTERS = (
    "fresh_cache_hits",
    "material_cache_hits",
    "material_cache_stores",
    "material_inflight_waits",
    "material_inflight_misses",
    "gateway_cache_hits",
    "llm_caller_cache_hits",
    "mechanical_hits",
    "llm_unconfigured",
    "llm_calls",
    "llm_success",
    "llm_empty",
    "llm_parse_empty",
    "llm_failures",
    "estimated_spend_usd",
    "estimated_saved_usd",
)
_REASONING_AMOUNT_COUNTERS = {"estimated_spend_usd", "estimated_saved_usd"}

_LOCK = threading.RLock()
_STATS: dict[str, dict[str, int | float]] = {
    purpose: {counter: 0 for counter in _REASONING_COUNTERS}
    for purpose in _REASONING_PURPOSES
}
_MATERIAL_REPLAY_CACHE: dict[str, dict[str, Any]] = {}
_MATERIAL_REPLAY_SAVED_USD: dict[str, float] = {}
_MATERIAL_INFLIGHT: dict[str, tuple[threading.Event, Any | None]] = {}
_MATERIAL_INFLIGHT_WAIT_SECONDS = 120.0
_MATERIAL_FINGERPRINT_SCHEMA = "chili.reasoning.material.v3"


def increment_reasoning_cost_stat(purpose: str, counter: str, amount: int = 1) -> None:
    with _LOCK:
        purpose_stats = _STATS.setdefault(
            purpose,
            {name: 0 for name in _REASONING_COUNTERS},
        )
        purpose_stats[counter] = int(purpose_stats.get(counter, 0)) + int(amount)


def add_reasoning_cost_usd(purpose: str, counter: str, amount: object) -> None:
    if counter not in _REASONING_AMOUNT_COUNTERS:
        return
    try:
        value = float(amount or 0.0)
    except (TypeError, ValueError):
        return
    if value <= 0:
        return
    with _LOCK:
        purpose_stats = _STATS.setdefault(
            purpose,
            {name: 0 for name in _REASONING_COUNTERS},
        )
        current = float(purpose_stats.get(counter, 0.0) or 0.0)
        purpose_stats[counter] = round(current + value, 10)


def estimated_reasoning_mechanical_saved_usd(purpose: str, payload: object | None = None) -> float:
    prompt_payload = {
        "purpose": purpose,
        "mechanical_payload": payload if payload is not None else {"source": "mechanical"},
    }
    prompt_text = json.dumps(prompt_payload, sort_keys=True, separators=(",", ":"), default=str)
    return estimate_cost_usd(
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=approximate_tokens("Reasoning Brain deterministic replacement for a paid model call.")
        + approximate_tokens(prompt_text),
        completion_tokens=128,
    )


def record_reasoning_mechanical_hit(purpose: str, payload: object | None = None) -> None:
    increment_reasoning_cost_stat(purpose, "mechanical_hits")
    add_reasoning_cost_usd(
        purpose,
        "estimated_saved_usd",
        estimated_reasoning_mechanical_saved_usd(purpose, payload),
    )


def reasoning_material_fingerprint(payload: object) -> str:
    return stable_material_fingerprint(schema=_MATERIAL_FINGERPRINT_SCHEMA, payload=payload)


def get_reasoning_material_replay(purpose: str, fingerprint: str) -> Any | None:
    key = f"{purpose}:{fingerprint}"
    with _LOCK:
        if key not in _MATERIAL_REPLAY_CACHE:
            return None
        increment_reasoning_cost_stat(purpose, "material_cache_hits")
        add_reasoning_cost_usd(purpose, "estimated_saved_usd", _MATERIAL_REPLAY_SAVED_USD.get(key))
        return copy.deepcopy(_MATERIAL_REPLAY_CACHE[key])


def begin_reasoning_material_inflight(purpose: str, fingerprint: str) -> tuple[threading.Event, bool]:
    key = f"{purpose}:{fingerprint}"
    with _LOCK:
        existing = _MATERIAL_INFLIGHT.get(key)
        if existing is not None:
            return existing[0], False
        event = threading.Event()
        _MATERIAL_INFLIGHT[key] = (event, None)
        return event, True


def finish_reasoning_material_inflight(purpose: str, fingerprint: str, value: Any | None) -> None:
    key = f"{purpose}:{fingerprint}"
    with _LOCK:
        existing = _MATERIAL_INFLIGHT.get(key)
        if existing is None:
            return
        event, _ = existing
        _MATERIAL_INFLIGHT[key] = (event, copy.deepcopy(value))
        _MATERIAL_INFLIGHT.pop(key, None)
        event.set()


def wait_reasoning_material_inflight(
    purpose: str,
    fingerprint: str,
    event: threading.Event,
) -> Any | None:
    if not event.wait(timeout=_MATERIAL_INFLIGHT_WAIT_SECONDS):
        increment_reasoning_cost_stat(purpose, "material_inflight_misses")
        return None
    key = f"{purpose}:{fingerprint}"
    with _LOCK:
        existing = _MATERIAL_INFLIGHT.get(key)
        value = existing[1] if existing is not None else _MATERIAL_REPLAY_CACHE.get(key)
        if value is None:
            increment_reasoning_cost_stat(purpose, "material_inflight_misses")
            return None
        increment_reasoning_cost_stat(purpose, "material_inflight_waits")
        add_reasoning_cost_usd(purpose, "estimated_saved_usd", _MATERIAL_REPLAY_SAVED_USD.get(key))
        return copy.deepcopy(value)


def set_reasoning_material_replay(
    purpose: str,
    fingerprint: str,
    value: Any,
    *,
    estimated_saved_usd: object = 0.0,
) -> None:
    key = f"{purpose}:{fingerprint}"
    with _LOCK:
        _MATERIAL_REPLAY_CACHE[key] = copy.deepcopy(value)
        try:
            saved = max(0.0, float(estimated_saved_usd or 0.0))
        except (TypeError, ValueError):
            saved = 0.0
        if saved:
            _MATERIAL_REPLAY_SAVED_USD[key] = saved
        else:
            _MATERIAL_REPLAY_SAVED_USD.pop(key, None)
        purpose_stats = _STATS.setdefault(
            purpose,
            {name: 0 for name in _REASONING_COUNTERS},
        )
        purpose_stats["material_cache_stores"] = int(purpose_stats.get("material_cache_stores", 0)) + 1


def record_reasoning_gateway_cost(purpose: str, result: object) -> None:
    if not isinstance(result, dict):
        return
    cache_status = str(result.get("cache_status") or "").lower()
    if "cache_hit" in cache_status or "coalesced" in cache_status:
        counter = "llm_caller_cache_hits" if cache_status.startswith("llm_caller_") else "gateway_cache_hits"
        increment_reasoning_cost_stat(purpose, counter)
        saved = _reasoning_saved_value(purpose, result)
        add_reasoning_cost_usd(purpose, "estimated_saved_usd", saved)
        return
    add_reasoning_cost_usd(purpose, "estimated_spend_usd", result.get("estimated_cost_usd"))
    add_reasoning_cost_usd(purpose, "estimated_saved_usd", result.get("estimated_saved_usd"))


def reasoning_gateway_replay_saved_usd(purpose: str, result: object) -> float:
    if isinstance(result, dict):
        return _reasoning_saved_value(purpose, result)
    return 0.0


def _reasoning_saved_value(purpose: str, result: dict[str, Any]) -> float:
    for key in ("estimated_saved_usd", "estimated_cost_usd"):
        try:
            value = max(0.0, float(result.get(key) or 0.0))
        except (TypeError, ValueError):
            value = 0.0
        if value:
            return value
    return estimated_reasoning_mechanical_saved_usd(purpose, {"source": "provider_cache"})


def reset_reasoning_cost_stats() -> None:
    with _LOCK:
        for purpose in list(_STATS):
            for counter in _REASONING_COUNTERS:
                _STATS[purpose][counter] = 0
        _MATERIAL_REPLAY_CACHE.clear()
        _MATERIAL_REPLAY_SAVED_USD.clear()
        _MATERIAL_INFLIGHT.clear()


def get_reasoning_cost_stats() -> dict[str, object]:
    with _LOCK:
        by_purpose = {
            purpose: dict(counters)
            for purpose, counters in _STATS.items()
        }

    totals: dict[str, int | float] = {
        counter: 0.0 if counter in _REASONING_AMOUNT_COUNTERS else 0
        for counter in _REASONING_COUNTERS
    }
    for counters in by_purpose.values():
        for counter in _REASONING_COUNTERS:
            if counter in _REASONING_AMOUNT_COUNTERS:
                totals[counter] = round(
                    float(totals.get(counter, 0.0) or 0.0) + float(counters.get(counter, 0.0) or 0.0),
                    10,
                )
            else:
                totals[counter] = int(totals.get(counter, 0)) + int(counters.get(counter, 0))

    def add_derived(counters: dict[str, int | float]) -> dict[str, int | float]:
        saved = (
            int(counters.get("fresh_cache_hits", 0))
            + int(counters.get("material_cache_hits", 0))
            + int(counters.get("material_inflight_waits", 0))
            + int(counters.get("gateway_cache_hits", 0))
            + int(counters.get("llm_caller_cache_hits", 0))
            + int(counters.get("mechanical_hits", 0))
            + int(counters.get("llm_unconfigured", 0))
        )
        total = saved + int(counters.get("llm_calls", 0))
        enriched: dict[str, int | float] = dict(counters)
        enriched["saved_responses"] = saved
        enriched["total_requests_observed"] = total
        enriched["avoidance_rate"] = round(saved / total, 4) if total else 0.0
        enriched["llm_call_rate"] = round(int(counters.get("llm_calls", 0)) / total, 4) if total else 0.0
        return enriched

    return {
        "by_purpose": {
            purpose: add_derived(counters)
            for purpose, counters in by_purpose.items()
        },
        **add_derived(totals),
    }
