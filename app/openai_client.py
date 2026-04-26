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
from typing import Any
from openai import OpenAI, RateLimitError, APIStatusError

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

# The OpenAI SDK retries internally by default; our own retry loop already
# handles transient rate limits, so zeroing the SDK's retry prevents
# multiplicative call amplification on 429 storms (Phase A, a2).
_SDK_MAX_RETRIES = 0

_token_budget_lock = threading.Lock()
# Per-provider daily token buckets keyed by host (api.groq.com, api.openai.com,
# generativelanguage.googleapis.com). Groq keeps its historical 85K preemptive
# threshold (free-tier 100K TPD). OpenAI + premium limits default to 0
# (unlimited) and are configurable via settings for Phase C, c2.
_daily_tokens: dict[str, dict[str, int]] = {}
_DAILY_TOKEN_LIMIT_GROQ = 85000

# Error codes that are NEVER recoverable by retrying or cascading to the
# same provider. Classification drives:
#   * _call_provider / _stream_provider: skip per-attempt retry.
#   * chat_stream: skip the non-streaming fallback when any tier surfaced
#     one of these (Phase A, a1 + a3).
_PERMANENT_ERROR_CODES = frozenset({
    "insufficient_quota",
    "invalid_api_key",
    "model_not_found",
    "account_disabled",
    "account_deactivated",
    "billing_hard_limit_reached",
})


def _provider_host(base_url: str) -> str:
    """Shortname for per-provider bucket keys."""
    u = (base_url or "").lower()
    if "groq.com" in u:
        return "groq"
    if "generativelanguage.googleapis.com" in u:
        return "gemini"
    if "openai.com" in u:
        return "openai"
    return "other"


def _provider_limit(host: str) -> int:
    """Preemptive daily token cap per provider; 0 means unlimited."""
    try:
        if host == "groq":
            return _DAILY_TOKEN_LIMIT_GROQ
        if host == "openai":
            return int(getattr(settings, "openai_daily_token_limit", 0) or 0)
        if host == "gemini":
            return int(getattr(settings, "premium_daily_token_limit", 0) or 0)
    except Exception:
        return 0
    return 0


def _track_tokens(count: int, base_url: str = "") -> None:
    """Track daily per-provider token usage."""
    if count <= 0:
        return
    host = _provider_host(base_url) if base_url else "groq"
    today = date.today().isoformat()
    with _token_budget_lock:
        bucket = _daily_tokens.get(host)
        if bucket is None or bucket.get("date") != today:
            bucket = {"date": today, "used": 0}
            _daily_tokens[host] = bucket
        bucket["used"] = int(bucket.get("used", 0)) + int(count)


def _near_daily_limit(base_url: str = "") -> bool:
    """Return True if this provider's bucket is ≥ its preemptive cap.

    A provider with limit=0 (unlimited / no cap) is never throttled here.
    """
    host = _provider_host(base_url) if base_url else "groq"
    limit = _provider_limit(host)
    if limit <= 0:
        return False
    today = date.today().isoformat()
    with _token_budget_lock:
        bucket = _daily_tokens.get(host)
        if bucket is None or bucket.get("date") != today:
            return False
        return int(bucket.get("used", 0)) >= limit


def _provider_used(base_url: str) -> int:
    """Current tokens used today for a provider host (0 on fresh day)."""
    host = _provider_host(base_url) if base_url else "groq"
    today = date.today().isoformat()
    with _token_budget_lock:
        bucket = _daily_tokens.get(host)
        if bucket is None or bucket.get("date") != today:
            return 0
        return int(bucket.get("used", 0))


def get_daily_token_usage() -> dict[str, Any]:
    """Operator metric: tokens used today per provider bucket."""
    today = date.today().isoformat()
    out: dict[str, Any] = {"date": today, "providers": {}}
    with _token_budget_lock:
        for host, bucket in _daily_tokens.items():
            if bucket.get("date") != today:
                continue
            out["providers"][host] = {
                "used": int(bucket.get("used", 0)),
                "limit": _provider_limit(host),
            }
    return out


