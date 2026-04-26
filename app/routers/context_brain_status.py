"""Read-only inspection + operator controls for the Context Brain.

Mirrors the shape of code_brain_status. Endpoints:

  GET  /api/brain/context/status        — snapshot
  GET  /api/brain/context/assemblies    — recent context_assembly_log rows
  GET  /api/brain/context/candidates    — drill into one assembly
  GET  /api/brain/context/weights       — current learned_context_weights
  GET  /api/brain/context/sources       — source contribution breakdown
  POST /api/brain/context/mode          — set mode (operator-only)
  POST /api/brain/context/budget        — tune token budget / caps (operator-only)
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx
from ..services.context_brain import runtime_state

router = APIRouter(prefix="/api/brain/context", tags=["context-brain"])


def _guest_guard(ctx: dict) -> None:
    if bool(ctx.get("is_guest", True) or ctx.get("user_id") is None):
        raise HTTPException(status_code=403, detail="Operator session required")


def _dec(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    return v


def _state_to_dict(s: runtime_state.ContextBrainRuntimeState) -> dict[str, Any]:
    return {
        "mode": s.mode,
        "token_budget_per_request": s.token_budget_per_request,
        "distillation_threshold_tokens": s.distillation_threshold_tokens,
        "daily_distillation_usd_cap": _dec(s.daily_distillation_usd_cap),
        "spent_today_distillation_usd": _dec(s.spent_today_distillation_usd),
        "spend_reset_date": s.spend_reset_date.isoformat() if s.spend_reset_date else None,
        "learning_enabled": s.learning_enabled,
        "distillation_enabled": s.distillation_enabled,
        "learned_strategy_version": s.learned_strategy_version,
        "last_learning_cycle_at": (
            s.last_learning_cycle_at.isoformat()
            if isinstance(s.last_learning_cycle_at, datetime) else None
        ),
        "updated_at": (
            s.updated_at.isoformat() if isinstance(s.updated_at, datetime) else None
        ),
    }


@router.get("/status")
def context_brain_status(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    state = runtime_state.get_state(db)

    counts_24h = db.execute(text(
        "SELECT intent, COUNT(*) AS n FROM context_assembly_log "
        "WHERE created_at > NOW() - INTERVAL '24 hours' "
        "GROUP BY intent ORDER BY n DESC"
    )).fetchall()
    intent_distribution = {r[0]: int(r[1]) for r in counts_24h or []}

    last_assembly = db.execute(text(
        "SELECT id, created_at, intent, intent_confidence, total_tokens_input, "
        "       budget_token_cap, budget_used_pct, elapsed_ms, distilled, sources_used "
        "FROM context_assembly_log ORDER BY id DESC LIMIT 1"
    )).fetchone()
    last = None
    if last_assembly:
        last = {
            "id": int(last_assembly[0]),
            "created_at": last_assembly[1].isoformat() if last_assembly[1] else None,
            "intent": last_assembly[2],
            "intent_confidence": _dec(last_assembly[3]),
            "total_tokens_input": int(last_assembly[4] or 0),
            "budget_token_cap": int(last_assembly[5] or 0),
            "budget_used_pct": _dec(last_assembly[6]),
            "elapsed_ms": int(last_assembly[7] or 0),
            "distilled": bool(last_assembly[8]),
            "sources_used": last_assembly[9] or {},
        }

    weight_count = db.execute(text(
        "SELECT COUNT(*) FROM learned_context_weights"
    )).fetchone()
    distillation_cache_size = db.execute(text(
        "SELECT COUNT(*) FROM context_distillation_cache"
    )).fetchone()

    return {
        "is_guest": bool(ctx.get("is_guest", True)),
        "runtime_state": _state_to_dict(state),
        "intent_distribution_24h": intent_distribution,
        "last_assembly": last,
        "learned_weights_count": int(weight_count[0]) if weight_count else 0,
        "distillation_cache_size": int(distillation_cache_size[0]) if distillation_cache_size else 0,
    }


@router.get("/assemblies")
def context_assemblies(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    get_identity_ctx(request, db)
    rows = db.execute(text(
        "SELECT id, created_at, intent, intent_confidence, total_tokens_input, "
        "       budget_token_cap, budget_used_pct, elapsed_ms, distilled, "
        "       sources_used, llm_call_log_id "
        "FROM context_assembly_log ORDER BY id DESC LIMIT :lim"
    ), {"lim": int(limit)}).fetchall()

    items: list[dict[str, Any]] = []
    for r in rows or []:
        items.append({
            "id": int(r[0]),
            "created_at": r[1].isoformat() if r[1] else None,
            "intent": r[2],
            "intent_confidence": _dec(r[3]),
            "total_tokens_input": int(r[4] or 0),
            "budget_token_cap": int(r[5] or 0),
            "budget_used_pct": _dec(r[6]),
            "elapsed_ms": int(r[7] or 0),
            "distilled": bool(r[8]),
            "sources_used": r[9] or {},
            "llm_call_log_id": (int(r[10]) if r[10] is not None else None),
        })
    return {"items": items, "count": len(items)}


@router.get("/candidates")
def context_candidates(
    request: Request,
    assembly_id: int = Query(...),
    db: Session = Depends(get_db),
):
    get_identity_ctx(request, db)
    rows = db.execute(text(
        "SELECT source_id, raw_score, relevance_score, final_weight, selected, "
        "       tokens_estimated, content_hash, distilled, preview "
        "FROM context_candidate_log "
        "WHERE assembly_id = :a "
        "ORDER BY selected DESC, relevance_score DESC"
    ), {"a": int(assembly_id)}).fetchall()

    items: list[dict[str, Any]] = []
    for r in rows or []:
        items.append({
            "source_id": r[0],
            "raw_score": _dec(r[1]),
            "relevance_score": _dec(r[2]),
            "final_weight": _dec(r[3]),
            "selected": bool(r[4]),
            "tokens_estimated": int(r[5] or 0),
            "content_hash": r[6],
            "distilled": bool(r[7]),
            "preview": r[8],
        })
    return {"items": items, "count": len(items), "assembly_id": assembly_id}


@router.get("/weights")
def context_weights(request: Request, db: Session = Depends(get_db)):
    get_identity_ctx(request, db)
    rows = db.execute(text(
        "SELECT intent, source_id, weight, sample_count, last_outcome_quality, last_updated "
        "FROM learned_context_weights ORDER BY intent, weight DESC"
    )).fetchall()

    items: list[dict[str, Any]] = []
    for r in rows or []:
        items.append({
            "intent": r[0],
            "source_id": r[1],
            "weight": _dec(r[2]),
            "sample_count": int(r[3] or 0),
            "last_outcome_quality": _dec(r[4]),
            "last_updated": r[5].isoformat() if r[5] else None,
        })
    return {"items": items, "count": len(items)}


@router.get("/sources")
def context_sources(request: Request, db: Session = Depends(get_db)):
    """Per-source contribution over the last 24 hours."""
    get_identity_ctx(request, db)
    rows = db.execute(text(
        "SELECT cl.source_id, "
        "       COUNT(*) AS total_returned, "
        "       SUM(CASE WHEN cl.selected THEN 1 ELSE 0 END) AS total_selected, "
        "       AVG(cl.raw_score) AS avg_raw, "
        "       AVG(cl.relevance_score) AS avg_rel "
        "FROM context_candidate_log cl "
        "JOIN context_assembly_log al ON al.id = cl.assembly_id "
        "WHERE al.created_at > NOW() - INTERVAL '24 hours' "
        "GROUP BY cl.source_id ORDER BY total_selected DESC"
    )).fetchall()

    items: list[dict[str, Any]] = []
    for r in rows or []:
        total = int(r[1] or 0)
        sel = int(r[2] or 0)
        items.append({
            "source_id": r[0],
            "total_returned": total,
            "total_selected": sel,
            "selection_rate": round(sel / total, 4) if total else 0.0,
            "avg_raw_score": _dec(r[3]),
            "avg_relevance_score": _dec(r[4]),
        })
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Operator-only mutations
# ---------------------------------------------------------------------------

class _SetModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = Field(min_length=1, max_length=32)


@router.post("/mode")
def context_set_mode(
    body: _SetModeBody,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    _guest_guard(ctx)
    try:
        runtime_state.set_mode(db, body.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _state_to_dict(runtime_state.get_state(db))


class _SetBudgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_budget_per_request: Optional[int] = Field(default=None, ge=512, le=64000)
    distillation_threshold_tokens: Optional[int] = Field(default=None, ge=1024, le=128000)
    daily_distillation_usd_cap: Optional[float] = Field(default=None, ge=0, le=100)
    learning_enabled: Optional[bool] = None
    distillation_enabled: Optional[bool] = None


@router.post("/budget")
def context_set_budget(
    body: _SetBudgetBody,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    _guest_guard(ctx)

    parts: list[str] = []
    params: dict[str, Any] = {}
    if body.token_budget_per_request is not None:
        parts.append("token_budget_per_request = :tb")
        params["tb"] = int(body.token_budget_per_request)
    if body.distillation_threshold_tokens is not None:
        parts.append("distillation_threshold_tokens = :dt")
        params["dt"] = int(body.distillation_threshold_tokens)
    if body.daily_distillation_usd_cap is not None:
        parts.append("daily_distillation_usd_cap = :cap")
        params["cap"] = Decimal(str(body.daily_distillation_usd_cap))
    if body.learning_enabled is not None:
        parts.append("learning_enabled = :le")
        params["le"] = bool(body.learning_enabled)
    if body.distillation_enabled is not None:
        parts.append("distillation_enabled = :de")
        params["de"] = bool(body.distillation_enabled)

    if not parts:
        return _state_to_dict(runtime_state.get_state(db))

    parts.append("updated_at = NOW()")
    db.execute(
        text("UPDATE context_brain_runtime_state SET " + ", ".join(parts) + " WHERE id = 1"),
        params,
    )
    db.commit()
    return _state_to_dict(runtime_state.get_state(db))


# ---------------------------------------------------------------------------
# Phase F.10 — Gateway / decomposition tree inspection
# ---------------------------------------------------------------------------

@router.get("/gateway/log")
def gateway_log(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    purpose: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Recent llm_gateway_log rows. Filter by purpose if given."""
    get_identity_ctx(request, db)
    if purpose:
        rows = db.execute(text(
            "SELECT id, purpose, routing_strategy, decomposed, chunk_count, "
            "       cross_examined, primary_local_model, synthesizer_model, "
            "       ollama_calls_count, premium_calls_count, "
            "       ollama_total_tokens, premium_total_tokens, "
            "       premium_cost_usd, total_latency_ms, success, "
            "       error_kind, started_at, completed_at "
            "FROM llm_gateway_log WHERE purpose = :p "
            "ORDER BY id DESC LIMIT :lim"
        ), {"p": purpose, "lim": int(limit)}).fetchall()
    else:
        rows = db.execute(text(
            "SELECT id, purpose, routing_strategy, decomposed, chunk_count, "
            "       cross_examined, primary_local_model, synthesizer_model, "
            "       ollama_calls_count, premium_calls_count, "
            "       ollama_total_tokens, premium_total_tokens, "
            "       premium_cost_usd, total_latency_ms, success, "
            "       error_kind, started_at, completed_at "
            "FROM llm_gateway_log "
            "ORDER BY id DESC LIMIT :lim"
        ), {"lim": int(limit)}).fetchall()

    items: list[dict[str, Any]] = []
    for r in rows or []:
        items.append({
            "id": int(r[0]),
            "purpose": r[1],
            "routing_strategy": r[2],
            "decomposed": bool(r[3]),
            "chunk_count": int(r[4] or 0),
            "cross_examined": bool(r[5]),
            "primary_local_model": r[6],
            "synthesizer_model": r[7],
            "ollama_calls_count": int(r[8] or 0),
            "premium_calls_count": int(r[9] or 0),
            "ollama_total_tokens": int(r[10] or 0),
            "premium_total_tokens": int(r[11] or 0),
            "premium_cost_usd": _dec(r[12]),
            "total_latency_ms": int(r[13] or 0),
            "success": bool(r[14]) if r[14] is not None else None,
            "error_kind": r[15],
            "started_at": r[16].isoformat() if r[16] else None,
            "completed_at": r[17].isoformat() if r[17] else None,
        })
    return {"items": items, "count": len(items)}


