"""Thin Ollama HTTP client for tier-1 calls.

Talks directly to the docker-compose ollama service on :11434 (env override
via OLLAMA_HOST). No streaming for now — keep it simple and capture the full
completion for logging.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests


_DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
_MODEL_LIST_CACHE_TTL_SEC = 5.0
_model_list_cache: tuple[float, list[str]] | None = None


def chat(
    *,
    model: str,
    system: Optional[str],
    user: str,
    timeout_s: float = 60.0,
) -> tuple[str, Optional[int], Optional[int]]:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    resp = requests.post(
        f"{_DEFAULT_HOST}/api/chat",
        json={"model": model, "messages": messages, "stream": False},
        timeout=timeout_s,
    )
    resp.raise_for_status()
    payload = resp.json()
    msg = (payload.get("message") or {}).get("content", "")
    tokens_in = payload.get("prompt_eval_count")
    tokens_out = payload.get("eval_count")
    return msg, tokens_in, tokens_out


def list_models() -> list[str]:
    global _model_list_cache
    now = time.monotonic()
    if _model_list_cache is not None:
        stored_at, models = _model_list_cache
        if now - stored_at <= _MODEL_LIST_CACHE_TTL_SEC:
            return list(models)
        _model_list_cache = None
    try:
        resp = requests.get(f"{_DEFAULT_HOST}/api/tags", timeout=10.0)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
        if models:
            _model_list_cache = (time.monotonic(), list(models))
        return models
    except Exception:
        return []


def reset_model_list_cache_for_tests() -> None:
    global _model_list_cache
    _model_list_cache = None


def has_model(name: str) -> bool:
    return name in list_models()