_REFUSAL_PATTERNS = re.compile(
    r"(?i)(i\s+can(?:'?t| ?not)\s+(?:help|assist|provide|answer|do that))"
    r"|(as an ai|i(?:'m| am) (?:just )?(?:a language model|an ai))"
    r"|(i\s+don(?:'?t| ?not)\s+(?:have (?:the )?(?:ability|capability|information)))"
    r"|((?:sorry|apologi[zs]e),?\s+(?:but )?i\s+(?:can(?:'?t| ?not)|am (?:not |un)able))"
)


def _is_weak_response(
    reply: str,
    user_message: str,
    strict_escalation: bool = True,
) -> bool:
    """Detect if a response should be escalated to the premium model.

    ``strict_escalation=False`` (used by ``llm_caller.call_llm``) skips the
    length heuristic so legitimately short structured replies (e.g. JSON
    ``{"action":"hold"}``) don't force paid-tier escalation (Phase A, a4).
    """
    if not reply or len(reply.strip()) < 20:
        return True
    if _REFUSAL_PATTERNS.search(reply):
        return True
    if strict_escalation and len(user_message) > 100 and len(reply.strip()) < 60:
        return True
    return False


def _is_permanent_openai_error(exc: Exception) -> tuple[bool, str]:
    """Classify API errors as permanent (skip retry + skip stream→chat fallback).

    Returns ``(is_permanent, code_hint)`` where ``code_hint`` is a short
    label suitable for logging (never the full error body).
    """
    code = None
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict):
            code = err.get("code") or err.get("type")
    if not code:
        code = getattr(exc, "code", None)
    if code and isinstance(code, str) and code in _PERMANENT_ERROR_CODES:
        return True, code
    status = getattr(exc, "status_code", None)
    if status in (401, 404):
        return True, f"status_{status}"
    msg = str(exc)
    for tag in _PERMANENT_ERROR_CODES:
        if tag in msg:
            return True, tag
    return False, ""


def _classify_tier_error(exc: Exception) -> tuple[bool, str]:
    """Uniform classification for per-tier except blocks."""
    if isinstance(exc, (RateLimitError, APIStatusError)):
        return _is_permanent_openai_error(exc)
    return False, ""


def is_configured() -> bool:
    """True if any primary-style key exists (LLM_API_KEY or OPENAI_API_KEY)."""
    return bool(settings.primary_api_key and settings.primary_api_key.strip())


def _openai_official_configured() -> bool:
    """OPENAI_API_KEY set — use api.openai.com first."""
    return bool(settings.openai_api_key and settings.openai_api_key.strip())


def _groq_stack_configured() -> bool:
    """LLM_API_KEY set — Groq (or custom) stack; never mix with OPENAI_API_KEY on wrong host."""
    return bool(settings.llm_api_key and settings.llm_api_key.strip())


# ── Auth-failure short-circuit ───────────────────────────────────────
#
# When a provider's API key is invalid, every call raises a 401. Logging
# the full error at INFO per call is pure noise — the cascade falls
# through to the next provider regardless. We detect the first 401 per
# provider base-url, log it once at WARNING, then skip further calls to
# that URL for the lifetime of the process. A process restart (e.g.
# after key rotation via env) resets the skip set.
_auth_failed_urls: set[str] = set()
_auth_lock = threading.Lock()


def _mark_auth_failed(base_url: str, trace_id: str, err_text: str) -> None:
    key = (base_url or "").strip().rstrip("/")
    with _auth_lock:
        if key in _auth_failed_urls:
            return
        _auth_failed_urls.add(key)
    log_info(
        trace_id,
        f"auth_failed base_url={key} — suppressing further calls this process "
        f"(rotate env key + restart to re-enable): {err_text[:120]}",
    )


def _is_auth_failed(base_url: str) -> bool:
    key = (base_url or "").strip().rstrip("/")
    with _auth_lock:
        return key in _auth_failed_urls


def _looks_like_auth_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return (
        "invalid api key" in s
        or "invalid_api_key" in s
        or "401" in s
        or "unauthorized" in s
    )


def _premium_configured() -> bool:
    return bool(settings.premium_api_key and settings.premium_api_key.strip())


def _token_param(base_url: str, max_tokens: int) -> dict:
    if "openai.com" in base_url:
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def _temperature_param(model: str) -> dict:
    """Return ``{"temperature": 0.7}`` only for models that support custom
    temperature. The reasoning-tuned families (gpt-5*, o1*, o3*) reject
    anything other than their default (1.0) with HTTP 400
    ``unsupported_value``. Omitting the param uses the default and works.
    """
    m = (model or "").lower()
    if m.startswith("gpt-5") or m.startswith("o1") or m.startswith("o3"):
        return {}
    return {"temperature": 0.7}