@router.get("/gateway/summary")
def gateway_summary(request: Request, db: Session = Depends(get_db)):
    """Per-purpose 24h summary: call count, success rate, avg latency,
    avg ollama vs premium calls, total $ spent."""
    get_identity_ctx(request, db)
    rows = db.execute(text(
        "SELECT purpose, "
        "       COUNT(*) AS n, "
        "       SUM(CASE WHEN success THEN 1 ELSE 0 END) AS ok_count, "
        "       AVG(total_latency_ms) AS avg_ms, "
        "       AVG(ollama_calls_count) AS avg_ollama, "
        "       AVG(premium_calls_count) AS avg_premium, "
        "       SUM(premium_cost_usd) AS total_premium_usd, "
        "       SUM(ollama_total_tokens) AS total_ollama_tokens, "
        "       SUM(premium_total_tokens) AS total_premium_tokens "
        "FROM llm_gateway_log "
        "WHERE started_at > NOW() - INTERVAL '24 hours' "
        "GROUP BY purpose ORDER BY n DESC"
    )).fetchall()
    items: list[dict[str, Any]] = []
    for r in rows or []:
        n = int(r[1] or 0)
        ok = int(r[2] or 0)
        items.append({
            "purpose": r[0],
            "calls": n,
            "success_rate": round(ok / n, 4) if n else 0.0,
            "avg_latency_ms": int(_dec(r[3]) or 0),
            "avg_ollama_calls": round(float(_dec(r[4]) or 0), 2),
            "avg_premium_calls": round(float(_dec(r[5]) or 0), 2),
            "total_premium_usd": _dec(r[6]) or 0.0,
            "total_ollama_tokens": int(r[7] or 0),
            "total_premium_tokens": int(r[8] or 0),
        })
    return {"items": items, "count": len(items)}


