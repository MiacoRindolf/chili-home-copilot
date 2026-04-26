"""Operator status + controls for the reactive Code Brain.

Exposes the runtime state, recent routing decisions, queue depth, and
mined patterns so the Brain UI can show what the brain is actually
doing — and so an operator can pause it, change thresholds, or re-route
a stuck task without ssh'ing into the container.

Endpoints (read-only by default):
  GET  /api/brain/code/status          — high-level snapshot
  GET  /api/brain/code/decisions       — recent code_decision_router_log rows
  GET  /api/brain/code/events          — recent code_brain_events rows
  GET  /api/brain/code/patterns        — current code_patterns rows
  POST /api/brain/code/mode            — set mode (operator-only)
  POST /api/brain/code/thresholds      — tune thresholds (operator-only)
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
from ..services.code_brain import event_bus, repo_resolver, runtime_state

router = APIRouter(prefix="/api/brain/code", tags=["code-brain"])


def _guest_guard(ctx: dict) -> None:
    if bool(ctx.get("is_guest", True) or ctx.get("user_id") is None):
        raise HTTPException(status_code=403, detail="Operator session required")


def _decimal_to_float(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    return v


def _state_to_dict(s: runtime_state.CodeBrainRuntimeState) -> dict[str, Any]:
    return {
        "mode": s.mode,
        "daily_premium_usd_cap": _decimal_to_float(s.daily_premium_usd_cap),
        "spent_today_usd": _decimal_to_float(s.spent_today_usd),
        "spend_reset_date": s.spend_reset_date.isoformat() if s.spend_reset_date else None,
        "template_min_confidence": _decimal_to_float(s.template_min_confidence),
        "novelty_premium_threshold": _decimal_to_float(s.novelty_premium_threshold),
        "local_model_promoted": s.local_model_promoted,
        "local_model_tag": s.local_model_tag,
        "last_pattern_mining_at": (
            s.last_pattern_mining_at.isoformat()
            if isinstance(s.last_pattern_mining_at, datetime) else None
        ),
        "updated_at": (
            s.updated_at.isoformat() if isinstance(s.updated_at, datetime) else None
        ),
    }


@router.get("/status")
def code_brain_status(
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    state = runtime_state.get_state(db)

    queue_depth = event_bus.queue_depth(db)

    last_decision = db.execute(
        text(
            "SELECT id, decided_at, decision, task_id, outcome, cost_usd "
            "FROM code_decision_router_log "
            "ORDER BY id DESC LIMIT 1"
        )
    ).fetchone()
    last_decision_dict = None
    if last_decision:
        last_decision_dict = {
            "id": int(last_decision[0]),
            "decided_at": last_decision[1].isoformat() if last_decision[1] else None,
            "decision": last_decision[2],
            "task_id": int(last_decision[3]) if last_decision[3] is not None else None,
            "outcome": last_decision[4],
            "cost_usd": _decimal_to_float(last_decision[5] or 0),
        }

    decision_counts_24h = db.execute(
        text(
            "SELECT decision, COUNT(*) FROM code_decision_router_log "
            "WHERE decided_at > NOW() - INTERVAL '24 hours' "
            "GROUP BY decision"
        )
    ).fetchall()
    counts = {row[0]: int(row[1]) for row in decision_counts_24h or []}

    spend_24h_row = db.execute(
        text(
            "SELECT COALESCE(SUM(cost_usd), 0) "
            "FROM code_decision_router_log "
            "WHERE decided_at > NOW() - INTERVAL '24 hours'"
        )
    ).fetchone()
    spend_24h = _decimal_to_float(spend_24h_row[0] or 0) if spend_24h_row else 0.0

    pattern_count = db.execute(
        text("SELECT COUNT(*) FROM code_patterns")
    ).fetchone()
    high_confidence_count = db.execute(
        text(
            "SELECT COUNT(*) FROM code_patterns WHERE confidence >= :mc"
        ),
        {"mc": state.template_min_confidence},
    ).fetchone()

    return {
        "is_guest": bool(ctx.get("is_guest", True)),
        "runtime_state": _state_to_dict(state),
        "queue_depth": queue_depth,
        "last_decision": last_decision_dict,
        "decisions_24h": counts,
        "spend_24h_usd": spend_24h,
        "pattern_count": int(pattern_count[0]) if pattern_count else 0,
        "patterns_above_min_confidence": (
            int(high_confidence_count[0]) if high_confidence_count else 0
        ),
    }


@router.get("/decisions")
def code_brain_decisions(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    get_identity_ctx(request, db)
    rows = db.execute(
        text(
            "SELECT id, decided_at, decision, task_id, matched_pattern_id, "
            "       pattern_confidence, novelty_score, outcome, cost_usd, "
            "       llm_tokens_used "
            "FROM code_decision_router_log "
            "ORDER BY id DESC LIMIT :lim"
        ),
        {"lim": int(limit)},
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows or []:
        out.append({
            "id": int(row[0]),
            "decided_at": row[1].isoformat() if row[1] else None,
            "decision": row[2],
            "task_id": int(row[3]) if row[3] is not None else None,
            "matched_pattern_id": int(row[4]) if row[4] is not None else None,
            "pattern_confidence": _decimal_to_float(row[5]),
            "novelty_score": _decimal_to_float(row[6]),
            "outcome": row[7],
            "cost_usd": _decimal_to_float(row[8] or 0),
            "llm_tokens_used": int(row[9] or 0),
        })
    return {"items": out, "count": len(out)}


@router.get("/events")
def code_brain_events(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    only_unprocessed: bool = Query(False),
    db: Session = Depends(get_db),
):
    get_identity_ctx(request, db)
    where = ""
    if only_unprocessed:
        where = "WHERE processed_at IS NULL"
    rows = db.execute(
        text(
            f"SELECT id, event_type, subject_kind, subject_id, priority, "
            f"       enqueued_at, claimed_at, processed_at, outcome, error_message "
            f"FROM code_brain_events {where} "
            f"ORDER BY id DESC LIMIT :lim"
        ),
        {"lim": int(limit)},
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows or []:
        out.append({
            "id": int(row[0]),
            "event_type": row[1],
            "subject_kind": row[2],
            "subject_id": int(row[3]) if row[3] is not None else None,
            "priority": int(row[4]) if row[4] is not None else 5,
            "enqueued_at": row[5].isoformat() if row[5] else None,
            "claimed_at": row[6].isoformat() if row[6] else None,
            "processed_at": row[7].isoformat() if row[7] else None,
            "outcome": row[8],
            "error_message": row[9],
        })
    return {"items": out, "count": len(out)}


@router.get("/patterns")
def code_brain_patterns(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    get_identity_ctx(request, db)
    rows = db.execute(
        text(
            "SELECT id, name, diff_archetype, file_glob_pattern, "
            "       confidence, success_count, failure_count, "
            "       last_used_at, created_at "
            "FROM code_patterns "
            "ORDER BY confidence DESC, success_count DESC LIMIT :lim"
        ),
        {"lim": int(limit)},
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows or []:
        out.append({
            "id": int(row[0]),
            "name": row[1],
            "diff_archetype": row[2],
            "file_glob_pattern": row[3],
            "confidence": _decimal_to_float(row[4] or 0),
            "success_count": int(row[5] or 0),
            "failure_count": int(row[6] or 0),
            "last_used_at": row[7].isoformat() if row[7] else None,
            "created_at": row[8].isoformat() if row[8] else None,
        })
    return {"items": out, "count": len(out)}


# ---------------------------------------------------------------------------
# Operator-only mutations
# ---------------------------------------------------------------------------

class _SetModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = Field(min_length=1, max_length=32)


@router.post("/mode")
def code_brain_set_mode(
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


class _SetThresholdsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_min_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    novelty_premium_threshold: Optional[float] = Field(default=None, ge=0, le=1)
    daily_premium_usd_cap: Optional[float] = Field(default=None, ge=0, le=1000)


@router.post("/thresholds")
def code_brain_set_thresholds(
    body: _SetThresholdsBody,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    _guest_guard(ctx)
    runtime_state.set_thresholds(
        db,
        template_min_confidence=body.template_min_confidence,
        novelty_premium_threshold=body.novelty_premium_threshold,
        daily_premium_usd_cap=body.daily_premium_usd_cap,
    )
    return _state_to_dict(runtime_state.get_state(db))


# ---------------------------------------------------------------------------
# Repo registration — Phase E.2 dynamic resolver
# ---------------------------------------------------------------------------

class _RepoRegisterBody(BaseModel):
    """Single string input. The resolver figures out what kind it is.

    Examples that all work:
      ``C:\\dev\\some-other-project``
      ``D:/code/foo``                       (NOTE: only C:\\dev is mounted today)
      ``/host_dev/some-other-project``
      ``/workspace`` (the chili-home-copilot itself)
      ``https://github.com/MiacoRindolf/some-repo``
      ``https://github.com/MiacoRindolf/some-repo.git``
      ``git@github.com:MiacoRindolf/some-repo.git``
      ``MiacoRindolf/some-repo``           (GitHub shorthand)
      ``chili-home-copilot``                (bare name → existing-row lookup)

    The resolver returns the registered ``code_repos`` row plus a trace of
    what it did (parsed kind, whether it cloned, whether it ran git init).
    """
    model_config = ConfigDict(extra="forbid")
    input: str = Field(min_length=1, max_length=2048)
    allow_clone: bool = Field(default=True, description="Set false to refuse cloning")


@router.post("/repos")
def code_brain_register_repo(
    body: _RepoRegisterBody,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    _guest_guard(ctx)
    try:
        result = repo_resolver.resolve_or_register(db, body.input, allow_clone=body.allow_clone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    repo = result.repo
    return {
        "ok": True,
        "repo": {
            "id": int(repo.id),
            "name": repo.name,
            "host_path": getattr(repo, "host_path", None),
            "container_path": getattr(repo, "container_path", None),
            "path": getattr(repo, "path", None),
        },
        "parsed_kind": result.parsed.kind.value,
        "created": result.created,
        "cloned": result.cloned,
        "git_initialized": result.git_initialized,
        "notes": result.notes,
    }


@router.get("/repos")
def code_brain_list_repos(
    request: Request,
    db: Session = Depends(get_db),
):
    """List every registered code_repos row so the desktop app can render
    a picker on the Queue tab.
    """
    get_identity_ctx(request, db)
    rows = db.execute(
        text(
            "SELECT id, name, host_path, container_path, path "
            "FROM code_repos ORDER BY id ASC"
        )
    ).fetchall()
    items = []
    for row in rows or []:
        items.append({
            "id": int(row[0]),
            "name": row[1],
            "host_path": row[2],
            "container_path": row[3],
            "path": row[4],
        })
    return {"items": items, "count": len(items)}
