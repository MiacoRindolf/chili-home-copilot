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

import logging
import time
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import openai_client
from . import purpose_policy as policy_mod
from . import tree_coordinator as tree_mod
from .tree_types import GatewayCallResult, PurposePolicy

logger = logging.getLogger(__name__)


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
) -> None:
    if not log_id:
        return
    total_ms = int((time.monotonic() - started_at_mono) * 1000)
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
            "  completed_at = NOW() "
            "WHERE id = :id"
        ), {
            "de": decomposed, "cc": chunk_count, "ce": cross_examined,
            "oc": ollama_calls, "pc": premium_calls,
            "ot": ollama_tokens, "pt": premium_tokens,
            "pcu": float(premium_cost_usd),
            "tl": total_ms, "dl": decompose_ms, "chl": chunk_ms,
            "col": compile_ms, "sl": synth_ms,
            "ok": success, "ek": error_kind,
            "em": (error_message or "")[:1000] if error_message else None,
            "id": int(log_id),
        })
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
) -> dict:
    """Direct call to the legacy openai_client.chat() cascade."""
    return openai_client.chat(
        messages=messages,
        system_prompt=system_prompt,
        trace_id=trace_id,
        user_message=user_message,
        max_tokens=max_tokens,
        strict_escalation=strict_escalation,
    )


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
                )
    except Exception as e:
        logger.debug("[context_brain.gateway] augmented failed, passthrough: %s", e)

    # Fall through to passthrough on any failure
    return _passthrough(
        messages,
        system_prompt=system_prompt, trace_id=trace_id,
        user_message=user_message, max_tokens=max_tokens,
        strict_escalation=strict_escalation,
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
            # Inferred user message when caller didn't pass one
            inferred_user_message = user_message
            if not inferred_user_message and messages:
                last_user = next(
                    (m for m in reversed(messages) if (m.get("role") == "user")),
                    None,
                )
                if last_user:
                    inferred_user_message = (last_user.get("content") or "").strip()

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
                )
                _finalize_gateway_log(
                    db, log_id,
                    success=bool(result.get("reply")) and result.get("model") != "error",
                    started_at_mono=started_at,
                    premium_calls=1,
                    premium_tokens=int(result.get("tokens_used") or 0),
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
                )
                _finalize_gateway_log(
                    db, log_id,
                    success=bool(result.get("reply")) and result.get("model") != "error",
                    started_at_mono=started_at,
                    premium_calls=1,
                    premium_tokens=int(result.get("tokens_used") or 0),
                )
                if isinstance(result, dict):
                    result["gateway_log_id"] = int(log_id) if log_id else None
                return result
        except Exception as e:
            logger.exception("[context_brain.gateway] dispatch raised; passthrough fallback")
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
