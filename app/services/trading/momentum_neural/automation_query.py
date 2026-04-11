"""Query / view-model helpers for momentum automation monitor (Phase 5 — no runner)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func, inspect as sa_inspect
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import (
    BrainBatchJob,
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationRuntimeSnapshot,
    TradingAutomationSession,
    TradingAutomationSessionBinding,
    TradingAutomationSimulatedFill,
)
from ..brain_batch_job_log import brain_batch_job_record_completed
from ..batch_job_constants import JOB_SCHEDULER_WORKER_HEARTBEAT
from ..brain_neural_mesh.schema import mesh_enabled
from ..execution_family_registry import (
    ExecutionFamilyNotImplementedError,
    normalize_execution_family,
    resolve_live_spot_adapter_factory,
)
from ..execution_robustness import merge_repeatable_edge_robustness_into_readiness
from ..governance import get_kill_switch_status
from .operator_actions import (
    STATE_ARMED_PENDING_RUNNER,
    STATE_DRAFT,
    STATE_LIVE_ARM_PENDING,
    STATE_QUEUED,
)
from .paper_fsm import (
    PAPER_RUNNER_RUNNABLE_STATES,
    PAPER_RUNNER_TERMINAL_STATES,
    STATE_BAILOUT,
    STATE_COOLDOWN,
    STATE_CANCELLED,
    STATE_ENTERED,
    STATE_ENTRY_CANDIDATE,
    STATE_ERROR,
    STATE_EXITED,
    STATE_EXPIRED,
    STATE_FINISHED,
    STATE_PENDING_ENTRY,
    STATE_SCALING_OUT,
    STATE_TRAILING,
    STATE_WATCHING,
)
from .live_fsm import (
    LIVE_CANCELLABLE_STATES,
    LIVE_RUNNER_ACTIVE_SUMMARY_STATES,
    LIVE_RUNNER_RUNNABLE_STATES,
    LIVE_RUNNER_TERMINAL_STATES,
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
from .live_runner import summarize_live_execution, tick_live_session, _fmt_base_size
from .paper_runner import summarize_paper_execution, tick_paper_session
from .market_profile import asset_class_for_symbol, market_open_now
from .risk_evaluator import summarize_risk_from_snapshot
from .risk_policy import effective_policy_summary
from .operator_readiness import (
    blocked_reason_for_session,
    build_momentum_operator_readiness,
    next_action_required,
)
from .session_lifecycle import (
    apply_operator_pause,
    canonical_operator_state,
    clear_operator_pause,
    is_armed_only_live,
    is_live_orders_active,
    is_operator_paused,
    operator_pause_info,
    phase_hint,
)
from .persistence import (
    append_trading_automation_event,
    build_runtime_snapshot_values,
    create_trading_automation_session,
    default_session_binding,
)
from .strategy_params import summarize_strategy_params

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


def _table_exists(db: Session, name: str) -> bool:
    try:
        bind = db.get_bind()
        return name in set(sa_inspect(bind).get_table_names())
    except Exception:
        return False


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
        "is_active": bool(v.is_active),
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


_LIVE_TERMINAL_FOR_FOCUS = frozenset({STATE_LIVE_FINISHED, STATE_LIVE_CANCELLED, STATE_LIVE_ERROR})
_PAPER_TERMINAL_FOR_FOCUS = frozenset({STATE_FINISHED, STATE_CANCELLED, STATE_EXPIRED, STATE_ERROR})


def operator_fields_for_session(sess: TradingAutomationSession, readiness: dict[str, Any]) -> dict[str, Any]:
    alloc = sess.allocation_decision_json if isinstance(getattr(sess, "allocation_decision_json", None), dict) else {}
    if (
        alloc
        and not alloc.get("allowed_if_enforced", True)
        and bool(getattr(settings, "brain_allocator_live_hard_block_enabled", False))
    ):
        readiness = dict(readiness or {})
        readiness["_allocator_block_live"] = str(alloc.get("blocked_reason") or "allocator_blocked")
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    canon = canonical_operator_state(mode=sess.mode, state=sess.state, risk_snapshot_json=snap)
    hint = phase_hint(mode=sess.mode, state=sess.state, risk_snapshot_json=snap)
    blocked = blocked_reason_for_session(mode=sess.mode, readiness=readiness, canonical_state=canon)
    nxt = next_action_required(
        mode=sess.mode,
        state=sess.state,
        canonical_state=canon,
        readiness=readiness,
        blocked=blocked,
    )
    return {
        "canonical_operator_state": canon,
        "phase_hint": hint,
        "blocked_reason": blocked,
        "next_action_required": nxt,
        "is_armed_only_live": is_armed_only_live(mode=sess.mode, state=sess.state),
        "is_live_orders_active": is_live_orders_active(mode=sess.mode, state=sess.state),
    }


TERMINAL_STATES = frozenset(PAPER_RUNNER_TERMINAL_STATES) | frozenset(LIVE_RUNNER_TERMINAL_STATES)
PAUSABLE_STATES = frozenset(PAPER_RUNNER_RUNNABLE_STATES) | frozenset(LIVE_RUNNER_RUNNABLE_STATES)


def _variant_refinement_info(variant: MomentumStrategyVariant) -> dict[str, Any]:
    return {
        "is_refined": bool(getattr(variant, "parent_variant_id", None)),
        "parent_variant_id": getattr(variant, "parent_variant_id", None),
        "meta": variant.refinement_meta_json if isinstance(variant.refinement_meta_json, dict) else {},
    }


def _last_tick_from_snapshot(sess: TradingAutomationSession) -> str | None:
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    if sess.mode == "live":
        le = snap.get("momentum_live_execution")
        return le.get("last_tick_utc") if isinstance(le, dict) else None
    pe = snap.get("momentum_paper_execution")
    return pe.get("last_tick_utc") if isinstance(pe, dict) else None


def _latest_scheduler_heartbeat_at(db: Session) -> datetime | None:
    try:
        row = (
            db.query(BrainBatchJob.ended_at)
            .filter(
                BrainBatchJob.job_type == JOB_SCHEDULER_WORKER_HEARTBEAT,
                BrainBatchJob.status == "ok",
            )
            .order_by(BrainBatchJob.ended_at.desc())
            .first()
        )
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _runner_health_for_mode(
    db: Session,
    *,
    mode: str,
    sess: TradingAutomationSession | None = None,
) -> dict[str, Any]:
    now = datetime.utcnow()
    m = (mode or "paper").strip().lower()
    if m == "live":
        enabled = bool(settings.chili_momentum_live_runner_enabled)
        scheduler_enabled = bool(settings.chili_momentum_live_runner_scheduler_enabled)
        interval_minutes = int(settings.chili_momentum_live_runner_scheduler_interval_minutes)
    else:
        enabled = bool(settings.chili_momentum_paper_runner_enabled)
        scheduler_enabled = bool(settings.chili_momentum_paper_runner_scheduler_enabled)
        interval_minutes = int(settings.chili_momentum_paper_runner_scheduler_interval_minutes)

    hb_at = _latest_scheduler_heartbeat_at(db)
    hb_age = (now - hb_at).total_seconds() if hb_at else None
    blocked_reason = None
    if not enabled:
        blocked_reason = f"{m}_runner_disabled"
    elif not scheduler_enabled:
        blocked_reason = f"{m}_runner_scheduler_disabled"
    elif hb_age is None:
        blocked_reason = "scheduler_worker_heartbeat_missing"
    elif hb_age > max(420.0, float(interval_minutes) * 120.0):
        blocked_reason = "scheduler_worker_stale"

    last_tick = _last_tick_from_snapshot(sess) if sess is not None else None
    if last_tick is None:
        latest = (
            db.query(TradingAutomationSession)
            .filter(TradingAutomationSession.mode == m)
            .order_by(TradingAutomationSession.updated_at.desc())
            .first()
        )
        last_tick = _last_tick_from_snapshot(latest) if latest is not None else None

    next_tick_eta_seconds = None
    if enabled and scheduler_enabled and hb_at is not None:
        next_tick_eta_seconds = max(0, int(interval_minutes * 60 - max(0.0, hb_age or 0.0)))

    return {
        "mode": m,
        "enabled": enabled,
        "scheduler_enabled": scheduler_enabled,
        "interval_minutes": interval_minutes,
        "last_tick_utc": last_tick,
        "scheduler_heartbeat_utc": hb_at.isoformat() if hb_at else None,
        "next_tick_eta_seconds": next_tick_eta_seconds,
        "blocked_reason": blocked_reason,
    }


def _controls_for_session(
    sess: TradingAutomationSession,
    *,
    paused: bool,
    runner_health: dict[str, Any],
) -> dict[str, Any]:
    runner_enabled = bool(runner_health.get("enabled"))
    is_terminal = sess.state in TERMINAL_STATES or sess.state == STATE_ARCHIVED
    run_enabled = False
    if paused:
        run_enabled = False
    elif is_terminal:
        run_enabled = runner_enabled
    elif sess.mode == "paper" and sess.state in (STATE_DRAFT, STATE_IDLE, STATE_QUEUED):
        run_enabled = runner_enabled
    elif sess.mode == "live" and sess.state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE):
        run_enabled = runner_enabled

    pause_enabled = (sess.state in PAUSABLE_STATES) and not paused and sess.state not in (STATE_DRAFT, STATE_IDLE)
    resume_enabled = paused and runner_enabled
    stop_enabled = sess.state != STATE_ARCHIVED and sess.state not in TERMINAL_STATES
    delete_enabled = sess.state != STATE_ARCHIVED
    return {
        "run": {"enabled": run_enabled, "label": "Run again" if is_terminal else "Run"},
        "pause": {"enabled": pause_enabled, "label": "Pause"},
        "resume": {"enabled": resume_enabled, "label": "Resume"},
        "stop": {"enabled": stop_enabled, "label": "Stop"},
        "delete": {"enabled": delete_enabled, "label": "Delete"},
    }


def _fresh_run_snapshot(sess: TradingAutomationSession) -> dict[str, Any]:
    snap = dict(sess.risk_snapshot_json or {})
    for key in (
        "momentum_paper_execution",
        "momentum_live_execution",
        "operator_pause",
        "arm_token",
        "expires_at_utc",
        "arm_confirmed_at_utc",
        "arm_confirmed",
    ):
        snap.pop(key, None)
    return snap


def _find_duplicate_active_session(
    db: Session,
    *,
    user_id: int,
    sess: TradingAutomationSession,
) -> TradingAutomationSession | None:
    q = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == user_id,
            TradingAutomationSession.symbol == sess.symbol,
            TradingAutomationSession.variant_id == sess.variant_id,
            TradingAutomationSession.mode == sess.mode,
            TradingAutomationSession.id != int(sess.id),
            TradingAutomationSession.state != STATE_ARCHIVED,
        )
        .all()
    )
    for row in q:
        if row.state not in TERMINAL_STATES:
            return row
    return None


def _serialize_binding(binding: TradingAutomationSessionBinding | None, *, sess: TradingAutomationSession, quote_source: str | None = None, blocked_reason: str | None = None) -> dict[str, Any]:
    if binding is None:
        return default_session_binding(
            venue=sess.venue,
            mode=sess.mode,
            execution_family=sess.execution_family,
            quote_source=quote_source,
            gating_reason=blocked_reason,
        )
    meta_json = binding.meta_json if isinstance(binding.meta_json, dict) else {}
    return {
        "discovery_provider": binding.discovery_provider,
        "chart_provider": binding.chart_provider,
        "signal_provider": binding.signal_provider,
        "source_of_truth_provider": binding.source_of_truth_provider,
        "source_of_truth_exchange": binding.source_of_truth_exchange,
        "bar_builder": binding.bar_builder,
        "latency_class": binding.latency_class,
        "simulation_fidelity": binding.simulation_fidelity,
        "gating_reason": blocked_reason or binding.gating_reason,
        "meta_json": meta_json,
    }


def _focus_priority(sess: TradingAutomationSession) -> tuple[int, float]:
    if sess.state == STATE_ARCHIVED:
        return (2, 0.0)
    if sess.mode == "live" and sess.state not in _LIVE_TERMINAL_FOR_FOCUS:
        ts = (sess.updated_at or sess.started_at or datetime.utcnow()).timestamp()
        return (0, -ts)
    if sess.mode == "paper" and sess.state not in _PAPER_TERMINAL_FOR_FOCUS:
        ts = (sess.updated_at or sess.started_at or datetime.utcnow()).timestamp()
        return (1, -ts)
    ts = (sess.updated_at or sess.started_at or datetime.utcnow()).timestamp()
    return (2, -ts)


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
            "operator_readiness": build_momentum_operator_readiness(execution_family="coinbase_spot"),
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
    fill_counts: dict[int, int] = {}
    fills_present = _table_exists(db, "trading_automation_simulated_fills")
    runtime_present = _table_exists(db, "trading_automation_runtime_snapshots")
    binding_present = _table_exists(db, "trading_automation_session_bindings")
    if ids:
        for sid, cnt in (
            db.query(TradingAutomationEvent.session_id, func.count(TradingAutomationEvent.id))
            .filter(TradingAutomationEvent.session_id.in_(ids))
            .group_by(TradingAutomationEvent.session_id)
            .all()
        ):
            counts[int(sid)] = int(cnt)
        if fills_present:
            for sid, cnt in (
                db.query(TradingAutomationSimulatedFill.session_id, func.count(TradingAutomationSimulatedFill.id))
                .filter(TradingAutomationSimulatedFill.session_id.in_(ids))
                .group_by(TradingAutomationSimulatedFill.session_id)
                .all()
            ):
                fill_counts[int(sid)] = int(cnt)

    runtime_map: dict[int, TradingAutomationRuntimeSnapshot] = {}
    binding_map: dict[int, TradingAutomationSessionBinding] = {}
    if ids:
        if runtime_present:
            for row in (
                db.query(TradingAutomationRuntimeSnapshot)
                .filter(TradingAutomationRuntimeSnapshot.session_id.in_(ids))
                .all()
            ):
                runtime_map[int(row.session_id)] = row
        if binding_present:
            for row in (
                db.query(TradingAutomationSessionBinding)
                .filter(TradingAutomationSessionBinding.session_id.in_(ids))
                .all()
            ):
                binding_map[int(row.session_id)] = row

    symbols = [str(s[0].symbol) for s in rows]
    variant_ids = [int(s[0].variant_id) for s in rows]
    viability_map: dict[tuple[str, int], MomentumSymbolViability] = {}
    if symbols and variant_ids:
        for via in (
            db.query(MomentumSymbolViability)
            .filter(
                MomentumSymbolViability.symbol.in_(symbols),
                MomentumSymbolViability.variant_id.in_(variant_ids),
            )
            .all()
        ):
            viability_map[(str(via.symbol), int(via.variant_id))] = via

    sessions_out: list[dict[str, Any]] = []
    for sess, var in rows:
        ef = (sess.execution_family or "coinbase_spot").strip().lower()
        rd = build_momentum_operator_readiness(execution_family=ef, symbol=sess.symbol)
        rd = merge_repeatable_edge_robustness_into_readiness(
            rd, db, scan_pattern_id=getattr(var, "scan_pattern_id", None)
        )
        op_fields = operator_fields_for_session(sess, rd)
        paused = is_operator_paused(sess.risk_snapshot_json)
        pause_info = operator_pause_info(sess.risk_snapshot_json)
        runner_health = _runner_health_for_mode(db, mode=sess.mode, sess=sess)
        via = viability_map.get((str(sess.symbol), int(sess.variant_id)))
        runtime_values = build_runtime_snapshot_values(
            sess,
            variant=var,
            viability=via,
            trade_count=fill_counts.get(int(sess.id), 0),
            execution_readiness={
                "operator_readiness": rd,
                "blocked_reason": op_fields.get("blocked_reason"),
            },
        )
        runtime_row = runtime_map.get(int(sess.id))
        binding_payload = _serialize_binding(
            binding_map.get(int(sess.id)),
            sess=sess,
            quote_source=(runtime_row.metrics_json if runtime_row and isinstance(runtime_row.metrics_json, dict) else {}).get("paper_execution", {}).get("last_quote_source"),
            blocked_reason=op_fields.get("blocked_reason"),
        )
        data_fidelity = {
            "lane": runtime_values.get("lane"),
            "simulation_fidelity": binding_payload.get("simulation_fidelity"),
            "latency_class": binding_payload.get("latency_class"),
            "source_of_truth_provider": binding_payload.get("source_of_truth_provider"),
            "source_of_truth_exchange": binding_payload.get("source_of_truth_exchange"),
        }
        runtime_payload = {
            "seconds": runtime_values.get("runtime_seconds"),
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
        }
        row = {
            "id": sess.id,
            "symbol": sess.symbol,
            "variant_id": sess.variant_id,
            "variant": _variant_brief(var),
            "strategy_family": var.family,
            "mode": sess.mode,
            "venue": sess.venue,
            "execution_family": sess.execution_family,
            "state": sess.state,
            "asset_class": asset_class_for_symbol(sess.symbol),
            "market_open_now": market_open_now(sess.symbol),
            "created_at": sess.created_at.isoformat() if sess.created_at else None,
            "updated_at": sess.updated_at.isoformat() if sess.updated_at else None,
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
            "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
            "correlation_id": sess.correlation_id,
            "source_node_id": sess.source_node_id,
            "source_paper_session_id": getattr(sess, "source_paper_session_id", None),
            "event_count": counts.get(sess.id, 0),
            "status_summary": _status_summary(sess.state),
            "warnings": _session_warnings(sess),
            "risk_status": summarize_risk_from_snapshot(sess.risk_snapshot_json),
            "paper_execution": summarize_paper_execution(sess.risk_snapshot_json),
            "live_execution": summarize_live_execution(sess.risk_snapshot_json),
            "is_paused": paused,
            "pause_info": pause_info,
            "runner_health": runner_health,
            "lane": runtime_values.get("lane"),
            "runtime": runtime_payload,
            "thesis": runtime_values.get("thesis"),
            "confidence": runtime_values.get("confidence"),
            "conviction": runtime_values.get("conviction"),
            "current_position_state": runtime_values.get("current_position_state"),
            "last_action": runtime_values.get("last_action"),
            "execution_readiness": runtime_values.get("execution_readiness_json"),
            "data_binding": binding_payload,
            "data_fidelity": data_fidelity,
            "simulated_pnl": runtime_values.get("simulated_pnl_usd"),
            "trade_count": runtime_values.get("trade_count"),
            "chart_levels": runtime_values.get("latest_levels_json"),
            "strategy_params_summary": summarize_strategy_params(var.params_json),
            "refinement_info": _variant_refinement_info(var),
            "controls": _controls_for_session(sess, paused=paused, runner_health=runner_health),
            "repeatable_edge_readiness": {
                "execution_robustness": rd.get("repeatable_edge_execution_robustness"),
                "execution_robustness_v2": rd.get("repeatable_edge_execution_robustness_v2"),
                "allocation_state": rd.get("repeatable_edge_allocation_state"),
                "live_not_recommended": rd.get("repeatable_edge_live_not_recommended"),
                "live_not_recommended_reason": rd.get("repeatable_edge_live_not_recommended_reason"),
            },
            "allocation": sess.allocation_decision_json if isinstance(getattr(sess, "allocation_decision_json", None), dict) else {},
        }
        row.update(op_fields)
        sessions_out.append(row)

    return {
        "sessions": sessions_out,
        "neural": neural_config_strip(),
        "governance": governance_strip(),
        "risk_policy_summary": effective_policy_summary(),
        "limitations_note": LIMITATIONS_NOTE,
        "operator_readiness": build_momentum_operator_readiness(execution_family="coinbase_spot"),
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
    via_full = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == sess.variant_id,
        )
        .one_or_none()
    )

    momentum_feedback = None
    try:
        from .feedback_query import get_session_feedback_row

        momentum_feedback = get_session_feedback_row(db, session_id=sess.id)
    except Exception:
        momentum_feedback = None

    ef = (sess.execution_family or "coinbase_spot").strip().lower()
    readiness = build_momentum_operator_readiness(execution_family=ef, symbol=sess.symbol)
    readiness = merge_repeatable_edge_robustness_into_readiness(
        readiness, db, scan_pattern_id=getattr(var, "scan_pattern_id", None)
    )
    op_fields = operator_fields_for_session(sess, readiness)
    paused = is_operator_paused(sess.risk_snapshot_json)
    pause_info = operator_pause_info(sess.risk_snapshot_json)
    runner_health = _runner_health_for_mode(db, mode=sess.mode, sess=sess)
    fill_rows = []
    if _table_exists(db, "trading_automation_simulated_fills"):
        fill_rows = (
            db.query(TradingAutomationSimulatedFill)
            .filter(TradingAutomationSimulatedFill.session_id == sess.id)
            .order_by(TradingAutomationSimulatedFill.ts.desc())
            .limit(40)
            .all()
        )
    binding_row = None
    if _table_exists(db, "trading_automation_session_bindings"):
        binding_row = (
            db.query(TradingAutomationSessionBinding)
            .filter(TradingAutomationSessionBinding.session_id == sess.id)
            .one_or_none()
        )
    runtime_values = build_runtime_snapshot_values(
        sess,
        variant=var,
        viability=via_full,
        trade_count=len(fill_rows),
        execution_readiness={"operator_readiness": readiness, "blocked_reason": op_fields.get("blocked_reason")},
    )
    binding_payload = _serialize_binding(
        binding_row,
        sess=sess,
        quote_source=(runtime_values.get("metrics_json") or {}).get("paper_execution", {}).get("last_quote_source"),
        blocked_reason=op_fields.get("blocked_reason"),
    )

    src_id = getattr(sess, "source_paper_session_id", None)
    source_paper_brief = None
    if src_id:
        src = (
            db.query(TradingAutomationSession)
            .filter(TradingAutomationSession.id == int(src_id), TradingAutomationSession.user_id == user_id)
            .one_or_none()
        )
        if src:
            source_paper_brief = {
                "id": src.id,
                "symbol": src.symbol,
                "mode": src.mode,
                "state": src.state,
                "updated_at": src.updated_at.isoformat() if src.updated_at else None,
            }

    session_dict = {
        "id": sess.id,
        "symbol": sess.symbol,
        "variant_id": sess.variant_id,
        "variant": _variant_brief(var),
        "strategy_family": var.family,
        "mode": sess.mode,
        "venue": sess.venue,
        "execution_family": sess.execution_family,
        "state": sess.state,
        "asset_class": asset_class_for_symbol(sess.symbol),
        "market_open_now": market_open_now(sess.symbol),
        "created_at": sess.created_at.isoformat() if sess.created_at else None,
        "updated_at": sess.updated_at.isoformat() if sess.updated_at else None,
        "started_at": sess.started_at.isoformat() if sess.started_at else None,
        "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
        "correlation_id": sess.correlation_id,
        "source_node_id": sess.source_node_id,
        "source_paper_session_id": src_id,
        "source_paper_brief": source_paper_brief,
        "risk_snapshot_summary": risk_summary,
        "status_summary": _status_summary(sess.state),
        "warnings": _session_warnings(sess),
        "risk_status": summarize_risk_from_snapshot(sess.risk_snapshot_json),
        "paper_execution": summarize_paper_execution(sess.risk_snapshot_json),
        "live_execution": summarize_live_execution(sess.risk_snapshot_json),
        "is_paused": paused,
        "pause_info": pause_info,
        "runner_health": runner_health,
        "momentum_feedback": momentum_feedback,
        "lane": runtime_values.get("lane"),
        "runtime": {
            "seconds": runtime_values.get("runtime_seconds"),
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
        },
        "thesis": runtime_values.get("thesis"),
        "confidence": runtime_values.get("confidence"),
        "conviction": runtime_values.get("conviction"),
        "current_position_state": runtime_values.get("current_position_state"),
        "last_action": runtime_values.get("last_action"),
        "execution_readiness": runtime_values.get("execution_readiness_json"),
        "data_binding": binding_payload,
        "data_fidelity": {
            "simulation_fidelity": binding_payload.get("simulation_fidelity"),
            "latency_class": binding_payload.get("latency_class"),
            "source_of_truth_provider": binding_payload.get("source_of_truth_provider"),
            "source_of_truth_exchange": binding_payload.get("source_of_truth_exchange"),
        },
        "simulated_pnl": runtime_values.get("simulated_pnl_usd"),
        "trade_count": runtime_values.get("trade_count"),
        "chart_levels": runtime_values.get("latest_levels_json"),
        "strategy_params_summary": summarize_strategy_params(var.params_json),
        "refinement_info": _variant_refinement_info(var),
        "controls": _controls_for_session(sess, paused=paused, runner_health=runner_health),
        "repeatable_edge_readiness": {
            "execution_robustness": readiness.get("repeatable_edge_execution_robustness"),
            "execution_robustness_v2": readiness.get("repeatable_edge_execution_robustness_v2"),
            "allocation_state": readiness.get("repeatable_edge_allocation_state"),
            "live_not_recommended": readiness.get("repeatable_edge_live_not_recommended"),
            "live_not_recommended_reason": readiness.get("repeatable_edge_live_not_recommended_reason"),
        },
        "allocation": sess.allocation_decision_json if isinstance(getattr(sess, "allocation_decision_json", None), dict) else {},
    }
    session_dict.update(op_fields)

    return {
        "session": session_dict,
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
        "simulated_fills": [
            {
                "id": row.id,
                "ts": row.ts.isoformat() if row.ts else None,
                "lane": row.lane,
                "action": row.action,
                "fill_type": row.fill_type,
                "side": row.side,
                "quantity": row.quantity,
                "price": row.price,
                "reference_price": row.reference_price,
                "fees_usd": row.fees_usd,
                "pnl_usd": row.pnl_usd,
                "position_state_before": row.position_state_before,
                "position_state_after": row.position_state_after,
                "reason": row.reason,
                "marker_json": row.marker_json if isinstance(row.marker_json, dict) else {},
            }
            for row in fill_rows
        ],
        "viability_snapshot": viability_brief,
        "neural": neural_config_strip(),
        "governance": governance_strip(),
        "risk_policy_summary": effective_policy_summary(),
        "limitations_note": LIMITATIONS_NOTE,
        "operator_readiness": readiness,
    }


def _payload_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    keys = ("symbol", "variant_id", "reason", "note", "arm_token_prefix", "hello")
    return {k: payload[k] for k in keys if k in payload}


def get_operator_session_focus(
    db: Session,
    *,
    user_id: int,
    symbol: Optional[str] = None,
) -> dict[str, Any]:
    """Latest session the operator should care about + shared readiness (coinbase_spot default path)."""
    base_readiness = build_momentum_operator_readiness(execution_family="coinbase_spot", symbol=symbol)
    if not _tables_present(db):
        return {
            "ok": True,
            "operator_readiness": base_readiness,
            "focus_session": None,
            "events_preview": [],
            "session_lifecycle_doc": None,
        }

    expire_stale_live_arm_sessions(db, user_id=user_id)

    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.user_id == user_id,
        TradingAutomationSession.state != STATE_ARCHIVED,
    )
    if symbol:
        q = q.filter(TradingAutomationSession.symbol == symbol.strip().upper())
    rows = q.all()
    if not rows:
        return {
            "ok": True,
            "operator_readiness": base_readiness,
            "focus_session": None,
            "events_preview": [],
            "session_lifecycle_doc": None,
        }

    focus = min(rows, key=_focus_priority)
    ef = (focus.execution_family or "coinbase_spot").strip().lower()
    readiness = build_momentum_operator_readiness(execution_family=ef, symbol=focus.symbol)
    vrow = (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.id == int(focus.variant_id))
        .one_or_none()
    )
    readiness = merge_repeatable_edge_robustness_into_readiness(
        readiness,
        db,
        scan_pattern_id=getattr(vrow, "scan_pattern_id", None) if vrow else None,
    )

    ev_rows = (
        db.query(TradingAutomationEvent)
        .filter(TradingAutomationEvent.session_id == focus.id)
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(8)
        .all()
    )
    preview = [
        {
            "id": ev.id,
            "ts": ev.ts.isoformat() if ev.ts else None,
            "event_type": ev.event_type,
            "payload_summary": _payload_summary(ev.payload_json),
        }
        for ev in ev_rows
    ]

    src_id = getattr(focus, "source_paper_session_id", None)
    source_paper_brief = None
    if src_id:
        src = (
            db.query(TradingAutomationSession)
            .filter(TradingAutomationSession.id == int(src_id), TradingAutomationSession.user_id == user_id)
            .one_or_none()
        )
        if src:
            source_paper_brief = {
                "id": src.id,
                "symbol": src.symbol,
                "mode": src.mode,
                "state": src.state,
                "updated_at": src.updated_at.isoformat() if src.updated_at else None,
            }

    snap = focus.risk_snapshot_json if isinstance(focus.risk_snapshot_json, dict) else {}
    op = operator_fields_for_session(focus, readiness)
    from .session_lifecycle import session_state_machine_doc

    focus_out = {
        "id": focus.id,
        "symbol": focus.symbol,
        "variant_id": focus.variant_id,
        "mode": focus.mode,
        "venue": focus.venue,
        "execution_family": focus.execution_family,
        "state": focus.state,
        "source_paper_session_id": src_id,
        "source_paper_brief": source_paper_brief,
        "created_at": focus.created_at.isoformat() if focus.created_at else None,
        "updated_at": focus.updated_at.isoformat() if focus.updated_at else None,
        "last_transition_at": focus.updated_at.isoformat() if focus.updated_at else None,
        "risk_status": summarize_risk_from_snapshot(focus.risk_snapshot_json),
        "paper_execution": summarize_paper_execution(snap),
        "live_execution": summarize_live_execution(snap),
        "repeatable_edge_readiness": {
            "execution_robustness": readiness.get("repeatable_edge_execution_robustness"),
            "execution_robustness_v2": readiness.get("repeatable_edge_execution_robustness_v2"),
            "allocation_state": readiness.get("repeatable_edge_allocation_state"),
            "live_not_recommended": readiness.get("repeatable_edge_live_not_recommended"),
            "live_not_recommended_reason": readiness.get("repeatable_edge_live_not_recommended_reason"),
        },
        "allocation": focus.allocation_decision_json if isinstance(getattr(focus, "allocation_decision_json", None), dict) else {},
        **op,
    }

    return {
        "ok": True,
        "operator_readiness": readiness,
        "focus_session": focus_out,
        "events_preview": preview,
        "session_lifecycle_doc": session_state_machine_doc(),
    }


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
                "operator_readiness": build_momentum_operator_readiness(execution_family="coinbase_spot"),
                "paper_runner_health": _runner_health_for_mode(db, mode="paper"),
                "live_runner_health": _runner_health_for_mode(db, mode="live"),
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
            "operator_readiness": build_momentum_operator_readiness(execution_family="coinbase_spot"),
            "paper_runner_health": _runner_health_for_mode(db, mode="paper"),
            "live_runner_health": _runner_health_for_mode(db, mode="live"),
            "lanes": {
                "simulation": pending_draft + paper_queued + paper_active,
                "live-armed": pending_arm + armed,
                "live": live_queued + live_active,
            },
        }
    )
    return summary


def _clone_session_for_run(
    db: Session,
    *,
    user_id: int,
    sess: TradingAutomationSession,
) -> TradingAutomationSession:
    dup = _find_duplicate_active_session(db, user_id=user_id, sess=sess)
    if dup is not None:
        return dup
    new_state = STATE_QUEUED if sess.mode == "paper" else STATE_QUEUED_LIVE
    clone = create_trading_automation_session(
        db,
        user_id=user_id,
        venue=sess.venue,
        execution_family=sess.execution_family,
        mode=sess.mode,
        symbol=sess.symbol,
        variant_id=int(sess.variant_id),
        state=new_state,
        risk_snapshot_json=_fresh_run_snapshot(sess),
        correlation_id=str(uuid.uuid4()),
        source_node_id="momentum_automation_monitor",
        source_paper_session_id=getattr(sess, "source_paper_session_id", None),
    )
    append_trading_automation_event(
        db,
        clone.id,
        "session_cloned_for_run",
        {"source_session_id": int(sess.id), "source_state": sess.state},
        correlation_id=clone.correlation_id,
        source_node_id="momentum_automation_monitor",
    )
    return clone


def run_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}
    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {"ok": False, "error": "not_found"}

    runner_health = _runner_health_for_mode(db, mode=sess.mode, sess=sess)
    if not runner_health.get("enabled"):
        return {"ok": False, "error": "runner_disabled", "runner_health": runner_health}

    target = sess
    paused = is_operator_paused(sess.risk_snapshot_json)
    if paused:
        sess.risk_snapshot_json = clear_operator_pause(sess.risk_snapshot_json)
        sess.updated_at = datetime.utcnow()
        append_trading_automation_event(
            db,
            sess.id,
            "session_resumed",
            {"by": "operator_run"},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    elif sess.state in TERMINAL_STATES or sess.state == STATE_ARCHIVED:
        target = _clone_session_for_run(db, user_id=user_id, sess=sess)
    elif sess.mode == "paper" and sess.state in (STATE_DRAFT, STATE_IDLE):
        prev = sess.state
        sess.state = STATE_QUEUED
        sess.updated_at = datetime.utcnow()
        append_trading_automation_event(
            db,
            sess.id,
            "session_run_requested",
            {"previous_state": prev, "mode": "paper"},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    elif sess.mode == "live" and sess.state == STATE_ARMED_PENDING_RUNNER:
        sess.state = STATE_QUEUED_LIVE
        sess.updated_at = datetime.utcnow()
        append_trading_automation_event(
            db,
            sess.id,
            "session_run_requested",
            {"previous_state": STATE_ARMED_PENDING_RUNNER, "mode": "live"},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    elif sess.mode == "paper" and sess.state not in PAPER_RUNNER_RUNNABLE_STATES:
        return {"ok": False, "error": "not_runnable", "state": sess.state}
    elif sess.mode == "live" and sess.state not in LIVE_RUNNER_RUNNABLE_STATES:
        return {"ok": False, "error": "not_runnable", "state": sess.state}

    if target.mode == "paper":
        tick_result = tick_paper_session(db, int(target.id))
    else:
        tick_result = tick_live_session(db, int(target.id))
    return {
        "ok": True,
        "session_id": int(target.id),
        "cloned_from_session_id": int(sess.id) if target.id != sess.id else None,
        "state": target.state,
        "tick_result": tick_result,
    }


def pause_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}
    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {"ok": False, "error": "not_found"}
    if sess.state not in PAUSABLE_STATES:
        return {"ok": False, "error": "not_pausable", "state": sess.state}
    if is_operator_paused(sess.risk_snapshot_json):
        return {"ok": False, "error": "already_paused"}
    sess.risk_snapshot_json = apply_operator_pause(sess.risk_snapshot_json, state=sess.state)
    sess.updated_at = datetime.utcnow()
    append_trading_automation_event(
        db,
        sess.id,
        "session_paused",
        {"state": sess.state},
        correlation_id=sess.correlation_id,
        source_node_id="momentum_automation_monitor",
    )
    return {"ok": True, "session_id": int(sess.id), "state": sess.state, "pause_info": operator_pause_info(sess.risk_snapshot_json)}


def resume_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}
    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {"ok": False, "error": "not_found"}
    if not is_operator_paused(sess.risk_snapshot_json):
        return {"ok": False, "error": "not_paused"}
    return run_automation_session(db, user_id=user_id, session_id=int(sess.id))


def _flatten_live_session_for_stop(sess: TradingAutomationSession) -> dict[str, Any]:
    snap = dict(sess.risk_snapshot_json or {})
    le = snap.get("momentum_live_execution")
    le = dict(le) if isinstance(le, dict) else {}
    pos = le.get("position")
    entry_order_id = le.get("entry_order_id")
    if not entry_order_id and not isinstance(pos, dict):
        return {"ok": True, "action": "no_live_orders"}

    ef = normalize_execution_family(sess.execution_family)
    try:
        adapter = resolve_live_spot_adapter_factory(ef)()
    except ExecutionFamilyNotImplementedError:
        return {"ok": False, "error": "execution_family_not_implemented"}
    if not adapter.is_enabled():
        return {"ok": False, "error": "live_adapter_unavailable"}

    if entry_order_id and not isinstance(pos, dict):
        adapter.cancel_order(str(entry_order_id))
        return {"ok": True, "action": "cancelled_entry_order", "order_id": str(entry_order_id)}

    if isinstance(pos, dict) and float(pos.get("quantity") or 0.0) > 0:
        product_id = str(pos.get("product_id") or sess.symbol)
        qty = _fmt_base_size(float(pos.get("quantity") or 0.0))
        client_order_id = f"chili_ml_stop_{sess.id}_{uuid.uuid4().hex[:10]}"
        result = adapter.place_market_order(
            product_id=product_id,
            side="sell",
            base_size=qty,
            client_order_id=client_order_id,
        )
        le["exit_order_id"] = result.get("order_id")
        le["exit_client_order_id"] = result.get("client_order_id")
        le["last_exit_reason"] = "operator_stop"
        le["position"] = None
        snap["momentum_live_execution"] = le
        sess.risk_snapshot_json = snap
        return {"ok": True, "action": "flattened_live_position", "order_result": result}
    return {"ok": True, "action": "no_live_orders"}


def stop_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}
    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {"ok": False, "error": "not_found"}
    if sess.state == STATE_ARCHIVED or sess.state in TERMINAL_STATES:
        return {"ok": False, "error": "already_terminal", "state": sess.state}

    live_stop = None
    if sess.mode == "live":
        live_stop = _flatten_live_session_for_stop(sess)
        if not live_stop.get("ok"):
            return live_stop

    now = datetime.utcnow()
    prev = sess.state
    sess.state = STATE_LIVE_CANCELLED if sess.mode == "live" else STATE_CANCELLED
    sess.ended_at = now
    sess.updated_at = now
    sess.risk_snapshot_json = clear_operator_pause(sess.risk_snapshot_json)
    append_trading_automation_event(
        db,
        sess.id,
        "session_stopped",
        {"previous_state": prev, "terminal_state": sess.state, "live_stop": live_stop},
        correlation_id=sess.correlation_id,
        source_node_id="momentum_automation_monitor",
    )
    try:
        from .feedback_emit import emit_feedback_after_terminal_transition

        emit_feedback_after_terminal_transition(db, sess)
    except Exception:
        pass
    return {"ok": True, "session_id": int(sess.id), "state": sess.state, "live_stop": live_stop}


def delete_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    return archive_automation_session(db, user_id=user_id, session_id=session_id)


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
    sess.risk_snapshot_json = clear_operator_pause(sess.risk_snapshot_json)

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
    sess.risk_snapshot_json = clear_operator_pause(sess.risk_snapshot_json)

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
