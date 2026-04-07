"""Build neural graph JSON for the Trading Brain UI."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....models.trading import (
    BrainActivationEvent,
    BrainFireLog,
    BrainGraphEdge,
    BrainGraphNode,
    BrainNodeState,
)
from .repository import nodes_for_domain
from .schema import DEFAULT_DOMAIN, DEFAULT_GRAPH_VERSION


def _ring_radius(layer: int) -> float:
    """Layer 1 outermost; layer 7 inner (near center hub)."""
    return 52.0 + (8 - min(max(layer, 1), 7)) * 58.0


def _place_on_ring(idx: int, n: int, radius: float, phase: float) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 0.0
    angle = phase + (2 * math.pi * idx) / max(n, 1)
    return radius * math.cos(angle), radius * math.sin(angle)


def build_neural_graph_projection(
    db: Session,
    *,
    domain: str = DEFAULT_DOMAIN,
    graph_version: int = DEFAULT_GRAPH_VERSION,
    center_x: float = 400.0,
    center_y: float = 400.0,
    stale_after_sec: float = 480.0,
) -> dict[str, Any]:
    nodes_orm = list(nodes_for_domain(db, domain=domain, graph_version=graph_version))
    nids = [n.id for n in nodes_orm]
    states = {}
    if nids:
        for s in db.query(BrainNodeState).filter(BrainNodeState.node_id.in_(nids)).all():
            states[s.node_id] = s
    edges_orm = (
        db.query(BrainGraphEdge)
        .filter(BrainGraphEdge.graph_version == graph_version, BrainGraphEdge.enabled.is_(True))
        .all()
    )

    now = datetime.utcnow()
    by_layer: dict[int, list[BrainGraphNode]] = {}
    for n in nodes_orm:
        by_layer.setdefault(n.layer, []).append(n)

    out_nodes: list[dict[str, Any]] = []
    phase_by_layer: dict[int, float] = {L: (L * 0.37) for L in by_layer}

    # Slight offset for hub nodes (layer 3) so no single king coordinates
    hub_ids = {"nm_event_bus", "nm_working_memory", "nm_regime", "nm_contradiction"}

    for layer, nlist in sorted(by_layer.items(), key=lambda x: -x[0]):
        nlist.sort(key=lambda x: x.id)
        r = _ring_radius(layer)
        for i, node in enumerate(nlist):
            if node.id in hub_ids and layer == 3:
                ox, oy = _place_on_ring(i, len(nlist), r * 0.45, phase_by_layer[layer])
                x, y = center_x + ox * 0.85, center_y + oy * 0.85
            else:
                ox, oy = _place_on_ring(i, len(nlist), r, phase_by_layer[layer])
                x, y = center_x + ox, center_y + oy
            st = states.get(node.id)
            act = float(st.activation_score) if st else 0.0
            conf = float(st.confidence) if st else 0.5
            stale = False
            if st and st.staleness_at:
                stale = (now - st.staleness_at).total_seconds() > stale_after_sec
            recent_fire = None
            if st and st.last_fired_at:
                recent_fire = st.last_fired_at.isoformat()
            out_nodes.append(
                {
                    "id": node.id,
                    "label": node.label,
                    "layer": node.layer,
                    "node_type": node.node_type,
                    "activation_score": act,
                    "confidence": conf,
                    "stale": stale,
                    "enabled": node.enabled,
                    "is_observer": node.is_observer,
                    "fire_threshold": float(node.fire_threshold),
                    "cooldown_seconds": int(node.cooldown_seconds),
                    "last_fired_at": recent_fire,
                    "x": round(x, 2),
                    "y": round(y, 2),
                }
            )

    out_edges: list[dict[str, Any]] = []
    for e in edges_orm:
        out_edges.append(
            {
                "id": e.id,
                "from": e.source_node_id,
                "to": e.target_node_id,
                "signal_type": e.signal_type,
                "weight": float(e.weight),
                "polarity": e.polarity,
                "kind": "neural",
            }
        )

    meta = {
        "view": "neural",
        "domain": domain,
        "graph_version": graph_version,
        "description": (
            "Event-driven neural mesh: rings by cognitive layer; hub nodes share the center band. "
            "Edges carry typed signals; inhibitory edges reduce downstream activation."
        ),
    }
    return {"ok": True, "meta": meta, "nodes": out_nodes, "edges": out_edges}


def build_node_detail(
    db: Session,
    node_id: str,
    *,
    graph_version: int = DEFAULT_GRAPH_VERSION,
    fire_limit: int = 12,
) -> Optional[dict[str, Any]]:
    node = db.query(BrainGraphNode).filter(BrainGraphNode.id == node_id).one_or_none()
    if not node:
        return None
    st = db.query(BrainNodeState).filter(BrainNodeState.node_id == node_id).one_or_none()
    inbound = (
        db.query(BrainGraphEdge)
        .filter(
            BrainGraphEdge.target_node_id == node_id,
            BrainGraphEdge.graph_version == graph_version,
        )
        .all()
    )
    outbound = (
        db.query(BrainGraphEdge)
        .filter(
            BrainGraphEdge.source_node_id == node_id,
            BrainGraphEdge.graph_version == graph_version,
        )
        .all()
    )
    fires = (
        db.query(BrainFireLog)
        .filter(BrainFireLog.node_id == node_id)
        .order_by(BrainFireLog.fired_at.desc())
        .limit(fire_limit)
        .all()
    )
    return {
        "id": node.id,
        "label": node.label,
        "node_type": node.node_type,
        "layer": node.layer,
        "enabled": node.enabled,
        "is_observer": node.is_observer,
        "fire_threshold": float(node.fire_threshold),
        "cooldown_seconds": int(node.cooldown_seconds),
        "activation_score": float(st.activation_score) if st else 0.0,
        "confidence": float(st.confidence) if st else 0.5,
        "local_state": st.local_state if st and st.local_state else {},
        "last_fired_at": st.last_fired_at.isoformat() if st and st.last_fired_at else None,
        "staleness_at": st.staleness_at.isoformat() if st and st.staleness_at else None,
        "inbound_edges": [
            {
                "id": e.id,
                "from": e.source_node_id,
                "signal_type": e.signal_type,
                "weight": float(e.weight),
                "polarity": e.polarity,
                "enabled": e.enabled,
            }
            for e in inbound
        ],
        "outbound_edges": [
            {
                "id": e.id,
                "to": e.target_node_id,
                "signal_type": e.signal_type,
                "weight": float(e.weight),
                "polarity": e.polarity,
                "enabled": e.enabled,
            }
            for e in outbound
        ],
        "recent_fires": [
            {
                "fired_at": f.fired_at.isoformat(),
                "activation_score": float(f.activation_score),
                "confidence": float(f.confidence),
                "summary": f.summary,
                "correlation_id": f.correlation_id,
            }
            for f in fires
        ],
    }


def build_edge_detail(db: Session, edge_id: int) -> Optional[dict[str, Any]]:
    e = db.query(BrainGraphEdge).filter(BrainGraphEdge.id == edge_id).one_or_none()
    if not e:
        return None
    src = db.query(BrainGraphNode).filter(BrainGraphNode.id == e.source_node_id).one_or_none()
    tgt = db.query(BrainGraphNode).filter(BrainGraphNode.id == e.target_node_id).one_or_none()
    return {
        "id": e.id,
        "source_node_id": e.source_node_id,
        "target_node_id": e.target_node_id,
        "signal_type": e.signal_type,
        "weight": float(e.weight),
        "polarity": e.polarity,
        "delay_ms": int(e.delay_ms),
        "decay_half_life_seconds": e.decay_half_life_seconds,
        "gate_config": e.gate_config,
        "min_confidence": float(e.min_confidence),
        "enabled": e.enabled,
        "graph_version": e.graph_version,
        "source_label": src.label if src else None,
        "target_label": tgt.label if tgt else None,
    }


def list_recent_activations(
    db: Session,
    *,
    limit: int = 40,
    since: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    q = db.query(BrainActivationEvent)
    if since is not None:
        q = q.filter(BrainActivationEvent.created_at >= since)
    rows = q.order_by(BrainActivationEvent.created_at.desc()).limit(limit).all()
    return [
        {
            "id": int(r.id),
            "source_node_id": r.source_node_id,
            "cause": r.cause,
            "payload": r.payload,
            "confidence_delta": float(r.confidence_delta or 0.0),
            "propagation_depth": int(r.propagation_depth or 0),
            "correlation_id": r.correlation_id,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "processed_at": r.processed_at.isoformat() if r.processed_at else None,
        }
        for r in rows
    ]
