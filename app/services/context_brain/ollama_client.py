"""Thin Ollama HTTP wrapper used by every local-LLM step in the pipeline
(decomposer, chunk executor, cross-examiner, compiler).

Why a separate wrapper instead of reusing ``app/openai_client.py``?
The openai_client module wraps the OpenAI-compatible cascade
(Groq → OpenAI → Gemini) and is heavy with retry / cost / weak-response
heuristics. The Context Brain's pipeline talks to Ollama specifically:

  * No cost
  * Fast local round-trips (<2s typical)
  * No auth / cascade / sticky-failure logic needed
  * Different timeouts (we'd rather kill+retry quickly than wait 60s)

Single-purpose client keeps the pipeline simple and testable.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# The chili web service inside Docker reaches Ollama via the service hostname.
# When the same code runs from a host shell (e.g. tests), env var lets us
# override.
_DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST") or "http://ollama:11434"
_FALLBACK_OLLAMA_HOSTS = (
    "http://127.0.0.1:11434",
    "http://localhost:11434",
    "http://ollama:11434",
)
_LAST_WORKING_OLLAMA_HOSTS: dict[str, str] = {}
_LAST_MODEL_LIST_HOST: Optional[str] = None


def _candidate_hosts(base_url: Optional[str], *, model: str = "") -> list[str]:
    if base_url:
        return [base_url]
    ordered = [
        _LAST_WORKING_OLLAMA_HOSTS.get(model),
        _LAST_MODEL_LIST_HOST,
        _DEFAULT_OLLAMA_HOST,
        *_FALLBACK_OLLAMA_HOSTS,
    ]
    hosts: list[str] = []
    for value in ordered:
        host = str(value or "").rstrip("/")
        if host and host not in hosts:
            hosts.append(host)
    return hosts


@dataclass
class OllamaResult:
    ok: bool
    text: str = ""
    model: str = ""
    tokens_out: int = 0
    latency_ms: int = 0
    error: Optional[str] = None
    raw: Optional[dict] = field(default=None, repr=False)


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return json.loads(body.decode("utf-8", errors="replace"))


def chat(
    messages: list[dict],
    model: str,
    *,
    temperature: float = 0.3,
    timeout_sec: float = 30.0,
    base_url: Optional[str] = None,
    options: Optional[dict] = None,
    think: Optional[bool] = None,
) -> OllamaResult:
    """One Ollama /api/chat call. Returns an OllamaResult, never raises.

    ``messages`` is a list of {"role": "system"|"user"|"assistant",
    "content": "..."} just like OpenAI. Ollama supports this shape natively.
    """
    request_options: dict[str, Any] = {"temperature": temperature}
    if options:
        for key, value in options.items():
            if key in {"keep_alive", "format"} or value is None:
                continue
            request_options[key] = value
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": request_options,
    }
    if options and options.get("keep_alive"):
        payload["keep_alive"] = str(options["keep_alive"])
    if options and options.get("format"):
        payload["format"] = str(options["format"])
    if think is not None:
        payload["think"] = bool(think)
    bases = _candidate_hosts(base_url, model=model)
    errors: list[str] = []
    t0 = time.monotonic()
    deadline = t0 + max(0.001, float(timeout_sec))
    body: dict[str, Any] | None = None
    for raw_base in bases:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append("total request deadline exhausted before host fallback")
            break
        base = raw_base.rstrip("/")
        url = f"{base}/api/chat"
        try:
            body = _post_json(url, payload, timeout=remaining)
            if base_url is None:
                _LAST_WORKING_OLLAMA_HOSTS[model] = base
            break
        except TimeoutError as e:
            errors.append(f"{base}: {type(e).__name__}: {e}")
            logger.warning(
                "[context_brain.ollama] reachable host timed out model=%s at %s: %s",
                model,
                base,
                e,
            )
            break
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:600]
            except Exception:
                err_body = str(e)
            errors.append(f"{base}: http_{e.code}: {err_body}")
            logger.warning(
                "[context_brain.ollama] HTTP %s for model=%s at %s: %s",
                e.code, model, base, err_body,
            )
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            errors.append(f"{base}: {type(e).__name__}: {e}")
            logger.warning(
                "[context_brain.ollama] call failed model=%s at %s: %s", model, base, e,
            )
    else:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return OllamaResult(
            ok=False,
            model=model,
            latency_ms=latency_ms,
            error="; ".join(errors) or "Ollama call failed",
        )

    if body is None:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return OllamaResult(
            ok=False,
            model=model,
            latency_ms=latency_ms,
            error="; ".join(errors) or "Ollama call failed",
        )

    latency_ms = int((time.monotonic() - t0) * 1000)
    msg = (body or {}).get("message") or {}
    text = (msg.get("content") or "").strip()
    # Ollama returns eval_count = output token count
    tokens_out = int(body.get("eval_count") or 0)
    return OllamaResult(
        ok=True,
        text=text,
        model=str(body.get("model") or model),
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        raw=body,
    )


def list_models(base_url: Optional[str] = None, timeout_sec: float = 5.0) -> list[str]:
    """Return list of locally-available model tags. Empty list on failure."""
    global _LAST_MODEL_LIST_HOST
    bases = _candidate_hosts(base_url)
    deadline = time.monotonic() + max(0.001, float(timeout_sec))
    for raw_base in bases:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        base = raw_base.rstrip("/")
        url = f"{base}/api/tags"
        try:
            with urllib.request.urlopen(url, timeout=remaining) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
            models = [str(m.get("name") or "") for m in (body.get("models") or []) if m.get("name")]
            if models:
                if base_url is None:
                    _LAST_MODEL_LIST_HOST = base
                    for model in models:
                        _LAST_WORKING_OLLAMA_HOSTS[model] = base
                return models
        except Exception:
            continue
    return []


def has_model(name: str, base_url: Optional[str] = None) -> bool:
    """Cheap check used by cross-examiner to skip dual-call when the
    secondary model isn't pulled."""
    return name in list_models(base_url=base_url)
