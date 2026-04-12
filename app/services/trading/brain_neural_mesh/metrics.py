"""Lightweight mesh metrics: in-process counters + periodic DB upserts."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

    # Baselines at last DB flush — used to compute incremental deltas so
    # multiple workers don't overwrite each other's totals.
    _flushed_events_published: int = 0
    _flushed_events_processed: int = 0
    _flushed_node_fires: int = 0
    _flushed_suppressions: int = 0
    _flushed_inhibitions: int = 0
    _flushed_momentum_tick_failures: int = 0
    _flushed_depth_cutoffs: int = 0

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

    def flush_deltas(self) -> dict[str, float]:
        """Return incremental deltas since the last flush and advance baselines."""
        deltas = {
            "events_published_total": float(self.events_published - self._flushed_events_published),
            "events_processed_total": float(self.events_processed - self._flushed_events_processed),
            "node_fires_total": float(self.node_fires - self._flushed_node_fires),
            "suppressions_total": float(self.suppressions - self._flushed_suppressions),
            "inhibitions_total": float(self.inhibitions - self._flushed_inhibitions),
            "momentum_tick_failures_total": float(self.momentum_tick_failures - self._flushed_momentum_tick_failures),
            "depth_cutoffs_total": float(self.depth_cutoffs - self._flushed_depth_cutoffs),
        }
        self._flushed_events_published = self.events_published
        self._flushed_events_processed = self.events_processed
        self._flushed_node_fires = self.node_fires
        self._flushed_suppressions = self.suppressions
        self._flushed_inhibitions = self.inhibitions
        self._flushed_momentum_tick_failures = self.momentum_tick_failures
        self._flushed_depth_cutoffs = self.depth_cutoffs
        return deltas


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
    now_utc = datetime.now(timezone.utc)

    # Cumulative counters: flush as incremental deltas so multiple workers
    # add to the DB total instead of overwriting each other (last-write-wins).
    deltas = c.flush_deltas()
    for key, delta in deltas.items():
        if delta == 0.0:
            continue
        stmt = pg_insert(BrainGraphMetric).values(
            domain=domain,
            graph_version=graph_version,
            metric_key=key,
            value_num=delta,
            updated_at=now_utc,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["domain", "graph_version", "metric_key"],
            set_={
                "value_num": BrainGraphMetric.value_num + delta,
                "updated_at": now_utc,
            },
        )
        db.execute(stmt)

    # Gauge metrics: absolute last-value is correct (these are point-in-time).
    gauges = [
        ("avg_propagation_depth", float(depth_avg)),
        ("queue_lag_seconds", float(lag_sec or 0.0)),
        ("stale_node_count", float(stale_n)),
        ("starved_node_count", float(starved_n)),
    ]
    for key, val in gauges:
        stmt = pg_insert(BrainGraphMetric).values(
            domain=domain,
            graph_version=graph_version,
            metric_key=key,
            value_num=val,
            updated_at=now_utc,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["domain", "graph_version", "metric_key"],
            set_={"value_num": val, "updated_at": now_utc},
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
    return max(0.0, (datetime.now(timezone.utc) - row[0]).total_seconds())


def _stale_node_count(
    db: Session,
    *,
    domain: str,
    graph_version: int,
    stale_after_sec: float = 600.0,
) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_sec)
    return (
        db.query(BrainNodeState.node_id)
        .join(BrainGraphNode, BrainGraphNode.id == BrainNodeState.node_id)
        .filter(
            BrainGraphNode.domain == domain,
            BrainGraphNode.graph_version == graph_version,
            BrainGraphNode.enabled.is_(True),
            BrainNodeState.last_activated_at.isnot(None),
            BrainNodeState.last_activated_at < cutoff,
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
