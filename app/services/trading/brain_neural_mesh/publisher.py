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