def _call_provider(api_key: str, base_url: str, model: str, messages: list[dict],
                   system_prompt: str, trace_id: str,
                   max_tokens: int = 1024, timeout_override: float | None = None) -> dict:
    """Make a non-streaming call with automatic retry on transient rate limits."""
    timeout = timeout_override or (30.0 if "groq.com" in base_url else 60.0)
    if timeout_override is None and max_tokens > 4000:
        timeout = max(timeout, 120.0)
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=_SDK_MAX_RETRIES,
    )
    api_messages = [{"role": "system", "content": system_prompt}] + messages

    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=api_messages,
                **_temperature_param(model),
                **_token_param(base_url, max_tokens),
            )
            raw = response.choices[0].message.content
            reply = (raw or "").strip()
            tokens = response.usage.total_tokens if response.usage else 0
            log_info(trace_id, f"llm_reply model={model} tokens={tokens}")
            _track_tokens(tokens, base_url)
            return {"reply": reply, "tokens_used": tokens, "model": model}
        except RateLimitError as e:
            is_perm, hint = _is_permanent_openai_error(e)
            if is_perm:
                log_info(trace_id, f"permanent_error model={model} code={hint} no_retry")
                raise
            if attempt < _MAX_RETRIES:
                delay = _RETRY_DELAYS[attempt]
                log_info(trace_id, f"rate_limited model={model} attempt={attempt+1} retry_in={delay}s")
                time.sleep(delay)
                continue
            raise


def _stream_provider(api_key: str, base_url: str, model: str, messages: list[dict],
                     system_prompt: str, trace_id: str,
                     max_tokens: int = 1024):
    """Make a streaming call with automatic retry on transient rate limits."""
    timeout = 45.0 if "groq.com" in base_url else 90.0
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=_SDK_MAX_RETRIES,
    )
    api_messages = [{"role": "system", "content": system_prompt}] + messages

    stream = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=api_messages,
                **_temperature_param(model),
                **_token_param(base_url, max_tokens),
                stream=True,
            )
            break
        except RateLimitError as e:
            is_perm, hint = _is_permanent_openai_error(e)
            if is_perm:
                log_info(trace_id, f"permanent_error_stream model={model} code={hint} no_retry")
                raise
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


def _free_tier_first_enabled() -> bool:
    """Cascade reorder is only meaningful when both OpenAI and Groq are configured."""
    return (
        bool(getattr(settings, "llm_free_tier_first", False))
        and _openai_official_configured()
        and _groq_stack_configured()
    )


_ORDER_LOGGED = False


def _log_cascade_order_once(trace_id: str) -> None:
    global _ORDER_LOGGED
    if _ORDER_LOGGED:
        return
    _ORDER_LOGGED = True
    if _free_tier_first_enabled():
        log_info(trace_id, "llm_cascade_order=free_first (groq→groq_secondary→openai→gemini)")
    else:
        log_info(trace_id, "llm_cascade_order=legacy (openai→groq→groq_secondary→gemini)")


def _chat_openai(prompt, messages, user_message, trace_id, max_tokens, strict_escalation):
    if not _openai_official_configured():
        return None
    if _is_auth_failed(PAID_OPENAI_BASE_URL):
        return None
    if _near_daily_limit(PAID_OPENAI_BASE_URL):
        log_info(trace_id, f"openai_primary skipped (daily tokens ~{_provider_used(PAID_OPENAI_BASE_URL)})")
        return None
    try:
        result = _call_provider(PAID_OPENAI_API_KEY, PAID_OPENAI_BASE_URL, PAID_OPENAI_MODEL,
                                messages, prompt, trace_id, max_tokens=max_tokens)
        if not _is_weak_response(result["reply"], user_message, strict_escalation):
            return result
        log_info(trace_id, f"openai_primary weak ({len(result['reply'])} chars)")
    except Exception as e:
        if _looks_like_auth_error(e):
            _mark_auth_failed(PAID_OPENAI_BASE_URL, trace_id, str(e))
            return None
        log_info(trace_id, f"openai_primary_error={e}")
    return None


