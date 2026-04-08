"""Operator momentum actions: paper admission + live arm flow (paper runner FSM is ``paper_runner``)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumSymbolViability, TradingAutomationSession
from ..execution_family_registry import is_momentum_automation_implemented, normalize_execution_family
from .persistence import append_trading_automation_event, create_trading_automation_session
from .risk_evaluator import evaluate_proposed_momentum_automation
from .risk_policy import build_session_risk_snapshot, resolve_effective_risk_policy
from .live_fsm import STATE_QUEUED_LIVE
from .paper_fsm import (
    STATE_ARMED_PENDING_RUNNER,
    STATE_DRAFT,
    STATE_LIVE_ARM_PENDING,
    STATE_QUEUED,
)

_log = logging.getLogger(__name__)

ARM_TOKEN_TTL_SEC = 900


def _utcnow() -> datetime:
    return datetime.utcnow()


def _viability_brief(row: MomentumSymbolViability) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "variant_id": row.variant_id,
        "viability_score": row.viability_score,
        "paper_eligible": row.paper_eligible,
        "live_eligible": row.live_eligible,
        "freshness_ts": row.freshness_ts.isoformat() if row.freshness_ts else None,
    }


def _readiness_subset(row: MomentumSymbolViability) -> dict[str, Any]:
    ex = row.execution_readiness_json if isinstance(row.execution_readiness_json, dict) else {}
    keys = ("spread_bps", "slippage_estimate_bps", "fee_to_target_ratio", "product_tradable")
    return {k: ex.get(k) for k in keys if k in ex}


def enqueue_symbol_refresh(
    db: Session,
    *,
    symbol: str,
    execution_family: str = "coinbase_spot",
) -> dict[str, Any]:
    """Publish neural momentum_context_refresh with focused tickers."""
    from ..brain_neural_mesh.publisher import publish_momentum_context_refresh

    ef = normalize_execution_family(execution_family)
    if not is_momentum_automation_implemented(ef):
        return {
            "ok": False,
            "reason": "execution_family_not_implemented",
            "execution_family": ef,
        }

    sym = symbol.strip().upper()
    meta: dict[str, Any] = {"tickers": [sym], "execution_family": ef}
    return publish_momentum_context_refresh(db, meta=meta)


def create_paper_draft_session(
    db: Session,
    *,
    user_id: Optional[int],
    symbol: str,
    variant_id: int,
    execution_family: str = "coinbase_spot",
) -> dict[str, Any]:
    """Phase-4/6 UX: draft paper session + frozen risk snapshot (runner not started)."""
    if user_id is None:
        return {"ok": False, "error": "user_required", "message": "Paired user required."}

    sym = symbol.strip().upper()
    policy_full = resolve_effective_risk_policy()
    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=int(user_id),
        symbol=sym,
        variant_id=int(variant_id),
        mode="paper",
        execution_family=execution_family,
    )
    if not ev.get("allowed", False):
        return {
            "ok": False,
            "error": "risk_blocked",
            "message": "Risk policy blocks paper draft for this symbol/variant.",
            "risk_evaluation": ev,
        }

    row = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == sym, MomentumSymbolViability.variant_id == int(variant_id))
        .one_or_none()
    )
    vb = _viability_brief(row) if row else None
    rs = _readiness_subset(row) if row else None

    snap = build_session_risk_snapshot(
        policy_full=policy_full,
        evaluation=ev,
        viability_brief=vb,
        readiness_subset=rs,
        extra=None,
    )

    runner_on = bool(settings.chili_momentum_paper_runner_enabled)
    initial_state = STATE_QUEUED if runner_on else STATE_DRAFT
    ef = normalize_execution_family(execution_family)
    sess = create_trading_automation_session(
        db,
        user_id=user_id,
        venue="coinbase",
        execution_family=ef,
        mode="paper",
        symbol=sym,
        variant_id=int(variant_id),
        state=initial_state,
        risk_snapshot_json=snap,
        correlation_id=str(uuid.uuid4()),
        source_node_id="momentum_operator_api",
    )
    if runner_on:
        append_trading_automation_event(
            db,
            sess.id,
            "paper_runner_queued",
            {"symbol": sym, "variant_id": variant_id, "note": "phase7_admission"},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_operator_api",
        )
    else:
        append_trading_automation_event(
            db,
            sess.id,
            "paper_draft_created",
            {"symbol": sym, "variant_id": variant_id, "note": "phase6_risk_snapshot"},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_operator_api",
        )
    return {
        "ok": True,
        "session_id": sess.id,
        "state": sess.state,
        "mode": sess.mode,
        "paper_runner_enabled": runner_on,
        "message": (
            "Paper session queued for simulated runner (tick batch or scheduler)."
            if runner_on
            else "Paper session recorded as draft; enable CHILI_MOMENTUM_PAPER_RUNNER_ENABLED for Phase 7 runner."
        ),
        "risk_evaluation": ev,
    }


def begin_live_arm(
    db: Session,
    *,
    user_id: Optional[int],
    symbol: str,
    variant_id: int,
    execution_family: str = "coinbase_spot",
) -> dict[str, Any]:
    """Validate live_eligible + risk policy; create pending arm session + token."""
    if user_id is None:
        return {"ok": False, "error": "user_required", "message": "Paired user required."}

    sym = symbol.strip().upper()
    row = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sym,
            MomentumSymbolViability.variant_id == int(variant_id),
        )
        .one_or_none()
    )
    if not row:
        return {"ok": False, "error": "viability_not_found", "message": "No viability row for symbol/variant."}
    if not row.live_eligible:
        return {"ok": False, "error": "not_live_eligible", "message": "Strategy is not live-eligible."}

    policy_full = resolve_effective_risk_policy()
    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=int(user_id),
        symbol=sym,
        variant_id=int(variant_id),
        mode="live",
        execution_family=execution_family,
    )
    if not ev.get("allowed", False):
        return {
            "ok": False,
            "error": "risk_blocked",
            "message": "Risk policy blocks live arm for this symbol/variant.",
            "risk_evaluation": ev,
        }

    ttl = int(
        min(
            ARM_TOKEN_TTL_SEC,
            float(policy_full.get("auto_expire_pending_live_arm_seconds", ARM_TOKEN_TTL_SEC)),
        )
    )
    token = str(uuid.uuid4())
    expires = (_utcnow() + timedelta(seconds=ttl)).isoformat()

    snap = build_session_risk_snapshot(
        policy_full=policy_full,
        evaluation=ev,
        viability_brief=_viability_brief(row),
        readiness_subset=_readiness_subset(row),
        extra={
            "arm_token": token,
            "expires_at_utc": expires,
            "phase": 6,
        },
    )

    ef_live = normalize_execution_family(execution_family)
    sess = create_trading_automation_session(
        db,
        user_id=user_id,
        venue="coinbase",
        execution_family=ef_live,
        mode="live",
        symbol=sym,
        variant_id=int(variant_id),
        state=STATE_LIVE_ARM_PENDING,
        risk_snapshot_json=snap,
        correlation_id=str(uuid.uuid4()),
        source_node_id="momentum_operator_api",
    )
    append_trading_automation_event(
        db,
        sess.id,
        "live_arm_requested",
        {"symbol": sym, "variant_id": variant_id, "arm_token_prefix": token[:8]},
        correlation_id=sess.correlation_id,
        source_node_id="momentum_operator_api",
    )
    return {
        "ok": True,
        "arm_token": token,
        "session_id": sess.id,
        "expires_at_utc": expires,
        "risk_evaluation": ev,
        "confirmation": {
            "symbol": sym,
            "variant_id": variant_id,
            "viability_score": row.viability_score,
            "live_eligible": row.live_eligible,
            "freshness_ts": row.freshness_ts.isoformat() if row.freshness_ts else None,
            "warnings": list((row.explain_json or {}).get("warnings") or []),
            "risk_severity": ev.get("severity"),
            "disclaimer": (
                "This step does not place orders or start automation. "
                "Phase 6 records risk snapshot + operator intent only."
            ),
        },
    }


def confirm_live_arm(
    db: Session,
    *,
    user_id: Optional[int],
    arm_token: str,
    confirm: bool,
) -> dict[str, Any]:
    """Re-evaluate risk; freeze final snapshot; transition to armed_pending_runner."""
    if not confirm:
        return {"ok": False, "error": "confirm_required", "message": "confirm must be true."}
    tok = (arm_token or "").strip()
    if not tok:
        return {"ok": False, "error": "missing_token", "message": "arm_token required."}

    q = db.query(TradingAutomationSession).filter(TradingAutomationSession.state == STATE_LIVE_ARM_PENDING)
    if user_id is not None:
        q = q.filter(TradingAutomationSession.user_id == user_id)
    candidates = q.order_by(TradingAutomationSession.id.desc()).limit(50).all()

    sess: Optional[TradingAutomationSession] = None
    for c in candidates:
        snap = c.risk_snapshot_json if isinstance(c.risk_snapshot_json, dict) else {}
        if snap.get("arm_token") == tok:
            sess = c
            break

    if not sess:
        return {"ok": False, "error": "invalid_token", "message": "No matching pending arm session."}

    exp_raw = (sess.risk_snapshot_json or {}).get("expires_at_utc")
    exp = None
    if isinstance(exp_raw, str):
        try:
            exp = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
        except Exception:
            exp = None
    if exp is not None:
        now = _utcnow()
        if exp.tzinfo:
            exp = exp.replace(tzinfo=None)
        if now > exp:
            return {"ok": False, "error": "token_expired", "message": "Arm token expired; start arm flow again."}

    row = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == sess.variant_id,
        )
        .one_or_none()
    )
    if not row or not row.live_eligible:
        return {"ok": False, "error": "no_longer_eligible", "message": "Strategy is no longer live-eligible."}

    if user_id is None:
        return {"ok": False, "error": "user_required", "message": "Paired user required."}

    policy_full = resolve_effective_risk_policy()
    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=int(user_id),
        symbol=sess.symbol,
        variant_id=int(sess.variant_id),
        mode="live",
        execution_family=sess.execution_family,
        exclude_session_id=int(sess.id),
    )
    if not ev.get("allowed", False):
        return {
            "ok": False,
            "error": "risk_blocked",
            "message": "Risk policy no longer allows confirming live arm.",
            "risk_evaluation": ev,
        }

    final_snap = build_session_risk_snapshot(
        policy_full=policy_full,
        evaluation=ev,
        viability_brief=_viability_brief(row),
        readiness_subset=_readiness_subset(row),
        extra=dict(sess.risk_snapshot_json or {}),
    )
    final_snap["arm_confirmed_at_utc"] = _utcnow().isoformat()
    final_snap["arm_confirmed"] = True

    runner_on = bool(settings.chili_momentum_live_runner_enabled)
    sess.state = STATE_QUEUED_LIVE if runner_on else STATE_ARMED_PENDING_RUNNER
    sess.risk_snapshot_json = final_snap
    sess.updated_at = _utcnow()

    append_trading_automation_event(
        db,
        sess.id,
        "live_arm_confirmed",
        {
            "symbol": sess.symbol,
            "variant_id": sess.variant_id,
            "risk_severity": ev.get("severity"),
            "live_runner_enabled": runner_on,
            "initial_runner_state": sess.state,
        },
        correlation_id=sess.correlation_id,
        source_node_id="momentum_operator_api",
    )

    return {
        "ok": True,
        "session_id": sess.id,
        "state": sess.state,
        "mode": sess.mode,
        "live_runner_enabled": runner_on,
        "message": (
            "Live arm confirmed; session queued for guarded live runner (Phase 8)."
            if runner_on
            else "Live arm confirmed; enable CHILI_MOMENTUM_LIVE_RUNNER_ENABLED for live runner."
        ),
        "risk_evaluation": ev,
    }