@router.get("/gateway/tree")
def gateway_tree(
    request: Request,
    gateway_log_id: int = Query(...),
    db: Session = Depends(get_db),
):
    """Drill into one gateway call: the decomposition tree + every chunk."""
    get_identity_ctx(request, db)
    tree_row = db.execute(text(
        "SELECT id, parent_query, chunk_count, chunks_resolved, chunks_failed, "
        "       decomposition_strategy, decomposer_model, compiled_context_tokens, "
        "       created_at "
        "FROM decomposition_tree WHERE gateway_log_id = :gid LIMIT 1"
    ), {"gid": int(gateway_log_id)}).fetchone()
    if not tree_row:
        return {"tree": None, "chunks": []}
    tree = {
        "id": int(tree_row[0]),
        "parent_query": tree_row[1],
        "chunk_count": int(tree_row[2] or 0),
        "chunks_resolved": int(tree_row[3] or 0),
        "chunks_failed": int(tree_row[4] or 0),
        "decomposition_strategy": tree_row[5],
        "decomposer_model": tree_row[6],
        "compiled_context_tokens": int(tree_row[7] or 0),
        "created_at": tree_row[8].isoformat() if tree_row[8] else None,
    }
    chunk_rows = db.execute(text(
        "SELECT chunk_index, chunk_query, chunk_kind, primary_model, "
        "       primary_response, primary_tokens_out, primary_latency_ms, "
        "       secondary_model, secondary_response, secondary_tokens_out, "
        "       secondary_latency_ms, similarity_score, selected_response, "
        "       selection_reason, is_high_stakes, success, error_message "
        "FROM decomposition_chunk WHERE tree_id = :tid "
        "ORDER BY chunk_index"
    ), {"tid": tree["id"]}).fetchall()
    chunks: list[dict[str, Any]] = []
    for r in chunk_rows or []:
        chunks.append({
            "chunk_index": int(r[0] or 0),
            "chunk_query": r[1],
            "chunk_kind": r[2],
            "primary_model": r[3],
            "primary_response": r[4],
            "primary_tokens_out": int(r[5] or 0),
            "primary_latency_ms": int(r[6] or 0),
            "secondary_model": r[7],
            "secondary_response": r[8],
            "secondary_tokens_out": int(r[9] or 0),
            "secondary_latency_ms": int(r[10] or 0),
            "similarity_score": _dec(r[11]),
            "selected_response": r[12],
            "selection_reason": r[13],
            "is_high_stakes": bool(r[14]),
            "success": bool(r[15]) if r[15] is not None else None,
            "error_message": r[16],
        })
    return {"tree": tree, "chunks": chunks}


