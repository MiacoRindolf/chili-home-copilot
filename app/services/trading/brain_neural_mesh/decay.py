"""Time-based confidence decay for node states."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ....models.trading import BrainGraphNode, BrainNodeState
from .schema import DEFAULT_DOMAIN, DEFAULT_GRAPH_VERSION, LOG_PREFIX
from .propagation import apply_decay_to_state

_log = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_HALF_LIFE_SEC = 900.0


def apply_global_decay(
    db: Session,
    *,
    now: Optional[datetime] = None,
    half_life_seconds: float = DEFAULT_CONFIDENCE_HALF_LIFE_SEC,
    domain: str = DEFAULT_DOMAIN,
    graph_version: int = DEFAULT_GRAPH_VERSION,
) -> int:
    """Decay confidence on states whose nodes are enabled. Returns rows touched."""
    now = now or datetime.utcnow()
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
    touched = 0
    for st in states:
        if apply_decay_to_state(st, half_life_seconds=half_life_seconds, now=now):
            touched += 1
    if touched:
        _log.debug("%s decay touched %s node states (half_life=%ss)", LOG_PREFIX, touched, half_life_seconds)
    return touched
