"""Cost accounting wrapper for Project Brain agent LLM calls."""
from __future__ import annotations

import threading
import time
import re
import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ...llm_material import stable_material_fingerprint, stable_material_text
from ...llm_caller import call_llm as _call_llm

_PROJECT_AGENT_COST_LOCK = threading.Lock()
_PROJECT_AGENT_AMOUNT_KEYS = {"estimated_spend_usd", "estimated_saved_usd"}
_PROJECT_AGENT_STATS: dict[str, Any] = {
    "llm_calls": 0,
    "llm_success": 0,
    "llm_failures": 0,
    "material_cache_hits": 0,
    "material_cache_stores": 0,
    "material_cache_skip_writes": 0,
    "material_normalized_hits": 0,
    "material_inflight_waits": 0,
    "gateway_cache_hits": 0,
    "llm_caller_cache_hits": 0,
    "cache_cold_suppressed": 0,
    "mechanical_empty_material_skips": 0,
    "estimated_spend_usd": 0.0,
    "estimated_saved_usd": 0.0,
    "by_purpose": {},
}


@dataclass
class _MaterialEntry:
    expires_at: float
    result: dict[str, Any]
    saved_usd: float = 0.0


@dataclass
class _InflightMaterial:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


_MATERIAL_CACHE: "OrderedDict[str, _MaterialEntry]" = OrderedDict()
_MATERIAL_INFLIGHT: dict[str, _InflightMaterial] = {}
_MATERIAL_INFLIGHT_WAIT_SECONDS = 120.0
_SPECULATIVE_HELPER_TOKENS = (
    "prompt_suggestion",
    "promptsuggestion",
    "prompt-suggestion",
    "prompt suggestion",
    "autocomplete",
    "auto_complete",
    "auto-complete",
    "away_summary",
    "awaysummary",
    "away-summary",
    "away summary",
    "background_summary",
    "backgroundsummary",
    "background-summary",
    "background summary",
    "agent_summary",
    "agentsummary",
    "agent-summary",
    "agent summary",
    "progress_summary",
    "progresssummary",
    "progress-summary",
    "progress summary",
    "auto_compact",
    "autocompact",
    "auto-compact",
    "auto compact",
    "microcompact",
    "micro_compact",
    "micro-compact",
    "micro compact",
    "cachedeletion",
    "cache_deletion",
    "cache-deletion",
    "cache deletion",
    "extract_memories",
    "extractmemories",
    "extract-memories",
    "extract memories",
    "memory_extraction",
    "memoryextraction",
    "memory-extraction",
    "memory extraction",
    "session_memory",
    "sessionmemory",
    "session-memory",
    "session memory",
    "side_question",
    "sidequestion",
    "side-question",
    "side question",
    "query_context",
    "querycontext",
    "query-context",
    "query context",
    "forked_agent",
    "forkedagent",
    "forked-agent",
    "forked agent",
    "context_suggestion",
    "contextsuggestion",
    "context-suggestion",
    "context suggestion",
    "context_suggestions",
    "contextsuggestions",
    "context-suggestions",
    "context suggestions",
    "prompt_cache_break",
    "promptcachebreak",
    "prompt-cache-break",
    "prompt cache break",
)
_PROMPT_SUGGESTION_TOKENS = (
    "prompt_suggestion",
    "promptsuggestion",
    "prompt-suggestion",
    "prompt suggestion",
    "autocomplete",
    "auto_complete",
    "auto-complete",
    "auto complete",
)
_MAX_PARENT_UNCACHED_TOKENS = 10_000


