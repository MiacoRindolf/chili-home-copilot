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
) -> OllamaResult:
    """One Ollama /api/chat call. Returns an OllamaResult, never raises.

    ``messages`` is a list of {"role": "system"|"user"|"assistant",
    "content": "..."} just like OpenAI. Ollama supports this shape natively.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            **({"num_predict": int(options["num_predict"])} if options and "num_predict" in options else {}),
        },
    }
    bases = [base_url or _DEFAULT_OLLAMA_HOST]
    if base_url is None:
        bases.extend(host for host in _FALLBACK_OLLAMA_HOSTS if host not in bases)
    last_error: str | None = None
    t0 = time.monotonic()
    for raw_base in bases:
        base = raw_base.rstrip("/")
        url = f"{base}/api/chat"
        try:
            body = _post_json(url, payload, timeout=timeout_sec)
            break
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:600]
            except Exception:
                err_body = str(e)
            last_error = f"http_{e.code}: {err_body}"
            logger.warning(
                "[context_brain.ollama] HTTP %s for model=%s at %s: %s",
                e.code, model, base, err_body,
            )
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "[context_brain.ollama] call failed model=%s at %s: %s", model, base, e,
            )
    else:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return OllamaResult(
            ok=False,
            model=model,
            latency_ms=latency_ms,
            error=last_error or "Ollama call failed",
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
    bases = [base_url or _DEFAULT_OLLAMA_HOST]
    bases.extend(host for host in _FALLBACK_OLLAMA_HOSTS if host not in bases)
    for raw_base in bases:
        base = raw_base.rstrip("/")
        url = f"{base}/api/tags"
        try:
            with urllib.request.urlopen(url, timeout=timeout_sec) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
            models = [str(m.get("name") or "") for m in (body.get("models") or []) if m.get("name")]
            if models:
                return models
        except Exception:
            continue
    return []


def has_model(name: str, base_url: Optional[str] = None) -> bool:
    """Cheap check used by cross-examiner to skip dual-call when the
    secondary model isn't pulled."""
    return name in list_models(base_url=base_url)
