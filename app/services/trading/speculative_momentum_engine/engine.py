"""Public entry: build speculative slice for opportunity-board JSON."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ....models.trading import ScanResult
from ..market_data import is_crypto
from .clusters import resolve_cluster
from .features import SignalFeatures, build_features, passes_hot_gate
from .nodes import evaluate_all_nodes
from .reasoning import build_non_promotion, operator_hint
from .scoring import build_scoring_plane
from .schema import (
    CLUSTER_LABELS,
    ENGINE_ID,
    ENGINE_VERSION,
    HUB_NODE_ID,
    METHODOLOGY_KEY,
    NODE_VWAP_PULLBACK,
)

logger = logging.getLogger(__name__)


def build_speculative_momentum_slice(
    db: Session,
    *,
    limit: int = 12,
    min_scanner_score: float = 6.0,
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    methodology_note = (
        "Graph-native speculative momentum engine v1: node activations map to neural-mesh "
        f"subgraph (hub {HUB_NODE_ID}); evaluation is deterministic/heuristic over scanner rows, "
        "isolated from core promotion."
    )

    try:
        rows = (
            db.query(ScanResult)
            .order_by(desc(ScanResult.scanned_at))
            .limit(max(80, limit * 6))
            .all()
        )
    except Exception as e:
        logger.warning("[speculative_momentum_engine] scan query failed: %s", e)
        return {
            "ok": False,
            "engine": ENGINE_ID,
            "engine_version": ENGINE_VERSION,
            "methodology": METHODOLOGY_KEY,
            "methodology_note": methodology_note,
            "generated_at": generated_at,
            "items": [],
            "error": str(e),
        }

    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for sr in rows:
        if len(items) >= max(1, limit):
            break
        f = build_features(sr)
        if not f.ticker or f.ticker in seen:
            continue
        if not passes_hot_gate(f, min_score=min_scanner_score):
            continue

        seen.add(f.ticker)
        acts = evaluate_all_nodes(f)
        cluster = resolve_cluster(acts, scanner_score=f.scanner_score)
        scores = build_scoring_plane(f, acts, cluster)
        cluster_label = CLUSTER_LABELS.get(cluster.cluster_id, cluster.cluster_id)
        move_type_label = cluster_label

        codes, why_lines, non_promotion = build_non_promotion(scores=scores, cluster=cluster)

        m = {a.node_id: a.score for a in acts}
        hint = operator_hint(cluster.cluster_id, scores, m.get(NODE_VWAP_PULLBACK, 0.0))

        scanned_iso = None
        if sr.scanned_at:
            dt = sr.scanned_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            scanned_iso = dt.isoformat()

        active_nodes = [a.to_dict() for a in acts if a.score >= 0.35]
        graph_trace = {
            "engine": ENGINE_ID,
            "hub_node_id": HUB_NODE_ID,
            "nodes_evaluated": [a.to_dict() for a in acts],
            "cluster_id": cluster.cluster_id,
            "cluster_rationale": cluster.rationale,
        }

        items.append(
            {
                "ticker": f.ticker,
                "asset_class": "crypto" if is_crypto(f.ticker) else "stocks",
                "engine": ENGINE_ID,
                "engine_version": ENGINE_VERSION,
                "cluster_id": cluster.cluster_id,
                "cluster_label": cluster_label,
                "move_type_label": move_type_label,
                "operator_hint": hint,
                "why_interesting": (
                    f"Scanner flagged {sr.signal or 'n/a'} with confluence {f.scanner_score:.1f}/10"
                    + (f" (scanned {scanned_iso})" if scanned_iso else "")
                    + "."
                ),
                "why_speculative": (
                    "Evaluated through the speculative-momentum node graph over scanner text/fields — "
                    "not cross-checked against promoted pattern imminent rules or OOS evidence."
                ),
                "why_not_core_promoted_codes": codes,
                "why_not_core_promoted": why_lines,
                "non_promotion": non_promotion,
                "scores": scores,
                "active_nodes": active_nodes,
                "graph_trace": graph_trace,
                "scanner_signal": sr.signal,
                "scanner_score": f.scanner_score,
                "scanner_risk_level": sr.risk_level,
                "scanned_at_utc": scanned_iso,
            }
        )

    return {
        "ok": True,
        "engine": ENGINE_ID,
        "engine_version": ENGINE_VERSION,
        "methodology": METHODOLOGY_KEY,
        "methodology_note": methodology_note,
        "generated_at": generated_at,
        "items": items,
    }