def reset_project_agent_cost_stats() -> None:
    with _PROJECT_AGENT_COST_LOCK:
        _PROJECT_AGENT_STATS["llm_calls"] = 0
        _PROJECT_AGENT_STATS["llm_success"] = 0
        _PROJECT_AGENT_STATS["llm_failures"] = 0
        _PROJECT_AGENT_STATS["material_cache_hits"] = 0
        _PROJECT_AGENT_STATS["material_cache_stores"] = 0
        _PROJECT_AGENT_STATS["material_cache_skip_writes"] = 0
        _PROJECT_AGENT_STATS["material_normalized_hits"] = 0
        _PROJECT_AGENT_STATS["material_inflight_waits"] = 0
        _PROJECT_AGENT_STATS["gateway_cache_hits"] = 0
        _PROJECT_AGENT_STATS["llm_caller_cache_hits"] = 0
        _PROJECT_AGENT_STATS["cache_cold_suppressed"] = 0
        _PROJECT_AGENT_STATS["mechanical_empty_material_skips"] = 0
        _PROJECT_AGENT_STATS["estimated_spend_usd"] = 0.0
        _PROJECT_AGENT_STATS["estimated_saved_usd"] = 0.0
        _PROJECT_AGENT_STATS["by_purpose"] = {}
        _MATERIAL_CACHE.clear()
        _MATERIAL_INFLIGHT.clear()


def _purpose_stats(purpose: str) -> dict[str, int | float]:
    by_purpose = _PROJECT_AGENT_STATS.setdefault("by_purpose", {})
    return by_purpose.setdefault(
        purpose or "project_agent_unknown",
        {
            "llm_calls": 0,
            "llm_success": 0,
            "llm_failures": 0,
            "material_cache_hits": 0,
            "material_cache_stores": 0,
            "material_cache_skip_writes": 0,
            "material_normalized_hits": 0,
            "material_inflight_waits": 0,
            "gateway_cache_hits": 0,
            "llm_caller_cache_hits": 0,
            "cache_cold_suppressed": 0,
            "mechanical_empty_material_skips": 0,
            "estimated_spend_usd": 0.0,
            "estimated_saved_usd": 0.0,
        },
    )


def _add_count(purpose: str, key: str, amount: int = 1) -> None:
    with _PROJECT_AGENT_COST_LOCK:
        _PROJECT_AGENT_STATS[key] = int(_PROJECT_AGENT_STATS.get(key, 0)) + int(amount)
        row = _purpose_stats(purpose)
        row[key] = int(row.get(key, 0)) + int(amount)


def _add_amount(purpose: str, key: str, amount: Any) -> None:
    value = max(0.0, float(amount or 0.0))
    if value <= 0:
        return
    with _PROJECT_AGENT_COST_LOCK:
        _PROJECT_AGENT_STATS[key] = float(_PROJECT_AGENT_STATS.get(key, 0.0) or 0.0) + value
        row = _purpose_stats(purpose)
        row[key] = float(row.get(key, 0.0) or 0.0) + value


def _record_result(purpose: str, result: dict[str, Any]) -> str:
    reply = result.get("reply", "")
    text = reply if isinstance(reply, str) else str(reply)
    if text:
        _add_count(purpose, "llm_success")
    else:
        _add_count(purpose, "llm_failures")
    cache_status = str(result.get("cache_status") or "").lower()
    if "cache_hit" in cache_status or "coalesced" in cache_status:
        cache_counter = "llm_caller_cache_hits" if cache_status.startswith("llm_caller_") else "gateway_cache_hits"
        _add_count(purpose, cache_counter)
        _add_amount(purpose, "estimated_saved_usd", _saved_value(result))
    else:
        _add_amount(purpose, "estimated_spend_usd", result.get("estimated_cost_usd"))
        _add_amount(purpose, "estimated_saved_usd", result.get("estimated_saved_usd"))
    return text


