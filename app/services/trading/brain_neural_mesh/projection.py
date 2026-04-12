"""Build neural graph JSON for the Trading Brain UI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....models.trading import (
    BrainActivationEvent,
    BrainFireLog,
    BrainGraphEdge,
    BrainGraphNode,
    BrainNodeState,
)
from .layout_neural_graph import (
    MARGIN,
    VIEWPORT_H,
    VIEWPORT_W,
    compute_neural_positions,
    truncate_neural_label,
)
from .repository import nodes_for_domain
from .schema import DEFAULT_DOMAIN, DEFAULT_GRAPH_VERSION
from .waves import derive_overlay_hot_pulse_from_waves, group_activation_events_into_waves
from ..momentum_neural.brain_desk_summary import (
    MOMENTUM_GRAPH_NODE_IDS,
    build_momentum_neural_graph_context,
)

NEURAL_LAYOUT_VERSION = 2
# Bumped when neural graph node payload shape changes materially (Phase 10 momentum desk previews).
NEURAL_PROJECTION_SCHEMA_VERSION = 3

# Layer indices: 1 = outer ring (sensory), 7 = inner (meta-learning).
NEURAL_LAYER_LABELS: dict[int, str] = {
    1: "Sensory",
    2: "Feature Extraction",
    3: "Latent Market State",
    4: "Pattern / Association",
    5: "Evidence / Verification",
    6: "Action / Expression",
    7: "Meta-Learning / Reweighting",
}


def neural_layer_labels_meta() -> dict[str, str]:
    """String keys for JSON (layer number as str)."""
    return {str(k): v for k, v in sorted(NEURAL_LAYER_LABELS.items())}


def _node_cooling(
    last_fired_at: Optional[datetime],
    cooldown_seconds: int,
    *,
    now: datetime,
) -> bool:
    if last_fired_at is None or cooldown_seconds <= 0:
        return False
    return (now - last_fired_at).total_seconds() < float(cooldown_seconds)


def _node_stale_flag(staleness_at: Optional[datetime], *, now: datetime, stale_after_sec: float) -> bool:
    if staleness_at is None:
        return False
    return (now - staleness_at).total_seconds() > stale_after_sec


def build_neural_graph_projection(
    db: Session,
    *,
    domain: str = DEFAULT_DOMAIN,
    graph_version: int = DEFAULT_GRAPH_VERSION,
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

    now = datetime.now(timezone.utc)

    momentum_ctx: dict[str, Any] = {}
    try:
        momentum_ctx = build_momentum_neural_graph_context(db)
    except Exception:
        momentum_ctx = {}

    nodes_min = [
        {"id": n.id, "layer": int(n.layer), "is_observer": bool(n.is_observer)}
        for n in sorted(nodes_orm, key=lambda x: x.id)
    ]
    pos_map, layout_meta = compute_neural_positions(nodes_min)

    out_nodes: list[dict[str, Any]] = []
    for node in sorted(nodes_orm, key=lambda x: x.id):
        xy = pos_map.get(node.id)
        if not xy:
            continue
        x, y = xy
        st = states.get(node.id)
        act = float(st.activation_score) if st else 0.0
        conf = float(st.confidence) if st else 0.5
        stale = _node_stale_flag(st.staleness_at if st else None, now=now, stale_after_sec=stale_after_sec)
        recent_fire = st.last_fired_at.isoformat() if st and st.last_fired_at else None
        cooling = _node_cooling(
            st.last_fired_at if st else None,
            int(node.cooldown_seconds),
            now=now,
        )
        layer_label = NEURAL_LAYER_LABELS.get(int(node.layer), f"Layer {node.layer}")
        nd: dict[str, Any] = {
            "id": node.id,
            "label": node.label,
            "label_short": truncate_neural_label(node.label or "", 20),
            "layer": node.layer,
            "layer_label": layer_label,
            "node_type": node.node_type,
            "activation_score": act,
            "confidence": conf,
            "stale": stale,
            "cooling": cooling,
            "enabled": node.enabled,
            "is_observer": node.is_observer,
            "fire_threshold": float(node.fire_threshold),
            "cooldown_seconds": int(node.cooldown_seconds),
            "last_fired_at": recent_fire,
            "x": round(x, 2),
            "y": round(y, 2),
        }
        ls = st.local_state if st and isinstance(st.local_state, dict) else None
        if ls and ls.get("momentum_neural_version"):
            nd["momentum_preview"] = {
                "last_tick_utc": ls.get("last_tick_utc"),
                "top_preview": ls.get("top_preview") or ls.get("viability_rows"),
                "correlation_id": ls.get("correlation_id"),
            }
        if node.id in MOMENTUM_GRAPH_NODE_IDS and momentum_ctx.get("nodes"):
            card = momentum_ctx["nodes"].get(node.id)
            if isinstance(card, dict):
                nd["momentum_desk"] = {
                    "subtitle": card.get("subtitle"),
                    "title": card.get("title"),
                    "role": card.get("role"),
                }
        out_nodes.append(nd)

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

    panel = momentum_ctx.get("momentum_panel") if isinstance(momentum_ctx.get("momentum_panel"), dict) else {}
    meta = {
        "view": "neural",
        "domain": domain,
        "graph_version": graph_version,
        "layout_version": NEURAL_LAYOUT_VERSION,
        "projection_schema_version": NEURAL_PROJECTION_SCHEMA_VERSION,
        "viewport": {
            "w": VIEWPORT_W,
            "h": VIEWPORT_H,
            "margin": float(MARGIN),
            "cx": VIEWPORT_W / 2.0,
            "cy": VIEWPORT_H / 2.0,
        },
        "bounds": layout_meta["bounds"],
        "ring_radii_draw": layout_meta.get("ring_radii_draw", []),
        "layer_ring_cues": layout_meta.get("layer_ring_cues", []),
        "layer_labels": neural_layer_labels_meta(),
        "description": (
            "Event-driven neural mesh: rings by cognitive layer; hub nodes share the center band. "
            "Edges carry typed signals; inhibitory edges reduce downstream activation. "
            "Momentum crypto intel, viability pool, and evolution trace are neural-native (not learning-cycle)."
        ),
        "momentum_desk": {
            "version": momentum_ctx.get("version") or 0,
            "headline": panel.get("headline"),
            "badges": momentum_ctx.get("badges"),
            "paper_vs_live_30d": panel.get("paper_vs_live_30d"),
            "links": panel.get("links"),
        },
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
    now = datetime.now(timezone.utc)
    stale_after = 480.0
    stale = _node_stale_flag(st.staleness_at if st else None, now=now, stale_after_sec=stale_after)
    cooling = _node_cooling(st.last_fired_at if st else None, int(node.cooldown_seconds), now=now)

    def _pc(edges: list) -> dict[str, int]:
        o = {"excitatory": 0, "inhibitory": 0, "other": 0}
        for e in edges:
            pol = (getattr(e, "polarity", None) or "").lower()
            if pol == "inhibitory":
                o["inhibitory"] += 1
            elif pol == "excitatory":
                o["excitatory"] += 1
            else:
                o["other"] += 1
        return o

    ib_pc = _pc(inbound)
    ob_pc = _pc(outbound)
    last_wave_id = fires[0].correlation_id if fires and getattr(fires[0], "correlation_id", None) else None

    ov_events = list_recent_activations(db, limit=80)
    ov_waves = group_activation_events_into_waves(ov_events, time_window_sec=2.0)
    hot_overlay, _, last_act_wave = derive_overlay_hot_pulse_from_waves(ov_waves, {})

    out: dict[str, Any] = {
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
        "stale": stale,
        "cooling": cooling,
        "layer_label": NEURAL_LAYER_LABELS.get(int(node.layer), f"Layer {node.layer}"),
        "edge_polarity_inbound": ib_pc,
        "edge_polarity_outbound": ob_pc,
        "last_wave_correlation_id": last_wave_id,
        "in_last_activation_wave": bool(node.id in hot_overlay),
        "activation_wave_id": (last_act_wave.get("wave_id") if last_act_wave else None),
        "activation_wave_correlation_id": (last_act_wave.get("correlation_id") if last_act_wave else None),
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

    if node_id in MOMENTUM_GRAPH_NODE_IDS:
        try:
            mctx = build_momentum_neural_graph_context(db)
            out["momentum_desk_card"] = mctx.get("nodes", {}).get(node_id, {})
            out["momentum_badges"] = mctx.get("badges", {})
            out["momentum_panel"] = mctx.get("momentum_panel", {})
        except Exception:
            out["momentum_desk_card"] = {"error": "momentum_context_unavailable"}

    return out


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


def build_live_activation_overlay(
    db: Session,
    *,
    graph_version: int = DEFAULT_GRAPH_VERSION,
    activation_limit: int = 80,
    time_window_sec: float = 2.0,
) -> dict[str, Any]:
    """Summarize recent activation waves for the desk (hot nodes + edge pulse keys)."""
    events = list_recent_activations(db, limit=activation_limit, since=None)
    waves = group_activation_events_into_waves(events, time_window_sec=time_window_sec)

    edges_orm = (
        db.query(BrainGraphEdge)
        .filter(BrainGraphEdge.graph_version == graph_version, BrainGraphEdge.enabled.is_(True))
        .all()
    )
    outbound: dict[str, list[str]] = {}
    for e in edges_orm:
        outbound.setdefault(e.source_node_id, []).append(e.target_node_id)

    hot, pulse_keys, last_wave = derive_overlay_hot_pulse_from_waves(waves, outbound)

    return {
        "ok": True,
        "hot_node_ids": hot,
        "edge_pulse_keys": pulse_keys,
        "last_wave": last_wave,
        "waves": waves[:12],
        "wave_count": len(waves),
    }
