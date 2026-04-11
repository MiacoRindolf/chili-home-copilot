"""Neural-backed momentum operator API (Phase 4 — no runner)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from ...deps import get_db, get_identity_ctx
from ...config import settings
from ...schemas.momentum_operator import (
    MomentumArmLiveBody,
    MomentumConfirmLiveArmBody,
    MomentumLiveRunnerTickBody,
    MomentumPaperRunnerTickBody,
    MomentumPromotePaperBody,
    MomentumRefreshBody,
    MomentumRunPaperBody,
)
from ...services.trading.momentum_neural.operator_actions import (
    begin_live_arm,
    confirm_live_arm,
    create_paper_draft_session,
    enqueue_symbol_refresh,
    promote_paper_session_to_live_arm,
)
from ...services.trading.momentum_neural.risk_evaluator import evaluate_proposed_momentum_automation
from ...services.trading.momentum_neural.risk_policy import resolve_effective_risk_policy
from ...services.trading.momentum_neural.live_runner import tick_live_session
from ...services.trading.momentum_neural.paper_runner import tick_paper_session
from ...models.trading import TradingAutomationSession
from ...services.trading.momentum_neural.automation_query import (
    archive_automation_session,
    automation_summary,
    cancel_automation_session,
    delete_automation_session,
    get_automation_session_detail,
    get_operator_session_focus,
    list_automation_events,
    list_automation_sessions,
    pause_automation_session,
    resume_automation_session,
    run_automation_session,
    stop_automation_session,
)
from ...services.trading.momentum_neural.operator_readiness import build_momentum_operator_readiness
from ...services.trading.execution_family_registry import execution_family_capabilities
from ...services.trading.momentum_neural.feedback_query import (
    aggregate_outcome_counts_by_execution_family,
    get_symbol_variant_feedback_summary,
    get_variant_feedback_summary,
    list_recent_momentum_outcomes,
    momentum_outcomes_table_present,
)
from ...services.trading.momentum_neural.viable_query import build_viable_strategies_payload
from ...services.trading.momentum_neural.opportunities import list_momentum_opportunities

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading/momentum", tags=["trading-momentum"])


@router.get("/execution-families")
def get_momentum_execution_families() -> dict[str, Any]:
    """Read-only: documented execution_family values vs implementation status (Phase 11 seam)."""
    return {"ok": True, "families": execution_family_capabilities()}


def _http_detail_from_operator_result(result: dict[str, Any]) -> Any:
    err = result.get("error")
    if err == "risk_blocked":
        return {
            "error": err,
            "message": result.get("message"),
            "risk_evaluation": result.get("risk_evaluation"),
        }
    if err == "broker_not_ready":
        return {
            "error": err,
            "message": result.get("message"),
            "operator_readiness": result.get("operator_readiness"),
        }
    return result.get("message") or err or "operator_error"


def _require_user(request: Request, db: Session) -> tuple[dict[str, Any], int]:
    ctx = get_identity_ctx(request, db)
    if ctx.get("is_guest") or not ctx.get("user_id"):
        raise HTTPException(status_code=403, detail="Paired account required for this action.")
    return ctx, int(ctx["user_id"])


@router.get("/viable")
def get_momentum_viable(
    request: Request,
    db: Session = Depends(get_db),
    symbol: str = Query(..., min_length=1, max_length=36),
    mode: Optional[str] = Query(None, description="paper|live UI echo"),
) -> dict[str, Any]:
    """Viable momentum strategies for symbol (neural DB + optional hot merge)."""
    ctx = get_identity_ctx(request, db)
    uid = ctx.get("user_id") if not ctx.get("is_guest") else None
    m = (mode or "paper").lower()
    if m not in ("paper", "live"):
        m = "paper"
    return build_viable_strategies_payload(
        db,
        symbol=symbol,
        user_id=int(uid) if uid is not None else None,
        enrich_coinbase=True,
        operator_mode=m,
    )


@router.get("/opportunities")
def get_momentum_opportunities(
    request: Request,
    db: Session = Depends(get_db),
    mode: str = Query("paper", description="paper|live"),
    asset_class: str = Query("all", description="all|stock|crypto"),
    limit: int = Query(60, ge=1, le=200),
) -> dict[str, Any]:
    _require_user(request, db)
    return list_momentum_opportunities(db, mode=mode, asset_filter=asset_class, limit=limit)


@router.post("/refresh")
def post_momentum_refresh(
    request: Request,
    body: MomentumRefreshBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Enqueue symbol-focused neural momentum_context_refresh."""
    _, uid = _require_user(request, db)
    _ = uid
    out = enqueue_symbol_refresh(db, symbol=body.symbol, execution_family=body.execution_family)
    db.commit()
    if not out.get("ok"):
        return {
            "accepted": False,
            "reason": out.get("reason"),
            "execution_family": out.get("execution_family"),
            "symbol": body.symbol.strip().upper(),
        }
    return {
        "accepted": True,
        "symbol": body.symbol.strip().upper(),
        "correlation_id": out.get("correlation_id"),
        "activation_event_id": out.get("activation_event_id"),
    }