def _chat_groq(prompt, messages, user_message, trace_id, max_tokens, strict_escalation):
    if not _groq_stack_configured():
        return None
    # Skip entirely if a prior call already determined this base_url is
    # auth-failed. The auth-failure marker is keyed per URL so the
    # OpenAI stack is still considered independently.
    if _is_auth_failed(settings.llm_base_url):
        return None
    if not _near_daily_limit(settings.llm_base_url):
        try:
            result = _call_provider(settings.llm_api_key, settings.llm_base_url, settings.llm_model,
                                    messages, prompt, trace_id, max_tokens=max_tokens)
            if not _is_weak_response(result["reply"], user_message, strict_escalation):
                return result
            log_info(trace_id, f"primary reply weak ({len(result['reply'])} chars), trying secondary")
        except Exception as e:
            if _looks_like_auth_error(e):
                _mark_auth_failed(settings.llm_base_url, trace_id, str(e))
                return None
            log_info(trace_id, f"primary_error={e}")
    else:
        log_info(trace_id, f"primary groq skipped (daily tokens ~{_provider_used(settings.llm_base_url)})")

    try:
        result = _call_provider(settings.llm_api_key, settings.llm_base_url, _SECONDARY_MODEL,
                                messages, prompt, trace_id, max_tokens=max_tokens)
        if result["reply"]:
            # Under free-tier-first with OpenAI configured, a WEAK Groq secondary
            # reply bubbles up so the cascade can still reach paid OpenAI —
            # preserving quality while saving calls on the happy path (Phase B, b2).
            if (
                _free_tier_first_enabled()
                and _is_weak_response(result["reply"], user_message, strict_escalation)
            ):
                log_info(
                    trace_id,
                    f"secondary weak ({len(result['reply'])} chars); "
                    f"free_tier_first escalation to openai",
                )
                return None
            return result
    except Exception as e:
        if _looks_like_auth_error(e):
            _mark_auth_failed(settings.llm_base_url, trace_id, str(e))
            return None
        log_info(trace_id, f"secondary_error={e}")
    return None


def _chat_gemini(prompt, messages, user_message, trace_id, max_tokens, strict_escalation):
    if not _premium_configured():
        return None
    if _near_daily_limit(settings.premium_base_url):
        log_info(trace_id, f"gemini skipped (daily tokens ~{_provider_used(settings.premium_base_url)})")
        return None
    try:
        log_info(trace_id, "falling back to gemini (free)")
        return _call_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                              messages, prompt, trace_id, max_tokens=max_tokens)
    except Exception as e2:
        log_info(trace_id, f"gemini_fallback_error={e2}")
    return None


# ── DO NOT REMOVE — CHILI Dispatch (Phase D.0): llm_call_log writer ──
# Every chat() invocation produces one row per tier attempt, so distillation
# training data accumulates whether or not the cascade ultimately succeeded.
# Wrapped in try/except so a logging failure never breaks the LLM path.
def _safe_log_llm_call(
    *,
    trace_id: str,
    provider: str,
    tier: int,
    model: str,
    system_prompt: str,
    user_prompt: str,
    completion: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    latency_ms: int,
    success: bool,
    weak_response: bool,
    failure_kind: str | None,
) -> None:
    try:
        from sqlalchemy import text as _text
        from .db import SessionLocal as _SL

        # Bound prompt/completion sizes so a runaway log doesn't blow up the row.
        _MAX_PROMPT = 32_000
        _MAX_COMPLETION = 32_000
        sp = (system_prompt or "")[:_MAX_PROMPT]
        up = (user_prompt or "")[:_MAX_PROMPT]
        co = (completion or "")[:_MAX_COMPLETION] if completion is not None else None

        with _SL() as _s:
            _s.execute(
                _text(
                    "INSERT INTO llm_call_log "
                    "(trace_id, provider, model, tier, purpose, system_prompt, "
                    " user_prompt, completion, tokens_in, tokens_out, latency_ms, "
                    " success, weak_response, failure_kind, distillable) "
                    "VALUES (:trace_id, :provider, :model, :tier, :purpose, :sp, "
                    " :up, :co, :ti, :to, :lm, :ok, :weak, :fk, :dist)"
                ),
                {
                    "trace_id": trace_id,
                    "provider": provider,
                    "model": model,
                    "tier": tier,
                    "purpose": (trace_id.split("-")[0] if trace_id else "llm"),
                    "sp": sp,
                    "up": up,
                    "co": co,
                    "ti": tokens_in,
                    "to": tokens_out,
                    "lm": latency_ms,
                    "ok": success,
                    "weak": weak_response,
                    "fk": failure_kind,
                    "dist": bool(success and not weak_response and co),
                },
            )
            _s.commit()
    except Exception as _e:  # logging must never break the LLM path
        log_info(trace_id, f"llm_call_log_write_failed={_e}")


