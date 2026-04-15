"""Tiered LLM API client for CHILI's general chat.

Cascade (when keys are set):
  1. OpenAI (api.openai.com) — OPENAI_API_KEY, model gpt-4o-mini
  2. Groq primary: LLM_API_KEY + LLM_BASE_URL + LLM_MODEL (default Llama 3.3 70B)
  3. Groq secondary: same key, llama-3.1-8b-instant (separate rate limit)
  4. Google Gemini (PREMIUM_* OpenAI-compatible endpoint)

Groq daily usage is tracked; near the free-tier limit the primary Groq model is skipped.

Configure via env vars:
  OPENAI_API_KEY       — Paid OpenAI; tried first when set
  LLM_API_KEY          — Groq (or other OpenAI-compat) for tiers 2–3
  LLM_MODEL / LLM_BASE_URL — Groq defaults if unset
  PREMIUM_API_KEY / PREMIUM_MODEL / PREMIUM_BASE_URL — Gemini fallback
"""
import re
import time
import threading
from datetime import date
from openai import OpenAI, RateLimitError

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

# OpenAI (official API — first tier when OPENAI_API_KEY is set)
PAID_OPENAI_API_KEY = settings.openai_api_key
PAID_OPENAI_MODEL = "gpt-4o-mini"
PAID_OPENAI_BASE_URL = "https://api.openai.com/v1"

SYSTEM_PROMPT = load_prompt("system_base")

_SECONDARY_MODEL = "llama-3.1-8b-instant"

_MAX_RETRIES = 2
_RETRY_DELAYS = [1.0, 3.0]

_token_budget_lock = threading.Lock()
_daily_tokens: dict[str, int] = {"date": "", "used": 0}
_DAILY_TOKEN_LIMIT = 85000  # preemptive threshold (Groq free = 100K TPD)


def _track_tokens(count: int) -> None:
    """Track daily Groq token usage."""
    today = date.today().isoformat()
    with _token_budget_lock:
        if _daily_tokens["date"] != today:
            _daily_tokens["date"] = today
            _daily_tokens["used"] = 0
        _daily_tokens["used"] += count


def _near_daily_limit() -> bool:
    """Check if approaching the daily Groq token limit."""
    today = date.today().isoformat()
    with _token_budget_lock:
        if _daily_tokens["date"] != today:
            return False
        return _daily_tokens["used"] >= _DAILY_TOKEN_LIMIT


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
    """True if any primary-style key exists (LLM_API_KEY or OPENAI_API_KEY)."""
    return bool(settings.primary_api_key and settings.primary_api_key.strip())


def _openai_official_configured() -> bool:
    """OPENAI_API_KEY set — use api.openai.com first."""
    return bool(settings.openai_api_key and settings.openai_api_key.strip())


def _groq_stack_configured() -> bool:
    """LLM_API_KEY set — Groq (or custom) stack; never mix with OPENAI_API_KEY on wrong host."""
    return bool(settings.llm_api_key and settings.llm_api_key.strip())


def _premium_configured() -> bool:
    return bool(settings.premium_api_key and settings.premium_api_key.strip())


def _token_param(base_url: str, max_tokens: int) -> dict:
    if "openai.com" in base_url:
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def _call_provider(api_key: str, base_url: str, model: str, messages: list[dict],
                   system_prompt: str, trace_id: str,
                   max_tokens: int = 1024, timeout_override: float | None = None) -> dict:
    """Make a non-streaming call with automatic retry on rate limits."""
    timeout = timeout_override or (30.0 if "groq.com" in base_url else 60.0)
    if timeout_override is None and max_tokens > 4000:
        timeout = max(timeout, 120.0)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    api_messages = [{"role": "system", "content": system_prompt}] + messages

    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=api_messages,
                temperature=0.7,
                **_token_param(base_url, max_tokens),
            )
            raw = response.choices[0].message.content
            reply = (raw or "").strip()
            tokens = response.usage.total_tokens if response.usage else 0
            log_info(trace_id, f"llm_reply model={model} tokens={tokens}")
            if "groq.com" in base_url:
                _track_tokens(tokens)
            return {"reply": reply, "tokens_used": tokens, "model": model}
        except RateLimitError:
            if attempt < _MAX_RETRIES:
                delay = _RETRY_DELAYS[attempt]
                log_info(trace_id, f"rate_limited model={model} attempt={attempt+1} retry_in={delay}s")
                time.sleep(delay)
                continue
            raise


def _stream_provider(api_key: str, base_url: str, model: str, messages: list[dict],
                     system_prompt: str, trace_id: str,
                     max_tokens: int = 1024):
    """Make a streaming call with automatic retry on rate limits."""
    timeout = 45.0 if "groq.com" in base_url else 90.0
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    api_messages = [{"role": "system", "content": system_prompt}] + messages

    stream = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=api_messages,
                temperature=0.7,
                **_token_param(base_url, max_tokens),
                stream=True,
            )
            break
        except RateLimitError:
            if attempt < _MAX_RETRIES:
                delay = _RETRY_DELAYS[attempt]
                log_info(trace_id, f"rate_limited_stream model={model} attempt={attempt+1} retry_in={delay}s")
                time.sleep(delay)
                continue
            raise

    for chunk in stream:
        if not chunk.choices:
            continue
        ch0 = chunk.choices[0]
        delta = getattr(ch0, "delta", None)
        if delta and getattr(delta, "content", None):
            yield delta.content, model
        else:
            fr = getattr(ch0, "finish_reason", None)
            if fr and fr not in (None, "stop"):
                log_info(trace_id, f"llm_stream_chunk model={model} finish_reason={fr}")
    log_info(trace_id, f"llm_stream_complete model={model}")


