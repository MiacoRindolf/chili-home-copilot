"""Fire handler registry: maps node IDs to callables invoked when a node fires.

When the propagation engine fires a node (activation crosses threshold + cooldown
elapsed), it checks this registry. If a handler is registered, it is called with
the DB session, the node, its state, and a fire context dict containing the
triggering event payload and child states.

Handlers may:
- Read children's ``local_state`` to aggregate context
- Write to the fired node's ``local_state`` (structured output)
- Trigger real-world side effects (dispatch_alert, etc.)
- Return a dict that gets logged in BrainFireLog.summary
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from ....models.trading import BrainGraphEdge, BrainNodeState
from .schema import LOG_PREFIX

_log = logging.getLogger(__name__)

FireHandlerFn = Callable[
    [Session, str, BrainNodeState, dict[str, Any]],
    Optional[dict[str, Any]],
]

_HANDLERS: dict[str, FireHandlerFn] = {}


def register_handler(node_id: str, fn: FireHandlerFn) -> None:
    """Register a fire handler for a specific node ID."""
    _HANDLERS[node_id] = fn
    _log.info("%s registered fire handler for node %s", LOG_PREFIX, node_id)


def get_handler(node_id: str) -> Optional[FireHandlerFn]:
    return _HANDLERS.get(node_id)


def has_handler(node_id: str) -> bool:
    return node_id in _HANDLERS


def collect_children_state(
    db: Session,
    node_id: str,
    *,
    graph_version: int = 1,
) -> dict[str, dict[str, Any]]:
    """Read local_state from all inbound (child) nodes for aggregation.

    Returns {child_node_id: local_state_dict}.
    """
    inbound = (
        db.query(BrainGraphEdge.source_node_id)
        .filter(
            BrainGraphEdge.target_node_id == node_id,
            BrainGraphEdge.enabled.is_(True),
            BrainGraphEdge.graph_version == graph_version,
        )
        .all()
    )
    child_ids = [r[0] for r in inbound]
    if not child_ids:
        return {}

    states = (
        db.query(BrainNodeState)
        .filter(BrainNodeState.node_id.in_(child_ids))
        .all()
    )
    return {
        s.node_id: (s.local_state or {})
        for s in states
    }


def invoke_handler(
    db: Session,
    node_id: str,
    state: BrainNodeState,
    *,
    event_payload: Optional[dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    graph_version: int = 1,
) -> Optional[dict[str, Any]]:
    """Invoke the registered handler for a node. Returns handler result or None."""
    fn = _HANDLERS.get(node_id)
    if fn is None:
        return None

    children = collect_children_state(db, node_id, graph_version=graph_version)
    context = {
        "event_payload": event_payload or {},
        "correlation_id": correlation_id,
        "children_state": children,
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "activation_score": float(state.activation_score),
        "confidence": float(state.confidence),
    }

    try:
        result = fn(db, node_id, state, context)
        _log.info(
            "%s handler fired for %s: %s",
            LOG_PREFIX,
            node_id,
            json.dumps(result, default=str)[:200] if result else "ok",
        )
        return result
    except Exception:
        _log.exception("%s handler for %s failed", LOG_PREFIX, node_id)
        return {"error": "handler_exception"}
