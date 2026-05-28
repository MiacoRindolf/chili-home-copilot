"""Universal LLM Gateway — the SOLE entry point for every LLM call.

Every place in CHILI that wants to call an LLM goes through
:func:`gateway_chat`. The function returns the same shape as
``openai_client.chat()`` so existing callers can switch to it with
minimal code changes (just add ``purpose=...``).

Routing decision:

    purpose → llm_purpose_policy table → routing_strategy

  * ``passthrough`` — straight ``openai_client.chat()``. No augmentation.
                      Used for trading JSON callers, vision, etc., where
                      the rich pipeline would harm rather than help.
  * ``augmented``   — Context Brain assembles a structured prompt
                      (Phase F.1-F.3) and we make ONE chat() call. No
                      decomposition, no Ollama-first.
  * ``tree``        — Full F.10 pipeline: decompose via Ollama → parallel
                      chunks via Ollama → compile via Ollama → final
                      synthesis via gpt-5.5 (the only paid call).

Every call writes one row to ``llm_gateway_log`` with full cost / latency
breakdown so we can mine which purposes are hot, where money goes, and
where the tree pipeline is buying us quality vs just adding latency.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ... import openai_client
from ...config import settings
from ..llm_cost import approximate_tokens, estimate_cost_usd, provider_from_base_url
from . import purpose_policy as policy_mod
from . import tree_coordinator as tree_mod
from .tree_types import GatewayCallResult, PurposePolicy

logger = logging.getLogger(__name__)

_GATEWAY_EXACT_CACHEABLE_PURPOSES = frozenset({
    "chat_search",
    "code_review",
    "code_search",
    "desktop_normalize_app",
    "desktop_refine_speech",
    "memory_extract",
    "personality_apply",
    "planner_intent",
    "project_ai_engineer",
    "project_architect",
    "project_backend_engineer",
    "project_devops_engineer",
    "project_frontend_engineer",
    "project_product_owner",
    "project_project_manager",
    "project_qa_engineer",
    "project_security_engineer",
    "project_ux_designer",
    "project_web_research",
    "reasoning_anticipate",
    "reasoning_evolve",
    "reasoning_proactive",
    "reasoning_user_model",
    "reasoning_web_research",
    "trading_analyze",
    "trading_analyze_stream",
    "trading_brain_assistant",
    "trading_pattern_mine",
    "trading_reflect",
    "trading_reasoning",
    "trading_smart_pick",
    "smart_pick_stream",
    "pattern_research_extract",
})
_GATEWAY_EXACT_CACHE_TTL_SEC = 600
_GATEWAY_EXACT_CACHE_MAX_ENTRIES = 128


class _GatewayInflight:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict[str, Any] | None = None


_gateway_exact_cache_lock = threading.Lock()
_gateway_exact_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_gateway_exact_inflight: dict[str, _GatewayInflight] = {}


def reset_gateway_cache() -> None:
    """Clear direct gateway exact-cache state for tests and maintenance hooks."""
    with _gateway_exact_cache_lock:
        _gateway_exact_cache.clear()
        _gateway_exact_inflight.clear()


def _gateway_cache_allowed(policy: PurposePolicy) -> bool:
    return (
        bool(policy.enabled)
        and not bool(policy.high_stakes)
        and policy.routing_strategy == "passthrough"
        and policy.purpose in _GATEWAY_EXACT_CACHEABLE_PURPOSES
    )


def _normalizable_messages(messages: list[dict]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for message in messages or []:
        if isinstance(message, dict):
            normalized.append({
                "role": str(message.get("role") or ""),
                "content": str(message.get("content") or ""),
            })
        else:
            normalized.append({"role": "", "content": str(message)})
    return normalized


def _gateway_cache_key(
    *,
    policy: PurposePolicy,
    messages: list[dict],
    system_prompt: Optional[str],
    user_message: str,
    max_tokens: int,
    strict_escalation: bool,
    model_override: Optional[str],
    user_id: Optional[int],
    project_id: Optional[int],
) -> str:
    payload = {
        "v": 1,
        "purpose": policy.purpose,
        "routing_strategy": policy.routing_strategy,
        "messages": _normalizable_messages(messages),
        "system_prompt": system_prompt or "",
        "user_message": user_message or "",
        "max_tokens": int(max_tokens or 0),
        "strict_escalation": bool(strict_escalation),
        "model_override": model_override or "",
        "user_id": user_id,
        "project_id": project_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _gateway_cache_snapshot(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if not result.get("reply") or result.get("model") == "error":
        return None
    snapshot = dict(result)
    snapshot.pop("gateway_log_id", None)
    return snapshot


def _gateway_cache_get(key: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _gateway_exact_cache_lock:
        entry = _gateway_exact_cache.get(key)
        if not entry:
            return None
        stored_at, result = entry
        if now - stored_at > _GATEWAY_EXACT_CACHE_TTL_SEC:
            _gateway_exact_cache.pop(key, None)
            return None
        _gateway_exact_cache.move_to_end(key)
        return dict(result)


def _gateway_cache_put(key: str, result: dict[str, Any] | None) -> dict[str, Any] | None:
    snapshot = _gateway_cache_snapshot(result)
    if snapshot is None:
        return None
    with _gateway_exact_cache_lock:
        _gateway_exact_cache[key] = (time.monotonic(), snapshot)
        _gateway_exact_cache.move_to_end(key)
        while len(_gateway_exact_cache) > _GATEWAY_EXACT_CACHE_MAX_ENTRIES:
            _gateway_exact_cache.popitem(last=False)
    return dict(snapshot)


def _gateway_inflight_begin(key: str) -> tuple[bool, _GatewayInflight]:
    with _gateway_exact_cache_lock:
        inflight = _gateway_exact_inflight.get(key)
        if inflight is not None:
            return False, inflight
        inflight = _GatewayInflight()
        _gateway_exact_inflight[key] = inflight
        return True, inflight


def _gateway_inflight_finish(key: str, result: dict[str, Any] | None) -> None:
    with _gateway_exact_cache_lock:
        inflight = _gateway_exact_inflight.pop(key, None)
        if inflight is None:
            return
        inflight.result = _gateway_cache_snapshot(result)
        inflight.event.set()


def _gateway_inflight_wait(inflight: _GatewayInflight) -> dict[str, Any] | None:
    inflight.event.wait(_GATEWAY_EXACT_CACHE_TTL_SEC)
    return dict(inflight.result) if inflight.result is not None else None


def _prompt_text_for_cache_accounting(
    messages: list[dict],
    system_prompt: Optional[str],
    user_message: str,
) -> str:
    pieces = [system_prompt or ""]
    pieces.extend(str(m.get("content") or "") for m in messages if isinstance(m, dict))
    if user_message:
        pieces.append(user_message)
    return "\n".join(p for p in pieces if p)


def _return_gateway_cache_hit(
    *,
    db: Session,
    log_id: Optional[int],
    started_at: float,
    messages: list[dict],
    system_prompt: Optional[str],
    user_message: str,
    cached: dict[str, Any],
    cache_status: str,
) -> dict[str, Any]:
    result = dict(cached)
    prompt_tokens = approximate_tokens(
        _prompt_text_for_cache_accounting(messages, system_prompt, user_message)
    )
    completion_tokens = approximate_tokens(str(result.get("reply") or ""))
    total_tokens = prompt_tokens + completion_tokens
    _finalize_gateway_log(
        db,
        log_id,
        success=True,
        started_at_mono=started_at,
        premium_calls=0,
        premium_tokens=0,
        premium_cost_usd=0.0,
        provider="cache",
        provider_base_url=None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=0,
        total_tokens=total_tokens,
        cache_status=cache_status,
        estimated_cost_usd=0.0,
    )
    result["gateway_log_id"] = int(log_id) if log_id else None
    return result


def _purpose_model_override(purpose: str, policy: PurposePolicy | None = None) -> str | None:
    """Configured non-high-stakes paid model override for a gateway purpose."""
    raw = getattr(settings, "chili_llm_purpose_model_overrides_json", "") or ""
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except Exception as e:
        logger.debug("[context_brain.gateway] invalid purpose override JSON: %s", e)
        return None
    if not isinstance(parsed, dict):
        return None
    model = parsed.get(purpose) or parsed.get("*")
    if not isinstance(model, str) or not model.strip():
        return None
    if policy is not None and policy.high_stakes:
        logger.info(
            "[context_brain.gateway] ignoring model override for high-stakes purpose=%s",
            purpose,
        )
        return None
    return model.strip()


def _open_db_session():
    """Helper: gateway is called from many contexts (with or without a
    db session in scope). Open a fresh SessionLocal when none provided."""
    from ...db import SessionLocal
    return SessionLocal()


def _write_gateway_log_start(
    db: Session,
    *,
    purpose: str,
    routing_strategy: str,
    user_id: Optional[int],
    chat_message_id: Optional[int],
    primary_local_model: Optional[str],
    secondary_local_model: Optional[str],
    synthesizer_model: Optional[str],
) -> Optional[int]:
    try:
        row = db.execute(text(
            "INSERT INTO llm_gateway_log "
            "(purpose, user_id, chat_message_id, routing_strategy, "
            " primary_local_model, secondary_local_model, synthesizer_model) "
            "VALUES (:p, :u, :cm, :rs, :pm, :sm, :ym) "
            "RETURNING id"
        ), {
            "p": purpose,
            "u": user_id,
            "cm": chat_message_id,
            "rs": routing_strategy,
            "pm": primary_local_model,
            "sm": secondary_local_model,
            "ym": synthesizer_model,
        }).fetchone()
        db.commit()
        return int(row[0]) if row else None
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("[context_brain.gateway] log_start failed: %s", e)
        return None


def _finalize_gateway_log(
    db: Session,
    log_id: Optional[int],
    *,
    success: bool,
    started_at_mono: float,
    decomposed: bool = False,
    chunk_count: int = 0,
    cross_examined: bool = False,
    ollama_calls: int = 0,
    premium_calls: int = 0,
    ollama_tokens: int = 0,
    premium_tokens: int = 0,
    premium_cost_usd: float = 0.0,
    decompose_ms: int = 0,
    chunk_ms: int = 0,
    compile_ms: int = 0,
    synth_ms: int = 0,
    error_kind: Optional[str] = None,
    error_message: Optional[str] = None,
    provider: Optional[str] = None,
    provider_base_url: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    total_tokens: int = 0,
    service_tier: Optional[str] = None,
    cache_status: Optional[str] = None,
    estimated_cost_usd: float = 0.0,
) -> None:
    if not log_id:
        return
    total_ms = int((time.monotonic() - started_at_mono) * 1000)
    params = {
        "de": decomposed, "cc": chunk_count, "ce": cross_examined,
        "oc": ollama_calls, "pc": premium_calls,
        "ot": ollama_tokens, "pt": premium_tokens,
        "pcu": float(premium_cost_usd),
        "tl": total_ms, "dl": decompose_ms, "chl": chunk_ms,
        "col": compile_ms, "sl": synth_ms,
        "ok": success, "ek": error_kind,
        "em": (error_message or "")[:1000] if error_message else None,
        "prov": provider,
        "pbu": provider_base_url,
        "prompt_t": int(prompt_tokens or 0),
        "completion_t": int(completion_tokens or 0),
        "cached_t": int(cached_tokens or 0),
        "reasoning_t": int(reasoning_tokens or 0),
        "total_t": int(total_tokens or 0),
        "service_t": service_tier,
        "cache_s": cache_status,
        "estimated": float(estimated_cost_usd or 0.0),
        "id": int(log_id),
    }
    try:
        try:
            db.execute(text(
                "UPDATE llm_gateway_log SET "
                "  decomposed = :de, chunk_count = :cc, cross_examined = :ce, "
                "  ollama_calls_count = :oc, premium_calls_count = :pc, "
                "  ollama_total_tokens = :ot, premium_total_tokens = :pt, "
                "  premium_cost_usd = :pcu, "
                "  total_latency_ms = :tl, decompose_latency_ms = :dl, "
                "  chunk_latency_ms = :chl, compile_latency_ms = :col, "
                "  synthesize_latency_ms = :sl, "
                "  success = :ok, error_kind = :ek, error_message = :em, "
                "  provider = :prov, provider_base_url = :pbu, "
                "  prompt_tokens = :prompt_t, completion_tokens = :completion_t, "
                "  cached_tokens = :cached_t, reasoning_tokens = :reasoning_t, "
                "  total_tokens = :total_t, service_tier = :service_t, "
                "  cache_status = :cache_s, estimated_cost_usd = :estimated, "
                "  completed_at = NOW() "
                "WHERE id = :id"
            ), params)
        except Exception:
            db.rollback()
            db.execute(text(
                "UPDATE llm_gateway_log SET "
                "  decomposed = :de, chunk_count = :cc, cross_examined = :ce, "
                "  ollama_calls_count = :oc, premium_calls_count = :pc, "
                "  ollama_total_tokens = :ot, premium_total_tokens = :pt, "
                "  premium_cost_usd = :pcu, "
                "  total_latency_ms = :tl, decompose_latency_ms = :dl, "
                "  chunk_latency_ms = :chl, compile_latency_ms = :col, "
                "  synthesize_latency_ms = :sl, "
                "  success = :ok, error_kind = :ek, error_message = :em, "
                "  completed_at = NOW() "
                "WHERE id = :id"
            ), params)
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("[context_brain.gateway] finalize_log failed: %s", e)


def _passthrough(
    messages: list[dict],
    *,
    system_prompt: Optional[str],
    trace_id: str,
    user_message: str,
    max_tokens: int,
    strict_escalation: bool,
    model_override: Optional[str] = None,
) -> dict:
    """Direct call to the legacy openai_client.chat() cascade."""
    return openai_client.chat(
        messages=messages,
        system_prompt=system_prompt,
        trace_id=trace_id,
        user_message=user_message,
        max_tokens=max_tokens,
        strict_escalation=strict_escalation,
        model_override=model_override,
    )


def _cost_fields_from_result(result: dict[str, Any]) -> dict[str, Any]:
    model = str(result.get("model") or "")
    provider_base_url = str(
        result.get("provider_base_url")
        or openai_client.provider_base_url_for_model(model)
        or ""
    )
    provider = str(result.get("provider") or provider_from_base_url(provider_base_url))
    prompt_tokens = int(result.get("prompt_tokens") or 0)
    completion_tokens = int(result.get("completion_tokens") or 0)
    total_tokens = int(result.get("total_tokens") or result.get("tokens_used") or 0)
    cached_tokens = int(result.get("cached_tokens") or 0)
    cost = float(
        result.get("estimated_cost_usd")
        or estimate_cost_usd(
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
        )
        or 0.0
    )
    return {
        "provider": provider,
        "provider_base_url": provider_base_url or None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": int(result.get("reasoning_tokens") or 0),
        "total_tokens": total_tokens,
        "service_tier": result.get("service_tier"),
        "estimated_cost_usd": cost,
        "premium_calls": 1 if provider == "openai" else 0,
        "premium_tokens": total_tokens if provider == "openai" else 0,
        "premium_cost_usd": cost if provider == "openai" else 0.0,
    }


def _augmented(
    messages: list[dict],
    *,
    system_prompt: Optional[str],
    trace_id: str,
    user_message: str,
    max_tokens: int,
    strict_escalation: bool,
    db: Session,
    user_id: Optional[int],
    project_id: Optional[int],
    model_override: Optional[str] = None,
) -> dict:
    """Phase F.1-F.3 pipeline: assemble structured context, single LLM
    call. Same as ``chat_service.gather_context_only`` but invokable
    from any caller. The brain_prompt becomes part of the system prompt.
    """
    try:
        from .assembly import assemble_context  # type: ignore
        if user_id is not None:
            assembled = assemble_context(
                user_message,
                db=db,
                user_id=int(user_id),
                project_id=project_id,
                trace_id=trace_id,
            )
            if assembled.prompt_text:
                # Wrap the brain prompt into the system prompt
                merged_system = (system_prompt or "").rstrip() + "\n\n" + assembled.prompt_text
                return openai_client.chat(
                    messages=messages,
                    system_prompt=merged_system,
                    trace_id=trace_id,
                    user_message=user_message,
                    max_tokens=max_tokens,
                    strict_escalation=strict_escalation,
                    model_override=model_override,
                )
    except Exception as e:
        logger.debug("[context_brain.gateway] augmented failed, passthrough: %s", e)

    # Fall through to passthrough on any failure
    return _passthrough(
        messages,
        system_prompt=system_prompt, trace_id=trace_id,
        user_message=user_message, max_tokens=max_tokens,
        strict_escalation=strict_escalation,
        model_override=model_override,
    )


def gateway_chat(
    messages: list[dict],
    *,
    purpose: str,
    system_prompt: Optional[str] = None,
    trace_id: str = "gateway",
    user_message: str = "",
    max_tokens: int = 1024,
    strict_escalation: bool = True,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    chat_message_id: Optional[int] = None,
    user_name: str = "you",
    db: Optional[Session] = None,
) -> dict:
    """Single entry point for every LLM call in CHILI.

    Returns the same dict shape as ``openai_client.chat()``::

        {"reply": str, "tokens_used": int, "model": str}

    so callers swap in ``gateway_chat(..., purpose=X)`` without changing
    downstream code.
    """
    started_at = time.monotonic()
    own_db = False
    if db is None:
        try:
            db = _open_db_session()
            own_db = True
        except Exception as e:
            logger.warning("[context_brain.gateway] no db session, falling through: %s", e)
            return _passthrough(
                messages, system_prompt=system_prompt, trace_id=trace_id,
                user_message=user_message, max_tokens=max_tokens,
                strict_escalation=strict_escalation,
            )

    try:
        # Resolve policy. Disabled purposes always passthrough.
        policy = policy_mod.get_policy(db, purpose)
        if not policy.enabled:
            policy = PurposePolicy(
                **{**policy.__dict__, "routing_strategy": "passthrough"},
            )
        model_override = _purpose_model_override(policy.purpose, policy)

        log_id = _write_gateway_log_start(
            db,
            purpose=policy.purpose,
            routing_strategy=policy.routing_strategy,
            user_id=user_id,
            chat_message_id=chat_message_id,
            primary_local_model=policy.primary_local_model,
            secondary_local_model=policy.secondary_local_model,
            synthesizer_model=policy.synthesizer_model,
        )

        try:
            gateway_cache_key: str | None = None
            gateway_cache_owner = False
            # Inferred user message when caller didn't pass one
            inferred_user_message = user_message
            if not inferred_user_message and messages:
                last_user = next(
                    (m for m in reversed(messages) if (m.get("role") == "user")),
                    None,
                )
                if last_user:
                    inferred_user_message = (last_user.get("content") or "").strip()

            if _gateway_cache_allowed(policy):
                gateway_cache_key = _gateway_cache_key(
                    policy=policy,
                    messages=messages,
                    system_prompt=system_prompt,
                    user_message=inferred_user_message,
                    max_tokens=max_tokens,
                    strict_escalation=strict_escalation,
                    model_override=model_override,
                    user_id=user_id,
                    project_id=project_id,
                )
                cached = _gateway_cache_get(gateway_cache_key)
                if cached is not None:
                    return _return_gateway_cache_hit(
                        db=db,
                        log_id=log_id,
                        started_at=started_at,
                        messages=messages,
                        system_prompt=system_prompt,
                        user_message=inferred_user_message,
                        cached=cached,
                        cache_status="gateway_cache_hit",
                    )

                gateway_cache_owner, inflight = _gateway_inflight_begin(gateway_cache_key)
                if not gateway_cache_owner:
                    coalesced = _gateway_inflight_wait(inflight)
                    if coalesced is not None:
                        return _return_gateway_cache_hit(
                            db=db,
                            log_id=log_id,
                            started_at=started_at,
                            messages=messages,
                            system_prompt=system_prompt,
                            user_message=inferred_user_message,
                            cached=coalesced,
                            cache_status="gateway_inflight_coalesced",
                        )
                    gateway_cache_key = None

            if policy.routing_strategy == "tree":
                outcome = tree_mod.run_tree(
                    inferred_user_message or user_message,
                    db=db,
                    policy=policy,
                    chat_history=messages,
                    user_name=user_name,
                    user_id=user_id,
                    trace_id=trace_id,
                )
                outcome.gateway_log_id = log_id
                # Re-link the tree row to the gateway log so the UI joins them
                if outcome.tree_id and log_id:
                    try:
                        db.execute(text(
                            "UPDATE decomposition_tree SET gateway_log_id = :glid "
                            "WHERE id = :tid AND gateway_log_id IS NULL"
                        ), {"glid": log_id, "tid": outcome.tree_id})
                        db.commit()
                    except Exception:
                        try: db.rollback()
                        except Exception: pass

                _finalize_gateway_log(
                    db, log_id,
                    success=outcome.success,
                    started_at_mono=started_at,
                    decomposed=(len(outcome.chunks) > 1),
                    chunk_count=len(outcome.chunks),
                    cross_examined=any(c.secondary_response for c in outcome.chunks),
                    ollama_calls=outcome.ollama_calls_count,
                    premium_calls=outcome.premium_calls_count,
                    ollama_tokens=outcome.ollama_total_tokens,
                    premium_tokens=outcome.premium_total_tokens,
                    premium_cost_usd=outcome.premium_cost_usd,
                    total_tokens=outcome.premium_total_tokens + outcome.ollama_total_tokens,
                    estimated_cost_usd=outcome.premium_cost_usd,
                    decompose_ms=outcome.decompose_latency_ms,
                    chunk_ms=outcome.chunk_latency_ms,
                    compile_ms=outcome.compile_latency_ms,
                    synth_ms=outcome.synthesize_latency_ms,
                    error_kind=("tree_error" if not outcome.success else None),
                    error_message=outcome.error,
                )
                # Build the legacy result dict (extended with gateway_log_id
                # so downstream callers can record outcomes against the call).
                return {
                    "reply": outcome.final_text or "",
                    "tokens_used": outcome.premium_total_tokens + outcome.ollama_total_tokens,
                    "model": outcome.synthesizer_model or "context_brain_tree",
                    "gateway_log_id": int(log_id) if log_id else None,
                }

            elif policy.routing_strategy == "augmented":
                result = _augmented(
                    messages,
                    system_prompt=system_prompt,
                    trace_id=trace_id,
                    user_message=inferred_user_message,
                    max_tokens=max_tokens,
                    strict_escalation=strict_escalation,
                    db=db, user_id=user_id, project_id=project_id,
                    model_override=model_override,
                )
                cost_fields = _cost_fields_from_result(result)
                _finalize_gateway_log(
                    db, log_id,
                    success=bool(result.get("reply")) and result.get("model") != "error",
                    started_at_mono=started_at,
                    **cost_fields,
                )
                if isinstance(result, dict):
                    result["gateway_log_id"] = int(log_id) if log_id else None
                return result

            else:  # passthrough
                result = _passthrough(
                    messages,
                    system_prompt=system_prompt,
                    trace_id=trace_id,
                    user_message=inferred_user_message,
                    max_tokens=max_tokens,
                    strict_escalation=strict_escalation,
                    model_override=model_override,
                )
                cost_fields = _cost_fields_from_result(result)
                if gateway_cache_key:
                    cost_fields["cache_status"] = "gateway_cache_miss"
                _finalize_gateway_log(
                    db, log_id,
                    success=bool(result.get("reply")) and result.get("model") != "error",
                    started_at_mono=started_at,
                    **cost_fields,
                )
                if gateway_cache_key and gateway_cache_owner:
                    snapshot = _gateway_cache_put(gateway_cache_key, result)
                    _gateway_inflight_finish(gateway_cache_key, snapshot)
                if isinstance(result, dict):
                    result["gateway_log_id"] = int(log_id) if log_id else None
                return result
        except Exception as e:
            logger.exception("[context_brain.gateway] dispatch raised; passthrough fallback")
            if gateway_cache_key and gateway_cache_owner:
                _gateway_inflight_finish(gateway_cache_key, None)
            _finalize_gateway_log(
                db, log_id, success=False, started_at_mono=started_at,
                error_kind="exception", error_message=str(e),
            )
            return _passthrough(
                messages,
                system_prompt=system_prompt, trace_id=trace_id,
                user_message=user_message, max_tokens=max_tokens,
                strict_escalation=strict_escalation,
            )
    finally:
        if own_db and db is not None:
            try: db.close()
            except Exception: pass


def gateway_chat_stream(
    messages: list[dict],
    *,
    purpose: str,
    system_prompt: Optional[str] = None,
    trace_id: str = "gateway-stream",
    user_message: str = "",
    max_tokens: int = 1024,
    strict_escalation: bool = True,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    chat_message_id: Optional[int] = None,
    db: Optional[Session] = None,
):
    """Streaming sibling of ``gateway_chat`` for SSE call sites.

    OpenAI-compatible streaming does not return token usage through our SDK
    path, so this records provider/base URL/model and approximate token/cost
    telemetry after the stream completes.
    """
    started_at = time.monotonic()
    own_db = False
    log_id: Optional[int] = None
    policy: Optional[PurposePolicy] = None
    gateway_cache_key: str | None = None
    finalized = False
    cache_status = "stream"
    if db is None:
        try:
            db = _open_db_session()
            own_db = True
        except Exception as e:
            logger.warning("[context_brain.gateway] no db session for stream, falling through: %s", e)
            yield from openai_client.chat_stream(
                messages=messages,
                system_prompt=system_prompt,
                trace_id=trace_id,
                user_message=user_message,
                max_tokens=max_tokens,
                strict_escalation=strict_escalation,
                model_override=_purpose_model_override(purpose),
            )
            return

    prompt = system_prompt or openai_client.SYSTEM_PROMPT
    reply_parts: list[str] = []
    model_seen: Optional[str] = None
    success = False
    error_message: Optional[str] = None

    try:
        inferred_user_message = user_message
        if not inferred_user_message and messages:
            last_user = next(
                (m for m in reversed(messages) if (m.get("role") == "user")),
                None,
            )
            if last_user:
                inferred_user_message = (last_user.get("content") or "").strip()

        try:
            policy = policy_mod.get_policy(db, purpose)
        except Exception as e:
            logger.warning("[context_brain.gateway] stream policy failed, falling through: %s", e)
            model_override = _purpose_model_override(purpose)
            for tok, model in openai_client.chat_stream(
                messages=messages,
                system_prompt=prompt,
                trace_id=trace_id,
                user_message=inferred_user_message,
                max_tokens=max_tokens,
                strict_escalation=strict_escalation,
                model_override=model_override,
            ):
                reply_parts.append(tok)
                model_seen = model
                yield tok, model
            success = bool(reply_parts)
            return
        if not policy.enabled or policy.routing_strategy == "tree":
            policy = PurposePolicy(
                **{**policy.__dict__, "routing_strategy": "passthrough"},
            )
        model_override = _purpose_model_override(policy.purpose, policy)

        log_id = _write_gateway_log_start(
            db,
            purpose=policy.purpose,
            routing_strategy=policy.routing_strategy,
            user_id=user_id,
            chat_message_id=chat_message_id,
            primary_local_model=policy.primary_local_model,
            secondary_local_model=policy.secondary_local_model,
            synthesizer_model=policy.synthesizer_model,
        )

        if policy.routing_strategy == "augmented":
            try:
                from .assembly import assemble_context  # type: ignore
                if user_id is not None:
                    assembled = assemble_context(
                        inferred_user_message,
                        db=db,
                        user_id=int(user_id),
                        project_id=project_id,
                        trace_id=trace_id,
                    )
                    if assembled.prompt_text:
                        prompt = prompt.rstrip() + "\n\n" + assembled.prompt_text
            except Exception as e:
                logger.debug("[context_brain.gateway] stream augmented failed, passthrough: %s", e)

        if _gateway_cache_allowed(policy):
            gateway_cache_key = _gateway_cache_key(
                policy=policy,
                messages=messages,
                system_prompt=prompt,
                user_message=inferred_user_message,
                max_tokens=max_tokens,
                strict_escalation=strict_escalation,
                model_override=model_override,
                user_id=user_id,
                project_id=project_id,
            )
            cached = _gateway_cache_get(gateway_cache_key)
            if cached is not None:
                result = _return_gateway_cache_hit(
                    db=db,
                    log_id=log_id,
                    started_at=started_at,
                    messages=messages,
                    system_prompt=prompt,
                    user_message=inferred_user_message,
                    cached=cached,
                    cache_status="gateway_stream_cache_hit",
                )
                finalized = True
                reply = str(result.get("reply") or "")
                if reply:
                    yield reply, str(result.get("model") or "cache")
                return

        for tok, model in openai_client.chat_stream(
            messages=messages,
            system_prompt=prompt,
            trace_id=trace_id,
            user_message=inferred_user_message,
            max_tokens=max_tokens,
            strict_escalation=strict_escalation,
            model_override=model_override,
        ):
            if model:
                model_seen = model
            reply_parts.append(tok)
            yield tok, model
        success = bool(reply_parts)
    except Exception as e:
        error_message = str(e)
        raise
    finally:
        if db is not None and log_id and not finalized:
            completion = "".join(reply_parts)
            prompt_text = (prompt or "") + "\n" + "\n".join(
                str(m.get("content") or "") for m in messages if isinstance(m, dict)
            )
            prompt_tokens = approximate_tokens(prompt_text)
            completion_tokens = approximate_tokens(completion)
            total_tokens = prompt_tokens + completion_tokens
            provider_base_url = openai_client.provider_base_url_for_model(model_seen)
            provider = provider_from_base_url(provider_base_url)
            estimated_cost = estimate_cost_usd(
                provider=provider,
                model=model_seen or "",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=0,
            )
            if success and gateway_cache_key:
                _gateway_cache_put(gateway_cache_key, {
                    "reply": completion,
                    "model": model_seen or "stream",
                    "provider": provider,
                    "provider_base_url": provider_base_url,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "estimated_cost_usd": estimated_cost,
                })
                cache_status = "gateway_stream_cache_miss"
            _finalize_gateway_log(
                db,
                log_id,
                success=success,
                started_at_mono=started_at,
                premium_calls=1 if provider == "openai" else 0,
                premium_tokens=total_tokens if provider == "openai" else 0,
                premium_cost_usd=estimated_cost if provider == "openai" else 0.0,
                error_kind=None if success else ("stream_exception" if error_message else "empty_stream"),
                error_message=error_message,
                provider=provider,
                provider_base_url=provider_base_url or None,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cache_status=cache_status,
                estimated_cost_usd=estimated_cost,
            )
            try:
                openai_client._safe_log_llm_call(  # telemetry-only private helper
                    trace_id=trace_id,
                    provider=provider,
                    tier=1 if provider == "openai" else (2 if provider == "groq" else 3),
                    model=model_seen or "unknown",
                    provider_base_url=provider_base_url or None,
                    system_prompt=prompt,
                    user_prompt="\n".join(
                        str(m.get("content") or "") for m in messages if isinstance(m, dict)
                    ),
                    completion=completion if completion else None,
                    tokens_in=prompt_tokens,
                    tokens_out=completion_tokens,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cached_tokens=0,
                    reasoning_tokens=0,
                    total_tokens=total_tokens,
                    cache_status=cache_status,
                    estimated_cost_usd=estimated_cost,
                    latency_ms=int((time.monotonic() - started_at) * 1000),
                    success=success,
                    weak_response=False,
                    failure_kind=None if success else "empty_stream",
                )
            except Exception:
                pass
        if own_db and db is not None:
            try: db.close()
            except Exception: pass