def chat(
    messages: list[dict],
    system_prompt: str | None = None,
    trace_id: str = "llm",
    user_message: str = "",
    max_tokens: int = 1024,
) -> dict:
    """Chat: OpenAI (if OPENAI_API_KEY) → Groq stack (if LLM_API_KEY) → Gemini."""
    prompt = system_prompt or SYSTEM_PROMPT

    # Tier 1: OpenAI official API
    if _openai_official_configured():
        try:
            result = _call_provider(PAID_OPENAI_API_KEY, PAID_OPENAI_BASE_URL, PAID_OPENAI_MODEL,
                                    messages, prompt, trace_id, max_tokens=max_tokens)
            if not _is_weak_response(result["reply"], user_message):
                return result
            log_info(trace_id, f"openai_primary weak ({len(result['reply'])} chars), trying groq")
        except Exception as e:
            log_info(trace_id, f"openai_primary_error={e}")

    # Tier 2–3: Groq (LLM_API_KEY only)
    if _groq_stack_configured():
        if not _near_daily_limit():
            try:
                result = _call_provider(settings.llm_api_key, settings.llm_base_url, settings.llm_model,
                                        messages, prompt, trace_id, max_tokens=max_tokens)
                if not _is_weak_response(result["reply"], user_message):
                    return result
                log_info(trace_id, f"primary reply weak ({len(result['reply'])} chars), trying secondary")
            except Exception as e:
                log_info(trace_id, f"primary_error={e}")
        else:
            log_info(trace_id, f"primary groq skipped (daily tokens ~{_daily_tokens['used']})")

        try:
            result = _call_provider(settings.llm_api_key, settings.llm_base_url, _SECONDARY_MODEL,
                                    messages, prompt, trace_id, max_tokens=max_tokens)
            if result["reply"]:
                return result
        except Exception as e:
            log_info(trace_id, f"secondary_error={e}")

    # Tier 4: Gemini
    if _premium_configured():
        try:
            log_info(trace_id, "falling back to gemini (free)")
            return _call_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                  messages, prompt, trace_id, max_tokens=max_tokens)
        except Exception as e2:
            log_info(trace_id, f"gemini_fallback_error={e2}")

    return {"reply": "", "tokens_used": 0, "model": "error"}


def chat_stream(
    messages: list[dict],
    system_prompt: str | None = None,
    trace_id: str = "llm-stream",
    user_message: str = "",
    max_tokens: int = 1024,
):
    """Stream: OpenAI → Groq primary/secondary → Gemini."""
    prompt = system_prompt or SYSTEM_PROMPT

    # Tier 1: OpenAI official API
    if _openai_official_configured():
        try:
            _got = False
            for tok, model in _stream_provider(PAID_OPENAI_API_KEY, PAID_OPENAI_BASE_URL, PAID_OPENAI_MODEL,
                                               messages, prompt, trace_id, max_tokens=max_tokens):
                _got = True
                yield tok, model
            if _got:
                return
            log_info(trace_id, "openai_stream yielded no tokens; trying groq")
        except Exception as e:
            log_info(trace_id, f"openai_primary_stream_error={e}")

    # Tier 2–3: Groq (LLM_API_KEY only)
    if _groq_stack_configured():
        if not _near_daily_limit():
            try:
                _got = False
                for tok, model in _stream_provider(settings.llm_api_key, settings.llm_base_url, settings.llm_model,
                                                   messages, prompt, trace_id, max_tokens=max_tokens):
                    _got = True
                    yield tok, model
                if _got:
                    return
                log_info(trace_id, "primary_stream yielded no tokens; trying secondary")
            except Exception as e:
                log_info(trace_id, f"primary_stream_error={e}")
        else:
            log_info(trace_id, f"primary stream skipped (daily tokens ~{_daily_tokens['used']})")

        try:
            _got = False
            for tok, model in _stream_provider(settings.llm_api_key, settings.llm_base_url, _SECONDARY_MODEL,
                                               messages, prompt, trace_id, max_tokens=max_tokens):
                _got = True
                yield tok, model
            if _got:
                return
            log_info(trace_id, "secondary_stream yielded no tokens; trying gemini")
        except Exception as e:
            log_info(trace_id, f"secondary_stream_error={e}")

    # Tier 4: Gemini
    if _premium_configured():
        try:
            log_info(trace_id, "stream falling back to gemini (free)")
            _got = False
            for tok, model in _stream_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                               messages, prompt, trace_id, max_tokens=max_tokens):
                _got = True
                yield tok, model
            if _got:
                return
            log_info(trace_id, "gemini_stream yielded no tokens")
        except Exception as e2:
            log_info(trace_id, f"gemini_stream_fallback_error={e2}")

    # All streaming tiers returned no token deltas (provider quirk, content filters, or empty stream).
    try:
        result = chat(
            messages=messages,
            system_prompt=prompt,
            trace_id=f"{trace_id}-stream-fallback",
            user_message=user_message,
            max_tokens=max_tokens,
        )
        reply = (result.get("reply") or "").strip()
        if reply:
            log_info(trace_id, "chat_stream: non-streaming fallback after empty stream")
            yield reply, result.get("model") or "chat-fallback"
    except Exception as e:
        log_info(trace_id, f"chat_stream non-stream fallback error: {e}")
