"""Tiered LLM API client for CHILI's general chat.

Free tier chain (no cost):
  1. Primary: Groq Llama 3.3 70B (high quality, 100K TPD free limit)
  2. Secondary: Groq Llama 3.1 8B Instant (lighter, separate higher rate limit)
  3. Tertiary: Google Gemini 2.0 Flash via OpenAI-compatible endpoint (free)

The system tracks daily Groq token usage and preemptively switches to the
secondary model when approaching the daily limit, avoiding hard 429 errors
and eliminating the need for paid premium fallback.

Configure via env vars:
  LLM_API_KEY   / OPENAI_API_KEY     — Primary API key (e.g. Groq gsk_...)
  LLM_MODEL     / OPENAI_MODEL       — Primary model (default: llama-3.3-70b-versatile)
  LLM_BASE_URL  / OPENAI_BASE_URL    — Primary base URL (default: Groq)
  PREMIUM_API_KEY                     — Tertiary/fallback API key (e.g. Google AI Studio)
  PREMIUM_MODEL                      — Tertiary model (default: gemini-2.0-flash)
  PREMIUM_BASE_URL                   — Tertiary base URL (default: Gemini OpenAI compat)
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

# Paid OpenAI (optional, true last resort)
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
    return bool(settings.primary_api_key and settings.primary_api_key.strip())


def _premium_configured() -> bool:
    return bool(settings.premium_api_key and settings.premium_api_key.strip())


def _token_param(base_url: str, max_tokens: int) -> dict:
    if "openai.com" in base_url:
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def _call_provider(api_key: str, base_url: str, model: str, messages: list[dict],
                   system_prompt: str, trace_id: str,
                   max_tokens: int = 1024) -> dict:
    """Make a non-streaming call with automatic retry on rate limits."""
    timeout = 30.0 if "groq.com" in base_url else 60.0
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
            reply = response.choices[0].message.content.strip()
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
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            yield delta.content, model
    log_info(trace_id, f"llm_stream_complete model={model}")


def chat(
    messages: list[dict],
    system_prompt: str | None = None,
    trace_id: str = "llm",
    user_message: str = "",
    max_tokens: int = 1024,
) -> dict:
    """Chat with free-tier cascade: primary → secondary (smaller Groq) → Gemini.

    Only uses paid models as a last resort. Preemptively downgrades to the
    secondary model when approaching the daily Groq token limit.
    """
    prompt = system_prompt or SYSTEM_PROMPT

    if not is_configured():
        if _premium_configured():
            try:
                return _call_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                      messages, prompt, trace_id, max_tokens=max_tokens)
            except Exception as e:
                log_info(trace_id, f"premium_error={e}")
        return {"reply": "", "tokens_used": 0, "model": "none"}

    # Tier 1: Primary model (skip if near daily limit)
    if not _near_daily_limit():
        try:
            result = _call_provider(settings.primary_api_key, settings.llm_base_url, settings.llm_model,
                                    messages, prompt, trace_id, max_tokens=max_tokens)
            if not _is_weak_response(result["reply"], user_message):
                return result
            log_info(trace_id, f"primary reply weak ({len(result['reply'])} chars), trying secondary")
        except Exception as e:
            log_info(trace_id, f"primary_error={e}")
    else:
        log_info(trace_id, f"primary skipped (daily tokens ~{_daily_tokens['used']})")

    # Tier 2: Secondary Groq model (smaller, separate rate limit)
    try:
        result = _call_provider(settings.primary_api_key, settings.llm_base_url, _SECONDARY_MODEL,
                                messages, prompt, trace_id, max_tokens=max_tokens)
        if result["reply"]:
            return result
    except Exception as e:
        log_info(trace_id, f"secondary_error={e}")

    # Tier 3: Free Gemini fallback
    if _premium_configured():
        try:
            log_info(trace_id, "falling back to gemini (free)")
            return _call_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                  messages, prompt, trace_id, max_tokens=max_tokens)
        except Exception as e2:
            log_info(trace_id, f"gemini_fallback_error={e2}")

    # Tier 4: Paid OpenAI (true last resort)
    if PAID_OPENAI_API_KEY:
        try:
            log_info(trace_id, "falling back to paid OpenAI as last resort")
            return _call_provider(PAID_OPENAI_API_KEY, PAID_OPENAI_BASE_URL, PAID_OPENAI_MODEL,
                                  messages, prompt, trace_id, max_tokens=max_tokens)
        except Exception as e3:
            log_info(trace_id, f"openai_last_resort_error={e3}")

    return {"reply": "", "tokens_used": 0, "model": "error"}


def chat_stream(
    messages: list[dict],
    system_prompt: str | None = None,
    trace_id: str = "llm-stream",
    user_message: str = "",
    max_tokens: int = 1024,
):
    """Stream tokens with free-tier cascade: primary → secondary → Gemini."""
    prompt = system_prompt or SYSTEM_PROMPT

    if not is_configured():
        if _premium_configured():
            try:
                for tok, model in _stream_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                                   messages, prompt, trace_id, max_tokens=max_tokens):
                    yield tok, model
            except Exception as e:
                log_info(trace_id, f"premium_stream_error={e}")
        return

    # Tier 1: Primary (skip if near daily limit)
    if not _near_daily_limit():
        try:
            for tok, model in _stream_provider(settings.primary_api_key, settings.llm_base_url, settings.llm_model,
                                               messages, prompt, trace_id, max_tokens=max_tokens):
                yield tok, model
            return
        except Exception as e:
            log_info(trace_id, f"primary_stream_error={e}")
    else:
        log_info(trace_id, f"primary stream skipped (daily tokens ~{_daily_tokens['used']})")

    # Tier 2: Secondary Groq model
    try:
        for tok, model in _stream_provider(settings.primary_api_key, settings.llm_base_url, _SECONDARY_MODEL,
                                           messages, prompt, trace_id, max_tokens=max_tokens):
            yield tok, model
        return
    except Exception as e:
        log_info(trace_id, f"secondary_stream_error={e}")

    # Tier 3: Free Gemini fallback
    if _premium_configured():
        try:
            log_info(trace_id, "stream falling back to gemini (free)")
            for tok, model in _stream_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                               messages, prompt, trace_id, max_tokens=max_tokens):
                yield tok, model
            return
        except Exception as e2:
            log_info(trace_id, f"gemini_stream_fallback_error={e2}")

    # Tier 4: Paid OpenAI (true last resort)
    if PAID_OPENAI_API_KEY:
        try:
            log_info(trace_id, "stream falling back to paid OpenAI as last resort")
            for tok, model in _stream_provider(PAID_OPENAI_API_KEY, PAID_OPENAI_BASE_URL, PAID_OPENAI_MODEL,
                                               messages, prompt, trace_id, max_tokens=max_tokens):
                yield tok, model
        except Exception as e3:
            log_info(trace_id, f"openai_stream_last_resort_error={e3}")