def chat(
    messages: list[dict],
    system_prompt: str | None = None,
    trace_id: str = "llm",
    user_message: str = "",
    max_tokens: int = 1024,
    strict_escalation: bool = True,
) -> dict:
    """Chat cascade. Order depends on ``settings.llm_free_tier_first``:

    - **free_tier_first (default):** Groq primary → Groq secondary →
      OpenAI official → Gemini. Weak-response escalation still fires so
      paid OpenAI is reached when Groq is inadequate.
    - **legacy:** OpenAI official → Groq primary/secondary → Gemini.

    ``strict_escalation=False`` disables the length-based weak-response
    heuristic; deterministic JSON callers (``llm_caller.call_llm``) opt out.
    """
    prompt = system_prompt or SYSTEM_PROMPT
    _log_cascade_order_once(trace_id)

    if _free_tier_first_enabled():
        order = ((_chat_groq, "groq", 2), (_chat_openai, "openai", 1), (_chat_gemini, "gemini", 3))
    else:
        order = ((_chat_openai, "openai", 1), (_chat_groq, "groq", 2), (_chat_gemini, "gemini", 3))

    # Concatenated user content for log fidelity (system prompt logged separately).
    _user_prompt = "\n".join(
        (m.get("content") or "") for m in messages if isinstance(m, dict) and m.get("role") != "system"
    )

    for step, provider_name, tier_num in order:
        _t0 = time.monotonic()
        result = step(prompt, messages, user_message, trace_id, max_tokens, strict_escalation)
        _latency_ms = int((time.monotonic() - _t0) * 1000)
        if result and result.get("reply"):
            _safe_log_llm_call(
                trace_id=trace_id, provider=provider_name, tier=tier_num,
                model=str(result.get("model", "unknown")),
                system_prompt=prompt, user_prompt=_user_prompt,
                completion=str(result.get("reply", "")),
                tokens_in=None, tokens_out=result.get("tokens_used"),
                latency_ms=_latency_ms, success=True,
                weak_response=False, failure_kind=None,
            )
            return result
        # Tier did not return a usable reply — log as a failure row so we have
        # debugging evidence and so distillation can learn what NOT to keep.
        _safe_log_llm_call(
            trace_id=trace_id, provider=provider_name, tier=tier_num,
            model="unknown", system_prompt=prompt, user_prompt=_user_prompt,
            completion=None, tokens_in=None, tokens_out=None,
            latency_ms=_latency_ms, success=False,
            weak_response=False, failure_kind="empty_or_weak_or_skipped",
        )

    return {"reply": "", "tokens_used": 0, "model": "error"}


def _stream_tier_openai(messages, prompt, trace_id, max_tokens, flags):
    if not _openai_official_configured():
        return
    if _near_daily_limit(PAID_OPENAI_BASE_URL):
        log_info(trace_id, f"openai_stream skipped (daily tokens ~{_provider_used(PAID_OPENAI_BASE_URL)})")
        return
    try:
        got = False
        for tok, model in _stream_provider(PAID_OPENAI_API_KEY, PAID_OPENAI_BASE_URL, PAID_OPENAI_MODEL,
                                           messages, prompt, trace_id, max_tokens=max_tokens):
            got = True
            yield tok, model
        if got:
            flags["done"] = True
        else:
            log_info(trace_id, "openai_stream yielded no tokens")
    except Exception as e:
        is_perm, _ = _classify_tier_error(e)
        if is_perm:
            flags["saw_permanent"] = True
        elif isinstance(e, RateLimitError):
            flags["saw_transient_429"] = True
        log_info(trace_id, f"openai_primary_stream_error={e}")


