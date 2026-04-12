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

# Maps cluster_id → last step sid (fires cluster completion when step completes)
_CLUSTER_LAST_STEP: dict[str, str] = {
    "c_state": "decay",
    "c_discovery": "seek",
    "c_validation": "bt_queue",
    "c_evolution": "breakout",
    "c_secondary": "synergy",
    "c_journal": "signals",
    "c_meta_learning": "ml",
    "c_decisioning": "proposals",
    "c_control": "finalize",
}


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
