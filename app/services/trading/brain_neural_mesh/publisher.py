"""Enqueue sensory / cycle-complete events (guarded by feature flag)."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....config import settings
from .metrics import get_counters
from .repository import enqueue_activation
from .schema import LOG_PREFIX, mesh_enabled

_log = logging.getLogger(__name__)


def publish_market_snapshots_refreshed(db: Session, *, meta: Optional[dict[str, Any]] = None) -> None:
    if not mesh_enabled():
        return
    try:
        cid = str(uuid.uuid4())
        enqueue_activation(
            db,
            source_node_id="nm_snap_daily",
            cause="brain_market_snapshots",
            payload={"signal_type": "snapshot_refresh", "meta": meta or {}},
            confidence_delta=0.25,
            propagation_depth=0,
            correlation_id=cid,
        )
        get_counters().note_publish(1)
        _log.info("%s published snapshot_refresh correlation=%s", LOG_PREFIX, cid)
    except Exception as e:
        _log.warning("%s publish_market_snapshots_refreshed failed: %s", LOG_PREFIX, e)
    publish_momentum_context_refresh(db, meta=meta)


def publish_setup_vitals_change(
    db: Session,
    *,
    trade_id: int,
    ticker: str,
    vitals: Any,
    previous_composite: Optional[float] = None,
) -> None:
    """Enqueue mesh activation when setup vitals materially shift (threshold crossing)."""
    if not mesh_enabled():
        return
    try:
        cur = float(getattr(vitals, "composite_health", 0.5) or 0.5)
        prev = float(previous_composite) if previous_composite is not None else None
        if prev is None:
            return
        crossed_low = prev >= 0.45 and cur < 0.45
        big_drop = (prev - cur) >= 0.15
        if not crossed_low and not big_drop:
            return
        cid = str(uuid.uuid4())
        enqueue_activation(
            db,
            source_node_id="nm_setup_health",
            cause="setup_vitals_change",
            payload={
                "signal_type": "setup_vitals_change",
                "trade_id": trade_id,
                "ticker": (ticker or "").upper(),
                "composite_health": cur,
                "previous_composite": prev,
                "vitals": vitals.to_dict() if hasattr(vitals, "to_dict") else {},
            },
            confidence_delta=0.12,
            propagation_depth=0,
            correlation_id=cid,
        )
        get_counters().note_publish(1)
        _log.debug("%s setup_vitals_change trade=%s ticker=%s cur=%s prev=%s", LOG_PREFIX, trade_id, ticker, cur, prev)
    except Exception as e:
        _log.warning("%s publish_setup_vitals_change failed: %s", LOG_PREFIX, e)


def publish_momentum_context_refresh(db: Session, *, meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Enqueue neural momentum context tick (neural mesh only; not learning-cycle).

    Returns a small status dict for operator APIs (correlation_id, activation row id).
    """
    base: dict[str, Any] = {
        "ok": False,
        "reason": "disabled",
        "correlation_id": None,
        "activation_event_id": None,
    }
    if not mesh_enabled():
        base["reason"] = "mesh_disabled"
        return base
    if not getattr(settings, "chili_momentum_neural_enabled", True):
        base["reason"] = "momentum_neural_disabled"
        return base
    try:
        cid = str(uuid.uuid4())
        eid = enqueue_activation(
            db,
            source_node_id="nm_event_bus",
            cause="momentum_context_refresh",
            payload={"signal_type": "momentum_context_refresh", "meta": meta or {}},
            confidence_delta=0.12,
            propagation_depth=0,
            correlation_id=cid,
        )
        get_counters().note_publish(1)
        _log.debug("%s published momentum_context_refresh correlation=%s", LOG_PREFIX, cid)
        return {
            "ok": True,
            "reason": None,
            "correlation_id": cid,
            "activation_event_id": eid,
        }
    except Exception as e:
        _log.warning("%s publish_momentum_context_refresh failed: %s", LOG_PREFIX, e)
        return {"ok": False, "reason": str(e), "correlation_id": None, "activation_event_id": None}


