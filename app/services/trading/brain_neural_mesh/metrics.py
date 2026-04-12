"""Lightweight mesh metrics: in-process counters + periodic DB upserts."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from sqlalchemy import func as sa_func

from ....models.trading import BrainActivationEvent, BrainGraphEdge, BrainGraphMetric, BrainGraphNode, BrainNodeState
from .schema import DEFAULT_DOMAIN, DEFAULT_GRAPH_VERSION, LOG_PREFIX

_log = logging.getLogger(__name__)


@dataclass
class MeshCounters:
    events_published: int = 0
    events_processed: int = 0
    node_fires: int = 0
    suppressions: int = 0
    inhibitions: int = 0
    propagation_depth_sum: int = 0
    momentum_tick_failures: int = 0
    depth_cutoffs: int = 0
    batches: int = 0
    last_flush_ts: float = field(default_factory=time.monotonic)

    def note_publish(self, n: int = 1) -> None:
        self.events_published += n

    def note_batch(
        self,
        *,
        processed: int,
        fires: int,
        suppressions: int,
        inhibitions: int,
        depth_sum: int,
    ) -> None:
        self.batches += 1
        self.events_processed += processed
        self.node_fires += fires
        self.suppressions += suppressions
        self.inhibitions += inhibitions
        self.propagation_depth_sum += depth_sum


_COUNTERS = MeshCounters()
_FLUSH_EVERY_SEC = 45.0


def get_counters() -> MeshCounters:
    return _COUNTERS


def maybe_flush_metrics(db: Session, *, domain: str = DEFAULT_DOMAIN, graph_version: int = DEFAULT_GRAPH_VERSION) -> None:
    now_m = time.monotonic()
    if now_m - _COUNTERS.last_flush_ts < _FLUSH_EVERY_SEC:
        return
    _COUNTERS.last_flush_ts = now_m
    flush_metrics_to_db(db, domain=domain, graph_version=graph_version)


def flush_metrics_to_db(db: Session, *, domain: str = DEFAULT_DOMAIN, graph_version: int = DEFAULT_GRAPH_VERSION) -> None:
    c = _COUNTERS
    depth_avg = (c.propagation_depth_sum / c.batches) if c.batches else 0.0
    lag_sec = _queue_lag_seconds(db)
    stale_n = _stale_node_count(db, domain=domain, graph_version=graph_version)
    starved_n = _starved_node_count(db, domain=domain, graph_version=graph_version)
    pairs = [
        ("events_published_total", float(c.events_published)),
        ("events_processed_total", float(c.events_processed)),
        ("node_fires_total", float(c.node_fires)),
        ("suppressions_total", float(c.suppressions)),
        ("inhibitions_total", float(c.inhibitions)),
        ("avg_propagation_depth", float(depth_avg)),
        ("momentum_tick_failures_total", float(c.momentum_tick_failures)),
        ("depth_cutoffs_total", float(c.depth_cutoffs)),
        ("queue_lag_seconds", float(lag_sec or 0.0)),
        ("stale_node_count", float(stale_n)),
        ("starved_node_count", float(starved_n)),
    ]
    for key, val in pairs:
        stmt = pg_insert(BrainGraphMetric).values(
            domain=domain,
            graph_version=graph_version,
            metric_key=key,
            value_num=val,
            updated_at=datetime.utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["domain", "graph_version", "metric_key"],
            set_={"value_num": val, "updated_at": datetime.utcnow()},
        )
        db.execute(stmt)
    _log.info(
        "%s metrics flush published=%s processed=%s fires=%s queue_lag_s=%.2f stale_nodes=%s",
        LOG_PREFIX,
        c.events_published,
        c.events_processed,
        c.node_fires,
        lag_sec or 0.0,
        stale_n,
    )


def _queue_lag_seconds(db: Session) -> Optional[float]:
    row = (
        db.query(BrainActivationEvent.created_at)
        .filter(BrainActivationEvent.status == "pending")
        .order_by(BrainActivationEvent.created_at.asc())
        .limit(1)
        .one_or_none()
    )
    if not row or not row[0]:
        return None
    return max(0.0, (datetime.utcnow() - row[0]).total_seconds())


def _stale_node_count(
    db: Session,
    *,
    domain: str,
    graph_version: int,
    stale_after_sec: float = 600.0,
) -> int:
    cutoff = datetime.utcnow() - timedelta(seconds=stale_after_sec)
    return (
        db.query(BrainNodeState.node_id)
        .join(BrainGraphNode, BrainGraphNode.id == BrainNodeState.node_id)
        .filter(
            BrainGraphNode.domain == domain,
            BrainGraphNode.graph_version == graph_version,
            BrainGraphNode.enabled.is_(True),
            BrainNodeState.staleness_at.isnot(None),
            BrainNodeState.staleness_at < cutoff,
        )
        .count()
    )


def _starved_node_count(
    db: Session,
    *,
    domain: str,
    graph_version: int,
) -> int:
    """Count enabled nodes whose confidence is below the minimum min_confidence of all inbound edges."""
    # Subquery: for each target node, find the lowest min_confidence across inbound edges.
    min_conf_sq = (
        db.query(
            BrainGraphEdge.target_node_id.label("node_id"),
            sa_func.min(BrainGraphEdge.min_confidence).label("min_gate"),
        )
        .filter(
            BrainGraphEdge.enabled.is_(True),
            BrainGraphEdge.graph_version == graph_version,
        )
        .group_by(BrainGraphEdge.target_node_id)
        .subquery()
    )
    return (
        db.query(BrainNodeState.node_id)
        .join(BrainGraphNode, BrainGraphNode.id == BrainNodeState.node_id)
        .join(min_conf_sq, min_conf_sq.c.node_id == BrainNodeState.node_id)
        .filter(
            BrainGraphNode.domain == domain,
            BrainGraphNode.graph_version == graph_version,
            BrainGraphNode.enabled.is_(True),
            BrainNodeState.confidence < min_conf_sq.c.min_gate,
        )
        .count()
    )


def read_metrics_map(
    db: Session,
    *,
    domain: str = DEFAULT_DOMAIN,
    graph_version: int = DEFAULT_GRAPH_VERSION,
) -> dict[str, Any]:
    rows = (
        db.query(BrainGraphMetric)
        .filter(BrainGraphMetric.domain == domain, BrainGraphMetric.graph_version == graph_version)
        .all()
    )
    return {r.metric_key: r.value_num for r in rows}
