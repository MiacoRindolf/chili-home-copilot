"""Query / view-model helpers for momentum automation monitor (Phase 5 — no runner)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func, inspect as sa_inspect
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from ..brain_neural_mesh.schema import mesh_enabled
from ..governance import get_kill_switch_status
from .operator_actions import (
    STATE_ARMED_PENDING_RUNNER,
    STATE_DRAFT,
    STATE_LIVE_ARM_PENDING,
    STATE_QUEUED,
)
from .paper_fsm import (
    STATE_BAILOUT,
    STATE_COOLDOWN,
    STATE_ENTERED,
    STATE_ENTRY_CANDIDATE,
    STATE_ERROR,
    STATE_EXITED,
    STATE_FINISHED,
    STATE_PENDING_ENTRY,
    STATE_SCALING_OUT,
    STATE_TRAILING,
    STATE_WATCHING,
)
from .live_fsm import (
    LIVE_CANCELLABLE_STATES,
    LIVE_RUNNER_ACTIVE_SUMMARY_STATES,
    STATE_LIVE_BAILOUT,
    STATE_LIVE_CANCELLED,
    STATE_LIVE_COOLDOWN,
    STATE_LIVE_ENTERED,
    STATE_LIVE_ENTRY_CANDIDATE,
    STATE_LIVE_ERROR,
    STATE_LIVE_EXITED,
    STATE_LIVE_FINISHED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_LIVE_SCALING_OUT,
    STATE_LIVE_TRAILING,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
)
from .live_runner import summarize_live_execution
from .paper_runner import summarize_paper_execution
from .risk_evaluator import summarize_risk_from_snapshot
from .risk_policy import effective_policy_summary

_log = logging.getLogger(__name__)

STATE_IDLE = "idle"
STATE_CANCELLED = "cancelled"
STATE_ARCHIVED = "archived"
STATE_EXPIRED = "expired"

# Paper runner + pre-run: operator may cancel before terminal completion.
CANCELLABLE_STATES = frozenset(
    {
        STATE_DRAFT,
        STATE_QUEUED,
        STATE_LIVE_ARM_PENDING,
        STATE_ARMED_PENDING_RUNNER,
        STATE_IDLE,
        STATE_WATCHING,
        STATE_ENTRY_CANDIDATE,
        STATE_PENDING_ENTRY,
        STATE_ENTERED,
        STATE_SCALING_OUT,
        STATE_TRAILING,
        STATE_BAILOUT,
        STATE_EXITED,
        STATE_COOLDOWN,
    }
) | frozenset(LIVE_CANCELLABLE_STATES)

# Terminal-ish rows the operator may archive (hide from default list).
ARCHIVABLE_STATES = frozenset(
    {
        STATE_CANCELLED,
        STATE_EXPIRED,
        STATE_DRAFT,
        STATE_FINISHED,
        STATE_ERROR,
        STATE_LIVE_FINISHED,
        STATE_LIVE_CANCELLED,
        STATE_LIVE_ERROR,
    }
)

PAPER_RUNNER_ACTIVE_STATES = frozenset(
    {
        STATE_WATCHING,
        STATE_ENTRY_CANDIDATE,
        STATE_PENDING_ENTRY,
        STATE_ENTERED,
        STATE_SCALING_OUT,
        STATE_TRAILING,
        STATE_BAILOUT,
    }
)

LIMITATIONS_NOTE = (
    "Paper runner is simulated (CHILI_MOMENTUM_PAPER_RUNNER_ENABLED). "
    "Live runner places real orders only for the implemented execution_family (coinbase_spot today) "
    "when CHILI_MOMENTUM_LIVE_RUNNER_ENABLED — use with care."
)


def _tables_present(db: Session) -> bool:
    try:
        bind = db.get_bind()
        names = set(sa_inspect(bind).get_table_names())
    except Exception:
        return False
    return "trading_automation_sessions" in names


def _parse_expires(snap: dict[str, Any]) -> Optional[datetime]:
    raw = snap.get("expires_at_utc")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def expire_stale_live_arm_sessions(db: Session, *, user_id: int) -> int:
    """Mark expired live_arm_pending rows as ``expired``; returns rows updated."""
    if not _tables_present(db):
        return 0
    now = datetime.utcnow()
    rows = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == user_id,
            TradingAutomationSession.state == STATE_LIVE_ARM_PENDING,
        )
        .all()
    )
    n = 0
    for sess in rows:
        snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
        exp = _parse_expires(snap)
        if exp is None or now <= exp:
            continue
        sess.state = STATE_EXPIRED
        sess.ended_at = now
        sess.updated_at = now
        from .persistence import append_trading_automation_event

        append_trading_automation_event(
            db,
            sess.id,
            "live_arm_expired",
            {"reason": "expires_at_utc_passed", "arm_token_prefix": str(snap.get("arm_token", ""))[:8]},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
        n += 1
        try:
            from .feedback_emit import emit_feedback_after_terminal_transition

            emit_feedback_after_terminal_transition(db, sess)
        except Exception:
            pass
    return n


def neural_config_strip() -> dict[str, Any]:
    return {
        "mesh_enabled": bool(mesh_enabled()),
        "trading_brain_neural_mesh_enabled": bool(settings.trading_brain_neural_mesh_enabled),
        "momentum_neural_enabled": bool(settings.chili_momentum_neural_enabled),
        "coinbase_spot_adapter_enabled": bool(settings.chili_coinbase_spot_adapter_enabled),
        "coinbase_ws_enabled": bool(settings.chili_coinbase_ws_enabled),
        "coinbase_strict_freshness": bool(settings.chili_coinbase_strict_freshness),
        "paper_runner_enabled": bool(settings.chili_momentum_paper_runner_enabled),
        "paper_runner_scheduler_enabled": bool(settings.chili_momentum_paper_runner_scheduler_enabled),
        "paper_runner_scheduler_interval_minutes": int(
            settings.chili_momentum_paper_runner_scheduler_interval_minutes
        ),
        "live_runner_enabled": bool(settings.chili_momentum_live_runner_enabled),
        "live_runner_scheduler_enabled": bool(settings.chili_momentum_live_runner_scheduler_enabled),
        "live_runner_scheduler_interval_minutes": int(
            settings.chili_momentum_live_runner_scheduler_interval_minutes
        ),
        "neural_feedback_enabled": bool(settings.chili_momentum_neural_feedback_enabled),
        "trading_automation_hud_enabled": bool(settings.chili_trading_automation_hud_enabled),
    }


def governance_strip() -> dict[str, Any]:
    g = get_kill_switch_status()
    return {"kill_switch_active": bool(g.get("active")), "kill_switch_reason": g.get("reason")}


def _variant_brief(v: MomentumStrategyVariant) -> dict[str, Any]:
    return {
        "id": v.id,
        "family": v.family,
        "strategy_family": v.family,
        "variant_key": v.variant_key,
        "label": v.label,
        "version": v.version,
        "execution_family": v.execution_family,
    }


def _status_summary(state: str) -> str:
    return {
        STATE_DRAFT: "Draft — paper intent recorded; runner disabled or not admitted.",
        STATE_QUEUED: "Queued — waiting for paper runner tick (Phase 7).",
        STATE_WATCHING: "Paper runner watching — scanning viability / quotes.",
        STATE_ENTRY_CANDIDATE: "Paper — setup detected; confirming entry.",
        STATE_PENDING_ENTRY: "Paper — simulated entry in flight.",
        STATE_ENTERED: "Paper — simulated position open.",
        STATE_SCALING_OUT: "Paper — scaling / taking profit zone.",
        STATE_TRAILING: "Paper — trailing stop armed.",
        STATE_BAILOUT: "Paper — bailout exit.",
        STATE_EXITED: "Paper — flat; entering cooldown.",
        STATE_COOLDOWN: "Paper — cooldown before finished.",
        STATE_FINISHED: "Paper — session complete (simulated).",
        STATE_ERROR: "Paper runner error — inspect events.",
        STATE_QUEUED_LIVE: "Live — queued for guarded runner.",
        STATE_WATCHING_LIVE: "Live runner watching.",
        STATE_LIVE_ENTRY_CANDIDATE: "Live — entry candidate.",
        STATE_LIVE_PENDING_ENTRY: "Live — entry order pending / reconciling.",
        STATE_LIVE_ENTERED: "Live — position open (venue).",
        STATE_LIVE_SCALING_OUT: "Live — scaling / profit zone.",
        STATE_LIVE_TRAILING: "Live — trailing stop.",
        STATE_LIVE_BAILOUT: "Live — bailout exit.",
        STATE_LIVE_EXITED: "Live — flat; cooldown.",
        STATE_LIVE_COOLDOWN: "Live — cooldown.",
        STATE_LIVE_FINISHED: "Live — session finished.",
        STATE_LIVE_CANCELLED: "Live — cancelled by operator.",
        STATE_LIVE_ERROR: "Live runner error — inspect events.",
        STATE_LIVE_ARM_PENDING: "Live arm pending — confirm in Trading or cancel here.",
        STATE_ARMED_PENDING_RUNNER: "Live armed — first live runner tick moves to queued/watching (Phase 8).",
        STATE_CANCELLED: "Cancelled by operator.",
        STATE_ARCHIVED: "Archived (hidden from default list).",
        STATE_EXPIRED: "Live arm confirmation window expired.",
        STATE_IDLE: "Idle / legacy placeholder.",
    }.get(state, "Unknown state — inspect events.")


def _session_warnings(sess: TradingAutomationSession) -> list[str]:
    w: list[str] = []
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    if sess.state == STATE_LIVE_ARM_PENDING:
        exp = _parse_expires(snap)
        if exp:
            left = (exp - datetime.utcnow()).total_seconds()
            if left < 120:
                w.append("Arm confirmation expires soon.")
    return w


def list_automation_sessions(
    db: Session,
    *,
    user_id: int,
    state: Optional[str] = None,
    mode: Optional[str] = None,
    symbol: Optional[str] = None,
    include_archived: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    if not _tables_present(db):
        return {
            "sessions": [],
            "neural": neural_config_strip(),
            "governance": governance_strip(),
            "risk_policy_summary": effective_policy_summary(),
            "limitations_note": LIMITATIONS_NOTE,
            "paper_runner_queued": 0,
            "paper_runner_active": 0,
            "live_runner_queued": 0,
            "live_runner_active": 0,
        }

    expire_stale_live_arm_sessions(db, user_id=user_id)

    q = (
        db.query(TradingAutomationSession, MomentumStrategyVariant)
        .join(MomentumStrategyVariant, MomentumStrategyVariant.id == TradingAutomationSession.variant_id)
        .filter(TradingAutomationSession.user_id == user_id)
        .order_by(TradingAutomationSession.updated_at.desc())
    )
    if not include_archived:
        q = q.filter(TradingAutomationSession.state != STATE_ARCHIVED)
    if state:
        q = q.filter(TradingAutomationSession.state == state.strip())
    if mode and mode.lower() in ("paper", "live"):
        q = q.filter(TradingAutomationSession.mode == mode.lower())
    if symbol:
        q = q.filter(TradingAutomationSession.symbol == symbol.strip().upper())

    rows = q.limit(min(max(limit, 1), 500)).all()
    ids = [int(s[0].id) for s in rows]
    counts: dict[int, int] = {}
    if ids:
        for sid, cnt in (
            db.query(TradingAutomationEvent.session_id, func.count(TradingAutomationEvent.id))
            .filter(TradingAutomationEvent.session_id.in_(ids))
            .group_by(TradingAutomationEvent.session_id)
            .all()
        ):
            counts[int(sid)] = int(cnt)

    sessions_out: list[dict[str, Any]] = []
    for sess, var in rows:
        sessions_out.append(
            {
                "id": sess.id,
                "symbol": sess.symbol,
                "variant_id": sess.variant_id,
                "variant": _variant_brief(var),
                "strategy_family": var.family,
                "mode": sess.mode,
                "venue": sess.venue,
                "execution_family": sess.execution_family,
                "state": sess.state,
                "created_at": sess.created_at.isoformat() if sess.created_at else None,
                "updated_at": sess.updated_at.isoformat() if sess.updated_at else None,
                "started_at": sess.started_at.isoformat() if sess.started_at else None,
                "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
                "correlation_id": sess.correlation_id,
                "source_node_id": sess.source_node_id,
                "event_count": counts.get(sess.id, 0),
                "status_summary": _status_summary(sess.state),
                "warnings": _session_warnings(sess),
                "risk_status": summarize_risk_from_snapshot(sess.risk_snapshot_json),
                "paper_execution": summarize_paper_execution(sess.risk_snapshot_json),
                "live_execution": summarize_live_execution(sess.risk_snapshot_json),
            }
        )

    return {
        "sessions": sessions_out,
        "neural": neural_config_strip(),
        "governance": governance_strip(),
        "risk_policy_summary": effective_policy_summary(),
        "limitations_note": LIMITATIONS_NOTE,
    }


def get_automation_session_detail(db: Session, *, user_id: int, session_id: int) -> Optional[dict[str, Any]]:
    if not _tables_present(db):
        return None

    expire_stale_live_arm_sessions(db, user_id=user_id)

    row = (
        db.query(TradingAutomationSession, MomentumStrategyVariant)
        .join(MomentumStrategyVariant, MomentumStrategyVariant.id == TradingAutomationSession.variant_id)
        .filter(
            TradingAutomationSession.id == int(session_id),
            TradingAutomationSession.user_id == user_id,
        )
        .one_or_none()
    )
    if not row:
        return None

    sess, var = row
    events = (
        db.query(TradingAutomationEvent)
        .filter(TradingAutomationEvent.session_id == sess.id)
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(80)
        .all()
    )

    via = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == sess.variant_id,
        )
        .one_or_none()
    )
    viability_brief: Optional[dict[str, Any]] = None
    if via:
        viability_brief = {
            "viability_score": via.viability_score,
            "paper_eligible": via.paper_eligible,
            "live_eligible": via.live_eligible,
            "freshness_ts": via.freshness_ts.isoformat() if via.freshness_ts else None,
        }

    risk = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    risk_summary = {k: risk[k] for k in list(risk.keys())[:24]}

    momentum_feedback = None
    try:
        from .feedback_query import get_session_feedback_row

        momentum_feedback = get_session_feedback_row(db, session_id=sess.id)
    except Exception:
        momentum_feedback = None

    return {
        "session": {
            "id": sess.id,
            "symbol": sess.symbol,
            "variant_id": sess.variant_id,
            "variant": _variant_brief(var),
            "strategy_family": var.family,
            "mode": sess.mode,
            "venue": sess.venue,
            "execution_family": sess.execution_family,
            "state": sess.state,
            "created_at": sess.created_at.isoformat() if sess.created_at else None,
            "updated_at": sess.updated_at.isoformat() if sess.updated_at else None,
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
            "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
            "correlation_id": sess.correlation_id,
            "source_node_id": sess.source_node_id,
            "risk_snapshot_summary": risk_summary,
            "status_summary": _status_summary(sess.state),
            "warnings": _session_warnings(sess),
            "risk_status": summarize_risk_from_snapshot(sess.risk_snapshot_json),
            "paper_execution": summarize_paper_execution(sess.risk_snapshot_json),
            "live_execution": summarize_live_execution(sess.risk_snapshot_json),
            "momentum_feedback": momentum_feedback,
        },
        "events": [
            {
                "id": ev.id,
                "ts": ev.ts.isoformat() if ev.ts else None,
                "event_type": ev.event_type,
                "payload_summary": _payload_summary(ev.payload_json),
                "correlation_id": ev.correlation_id,
                "source_node_id": ev.source_node_id,
            }
            for ev in events
        ],
        "viability_snapshot": viability_brief,
        "neural": neural_config_strip(),
        "governance": governance_strip(),
        "risk_policy_summary": effective_policy_summary(),
        "limitations_note": LIMITATIONS_NOTE,
    }


def _payload_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    keys = ("symbol", "variant_id", "reason", "note", "arm_token_prefix", "hello")
    return {k: payload[k] for k in keys if k in payload}


def list_automation_events(
    db: Session,
    *,
    user_id: int,
    session_id: Optional[int] = None,
    event_type: Optional[str] = None,
    limit: int = 50,
) -> dict[str, Any]:
    if not _tables_present(db):
        return {"events": [], "neural": neural_config_strip()}

    q = db.query(TradingAutomationEvent).join(
        TradingAutomationSession,
        TradingAutomationSession.id == TradingAutomationEvent.session_id,
    ).filter(TradingAutomationSession.user_id == user_id)

    if session_id is not None:
        q = q.filter(TradingAutomationEvent.session_id == int(session_id))
    if event_type:
        q = q.filter(TradingAutomationEvent.event_type == event_type.strip())

    rows = q.order_by(TradingAutomationEvent.ts.desc()).limit(min(max(limit, 1), 200)).all()
    return {
        "events": [
            {
                "id": ev.id,
                "session_id": ev.session_id,
                "ts": ev.ts.isoformat() if ev.ts else None,
                "event_type": ev.event_type,
                "payload_summary": _payload_summary(ev.payload_json),
                "correlation_id": ev.correlation_id,
            }
            for ev in rows
        ],
        "neural": neural_config_strip(),
    }


def automation_summary(db: Session, *, user_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        out = neural_config_strip()
        out.update(
            {
                "total_sessions": 0,
                "pending_paper_drafts": 0,
                "paper_runner_queued": 0,
                "paper_runner_active": 0,
                "live_runner_queued": 0,
                "live_runner_active": 0,
                "pending_live_arms": 0,
                "armed_awaiting_runner": 0,
                "cancelled": 0,
                "archived": 0,
                "expired": 0,
                "last_event_ts": None,
                "limitations_note": LIMITATIONS_NOTE,
                "governance": governance_strip(),
                "risk_policy_summary": effective_policy_summary(),
            }
        )
        return out

    expire_stale_live_arm_sessions(db, user_id=user_id)

    base = db.query(TradingAutomationSession).filter(TradingAutomationSession.user_id == user_id)
    total = base.count()
    pending_draft = base.filter(TradingAutomationSession.state == STATE_DRAFT).count()
    paper_queued = base.filter(
        TradingAutomationSession.mode == "paper",
        TradingAutomationSession.state == STATE_QUEUED,
    ).count()
    paper_active = base.filter(TradingAutomationSession.state.in_(PAPER_RUNNER_ACTIVE_STATES)).count()
    pending_arm = base.filter(TradingAutomationSession.state == STATE_LIVE_ARM_PENDING).count()
    armed = base.filter(TradingAutomationSession.state == STATE_ARMED_PENDING_RUNNER).count()
    live_queued = base.filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state == STATE_QUEUED_LIVE,
    ).count()
    live_active = base.filter(TradingAutomationSession.state.in_(LIVE_RUNNER_ACTIVE_SUMMARY_STATES)).count()
    cancelled = base.filter(
        TradingAutomationSession.state.in_((STATE_CANCELLED, STATE_LIVE_CANCELLED))
    ).count()
    archived = base.filter(TradingAutomationSession.state == STATE_ARCHIVED).count()
    expired = base.filter(TradingAutomationSession.state == STATE_EXPIRED).count()

    last_ev = (
        db.query(TradingAutomationEvent.ts)
        .join(TradingAutomationSession, TradingAutomationSession.id == TradingAutomationEvent.session_id)
        .filter(TradingAutomationSession.user_id == user_id)
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(1)
        .scalar()
    )

    summary = neural_config_strip()
    summary.update(
        {
            "total_sessions": total,
            "pending_paper_drafts": pending_draft,
            "paper_runner_queued": paper_queued,
            "paper_runner_active": paper_active,
            "pending_live_arms": pending_arm,
            "armed_awaiting_runner": armed,
            "live_runner_queued": live_queued,
            "live_runner_active": live_active,
            "cancelled": cancelled,
            "archived": archived,
            "expired": expired,
            "last_event_ts": last_ev.isoformat() if last_ev else None,
            "limitations_note": LIMITATIONS_NOTE,
            "governance": governance_strip(),
            "risk_policy_summary": effective_policy_summary(),
        }
    )
    return summary


def cancel_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}

    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {"ok": False, "error": "not_found"}
    if sess.state not in CANCELLABLE_STATES:
        return {"ok": False, "error": "not_cancellable", "state": sess.state}

    now = datetime.utcnow()
    prev = sess.state
    if sess.mode == "live" and prev in LIVE_CANCELLABLE_STATES:
        sess.state = STATE_LIVE_CANCELLED
    else:
        sess.state = STATE_CANCELLED
    sess.ended_at = now
    sess.updated_at = now

    from .persistence import append_trading_automation_event

    append_trading_automation_event(
        db,
        sess.id,
        "session_cancelled",
        {"previous_state": prev, "by": "operator", "terminal_state": sess.state},
        correlation_id=sess.correlation_id,
        source_node_id="momentum_automation_monitor",
    )
    if sess.mode == "paper" and prev != STATE_CANCELLED:
        append_trading_automation_event(
            db,
            sess.id,
            "paper_cancelled",
            {"previous_state": prev},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    if sess.mode == "live" and prev in LIVE_CANCELLABLE_STATES:
        append_trading_automation_event(
            db,
            sess.id,
            "live_cancelled",
            {"previous_state": prev},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    try:
        from .feedback_emit import emit_feedback_after_terminal_transition

        emit_feedback_after_terminal_transition(db, sess)
    except Exception:
        pass
    return {"ok": True, "session_id": sess.id, "state": sess.state}


def archive_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}

    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {"ok": False, "error": "not_found"}
    if sess.state == STATE_ARCHIVED:
        return {"ok": False, "error": "already_archived"}
    if sess.state not in ARCHIVABLE_STATES:
        return {"ok": False, "error": "not_archivable", "state": sess.state}

    prev = sess.state
    sess.state = STATE_ARCHIVED
    sess.updated_at = datetime.utcnow()

    from .persistence import append_trading_automation_event

    append_trading_automation_event(
        db,
        sess.id,
        "session_archived",
        {"previous_state": prev},
        correlation_id=sess.correlation_id,
        source_node_id="momentum_automation_monitor",
    )
    try:
        from .feedback_emit import emit_feedback_after_terminal_transition

        emit_feedback_after_terminal_transition(db, sess)
    except Exception:
        pass
    return {"ok": True, "session_id": sess.id, "state": sess.state}