_KEY_STEPS = frozenset({"mine", "bt_queue", "hypotheses", "ml", "depromote"})

# Maps cluster_id → last step sid (fires cluster completion when step completes).
# Must match the last ``CycleStepDef.sid`` in each cluster of
# ``learning_cycle_architecture.TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS`` (except c_universe).
_CLUSTER_LAST_STEP: dict[str, str] = {
    "c_state": "decay",
    "c_discovery": "seek",
    "c_validation": "bt_queue",
    "c_evolution": "breakout",
    "c_secondary_structure": "refine",
    "c_secondary_outcomes": "sizing",
    "c_secondary_signals": "synergy",
    "c_journal": "signals",
    "c_meta_learning": "ml",
    "c_decisioning": "proposals",
    "c_control": "finalize",
}


def notify_learning_cycle_step_committed(
    db: Session,
    *,
    cluster_id: str,
    step_sid: str,
    elapsed_sec: float,
    extra: str = "",
    correlation_id: Optional[str] = None,
) -> None:
    """Central seam: enqueue a mesh activation after a learning-cycle step DB commit.

    Call this once per completed architecture step (after ``Session.commit`` for that step).
    Safe when mesh is disabled (no-op). Must never raise to callers.
    """
    try:
        publish_learning_step_completed(
            db,
            cluster_id=cluster_id,
            step_sid=step_sid,
            elapsed_sec=float(elapsed_sec),
            extra=extra or "",
            correlation_id=correlation_id,
        )
    except Exception as e:
        _log.warning("%s notify_learning_cycle_step_committed failed: %s", LOG_PREFIX, e)


def publish_learning_step_completed(
    db: Session,
    *,
    cluster_id: str,
    step_sid: str,
    elapsed_sec: float,
    extra: str = "",
    correlation_id: Optional[str] = None,
) -> None:
    """Publish a learning step completion into the neural mesh."""
    if not mesh_enabled():
        return
    try:
        cid = correlation_id or str(uuid.uuid4())
        source_node = f"nm_lc_{step_sid}"
        delta = 0.25 if step_sid in _KEY_STEPS else 0.15
        enqueue_activation(
            db,
            source_node_id=source_node,
            cause="learning_step_completed",
            payload={
                "signal_type": "step_completed",
                "cluster_id": cluster_id,
                "step_sid": step_sid,
                "elapsed_sec": elapsed_sec,
                "extra": extra,
            },
            confidence_delta=delta,
            propagation_depth=0,
            correlation_id=cid,
        )
        get_counters().note_publish(1)
        _log.debug("%s published learning_step_completed step=%s cluster=%s", LOG_PREFIX, step_sid, cluster_id)
        # Auto-fire cluster completion when last step in cluster fires
        if _CLUSTER_LAST_STEP.get(cluster_id) == step_sid:
            publish_learning_cluster_completed(db, cluster_id=cluster_id, correlation_id=cid)
    except Exception as e:
        _log.warning("%s publish_learning_step_completed failed: %s", LOG_PREFIX, e)


def publish_learning_cluster_completed(
    db: Session,
    *,
    cluster_id: str,
    correlation_id: Optional[str] = None,
) -> None:
    """Publish a learning cluster completion into the neural mesh."""
    if not mesh_enabled():
        return
    try:
        cid = correlation_id or str(uuid.uuid4())
        source_node = f"nm_lc_{cluster_id}"
        enqueue_activation(
            db,
            source_node_id=source_node,
            cause="learning_cluster_completed",
            payload={"signal_type": "cluster_completed", "cluster_id": cluster_id},
            confidence_delta=0.20,
            propagation_depth=0,
            correlation_id=cid,
        )
        get_counters().note_publish(1)
        _log.debug("%s published learning_cluster_completed cluster=%s", LOG_PREFIX, cluster_id)
    except Exception as e:
        _log.warning("%s publish_learning_cluster_completed failed: %s", LOG_PREFIX, e)