@router.get("/gateway/policies")
def gateway_policies(request: Request, db: Session = Depends(get_db)):
    """List all per-purpose routing policies."""
    get_identity_ctx(request, db)
    rows = db.execute(text(
        "SELECT purpose, description, routing_strategy, decompose, "
        "       cross_examine, use_premium_synthesis, high_stakes, "
        "       primary_local_model, secondary_local_model, synthesizer_model, "
        "       max_chunks, chunk_timeout_sec, enabled "
        "FROM llm_purpose_policy ORDER BY purpose"
    )).fetchall()
    items: list[dict[str, Any]] = []
    for r in rows or []:
        items.append({
            "purpose": r[0],
            "description": r[1],
            "routing_strategy": r[2],
            "decompose": bool(r[3]),
            "cross_examine": bool(r[4]),
            "use_premium_synthesis": bool(r[5]),
            "high_stakes": bool(r[6]),
            "primary_local_model": r[7],
            "secondary_local_model": r[8],
            "synthesizer_model": r[9],
            "max_chunks": int(r[10] or 0),
            "chunk_timeout_sec": int(r[11] or 0),
            "enabled": bool(r[12]),
        })
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# F.4-F.6 — Learning loop endpoints
# ---------------------------------------------------------------------------


@router.get("/gateway/outcomes")
def gateway_outcomes(
    request: Request,
    db: Session = Depends(get_db),
    purpose: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """Recent outcome rows joined with their gateway call."""
    get_identity_ctx(request, db)
    where = ["1=1"]
    params: dict = {"lim": limit}
    if purpose:
        where.append("o.purpose = :p")
        params["p"] = purpose
    if source:
        where.append("o.outcome_source = :s")
        params["s"] = source
    sql = (
        "SELECT o.id, o.gateway_log_id, o.purpose, o.user_id, "
        "       o.quality_signal, o.thumbs_vote, o.outcome_source, "
        "       o.user_followed_up, o.user_regenerated, o.measured_at, "
        "       g.routing_strategy, g.success "
        "FROM context_brain_outcome o "
        "LEFT JOIN llm_gateway_log g ON g.id = o.gateway_log_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY o.measured_at DESC LIMIT :lim"
    )
    rows = db.execute(text(sql), params).fetchall()
    items = [
        {
            "id": int(r[0]),
            "gateway_log_id": int(r[1]) if r[1] is not None else None,
            "purpose": r[2],
            "user_id": int(r[3]) if r[3] is not None else None,
            "quality_signal": float(r[4]) if r[4] is not None else None,
            "thumbs_vote": int(r[5]) if r[5] is not None else None,
            "outcome_source": r[6],
            "user_followed_up": bool(r[7]) if r[7] is not None else None,
            "user_regenerated": bool(r[8]) if r[8] is not None else None,
            "measured_at": r[9].isoformat() if isinstance(r[9], datetime) else None,
            "routing_strategy": r[10],
            "success": bool(r[11]) if r[11] is not None else None,
        }
        for r in rows or []
    ]
    return {"items": items, "count": len(items)}


@router.get("/gateway/patterns")
def gateway_patterns(
    request: Request,
    db: Session = Depends(get_db),
    purpose: Optional[str] = Query(None),
    pattern_kind: Optional[str] = Query(None),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(100, ge=1, le=500),
):
    """Distilled patterns from the F.4 distiller."""
    get_identity_ctx(request, db)
    where = ["confidence >= :mc"]
    params: dict = {"mc": min_confidence, "lim": limit}
    if purpose:
        where.append("purpose = :p")
        params["p"] = purpose
    if pattern_kind:
        where.append("pattern_kind = :k")
        params["k"] = pattern_kind
    sql = (
        "SELECT id, purpose, pattern_kind, pattern_key, sample_count, "
        "       avg_quality, success_rate, avg_latency_ms, confidence, "
        "       description, last_seen_at "
        "FROM gateway_pattern "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY confidence DESC, last_seen_at DESC LIMIT :lim"
    )
    rows = db.execute(text(sql), params).fetchall()
    items = [
        {
            "id": int(r[0]),
            "purpose": r[1],
            "pattern_kind": r[2],
            "pattern_key": r[3],
            "sample_count": int(r[4]),
            "avg_quality": float(r[5]) if r[5] is not None else None,
            "success_rate": float(r[6]) if r[6] is not None else None,
            "avg_latency_ms": float(r[7]) if r[7] is not None else None,
            "confidence": float(r[8]),
            "description": r[9],
            "last_seen_at": r[10].isoformat() if isinstance(r[10], datetime) else None,
        }
        for r in rows or []
    ]
    return {"items": items, "count": len(items)}


@router.get("/gateway/proposals")
def gateway_proposals(
    request: Request,
    db: Session = Depends(get_db),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """Pending or historical policy change proposals."""
    get_identity_ctx(request, db)
    where = ["1=1"]
    params: dict = {"lim": limit}
    if status:
        where.append("status = :s")
        params["s"] = status
    sql = (
        "SELECT id, purpose, field_name, current_value, proposed_value, "
        "       justification, severity, status, decided_by, decided_at, "
        "       created_at, pattern_id "
        "FROM policy_change_proposal "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC LIMIT :lim"
    )
    rows = db.execute(text(sql), params).fetchall()
    items = [
        {
            "id": int(r[0]),
            "purpose": r[1],
            "field_name": r[2],
            "current_value": r[3],
            "proposed_value": r[4],
            "justification": r[5],
            "severity": r[6],
            "status": r[7],
            "decided_by": r[8],
            "decided_at": r[9].isoformat() if isinstance(r[9], datetime) else None,
            "created_at": r[10].isoformat() if isinstance(r[10], datetime) else None,
            "pattern_id": int(r[11]) if r[11] is not None else None,
        }
        for r in rows or []
    ]
    return {"items": items, "count": len(items)}


class _ProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(..., pattern="^(approve|reject)$")
    note: Optional[str] = None


@router.post("/gateway/proposals/{proposal_id}/decide")
def decide_proposal(
    proposal_id: int,
    body: _ProposalDecision,
    request: Request,
    db: Session = Depends(get_db),
):
    """Operator approval/rejection. Approve applies the change; reject closes
    the proposal without touching the policy."""
    ctx = get_identity_ctx(request, db)
    user_id = ctx.get("user_id")
    decided_by = f"user:{user_id}" if user_id else "anon"

    prop = db.execute(
        text(
            "SELECT purpose, field_name, proposed_value, status FROM "
            "policy_change_proposal WHERE id = :pid"
        ),
        {"pid": proposal_id},
    ).fetchone()
    if not prop:
        raise HTTPException(status_code=404, detail="proposal not found")
    if prop[3] != "pending":
        raise HTTPException(status_code=409, detail=f"proposal status={prop[3]}")

    if body.decision == "reject":
        db.execute(
            text(
                "UPDATE policy_change_proposal SET status='rejected', "
                "decided_by=:db, decided_at=NOW() WHERE id=:pid"
            ),
            {"db": decided_by, "pid": proposal_id},
        )
        db.commit()
        return {"ok": True, "status": "rejected"}

    # Approve — apply via the evolver helper (whitelisted fields only).
    from ..services.context_brain.policy_evolver import _apply_proposal
    if not _apply_proposal(db, proposal_id):
        raise HTTPException(status_code=500, detail="apply failed")
    # _apply_proposal sets status='auto_applied' — re-stamp as 'approved' so
    # the audit trail distinguishes operator vs auto.
    db.execute(
        text(
            "UPDATE policy_change_proposal SET status='approved', "
            "decided_by=:db WHERE id=:pid"
        ),
        {"db": decided_by, "pid": proposal_id},
    )
    db.commit()
    return {"ok": True, "status": "approved"}


class _ThumbsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gateway_log_id: int
    vote: int = Field(..., ge=-1, le=1)
    note: Optional[str] = None


@router.post("/gateway/thumbs")
def gateway_thumbs(
    body: _ThumbsBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Record an explicit thumbs reaction to a previous gateway call."""
    ctx = get_identity_ctx(request, db)
    from ..services.context_brain.outcome_tracker import record_thumbs

    # Resolve purpose from the gateway log so the outcome row is filterable.
    row = db.execute(
        text("SELECT purpose, user_id FROM llm_gateway_log WHERE id = :i"),
        {"i": body.gateway_log_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="gateway_log_id not found")

    outcome_id = record_thumbs(
        db,
        gateway_log_id=body.gateway_log_id,
        vote=body.vote,
        user_id=ctx.get("user_id"),
        purpose=row[0],
        note=body.note,
    )
    return {"ok": outcome_id is not None, "outcome_id": outcome_id}


@router.post("/gateway/learn/run")
def run_learning_pass(
    request: Request,
    db: Session = Depends(get_db),
    phase: str = Query("both", pattern="^(distill|evolve|both)$"),
):
    """Manual trigger for the F.4 distiller and/or F.6 evolver.

    The brain-worker schedule will call these on a cadence; this endpoint
    is for operator-on-demand or for tests.
    """
    get_identity_ctx(request, db)
    out: dict = {}
    if phase in ("distill", "both"):
        from ..services.context_brain.distiller import distill_patterns
        out["distill"] = distill_patterns(db)
    if phase in ("evolve", "both"):
        from ..services.context_brain.policy_evolver import evolve_policies
        out["evolve"] = evolve_policies(db)
    return out


@router.get("/gateway/learn/runs")
def learning_runs(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
):
    """Recent distiller/evolver runs."""
    get_identity_ctx(request, db)
    rows = db.execute(
        text(
            "SELECT id, phase, started_at, ended_at, success, "
            "       patterns_touched, proposals_created, "
            "       proposals_auto_applied, error_message "
            "FROM gateway_learning_run "
            "ORDER BY started_at DESC LIMIT :lim"
        ),
        {"lim": limit},
    ).fetchall()
    items = [
        {
            "id": int(r[0]),
            "phase": r[1],
            "started_at": r[2].isoformat() if isinstance(r[2], datetime) else None,
            "ended_at": r[3].isoformat() if isinstance(r[3], datetime) else None,
            "success": r[4],
            "patterns_touched": int(r[5] or 0),
            "proposals_created": int(r[6] or 0),
            "proposals_auto_applied": int(r[7] or 0),
            "error_message": r[8],
        }
        for r in rows or []
    ]
    return {"items": items, "count": len(items)}