def _result_amount(result: dict[str, Any], key: str) -> float:
    try:
        return max(0.0, float(result.get(key) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _material_cache_config() -> tuple[int, int]:
    try:
        from ....config import settings

        max_entries = int(getattr(settings, "llm_cache_max_entries", 256) or 0)
        ttl_seconds = int(getattr(settings, "llm_cache_ttl_seconds", 600) or 0)
    except Exception:
        max_entries, ttl_seconds = 256, 600
    return max(0, max_entries), max(0, ttl_seconds)


def _material_key(kwargs: dict[str, Any]) -> str | None:
    if not kwargs.get("cacheable"):
        return None
    try:
        payload = {
            "purpose": kwargs.get("purpose") or "project_agent_unknown",
            "model": kwargs.get("model") or "",
            "max_tokens": int(kwargs.get("max_tokens") or 0),
            "system_prompt": kwargs.get("system_prompt") or "",
            "messages": kwargs.get("messages") or [],
        }
    except Exception:
        return None
    return stable_material_fingerprint(schema="chili.project_agent.material.v4", payload=payload)


def _raw_material_key(kwargs: dict[str, Any]) -> str | None:
    if not kwargs.get("cacheable"):
        return None
    try:
        payload = {
            "purpose": kwargs.get("purpose") or "project_agent_unknown",
            "model": kwargs.get("model") or "",
            "max_tokens": int(kwargs.get("max_tokens") or 0),
            "system_prompt": kwargs.get("system_prompt") or "",
            "messages": kwargs.get("messages") or [],
        }
        encoded = json.dumps(
            {"schema": "chili.project_agent.material.raw.v1", "material": payload},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        return None
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalized_material_key_hit(raw_key: str | None, material_key: str | None) -> bool:
    return bool(raw_key and material_key and raw_key != material_key)


def _looks_like_speculative_helper_call(kwargs: dict[str, Any]) -> bool:
    haystack = _helper_haystack(kwargs)
    return any(token.replace("-", "_").replace(" ", "_") in haystack for token in _SPECULATIVE_HELPER_TOKENS)


def _looks_like_prompt_suggestion_call(kwargs: dict[str, Any]) -> bool:
    haystack = _helper_haystack(kwargs)
    return any(token.replace("-", "_").replace(" ", "_") in haystack for token in _PROMPT_SUGGESTION_TOKENS)


def _helper_haystack(kwargs: dict[str, Any]) -> str:
    parts: list[str] = [
        str(kwargs.get("purpose") or ""),
        str(kwargs.get("trace_id") or ""),
        str(kwargs.get("system_prompt") or ""),
    ]
    try:
        for message in kwargs.get("messages") or []:
            if not isinstance(message, dict):
                parts.append(str(message))
                continue
            parts.append(str(message.get("role") or ""))
            parts.append(str(message.get("content") or ""))
    except Exception:
        pass
    return re.sub(r"[\s-]+", "_", " ".join(parts).lower())


def _is_in_progress_assistant_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    role = str(message.get("role") or message.get("type") or "").lower()
    if role != "assistant":
        return False
    if "stop_reason" in message and message.get("stop_reason") is None:
        return True
    nested = message.get("message")
    return isinstance(nested, dict) and "stop_reason" in nested and nested.get("stop_reason") is None


def _strip_in_progress_assistant_tail(messages: Any) -> Any:
    if isinstance(messages, list) and any(_is_in_progress_assistant_message(message) for message in messages):
        return [
            message
            for message in messages
            if not _is_in_progress_assistant_message(message)
        ]
    return messages


def _stable_project_agent_messages(messages: Any) -> Any:
    if not isinstance(messages, list):
        return messages
    stable_messages: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            stable_messages.append(stable_material_text(str(message)))
            continue
        clean = dict(message)
        if "content" in clean:
            content = clean.get("content")
            if isinstance(content, str):
                clean["content"] = stable_material_text(content).strip()
            elif isinstance(content, (dict, list)):
                clean["content"] = stable_material_text(content).strip()
        stable_messages.append(clean)
    return stable_messages


def _stable_project_agent_system_prompt(system_prompt: Any) -> Any:
    if isinstance(system_prompt, str):
        return stable_material_text(system_prompt).strip()
    if isinstance(system_prompt, (dict, list)):
        return stable_material_text(system_prompt).strip()
    return system_prompt


def _has_material_message_content(messages: Any) -> bool:
    stable_messages = _stable_project_agent_messages(messages)
    if not isinstance(stable_messages, list):
        return bool(stable_material_text(stable_messages).strip())
    for message in stable_messages:
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return True
            if isinstance(content, (dict, list)) and stable_material_text(content).strip():
                return True
            continue
        if stable_material_text(message).strip():
            return True
    return False


def _usage_uncached_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0

    def _int_value(*names: str) -> int:
        for name in names:
            try:
                value = int(float(usage.get(name) or 0))
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
        return 0

    input_tokens = _int_value("input_tokens", "prompt_tokens")
    cache_write_tokens = _int_value("cache_creation_input_tokens", "cache_write_tokens")
    output_tokens = _int_value("output_tokens", "completion_tokens")
    cached_tokens = _int_value("cached_tokens", "cache_read_input_tokens")
    return max(0, input_tokens - cached_tokens) + max(0, cache_write_tokens) + max(0, output_tokens)


def _parent_uncached_tokens(kwargs: dict[str, Any]) -> int:
    for key in ("parent_uncached_tokens", "parent_uncached_token_count"):
        try:
            explicit = int(float(kwargs.get(key) or 0))
        except (TypeError, ValueError):
            explicit = 0
        if explicit > 0:
            return explicit
    usage_tokens = _usage_uncached_tokens(kwargs.get("parent_usage"))
    if usage_tokens > 0:
        return usage_tokens
    try:
        for message in reversed(kwargs.get("messages") or []):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or message.get("type") or "").lower()
            if role != "assistant":
                continue
            usage_tokens = _usage_uncached_tokens(message.get("usage"))
            if usage_tokens > 0:
                return usage_tokens
    except Exception:
        pass
    return 0


def _parent_cache_is_cold(kwargs: dict[str, Any]) -> bool:
    cold_values = {"cache_cold", "cold", "cache_miss", "miss", "uncached"}
    for key in (
        "parent_cache_status",
        "parent_cache_reason",
        "parent_cache_suppress_reason",
        "parent_cache_state",
    ):
        value = str(kwargs.get(key) or "").strip().lower()
        if value in cold_values:
            return True
    return False


def _should_suppress_cache_cold_prompt_suggestion(kwargs: dict[str, Any]) -> bool:
    return (
        _looks_like_prompt_suggestion_call(kwargs)
        and (
            _parent_uncached_tokens(kwargs) > _MAX_PARENT_UNCACHED_TOKENS
            or _parent_cache_is_cold(kwargs)
        )
    )


def _saved_value(result: dict[str, Any]) -> float:
    return max(
        _result_amount(result, "estimated_cost_usd"),
        _result_amount(result, "estimated_saved_usd"),
    )


def _cache_result_for_replay(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    out["estimated_saved_usd"] = _saved_value(result)
    out["estimated_cost_usd"] = 0.0
    out["cache_status"] = "project_agent_material_cache_hit"
    return out


def _material_get(purpose: str, key: str | None, *, normalized_hit: bool = False) -> dict[str, Any] | None:
    if not key:
        return None
    max_entries, ttl_seconds = _material_cache_config()
    if max_entries <= 0 or ttl_seconds <= 0:
        return None
    now = time.monotonic()
    with _PROJECT_AGENT_COST_LOCK:
        entry = _MATERIAL_CACHE.get(key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            _MATERIAL_CACHE.pop(key, None)
            return None
        _MATERIAL_CACHE.move_to_end(key)
    _add_count(purpose, "material_cache_hits")
    if normalized_hit:
        _add_count(purpose, "material_normalized_hits")
    _add_amount(purpose, "estimated_saved_usd", entry.saved_usd)
    return _cache_result_for_replay(entry.result)


def _material_put(purpose: str, key: str | None, result: dict[str, Any]) -> None:
    if not key or not result.get("reply"):
        return
    max_entries, ttl_seconds = _material_cache_config()
    if max_entries <= 0 or ttl_seconds <= 0:
        return
    saved_usd = _saved_value(result)
    expires_at = time.monotonic() + ttl_seconds
    with _PROJECT_AGENT_COST_LOCK:
        _MATERIAL_CACHE[key] = _MaterialEntry(expires_at=expires_at, result=dict(result), saved_usd=saved_usd)
        _MATERIAL_CACHE.move_to_end(key)
        while len(_MATERIAL_CACHE) > max_entries:
            _MATERIAL_CACHE.popitem(last=False)
    _add_count(purpose, "material_cache_stores")


def _material_skip_write(purpose: str, key: str | None) -> None:
    if not key:
        return
    _add_count(purpose, "material_cache_skip_writes")


def _material_inflight_begin(key: str | None) -> tuple[_InflightMaterial | None, bool]:
    if not key:
        return None, False
    with _PROJECT_AGENT_COST_LOCK:
        existing = _MATERIAL_INFLIGHT.get(key)
        if existing is not None:
            return existing, False
        call = _InflightMaterial()
        _MATERIAL_INFLIGHT[key] = call
        return call, True


def _material_inflight_finish(key: str | None, result: dict[str, Any] | None) -> None:
    if not key:
        return
    with _PROJECT_AGENT_COST_LOCK:
        call = _MATERIAL_INFLIGHT.pop(key, None)
        if call is None:
            return
        call.result = dict(result or {})
        call.event.set()


def _material_inflight_wait(
    purpose: str,
    key: str | None,
    call: _InflightMaterial | None,
    *,
    normalized_hit: bool = False,
) -> dict[str, Any] | None:
    if not key or call is None:
        return None
    if not call.event.wait(timeout=_MATERIAL_INFLIGHT_WAIT_SECONDS):
        return None
    if not call.result or not call.result.get("reply"):
        return None
    replay = _cache_result_for_replay(call.result)
    _add_count(purpose, "material_inflight_waits")
    if normalized_hit:
        _add_count(purpose, "material_normalized_hits")
    _add_amount(purpose, "estimated_saved_usd", replay.get("estimated_saved_usd"))
    return replay


def _return_agent_result(result: dict[str, Any], caller_requested_meta: bool) -> str | dict[str, Any]:
    return result if caller_requested_meta else str(result.get("reply") or "")


def _cache_cold_suppressed_result(purpose: str, caller_requested_meta: bool) -> str | dict[str, Any]:
    _add_count(purpose, "cache_cold_suppressed")
    result = {
        "reply": "",
        "cache_status": "project_agent_cache_cold_suppressed",
        "suppression_reason": "cache_cold",
        "estimated_cost_usd": 0.0,
        "estimated_saved_usd": 0.0,
    }
    return _return_agent_result(result, caller_requested_meta)


def _empty_material_suppressed_result(purpose: str, caller_requested_meta: bool) -> str | dict[str, Any]:
    _add_count(purpose, "mechanical_empty_material_skips")
    result = {
        "reply": "",
        "cache_status": "project_agent_empty_material_skip",
        "suppression_reason": "empty_material",
        "estimated_cost_usd": 0.0,
        "estimated_saved_usd": 0.0,
    }
    return _return_agent_result(result, caller_requested_meta)


def call_agent_llm(*args: Any, **kwargs: Any) -> str | dict[str, Any]:
    """Call the shared LLM gateway and account for Project Brain agent cost.

    The agents historically expect a plain reply string. This wrapper preserves
    that API while requesting metadata from the gateway so cache hits and saved
    spend are visible in aggregate telemetry.
    """
    purpose = str(kwargs.get("purpose") or "project_agent_unknown")
    caller_requested_meta = bool(kwargs.get("return_meta"))
    explicit_skip_cache_write = bool(kwargs.get("skip_cache_write"))
    if _should_suppress_cache_cold_prompt_suggestion(kwargs):
        return _cache_cold_suppressed_result(purpose, caller_requested_meta)
    speculative_helper = _looks_like_speculative_helper_call(kwargs)
    if speculative_helper:
        kwargs["messages"] = _strip_in_progress_assistant_tail(kwargs.get("messages"))
    if speculative_helper and "cacheable" not in kwargs:
        kwargs["cacheable"] = True
    if kwargs.get("cacheable") and not _has_material_message_content(kwargs.get("messages")):
        return _empty_material_suppressed_result(purpose, caller_requested_meta)
    raw_material_key = _raw_material_key(kwargs)
    if kwargs.get("cacheable"):
        kwargs["messages"] = _stable_project_agent_messages(kwargs.get("messages"))
        if "system_prompt" in kwargs:
            kwargs["system_prompt"] = _stable_project_agent_system_prompt(kwargs.get("system_prompt"))
    material_key = _material_key(kwargs)
    normalized_hit = _normalized_material_key_hit(raw_material_key, material_key)
    skip_material_write = explicit_skip_cache_write and not speculative_helper
    if explicit_skip_cache_write or speculative_helper:
        kwargs["skip_cache_write"] = True
    cached = _material_get(purpose, material_key, normalized_hit=normalized_hit)
    if cached is not None:
        return _return_agent_result(cached, caller_requested_meta)

    inflight, inflight_owner = _material_inflight_begin(material_key)
    if not inflight_owner:
        replay = _material_inflight_wait(purpose, material_key, inflight, normalized_hit=normalized_hit)
        if replay is not None:
            return _return_agent_result(replay, caller_requested_meta)

    kwargs["return_meta"] = True
    _add_count(purpose, "llm_calls")
    result: Any = {"reply": ""}
    try:
        result = _call_llm(*args, **kwargs)
        if isinstance(result, dict):
            text = _record_result(purpose, result)
            if text:
                if skip_material_write:
                    _material_skip_write(purpose, material_key)
                else:
                    _material_put(purpose, material_key, result)
            return result if caller_requested_meta else text
        text = result if isinstance(result, str) else str(result or "")
        if text:
            _add_count(purpose, "llm_success")
            if skip_material_write:
                _material_skip_write(purpose, material_key)
            else:
                _material_put(purpose, material_key, {"reply": text})
        else:
            _add_count(purpose, "llm_failures")
        return result if caller_requested_meta else text
    finally:
        _material_inflight_finish(
            material_key,
            result if isinstance(result, dict) else {"reply": result if isinstance(result, str) else ""},
        )


def get_project_agent_cost_stats() -> dict[str, Any]:
    with _PROJECT_AGENT_COST_LOCK:
        by_purpose = {}
        for purpose, row in dict(_PROJECT_AGENT_STATS.get("by_purpose") or {}).items():
            enriched = {
                key: (round(float(value), 6) if key in _PROJECT_AGENT_AMOUNT_KEYS else int(value))
                for key, value in row.items()
            }
            saved = (
                int(enriched.get("material_cache_hits") or 0)
                + int(enriched.get("material_inflight_waits") or 0)
                + int(enriched.get("gateway_cache_hits") or 0)
                + int(enriched.get("llm_caller_cache_hits") or 0)
                + int(enriched.get("cache_cold_suppressed") or 0)
                + int(enriched.get("mechanical_empty_material_skips") or 0)
            )
            total_observed = (
                int(enriched.get("llm_calls") or 0)
                + int(enriched.get("material_cache_hits") or 0)
                + int(enriched.get("material_inflight_waits") or 0)
                + int(enriched.get("cache_cold_suppressed") or 0)
                + int(enriched.get("mechanical_empty_material_skips") or 0)
            )
            enriched["saved_responses"] = saved
            enriched["total_requests_observed"] = total_observed
            enriched["avoidance_rate"] = round(saved / max(1, total_observed), 4) if total_observed else 0.0
            by_purpose[purpose] = enriched
        return {
            "llm_calls": int(_PROJECT_AGENT_STATS["llm_calls"]),
            "llm_success": int(_PROJECT_AGENT_STATS["llm_success"]),
            "llm_failures": int(_PROJECT_AGENT_STATS["llm_failures"]),
            "material_cache_hits": int(_PROJECT_AGENT_STATS["material_cache_hits"]),
            "material_cache_stores": int(_PROJECT_AGENT_STATS["material_cache_stores"]),
            "material_cache_skip_writes": int(_PROJECT_AGENT_STATS["material_cache_skip_writes"]),
            "material_normalized_hits": int(_PROJECT_AGENT_STATS["material_normalized_hits"]),
            "material_inflight_waits": int(_PROJECT_AGENT_STATS["material_inflight_waits"]),
            "gateway_cache_hits": int(_PROJECT_AGENT_STATS["gateway_cache_hits"]),
            "llm_caller_cache_hits": int(_PROJECT_AGENT_STATS["llm_caller_cache_hits"]),
            "cache_cold_suppressed": int(_PROJECT_AGENT_STATS["cache_cold_suppressed"]),
            "mechanical_empty_material_skips": int(_PROJECT_AGENT_STATS["mechanical_empty_material_skips"]),
            "estimated_spend_usd": round(float(_PROJECT_AGENT_STATS["estimated_spend_usd"]), 6),
            "estimated_saved_usd": round(float(_PROJECT_AGENT_STATS["estimated_saved_usd"]), 6),
            "saved_responses": int(_PROJECT_AGENT_STATS["material_cache_hits"])
            + int(_PROJECT_AGENT_STATS["material_inflight_waits"])
            + int(_PROJECT_AGENT_STATS["gateway_cache_hits"])
            + int(_PROJECT_AGENT_STATS["llm_caller_cache_hits"])
            + int(_PROJECT_AGENT_STATS["cache_cold_suppressed"])
            + int(_PROJECT_AGENT_STATS["mechanical_empty_material_skips"]),
            "total_requests_observed": int(_PROJECT_AGENT_STATS["llm_calls"])
            + int(_PROJECT_AGENT_STATS["material_cache_hits"])
            + int(_PROJECT_AGENT_STATS["material_inflight_waits"])
            + int(_PROJECT_AGENT_STATS["cache_cold_suppressed"])
            + int(_PROJECT_AGENT_STATS["mechanical_empty_material_skips"]),
            "avoidance_rate": round(
                (
                    (
                        int(_PROJECT_AGENT_STATS["material_cache_hits"])
                        + int(_PROJECT_AGENT_STATS["material_inflight_waits"])
                        + int(_PROJECT_AGENT_STATS["gateway_cache_hits"])
                        + int(_PROJECT_AGENT_STATS["llm_caller_cache_hits"])
                        + int(_PROJECT_AGENT_STATS["cache_cold_suppressed"])
                        + int(_PROJECT_AGENT_STATS["mechanical_empty_material_skips"])
                    )
                    / max(
                        1,
                        int(_PROJECT_AGENT_STATS["llm_calls"])
                        + int(_PROJECT_AGENT_STATS["material_cache_hits"])
                        + int(_PROJECT_AGENT_STATS["material_inflight_waits"])
                        + int(_PROJECT_AGENT_STATS["cache_cold_suppressed"])
                        + int(_PROJECT_AGENT_STATS["mechanical_empty_material_skips"]),
                    )
                ),
                4,
            )
            if (
                int(_PROJECT_AGENT_STATS["llm_calls"])
                + int(_PROJECT_AGENT_STATS["material_cache_hits"])
                + int(_PROJECT_AGENT_STATS["material_inflight_waits"])
                + int(_PROJECT_AGENT_STATS["cache_cold_suppressed"])
                + int(_PROJECT_AGENT_STATS["mechanical_empty_material_skips"])
            )
            else 0.0,
            "material_cache_size": len(_MATERIAL_CACHE),
            "by_purpose": by_purpose,
        }
