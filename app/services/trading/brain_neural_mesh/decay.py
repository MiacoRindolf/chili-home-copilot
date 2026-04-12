"""Time-based confidence decay for node states."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from ....models.trading import BrainGraphEdge, BrainGraphNode, BrainNodeState
from .schema import DEFAULT_DOMAIN, DEFAULT_GRAPH_VERSION, LOG_PREFIX
from .propagation import apply_decay_to_state

_log = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_HALF_LIFE_SEC = 900.0


def _build_node_half_life_map(
    db: Session,
    *,
    graph_version: int,
    default: float,
) -> dict[str, float]:
    """Per-node effective half-life = min of inbound edge decay_half_life_seconds.

    Nodes with no inbound edges (or all NULL) use the default.
    """
    rows = (
        db.query(
            BrainGraphEdge.target_node_id,
            sa_func.min(BrainGraphEdge.decay_half_life_seconds).label("min_hl"),
        )
        .filter(
            BrainGraphEdge.enabled.is_(True),
            BrainGraphEdge.graph_version == graph_version,
            BrainGraphEdge.decay_half_life_seconds.isnot(None),
            BrainGraphEdge.decay_half_life_seconds > 0,
        )
        .group_by(BrainGraphEdge.target_node_id)
        .all()
    )
    return {r[0]: float(r[1]) for r in rows}


def apply_global_decay(
    db: Session,
    *,
    now: Optional[datetime] = None,
    half_life_seconds: float = DEFAULT_CONFIDENCE_HALF_LIFE_SEC,
    domain: str = DEFAULT_DOMAIN,
    graph_version: int = DEFAULT_GRAPH_VERSION,
) -> int:
    """Decay confidence on states whose nodes are enabled. Returns rows touched."""
    now = now or datetime.now(timezone.utc)
    states = (
        db.query(BrainNodeState)
        .join(BrainGraphNode, BrainGraphNode.id == BrainNodeState.node_id)
        .filter(
            BrainGraphNode.domain == domain,
            BrainGraphNode.graph_version == graph_version,
            BrainGraphNode.enabled.is_(True),
        )
        .all()
    )
    # Use per-edge decay_half_life_seconds when available; fall back to the
    # global default for nodes with no inbound edge half-life configured.
    hl_map = _build_node_half_life_map(db, graph_version=graph_version, default=half_life_seconds)

    touched = 0
    for st in states:
        node_hl = hl_map.get(st.node_id, half_life_seconds)
        if apply_decay_to_state(st, half_life_seconds=node_hl, now=now):
            touched += 1
    if touched:
        _log.debug("%s decay touched %s node states (default_hl=%ss)", LOG_PREFIX, touched, half_life_seconds)
    return touched