@router.post("/run-paper")
def post_momentum_run_paper(
    request: Request,
    body: MomentumRunPaperBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record draft paper session only (no runner)."""
    ctx, uid = _require_user(request, db)
    _ = ctx
    payload = build_viable_strategies_payload(
        db,
        symbol=body.symbol,
        user_id=uid,
        enrich_coinbase=False,
    )
    allowed = {int(s["variant_id"]) for s in payload.get("strategies") or [] if s.get("actions", {}).get("can_run_paper")}
    if int(body.variant_id) not in allowed:
        raise HTTPException(status_code=400, detail="Variant not paper-eligible for this symbol.")
    resp = create_paper_draft_session(
        db,
        user_id=uid,
        symbol=body.symbol,
        variant_id=body.variant_id,
        execution_family=body.execution_family,
    )
    if not resp.get("ok"):
        raise HTTPException(status_code=400, detail=_http_detail_from_operator_result(resp))
    db.commit()
    return resp


@router.post("/arm-live")
def post_momentum_arm_live(
    request: Request,
    body: MomentumArmLiveBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ctx, uid = _require_user(request, db)
    _ = ctx
    result = begin_live_arm(
        db,
        user_id=uid,
        symbol=body.symbol,
        variant_id=body.variant_id,
        execution_family=body.execution_family,
    )
    if not result.get("ok"):
        err = result.get("error")
        if err == "not_live_eligible":
            raise HTTPException(status_code=403, detail=result.get("message") or err)
        if err == "risk_blocked":
            raise HTTPException(status_code=400, detail=_http_detail_from_operator_result(result))
        raise HTTPException(status_code=400, detail=result.get("message") or err)
    db.commit()
    return result


@router.post("/confirm-live-arm")
def post_momentum_confirm_live_arm(
    request: Request,
    body: MomentumConfirmLiveArmBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ctx, uid = _require_user(request, db)
    _ = ctx
    result = confirm_live_arm(db, user_id=uid, arm_token=body.arm_token, confirm=body.confirm)
    if not result.get("ok"):
        err = result.get("error")
        if err == "token_expired":
            raise HTTPException(status_code=410, detail=result.get("message") or err)
        if err == "risk_blocked":
            raise HTTPException(status_code=400, detail=_http_detail_from_operator_result(result))
        if err == "broker_not_ready":
            raise HTTPException(status_code=409, detail=_http_detail_from_operator_result(result))
        code = 400
        raise HTTPException(status_code=code, detail=result.get("message") or err)
    db.commit()
    return result


@router.post("/promote-paper")
def post_momentum_promote_paper(
    request: Request,
    body: MomentumPromotePaperBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Strict promotion: eligible paper session → new live_arm_pending session (lineage FK)."""
    _, uid = _require_user(request, db)
    result = promote_paper_session_to_live_arm(
        db,
        user_id=uid,
        paper_session_id=body.paper_session_id,
        execution_family=body.execution_family,
    )
    if not result.get("ok"):
        err = result.get("error")
        if err == "not_found":
            raise HTTPException(status_code=404, detail=result.get("message") or err)
        if err == "not_live_eligible":
            raise HTTPException(status_code=403, detail=result.get("message") or err)
        if err == "risk_blocked":
            raise HTTPException(status_code=400, detail=_http_detail_from_operator_result(result))
        if err in ("paper_completed_stale", "paper_not_promotable", "paper_state_not_promotable"):
            raise HTTPException(status_code=400, detail=result.get("message") or err)
        if err == "execution_family_not_implemented":
            raise HTTPException(status_code=400, detail=result.get("message") or err)
        raise HTTPException(status_code=400, detail=result.get("message") or err)
    db.commit()
    return result


@router.get("/operator/readiness")
def get_momentum_operator_readiness(
    symbol: Optional[str] = Query(None, max_length=36),
    execution_family: str = Query("coinbase_spot", max_length=32),
) -> dict[str, Any]:
    """Shared readiness truth (no auth — no user-specific secrets)."""
    return {
        "ok": True,
        "operator_readiness": build_momentum_operator_readiness(
            execution_family=execution_family,
            symbol=symbol.strip().upper() if symbol else None,
        ),
    }


@router.get("/operator/current-session")
def get_momentum_operator_current_session(
    request: Request,
    db: Session = Depends(get_db),
    symbol: Optional[str] = Query(None, max_length=36),
) -> dict[str, Any]:
    """Latest focus session + readiness for paired user."""
    _, uid = _require_user(request, db)
    return get_operator_session_focus(db, user_id=uid, symbol=symbol)


# ── Risk policy (Phase 6 — read-only) ───────────────────────────────────────


@router.get("/risk/policy")
def get_momentum_risk_policy() -> dict[str, Any]:
    """Effective momentum automation risk policy (config-backed)."""
    return resolve_effective_risk_policy()


@router.get("/risk/evaluate")
def get_momentum_risk_evaluate(
    request: Request,
    db: Session = Depends(get_db),
    symbol: str = Query(..., min_length=1, max_length=36),
    variant_id: int = Query(..., ge=1),
    mode: str = Query(..., description="paper or live"),
    execution_family: str = Query("coinbase_spot", min_length=1, max_length=32),
) -> dict[str, Any]:
    """Evaluate a hypothetical session (uses paired user for concurrency limits)."""
    _, uid = _require_user(request, db)
    m = mode.strip().lower()
    if m not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be paper or live")
    return evaluate_proposed_momentum_automation(
        db,
        user_id=uid,
        symbol=symbol,
        variant_id=variant_id,
        mode=m,
        execution_family=execution_family,
    )


# ── Paper runner (Phase 7 — dev tick; scheduler optional) ───────────────────


@router.post("/paper-runner/tick")
def post_momentum_paper_runner_tick(
    request: Request,
    body: MomentumPaperRunnerTickBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Advance one paper session by one simulated step (paired; off unless dev flag)."""
    if not settings.chili_momentum_paper_runner_dev_tick_enabled:
        raise HTTPException(status_code=404, detail="Not found.")
    if not settings.chili_momentum_paper_runner_enabled:
        raise HTTPException(status_code=400, detail="Paper runner is disabled (CHILI_MOMENTUM_PAPER_RUNNER_ENABLED).")
    _, uid = _require_user(request, db)
    sess = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.id == int(body.session_id),
            TradingAutomationSession.user_id == uid,
        )
        .one_or_none()
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found.")
    if sess.mode != "paper":
        raise HTTPException(status_code=400, detail="Not a paper automation session.")
    out = tick_paper_session(db, int(body.session_id))
    db.commit()
    return out


@router.post("/live-runner/tick")
def post_momentum_live_runner_tick(
    request: Request,
    body: MomentumLiveRunnerTickBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Advance one live session one step (paired; dev flag; real orders if path executes)."""
    if not settings.chili_momentum_live_runner_dev_tick_enabled:
        raise HTTPException(status_code=404, detail="Not found.")
    if not settings.chili_momentum_live_runner_enabled:
        raise HTTPException(status_code=400, detail="Live runner is disabled (CHILI_MOMENTUM_LIVE_RUNNER_ENABLED).")
    _, uid = _require_user(request, db)
    sess = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.id == int(body.session_id),
            TradingAutomationSession.user_id == uid,
        )
        .one_or_none()
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found.")
    if sess.mode != "live":
        raise HTTPException(status_code=400, detail="Not a live automation session.")
    out = tick_live_session(db, int(body.session_id))
    db.commit()
    return out


# ── Automation monitor (Phase 5 — no runner) ────────────────────────────────


def _automation_user_id(request: Request, db: Session) -> int:
    _, uid = _require_user(request, db)
    return uid


@router.get("/automation/summary")
def get_automation_summary(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    return automation_summary(db, user_id=_automation_user_id(request, db))


@router.get("/automation/sessions")
def get_automation_sessions(
    request: Request,
    db: Session = Depends(get_db),
    state: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    include_archived: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    return list_automation_sessions(
        db,
        user_id=_automation_user_id(request, db),
        state=state,
        mode=mode,
        symbol=symbol,
        include_archived=include_archived,
        limit=limit,
    )


@router.get("/automation/sessions/{session_id}")
def get_automation_session(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    detail = get_automation_session_detail(
        db, user_id=_automation_user_id(request, db), session_id=session_id
    )
    if not detail:
        raise HTTPException(status_code=404, detail="Session not found.")
    return detail


@router.get("/automation/events")
def get_automation_events(
    request: Request,
    db: Session = Depends(get_db),
    session_id: Optional[int] = Query(None),
    event_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    return list_automation_events(
        db,
        user_id=_automation_user_id(request, db),
        session_id=session_id,
        event_type=event_type,
        limit=limit,
    )


@router.post("/automation/sessions/{session_id}/cancel")
def post_automation_session_cancel(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    out = cancel_automation_session(db, user_id=_automation_user_id(request, db), session_id=session_id)
    if not out.get("ok"):
        err = out.get("error")
        code = 404 if err == "not_found" else 400
        raise HTTPException(status_code=code, detail=out.get("error", "cancel_failed"))
    db.commit()
    return out


@router.post("/automation/sessions/{session_id}/archive")
def post_automation_session_archive(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    out = archive_automation_session(db, user_id=_automation_user_id(request, db), session_id=session_id)
    if not out.get("ok"):
        err = out.get("error")
        code = 404 if err == "not_found" else 400
        raise HTTPException(status_code=code, detail=out.get("error", "archive_failed"))
    db.commit()
    return out


@router.post("/automation/sessions/{session_id}/run")
def post_automation_session_run(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    out = run_automation_session(db, user_id=_automation_user_id(request, db), session_id=session_id)
    if not out.get("ok"):
        err = out.get("error")
        code = 404 if err == "not_found" else 400
        raise HTTPException(status_code=code, detail=err or "run_failed")
    db.commit()
    return out


@router.post("/automation/sessions/{session_id}/pause")
def post_automation_session_pause(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    out = pause_automation_session(db, user_id=_automation_user_id(request, db), session_id=session_id)
    if not out.get("ok"):
        err = out.get("error")
        code = 404 if err == "not_found" else 400
        raise HTTPException(status_code=code, detail=err or "pause_failed")
    db.commit()
    return out


@router.post("/automation/sessions/{session_id}/resume")
def post_automation_session_resume(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    out = resume_automation_session(db, user_id=_automation_user_id(request, db), session_id=session_id)
    if not out.get("ok"):
        err = out.get("error")
        code = 404 if err == "not_found" else 400
        raise HTTPException(status_code=code, detail=err or "resume_failed")
    db.commit()
    return out


@router.post("/automation/sessions/{session_id}/stop")
def post_automation_session_stop(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    out = stop_automation_session(db, user_id=_automation_user_id(request, db), session_id=session_id)
    if not out.get("ok"):
        err = out.get("error")
        code = 404 if err == "not_found" else 400
        raise HTTPException(status_code=code, detail=err or "stop_failed")
    db.commit()
    return out


@router.post("/automation/sessions/{session_id}/delete")
def post_automation_session_delete(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    out = delete_automation_session(db, user_id=_automation_user_id(request, db), session_id=session_id)
    if not out.get("ok"):
        err = out.get("error")
        code = 404 if err == "not_found" else 400
        raise HTTPException(status_code=code, detail=err or "delete_failed")
    db.commit()
    return out


# ── Neural feedback read-model (Phase 9) ─────────────────────────────────────


@router.get("/feedback/recent")
def get_momentum_feedback_recent(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(40, ge=1, le=200),
    variant_id: Optional[int] = Query(None),
    symbol: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    execution_family: Optional[str] = Query(None, max_length=32),
) -> dict[str, Any]:
    uid = _automation_user_id(request, db)
    if not momentum_outcomes_table_present(db):
        return {"ok": True, "outcomes": [], "table_present": False}
    rows = list_recent_momentum_outcomes(
        db,
        limit=limit,
        user_id=uid,
        variant_id=variant_id,
        symbol=symbol,
        mode=mode,
        execution_family=execution_family,
    )
    return {
        "ok": True,
        "outcomes": rows,
        "table_present": True,
        "by_execution_family_30d": aggregate_outcome_counts_by_execution_family(db, days=30),
    }


@router.get("/feedback/variant/{variant_id}")
def get_momentum_feedback_variant(
    request: Request,
    variant_id: int,
    db: Session = Depends(get_db),
    days: int = Query(14, ge=1, le=365),
) -> dict[str, Any]:
    _automation_user_id(request, db)
    if not momentum_outcomes_table_present(db):
        return {"ok": True, "table_present": False, "summary": None}
    return {
        "ok": True,
        "table_present": True,
        "summary": get_variant_feedback_summary(db, variant_id=int(variant_id), days=days),
    }


@router.get("/feedback/symbol-variant")
def get_momentum_feedback_symbol_variant(
    request: Request,
    db: Session = Depends(get_db),
    symbol: str = Query(..., min_length=2, max_length=36),
    variant_id: int = Query(..., ge=1),
    days: int = Query(14, ge=1, le=365),
) -> dict[str, Any]:
    _automation_user_id(request, db)
    if not momentum_outcomes_table_present(db):
        return {"ok": True, "table_present": False, "summary": None}
    return {
        "ok": True,
        "table_present": True,
        "summary": get_symbol_variant_feedback_summary(
            db, symbol=symbol.strip(), variant_id=int(variant_id), days=days
        ),
    }
