"""Edge gating, delta application, and fire evaluation (testable pure helpers + DB steps)."""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....models.trading import BrainFireLog, BrainGraphEdge, BrainGraphNode, BrainNodeState
from .repository import get_node, get_or_create_state, outbound_edges
from .schema import LOG_PREFIX

_log = logging.getLogger(__name__)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def signal_from_payload(payload: Optional[dict[str, Any]]) -> str:
    if not payload:
        return "*"
    st = payload.get("signal_type")
    if isinstance(st, str) and st.strip():
        return st.strip()
    return "*"


def gate_allows(edge: BrainGraphEdge, event_signal: str) -> bool:
    """Structured v1 gates only (no expression language)."""
    cfg = edge.gate_config if isinstance(edge.gate_config, dict) else {}
    allowed = cfg.get("allowed_signal_types")
    if isinstance(allowed, list):
        return "*" in allowed or event_signal in allowed or event_signal == "*"
    if not edge.signal_type or edge.signal_type in ("*", ""):
        return True
    return event_signal == edge.signal_type or event_signal == "*"


def min_confidence_ok(edge: BrainGraphEdge, state_confidence: float) -> bool:
    return float(state_confidence) >= float(edge.min_confidence or 0.0)


def compute_activation_delta(
    edge: BrainGraphEdge,
    *,
    confidence_delta: float,
    polarity: str,
) -> float:
    """Signed change to apply to target activation (before clamp)."""
    base = abs(float(confidence_delta)) if confidence_delta != 0.0 else 0.12
    w = float(edge.weight or 1.0)
    mag = w * base * 0.35
    if polarity == "inhibitory":
        return -mag
    return mag


def should_fire(
    node: BrainGraphNode,
    state: BrainNodeState,
    now: datetime,
) -> bool:
    if not node.enabled:
        return False
    if float(state.activation_score) < float(node.fire_threshold):
        return False
    if state.last_fired_at is None:
        return True
    elapsed = (now - state.last_fired_at).total_seconds()
    return elapsed >= int(node.cooldown_seconds or 0)


@dataclass
class PropagationResult:
    targets_touched: int
    fires: int
    downstream_events: int
    inhibitions_applied: int
    suppressions: int
    truncated: bool = False


def propagate_one_event(
    db: Session,
    *,
    source_node_id: Optional[str],
    confidence_delta: float,
    propagation_depth: int,
    correlation_id: Optional[str],
    payload: Optional[dict[str, Any]],
    max_depth: int,
    graph_version: int,
    now: Optional[datetime] = None,
) -> PropagationResult:
    """Single-hop propagation from ``source_node_id`` to outbound targets; may enqueue downstream."""
    from . import repository as repo

    now = now or datetime.utcnow()
    res = PropagationResult(0, 0, 0, 0, 0)
    if not source_node_id:
        return res
    if propagation_depth >= max_depth:
        _log.debug(
            "%s propagation depth cutoff at %s/%s for source=%s",
            LOG_PREFIX, propagation_depth, max_depth, source_node_id,
        )
        res.truncated = True
        return res

    ev_signal = signal_from_payload(payload)
    src_node = get_node(db, source_node_id)
    if not src_node or not src_node.enabled:
        return res

    edges = list(outbound_edges(db, source_node_id, graph_version=graph_version))
    for edge in edges:
        if not gate_allows(edge, ev_signal):
            continue

        tgt = get_node(db, edge.target_node_id)
        if not tgt or not tgt.enabled:
            continue

        state = get_or_create_state(db, tgt.id)
        if not min_confidence_ok(edge, state.confidence):
            continue

        before_act = float(state.activation_score)
        delta = compute_activation_delta(edge, confidence_delta=confidence_delta, polarity=edge.polarity)
        if edge.polarity == "inhibitory":
            res.inhibitions_applied += 1

        state.activation_score = _clamp01(before_act + delta)
        state.staleness_at = now
        state.updated_at = now

        # Suppression: was at/above threshold, inhibitory pulled below
        if edge.polarity == "inhibitory" and before_act >= float(tgt.fire_threshold) and state.activation_score < float(
            tgt.fire_threshold
        ):
            res.suppressions += 1

        res.targets_touched += 1

        pre_fire = should_fire(tgt, state, now)
        if pre_fire:
            db.add(
                BrainFireLog(
                    node_id=tgt.id,
                    fired_at=now,
                    activation_score=state.activation_score,
                    confidence=state.confidence,
                    correlation_id=correlation_id,
                    summary=f"cause=propagate depth={propagation_depth} edge={edge.id}",
                )
            )
            state.last_fired_at = now
            state.activation_score = _clamp01(state.activation_score * 0.4)
            res.fires += 1
            if propagation_depth + 1 < max_depth and not tgt.is_observer:
                repo.enqueue_activation(
                    db,
                    source_node_id=tgt.id,
                    cause="fired",
                    payload={"signal_type": "fired", "from_edge": edge.id},
                    confidence_delta=state.confidence,
                    propagation_depth=propagation_depth + 1,
                    correlation_id=correlation_id,
                )
                res.downstream_events += 1

    return res


def apply_decay_to_state(
    state: BrainNodeState,
    *,
    half_life_seconds: float,
    now: datetime,
) -> bool:
    """Exponential decay on confidence when stale. Returns True if mutated."""
    if half_life_seconds <= 0:
        return False
    ref = state.staleness_at
    if ref is None:
        return False
    dt = (now - ref).total_seconds()
    if dt <= 0:
        return False
    factor = math.exp(-math.log(2) * dt / half_life_seconds)
    new_c = _clamp01(float(state.confidence) * factor)
    if abs(new_c - state.confidence) < 1e-6:
        return False
    state.confidence = new_c
    state.updated_at = now
    return True
