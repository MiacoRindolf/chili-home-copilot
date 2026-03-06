"""Tiered LLM API client for CHILI's general chat.

Primary model (free):  Groq Llama 3.3 70B (or any OpenAI-compatible API)
Premium model (paid):  OpenAI GPT-5.2 (or whatever PREMIUM_MODEL is set to)

Auto-escalation: if the primary model returns an empty, errored, or
refused response, the premium model is tried automatically.

Configure via env vars:
  LLM_API_KEY   / OPENAI_API_KEY     — Primary API key (e.g. Groq gsk_...)
  LLM_MODEL     / OPENAI_MODEL       — Primary model (default: llama-3.3-70b-versatile)
  LLM_BASE_URL  / OPENAI_BASE_URL    — Primary base URL (default: Groq)
  PREMIUM_API_KEY                     — Premium API key (e.g. OpenAI sk-...)
  PREMIUM_MODEL                      — Premium model (default: gpt-5.2)
  PREMIUM_BASE_URL                   — Premium base URL (default: OpenAI)
"""
import re
from openai import OpenAI

from .config import settings
from .logger import log_info
from .prompts import load_prompt

# Backward compat aliases for code that imports these
OPENAI_API_KEY = settings.primary_api_key
OPENAI_MODEL = settings.llm_model
LLM_API_KEY = settings.primary_api_key
LLM_MODEL = settings.llm_model
LLM_BASE_URL = settings.llm_base_url
PREMIUM_API_KEY = settings.premium_api_key
PREMIUM_MODEL = settings.premium_model
PREMIUM_BASE_URL = settings.premium_base_url

SYSTEM_PROMPT = load_prompt("system_base")

_REFUSAL_PATTERNS = re.compile(
    r"(?i)(i\s+can(?:'?t| ?not)\s+(?:help|assist|provide|answer|do that))"
    r"|(as an ai|i(?:'m| am) (?:just )?(?:a language model|an ai))"
    r"|(i\s+don(?:'?t| ?not)\s+(?:have (?:the )?(?:ability|capability|information)))"
    r"|((?:sorry|apologi[zs]e),?\s+(?:but )?i\s+(?:can(?:'?t| ?not)|am (?:not |un)able))"
)


def _is_weak_response(reply: str, user_message: str) -> bool:
    """Detect if a response should be escalated to the premium model."""
    if not reply or len(reply.strip()) < 20:
        return True
    if _REFUSAL_PATTERNS.search(reply):
        return True
    if len(user_message) > 100 and len(reply.strip()) < 60:
        return True
    return False


def is_configured() -> bool:
    return bool(settings.primary_api_key and settings.primary_api_key.strip())


def _premium_configured() -> bool:
    return bool(settings.premium_api_key and settings.premium_api_key.strip())


def _call_provider(api_key: str, base_url: str, model: str, messages: list[dict],
                   system_prompt: str, trace_id: str) -> dict:
    """Make a non-streaming call to an OpenAI-compatible API."""
    client = OpenAI(api_key=api_key, base_url=base_url)
    api_messages = [{"role": "system", "content": system_prompt}] + messages

    response = client.chat.completions.create(
        model=model,
        messages=api_messages,
        temperature=0.7,
        max_tokens=1024,
    )
    reply = response.choices[0].message.content.strip()
    tokens = response.usage.total_tokens if response.usage else 0
    log_info(trace_id, f"llm_reply model={model} tokens={tokens}")
    return {"reply": reply, "tokens_used": tokens, "model": model}


def _stream_provider(api_key: str, base_url: str, model: str, messages: list[dict],
                     system_prompt: str, trace_id: str):
    """Make a streaming call to an OpenAI-compatible API, yielding (token, model)."""
    client = OpenAI(api_key=api_key, base_url=base_url)
    api_messages = [{"role": "system", "content": system_prompt}] + messages

    stream = client.chat.completions.create(
        model=model,
        messages=api_messages,
        temperature=0.7,
        max_tokens=1024,
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            yield delta.content, model
    log_info(trace_id, f"llm_stream_complete model={model}")


def chat(
    messages: list[dict],
    system_prompt: str | None = None,
    trace_id: str = "llm",
    user_message: str = "",
) -> dict:
    """Chat with auto-escalation: primary model first, premium if response is weak."""
    prompt = system_prompt or SYSTEM_PROMPT

    if not is_configured():
        if _premium_configured():
            try:
                return _call_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                      messages, prompt, trace_id)
            except Exception as e:
                log_info(trace_id, f"premium_error={e}")
        return {"reply": "", "tokens_used": 0, "model": "none"}

    try:
        result = _call_provider(settings.primary_api_key, settings.llm_base_url, settings.llm_model, messages, prompt, trace_id)

        if _is_weak_response(result["reply"], user_message) and _premium_configured():
            log_info(trace_id, f"escalating to premium: primary reply was weak ({len(result['reply'])} chars)")
            try:
                premium_result = _call_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                                messages, prompt, trace_id)
                if premium_result["reply"]:
                    return premium_result
            except Exception as e:
                log_info(trace_id, f"premium_escalation_error={e}")

        return result

    except Exception as e:
        log_info(trace_id, f"primary_error={e}")
        if _premium_configured():
            try:
                log_info(trace_id, "primary failed, falling back to premium")
                return _call_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                      messages, prompt, trace_id)
            except Exception as e2:
                log_info(trace_id, f"premium_fallback_error={e2}")
        return {"reply": "", "tokens_used": 0, "model": "error"}


def chat_stream(
    messages: list[dict],
    system_prompt: str | None = None,
    trace_id: str = "llm-stream",
    user_message: str = "",
):
    """Stream tokens immediately (true streaming). No buffering for quality check;
    escalation to premium happens only on primary failure, not post-hoc."""
    prompt = system_prompt or SYSTEM_PROMPT

    if not is_configured():
        if _premium_configured():
            try:
                for tok, model in _stream_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                                   messages, prompt, trace_id):
                    yield tok, model
            except Exception as e:
                log_info(trace_id, f"premium_stream_error={e}")
        return

    try:
        for tok, model in _stream_provider(settings.primary_api_key, settings.llm_base_url, settings.llm_model,
                                           messages, prompt, trace_id):
            yield tok, model
    except Exception as e:
        log_info(trace_id, f"primary_stream_error={e}")
        if _premium_configured():
            try:
                log_info(trace_id, "primary stream failed, falling back to premium")
                for tok, model in _stream_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                                   messages, prompt, trace_id):
                    yield tok, model
            except Exception as e2:
                log_info(trace_id, f"premium_stream_fallback_error={e2}")
