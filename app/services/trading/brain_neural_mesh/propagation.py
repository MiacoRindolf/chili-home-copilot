"""Edge gating, delta application, and fire evaluation (testable pure helpers + DB steps)."""

from __future__ import annotations

import json
import math
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
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


def source_confidence_ok(edge: BrainGraphEdge, source_confidence: float) -> bool:
    """Check source node's confidence against edge's min_source_confidence threshold."""
    min_src = float(getattr(edge, "min_source_confidence", 0.0) or 0.0)
    return float(source_confidence) >= min_src


# Edge-type scaling factors: veto edges hit harder, evidence edges carry more weight.
_EDGE_TYPE_SCALE: dict[str, float] = {
    "dataflow": 1.0,
    "evidence": 1.3,
    "veto": 1.5,
    "feedback": 0.9,
    "control": 0.8,
    "operator_output": 1.0,
}


def compute_activation_delta(
    edge: BrainGraphEdge,
    *,
    confidence_delta: float,
    polarity: str,
    source_confidence: float = 1.0,
) -> float:
    """Signed change to apply to target activation (before clamp).

    Scales by edge weight, edge type, and source confidence quality.
    """
    base = abs(float(confidence_delta)) if confidence_delta != 0.0 else 0.12
    w = float(edge.weight or 1.0)
    edge_type = getattr(edge, "edge_type", "dataflow") or "dataflow"
    type_scale = _EDGE_TYPE_SCALE.get(edge_type, 1.0)
    # Source confidence attenuates: weak sources don't strongly activate targets.
    src_factor = max(0.3, float(source_confidence))
    mag = w * base * 0.35 * type_scale * src_factor
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
    gated_by_signal: int = 0
    gated_by_confidence: int = 0


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

    now = now or datetime.now(timezone.utc)
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

    # Load source state for source-quality gating
    src_state = get_or_create_state(db, source_node_id)
    src_confidence = float(src_state.confidence) if src_state else 0.5

    # Freshness scaling: attenuate confidence_delta for stale data
    effective_delta = confidence_delta
    if payload and isinstance(payload, dict):
        freshness_sec = payload.get("freshness_seconds")
        if isinstance(freshness_sec, (int, float)) and freshness_sec > 120.0:
            # Linear attenuation from 120s to 900s (15 min), floor at 0.3x
            effective_delta *= max(0.3, 1.0 - (freshness_sec - 120.0) / 780.0)

    edges = list(outbound_edges(db, source_node_id, graph_version=graph_version))
    for edge in edges:
        if not gate_allows(edge, ev_signal):
            res.gated_by_signal += 1
            continue

        tgt = get_node(db, edge.target_node_id)
        if not tgt or not tgt.enabled:
            continue

        state = get_or_create_state(db, tgt.id)
        if not min_confidence_ok(edge, state.confidence):
            res.gated_by_confidence += 1
            continue

        # Source-quality gating: weak sources are blocked by min_source_confidence
        if not source_confidence_ok(edge, src_confidence):
            res.gated_by_confidence += 1
            continue

        if edge.delay_ms and int(edge.delay_ms) > 0:
            _log.debug(
                "%s edge %s has delay_ms=%s but delay is not yet implemented; firing instantly",
                LOG_PREFIX, edge.id, edge.delay_ms,
            )

        before_act = float(state.activation_score)
        delta = compute_activation_delta(
            edge,
            confidence_delta=effective_delta,
            polarity=edge.polarity,
            source_confidence=src_confidence,
        )
        if edge.polarity == "inhibitory":
            res.inhibitions_applied += 1

        state.activation_score = _clamp01(before_act + delta)
        state.last_activated_at = now
        state.updated_at = now

        # Suppression: was at/above threshold, inhibitory pulled below
        if edge.polarity == "inhibitory" and before_act >= float(tgt.fire_threshold) and state.activation_score < float(
            tgt.fire_threshold
        ):
            res.suppressions += 1

        res.targets_touched += 1

        pre_fire = should_fire(tgt, state, now)
        if pre_fire:
            handler_summary = None
            try:
                from .handlers import has_handler, invoke_handler

                if has_handler(tgt.id):
                    handler_summary = invoke_handler(
                        db,
                        tgt.id,
                        state,
                        event_payload=payload,
                        correlation_id=correlation_id,
                        graph_version=graph_version,
                    )
            except Exception as _he:
                _log.warning("%s handler invocation failed for %s: %s", LOG_PREFIX, tgt.id, _he)

            fire_summary = f"cause=propagate depth={propagation_depth} edge={edge.id}"
            if handler_summary:
                fire_summary += f" handler={json.dumps(handler_summary, default=str)[:300]}"

            db.add(
                BrainFireLog(
                    node_id=tgt.id,
                    fired_at=now,
                    activation_score=state.activation_score,
                    confidence=state.confidence,
                    correlation_id=correlation_id,
                    summary=fire_summary,
                )
            )
            state.last_fired_at = now
            state.activation_score = _clamp01(state.activation_score * 0.4)
            res.fires += 1
            if propagation_depth + 1 < max_depth and not tgt.is_observer:
                # Fixed fire-propagation delta (0.20) — not the node's absolute
                # confidence.  Using state.confidence here would scale downstream
                # activation by the source's absolute confidence level, causing
                # high-confidence nodes to over-amplify and low-confidence ones
                # to barely propagate.
                repo.enqueue_activation(
                    db,
                    source_node_id=tgt.id,
                    cause="fired",
                    payload={"signal_type": "fired", "from_edge": edge.id},
                    confidence_delta=0.20,
                    propagation_depth=propagation_depth + 1,
                    correlation_id=correlation_id,
                )
                res.downstream_events += 1

    return res


ACTIVATION_DECAY_HALF_LIFE_SEC = 1800.0  # 30 minutes — longer than confidence


def apply_decay_to_state(
    state: BrainNodeState,
    *,
    half_life_seconds: float,
    now: datetime,
    activation_half_life_seconds: float = ACTIVATION_DECAY_HALF_LIFE_SEC,
) -> bool:
    """Exponential decay on confidence and activation_score when stale. Returns True if mutated."""
    if half_life_seconds <= 0:
        return False
    ref = state.last_activated_at
    if ref is None:
        return False
    if now.tzinfo is not None and ref.tzinfo is None:
        now = now.replace(tzinfo=None)
    elif now.tzinfo is None and ref.tzinfo is not None:
        ref = ref.replace(tzinfo=None)
    dt = (now - ref).total_seconds()
    if dt <= 0:
        return False

    mutated = False

    # Confidence decay
    factor = math.exp(-math.log(2) * dt / half_life_seconds)
    new_c = _clamp01(float(state.confidence) * factor)
    if abs(new_c - state.confidence) >= 1e-6:
        state.confidence = new_c
        mutated = True

    # Activation score decay (longer half-life so near-threshold nodes
    # drain over ~30 min instead of sitting permanently near fire_threshold).
    if activation_half_life_seconds > 0:
        act_factor = math.exp(-math.log(2) * dt / activation_half_life_seconds)
        new_a = _clamp01(float(state.activation_score) * act_factor)
        if abs(new_a - state.activation_score) >= 1e-6:
            state.activation_score = new_a
            mutated = True

    if mutated:
        state.updated_at = now
    return mutated