def publish_learning_cycle_completed(db: Session, *, elapsed_s: Optional[float] = None) -> None:
    if not mesh_enabled():
        return
    try:
        cid = str(uuid.uuid4())
        enqueue_activation(
            db,
            source_node_id="nm_event_bus",
            cause="learning_cycle_completed",
            payload={"signal_type": "state_tick", "elapsed_s": elapsed_s},
            confidence_delta=0.08,
            propagation_depth=0,
            correlation_id=cid,
        )
        get_counters().note_publish(1)
        _log.debug("%s published learning_cycle_completed correlation=%s", LOG_PREFIX, cid)
    except Exception as e:
        _log.warning("%s publish_learning_cycle_completed failed: %s", LOG_PREFIX, e)


def publish_brain_work_outcome(
    db: Session,
    *,
    outcome_type: str,
    scan_pattern_id: Optional[int] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Neural mesh observation for durable work outcomes (not the work ledger)."""
    if not mesh_enabled():
        return
    try:
        cid = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "signal_type": "brain_work_outcome",
            "outcome_type": outcome_type,
            "scan_pattern_id": scan_pattern_id,
            **(extra or {}),
        }
        enqueue_activation(
            db,
            source_node_id="nm_evidence_bt",
            cause="brain_work_outcome",
            payload=payload,
            confidence_delta=0.12,
            propagation_depth=0,
            correlation_id=cid,
        )
        get_counters().note_publish(1)
        _log.debug("%s brain_work_outcome type=%s pattern=%s", LOG_PREFIX, outcome_type, scan_pattern_id)
    except Exception as e:
        _log.warning("%s publish_brain_work_outcome failed: %s", LOG_PREFIX, e)


# ── Sensor node publishers (Phase 2: edge nodes publish structured output) ──


def publish_stop_eval(
    db: Session,
    *,
    trade_id: int,
    ticker: str,
    alert_event: str | None,
    state: str,
    old_stop: float | None,
    new_stop: float | None,
    reason: str,
    price: float,
    brain_context: dict[str, Any] | None = None,
    user_id: int | None = None,
) -> None:
    """Stop engine sensor: publish evaluation result to nm_stop_eval node."""
    if not mesh_enabled():
        return
    try:
        from .repository import get_or_create_state

        node_state = get_or_create_state(db, "nm_stop_eval")
        node_state.local_state = {
            "trade_id": trade_id,
            "ticker": (ticker or "").upper(),
            "alert_event": alert_event,
            "state": state,
            "old_stop": old_stop,
            "new_stop": new_stop,
            "stop_level": new_stop or old_stop,
            "reason": (reason or "")[:500],
            "price": price,
            "current_price": price,
            "brain_context": brain_context or {},
            "user_id": user_id,
            "action": alert_event,
            "urgency": "critical" if alert_event in ("STOP_HIT", "TIME_EXIT") else "info",
            "updated_at": _now_iso(),
        }

        urgency_delta = {
            "STOP_HIT": 0.35,
            "TIME_EXIT": 0.35,
            "STOP_APPROACHING": 0.20,
            "STOP_TIGHTENED": 0.12,
            "BREAKEVEN_REACHED": 0.10,
        }
        delta = urgency_delta.get(alert_event or "", 0.08)

        cid = str(uuid.uuid4())
        enqueue_activation(
            db,
            source_node_id="nm_stop_eval",
            cause="stop_eval",
            payload={
                "signal_type": alert_event or "stop_check",
                "trade_id": trade_id,
                "ticker": (ticker or "").upper(),
                "old_stop": old_stop,
                "new_stop": new_stop,
                "urgency": node_state.local_state["urgency"],
            },
            confidence_delta=delta,
            propagation_depth=0,
            correlation_id=cid,
        )
        get_counters().note_publish(1)
    except Exception as e:
        _log.warning("%s publish_stop_eval failed: %s", LOG_PREFIX, e)


def publish_pattern_health(
    db: Session,
    *,
    trade_id: int,
    ticker: str,
    action: str,
    health_score: float,
    health_delta: float | None,
    reasoning: str,
    new_stop: float | None = None,
    new_target: float | None = None,
    current_price: float = 0,
    pnl_pct: float | None = None,
    user_id: int | None = None,
    scan_pattern_id: int | None = None,
) -> None:
    """Pattern monitor sensor: publish health evaluation to nm_pattern_health node."""
    if not mesh_enabled():
        return
    try:
        from .repository import get_or_create_state

        node_state = get_or_create_state(db, "nm_pattern_health")

        urgency = "info"
        if action == "exit_now":
            urgency = "critical"
        elif action == "tighten_stop":
            urgency = "warning"

        node_state.local_state = {
            "trade_id": trade_id,
            "ticker": (ticker or "").upper(),
            "action": action,
            "health_score": health_score,
            "health_delta": health_delta,
            "reasoning": (reasoning or "")[:500],
            "new_stop": new_stop,
            "new_target": new_target,
            "price": current_price,
            "current_price": current_price,
            "pnl_pct": pnl_pct,
            "user_id": user_id,
            "scan_pattern_id": scan_pattern_id,
            "urgency": urgency,
            "updated_at": _now_iso(),
        }

        delta = {"exit_now": 0.35, "tighten_stop": 0.18, "loosen_target": 0.10}.get(action, 0.06)
        cid = str(uuid.uuid4())
        enqueue_activation(
            db,
            source_node_id="nm_pattern_health",
            cause="pattern_health",
            payload={
                "signal_type": action,
                "trade_id": trade_id,
                "ticker": (ticker or "").upper(),
                "health_score": health_score,
                "urgency": urgency,
            },
            confidence_delta=delta,
            propagation_depth=0,
            correlation_id=cid,
        )
        get_counters().note_publish(1)
    except Exception as e:
        _log.warning("%s publish_pattern_health failed: %s", LOG_PREFIX, e)


def publish_imminent_eval(
    db: Session,
    *,
    scan_pattern_id: int,
    ticker: str,
    composite_score: float,
    readiness: float,
    eta_lo: float,
    eta_hi: float,
    price: float | int,
    user_id: int | None = None,
) -> None:
    """Imminent breakout sensor: publish evaluation to nm_imminent_eval node."""
    if not mesh_enabled():
        return
    try:
        from .repository import get_or_create_state

        node_state = get_or_create_state(db, "nm_imminent_eval")
        node_state.local_state = {
            "scan_pattern_id": scan_pattern_id,
            "ticker": (ticker or "").upper(),
            "composite_score": composite_score,
            "readiness": readiness,
            "eta_lo": eta_lo,
            "eta_hi": eta_hi,
            "price": price,
            "current_price": price,
            "user_id": user_id,
            "urgency": "info" if composite_score < 0.75 else "warning",
            "action": "imminent_breakout",
            "updated_at": _now_iso(),
        }

        delta = 0.15 if composite_score >= 0.75 else 0.08
        cid = str(uuid.uuid4())
        enqueue_activation(
            db,
            source_node_id="nm_imminent_eval",
            cause="imminent_eval",
            payload={
                "signal_type": "imminent_breakout",
                "scan_pattern_id": scan_pattern_id,
                "ticker": (ticker or "").upper(),
                "composite_score": composite_score,
                "readiness": readiness,
            },
            confidence_delta=delta,
            propagation_depth=0,
            correlation_id=cid,
        )
        get_counters().note_publish(1)
    except Exception as e:
        _log.warning("%s publish_imminent_eval failed: %s", LOG_PREFIX, e)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