def _stream_tier_groq(messages, prompt, trace_id, max_tokens, flags):
    if not _groq_stack_configured():
        return

    if not _near_daily_limit(settings.llm_base_url):
        try:
            got = False
            for tok, model in _stream_provider(settings.llm_api_key, settings.llm_base_url, settings.llm_model,
                                               messages, prompt, trace_id, max_tokens=max_tokens):
                got = True
                yield tok, model
            if got:
                flags["done"] = True
                return
            log_info(trace_id, "primary_stream yielded no tokens; trying secondary")
        except Exception as e:
            is_perm, _ = _classify_tier_error(e)
            if is_perm:
                flags["saw_permanent"] = True
            elif isinstance(e, RateLimitError):
                flags["saw_transient_429"] = True
            log_info(trace_id, f"primary_stream_error={e}")
    else:
        log_info(trace_id, f"primary stream skipped (daily tokens ~{_provider_used(settings.llm_base_url)})")

    try:
        got = False
        for tok, model in _stream_provider(settings.llm_api_key, settings.llm_base_url, _SECONDARY_MODEL,
                                           messages, prompt, trace_id, max_tokens=max_tokens):
            got = True
            yield tok, model
        if got:
            flags["done"] = True
        else:
            log_info(trace_id, "secondary_stream yielded no tokens")
    except Exception as e:
        is_perm, _ = _classify_tier_error(e)
        if is_perm:
            flags["saw_permanent"] = True
        elif isinstance(e, RateLimitError):
            flags["saw_transient_429"] = True
        log_info(trace_id, f"secondary_stream_error={e}")


def _stream_tier_gemini(messages, prompt, trace_id, max_tokens, flags):
    if not _premium_configured():
        return
    if _near_daily_limit(settings.premium_base_url):
        log_info(trace_id, f"gemini_stream skipped (daily tokens ~{_provider_used(settings.premium_base_url)})")
        return
    try:
        log_info(trace_id, "stream falling back to gemini (free)")
        got = False
        for tok, model in _stream_provider(settings.premium_api_key, settings.premium_base_url, settings.premium_model,
                                           messages, prompt, trace_id, max_tokens=max_tokens):
            got = True
            yield tok, model
        if got:
            flags["done"] = True
        else:
            log_info(trace_id, "gemini_stream yielded no tokens")
    except Exception as e2:
        is_perm, _ = _classify_tier_error(e2)
        if is_perm:
            flags["saw_permanent"] = True
        elif isinstance(e2, RateLimitError):
            flags["saw_transient_429"] = True
        log_info(trace_id, f"gemini_stream_fallback_error={e2}")


def chat_stream(
    messages: list[dict],
    system_prompt: str | None = None,
    trace_id: str = "llm-stream",
    user_message: str = "",
    max_tokens: int = 1024,
    strict_escalation: bool = True,
):
    """Stream cascade (order depends on ``settings.llm_free_tier_first``).

    The non-streaming fallback at the tail only runs when every tier
    exited *silently* (zero tokens, no exceptions). If any tier raised a
    permanent error or a non-recoverable rate-limit, the non-streaming
    cascade is skipped — retrying a dead key is pure waste (Phase A, a3).
    """
    prompt = system_prompt or SYSTEM_PROMPT
    _log_cascade_order_once(trace_id)

    if _free_tier_first_enabled():
        tiers = (_stream_tier_groq, _stream_tier_openai, _stream_tier_gemini)
    else:
        tiers = (_stream_tier_openai, _stream_tier_groq, _stream_tier_gemini)

    flags = {"done": False, "saw_permanent": False, "saw_transient_429": False}
    for tier in tiers:
        yield from tier(messages, prompt, trace_id, max_tokens, flags)
        if flags["done"]:
            return

    if flags["saw_permanent"] or flags["saw_transient_429"]:
        log_info(
            trace_id,
            "chat_stream: skip_nonstream_fallback "
            f"permanent={flags['saw_permanent']} rate_limit={flags['saw_transient_429']}",
        )
        return

    try:
        result = chat(
            messages=messages,
            system_prompt=prompt,
            trace_id=f"{trace_id}-stream-fallback",
            user_message=user_message,
            max_tokens=max_tokens,
            strict_escalation=strict_escalation,
        )
        reply = (result.get("reply") or "").strip()
        if reply:
            log_info(trace_id, "chat_stream: non-streaming fallback after empty stream")
            yield reply, result.get("model") or "chat-fallback"
    except Exception as e:
        log_info(trace_id, f"chat_stream non-stream fallback error: {e}")
