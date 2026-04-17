"""Thin LLM wrapper for services that need LLM calls without importing chat_service.

Avoids circular dependencies: code_brain modules can import this instead of
chat_service, which itself imports code_brain at top level.

Adds an in-process LRU+TTL content-hash cache (Phase B, b3) that callers
opt in to via ``cacheable=True``. The cache is keyed by
``sha256(model|max_tokens|system|user_json)`` so identical prompts reuse
previous replies without another LLM call. Non-deterministic call sites
(user chat, personality, wellness, brain assistant) MUST NOT opt in.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


_cache_lock = threading.Lock()
_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_cache_stats = {"hits": 0, "misses": 0, "evictions": 0}


def _cache_config() -> tuple[int, int]:
    try:
        from ..config import settings
        max_entries = int(getattr(settings, "llm_cache_max_entries", 256) or 0)
        ttl_seconds = int(getattr(settings, "llm_cache_ttl_seconds", 600) or 0)
    except Exception:
        max_entries, ttl_seconds = 256, 600
    return max_entries, ttl_seconds


def _cache_key(messages: list[dict[str, Any]], max_tokens: int, system_prompt: str | None) -> str:
    """Deterministic content hash; prompt text drives the key, not the model name.

    (We intentionally don't include trace_id.)
    """
    try:
        from ..openai_client import (
            _free_tier_first_enabled,
            PAID_OPENAI_MODEL,
        )
        from ..config import settings as _cfg
        primary_model = (
            _cfg.llm_model if _free_tier_first_enabled() else PAID_OPENAI_MODEL
        )
    except Exception:
        primary_model = "llm"

    payload = {
        "m": primary_model,
        "t": int(max_tokens or 0),
        "s": system_prompt or "",
        "u": json.dumps(messages, sort_keys=True, default=str),
    }
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _cache_get(key: str) -> str | None:
    max_entries, ttl = _cache_config()
    if max_entries <= 0 or ttl <= 0:
        return None
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            _cache_stats["misses"] += 1
            return None
        expiry, reply = entry
        if expiry < now:
            _cache.pop(key, None)
            _cache_stats["misses"] += 1
            _cache_stats["evictions"] += 1
            return None
        _cache.move_to_end(key)
        _cache_stats["hits"] += 1
        return reply


def _cache_put(key: str, reply: str) -> None:
    max_entries, ttl = _cache_config()
    if max_entries <= 0 or ttl <= 0 or not reply:
        return
    # Jitter prevents synchronized mass expiry across parallel workers.
    expiry = time.monotonic() + ttl + random.uniform(0.0, max(1.0, ttl * 0.05))
    with _cache_lock:
        _cache[key] = (expiry, reply)
        _cache.move_to_end(key)
        while len(_cache) > max_entries:
            _cache.popitem(last=False)
            _cache_stats["evictions"] += 1


def get_cache_stats() -> dict[str, Any]:
    with _cache_lock:
        size = len(_cache)
        hits = _cache_stats["hits"]
        misses = _cache_stats["misses"]
        evictions = _cache_stats["evictions"]
    total = hits + misses
    hit_rate = (hits / total) if total else 0.0
    max_entries, ttl_seconds = _cache_config()
    return {
        "hits": hits,
        "misses": misses,
        "evictions": evictions,
        "size": size,
        "hit_rate": round(hit_rate, 4),
        "max_entries": max_entries,
        "ttl_seconds": ttl_seconds,
    }


def reset_cache() -> None:
    """Intended for tests only."""
    with _cache_lock:
        _cache.clear()
        _cache_stats["hits"] = 0
        _cache_stats["misses"] = 0
        _cache_stats["evictions"] = 0


def call_llm(
    messages: list[dict[str, Any]],
    max_tokens: int = 800,
    trace_id: str = "llm-caller",
    cacheable: bool = False,
    system_prompt: str | None = None,
) -> str:
    """Call the configured LLM and return the reply text (or empty string on failure).

    ``cacheable=True`` opts this call into the in-process LRU+TTL cache
    keyed by ``(model, max_tokens, system_prompt, messages)``. Use only
    for deterministic prompts — never for user chat, personality,
    wellness, or brain-assistant paths.
    """
    from ..openai_client import chat as llm_chat, is_configured

    if not is_configured():
        logger.debug("[llm_caller] LLM not configured")
        return ""

    cache_key = None
    if cacheable:
        try:
            cache_key = _cache_key(messages, max_tokens, system_prompt)
            cached = _cache_get(cache_key)
            if cached is not None:
                logger.debug("[llm_caller] cache_hit trace=%s key=%s", trace_id, cache_key[:12])
                return cached
        except Exception as e:
            logger.debug("[llm_caller] cache read failed: %s", e)
            cache_key = None

    try:
        chat_kwargs: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "trace_id": trace_id,
            "strict_escalation": False,
        }
        if system_prompt is not None:
            chat_kwargs["system_prompt"] = system_prompt
        result = llm_chat(**chat_kwargs)
        reply = result.get("reply", "")
        text = reply if isinstance(reply, str) else str(reply)
        if cacheable and cache_key is not None and text:
            try:
                _cache_put(cache_key, text)
            except Exception as e:
                logger.debug("[llm_caller] cache write failed: %s", e)
        return text
    except Exception as e:
        logger.warning("[llm_caller] LLM call failed: %s", e)
        return ""
