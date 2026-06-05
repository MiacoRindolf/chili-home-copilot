"""Cost accounting for advisory trading LLM streams."""
from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.RLock()
_AMOUNT_KEYS = {"estimated_spend_usd", "estimated_saved_usd"}
_STATS: dict[str, Any] = {
    "gateway_stream_calls": 0,
    "gateway_stream_success": 0,
    "gateway_stream_failures": 0,
    "gateway_cache_hits": 0,
    "llm_caller_cache_hits": 0,
    "material_cache_hits": 0,
    "mechanical_responses": 0,
    "saved_responses": 0,
    "total_requests_observed": 0,
    "estimated_spend_usd": 0.0,
    "estimated_saved_usd": 0.0,
    "by_purpose": {},
}


def reset_trading_stream_llm_cost_stats() -> None:
    with _LOCK:
        for key, value in list(_STATS.items()):
            if isinstance(value, dict):
                value.clear()
            else:
                _STATS[key] = 0.0 if key in _AMOUNT_KEYS else 0


def _purpose_row(purpose: str) -> dict[str, int | float]:
    by_purpose = _STATS.setdefault("by_purpose", {})
    if not isinstance(by_purpose, dict):
        by_purpose = {}
        _STATS["by_purpose"] = by_purpose
    return by_purpose.setdefault(
        purpose,
        {
            "gateway_stream_calls": 0,
            "gateway_stream_success": 0,
            "gateway_stream_failures": 0,
            "gateway_cache_hits": 0,
            "llm_caller_cache_hits": 0,
            "material_cache_hits": 0,
            "mechanical_responses": 0,
            "saved_responses": 0,
            "total_requests_observed": 0,
            "estimated_spend_usd": 0.0,
            "estimated_saved_usd": 0.0,
        },
    )


def _add(purpose: str, key: str, amount: int | float = 1) -> None:
    _STATS[key] = (_STATS.get(key) or 0) + amount
    row = _purpose_row(purpose)
    row[key] = (row.get(key) or 0) + amount


def _amount(result: dict[str, Any], key: str) -> float:
    try:
        return max(0.0, float(result.get(key) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _stream_cache_counter(cache_status: str) -> str:
    return "llm_caller_cache_hits" if str(cache_status or "").lower().startswith("llm_caller_") else "gateway_cache_hits"


def record_stream_gateway_result(purpose: str, result: dict[str, Any]) -> None:
    cache_status = str(result.get("cache_status") or "").lower()
    is_cache_hit = "cache_hit" in cache_status or "coalesced" in cache_status
    cost = _amount(result, "estimated_cost_usd")
    saved = _amount(result, "estimated_saved_usd")
    if is_cache_hit:
        saved = saved or cost
        cost = 0.0

    with _LOCK:
        _add(purpose, "gateway_stream_calls")
        _add(purpose, "total_requests_observed")
        if result.get("reply"):
            _add(purpose, "gateway_stream_success")
        else:
            _add(purpose, "gateway_stream_failures")
        if is_cache_hit:
            _add(purpose, _stream_cache_counter(cache_status))
            _add(purpose, "saved_responses")
        if cost:
            _add(purpose, "estimated_spend_usd", cost)
        if saved:
            _add(purpose, "estimated_saved_usd", saved)


def record_stream_gateway_failure(purpose: str) -> None:
    with _LOCK:
        _add(purpose, "gateway_stream_calls")
        _add(purpose, "total_requests_observed")
        _add(purpose, "gateway_stream_failures")


def record_material_cache_hit(purpose: str, estimated_saved_usd: float = 0.0) -> None:
    saved = max(0.0, float(estimated_saved_usd or 0.0))
    with _LOCK:
        _add(purpose, "material_cache_hits")
        _add(purpose, "saved_responses")
        _add(purpose, "total_requests_observed")
        if saved:
            _add(purpose, "estimated_saved_usd", saved)


def record_mechanical_response(purpose: str, estimated_saved_usd: float = 0.0) -> None:
    saved = max(0.0, float(estimated_saved_usd or 0.0))
    with _LOCK:
        _add(purpose, "mechanical_responses")
        _add(purpose, "saved_responses")
        _add(purpose, "total_requests_observed")
        if saved:
            _add(purpose, "estimated_saved_usd", saved)


def get_trading_stream_llm_cost_stats() -> dict[str, Any]:
    with _LOCK:
        by_purpose = {
            str(purpose): dict(values)
            for purpose, values in (_STATS.get("by_purpose") or {}).items()
            if isinstance(values, dict)
        }
        saved = int(_STATS.get("saved_responses") or 0)
        observed = int(_STATS.get("total_requests_observed") or 0)
        return {
            **{key: value for key, value in _STATS.items() if key != "by_purpose"},
            "estimated_spend_usd": round(float(_STATS.get("estimated_spend_usd") or 0.0), 6),
            "estimated_saved_usd": round(float(_STATS.get("estimated_saved_usd") or 0.0), 6),
            "avoidance_rate": round(saved / observed, 4) if observed else 0.0,
            "by_purpose": by_purpose,
        }
