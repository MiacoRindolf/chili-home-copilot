"""Bounded activation batch: claim events, propagate, decay, metrics."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from .decay import apply_global_decay
from .metrics import get_counters, maybe_flush_metrics
from .propagation import propagate_one_event
from .repository import mark_event_status, reap_dead_events
from .schema import DEFAULT_GRAPH_VERSION, LOG_PREFIX
from . import repository as repo

_log = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 5
DEFAULT_EVENT_BATCH = 22
REAP_INTERVAL_SEC = 300.0  # 5 minutes between dead-event reaping runs
_last_reap_ts: float = 0.0


def run_activation_batch(
    db: Session,
    *,
    time_budget_sec: float = 2.0,
    max_events: int = DEFAULT_EVENT_BATCH,
    max_depth: int = DEFAULT_MAX_DEPTH,
    graph_version: int = DEFAULT_GRAPH_VERSION,
    run_decay: bool = True,
) -> dict[str, Any]:
    """Process pending activation events with a wall-clock budget."""
    t0 = time.monotonic()
    ctr = get_counters()
    processed = 0
    fires = 0
    inhibitions = 0
    suppressions = 0
    downstream = 0
    depth_sum = 0

    if run_decay:
        try:
            apply_global_decay(db, graph_version=graph_version)
        except Exception as e:
            _log.warning("%s decay step failed: %s", LOG_PREFIX, e)

    while processed < max_events and (time.monotonic() - t0) < time_budget_sec:
        batch = repo.claim_pending_batch(db, limit=min(8, max_events - processed))
        if not batch:
            break
        for ev in batch:
            if (time.monotonic() - t0) >= time_budget_sec:
                mark_event_status(db, int(ev.id), "pending")
                db.flush()
                break
            depth_sum += int(ev.propagation_depth or 0)
            try:
                pr = propagate_one_event(
                    db,
                    source_node_id=ev.source_node_id,
                    confidence_delta=float(ev.confidence_delta or 0.0),
                    propagation_depth=int(ev.propagation_depth or 0),
                    correlation_id=ev.correlation_id,
                    payload=ev.payload if isinstance(ev.payload, dict) else None,
                    max_depth=max_depth,
                    graph_version=graph_version,
                    now=datetime.now(timezone.utc),
                )
                fires += pr.fires
                inhibitions += pr.inhibitions_applied
                suppressions += pr.suppressions
                downstream += pr.downstream_events
                if pr.truncated:
                    ctr.depth_cutoffs += 1
                try:
                    from ..momentum_neural.pipeline import maybe_run_momentum_neural_tick

                    maybe_run_momentum_neural_tick(db, ev, graph_version=graph_version)
                except Exception as mom_e:
                    _log.error(
                        "%s momentum neural tick failed for event=%s corr=%s: %s",
                        LOG_PREFIX, ev.id, ev.correlation_id, mom_e,
                        exc_info=True,
                    )
                    ctr.momentum_tick_failures += 1
                mark_event_status(db, int(ev.id), "done", processed_at=datetime.now(timezone.utc))
                processed += 1
            except Exception as e:
                _log.warning("%s event %s failed: %s", LOG_PREFIX, ev.id, e)
                mark_event_status(db, int(ev.id), "dead", processed_at=datetime.now(timezone.utc))
                processed += 1
        db.flush()

    ctr.note_batch(
        processed=processed,
        fires=fires,
        suppressions=suppressions,
        inhibitions=inhibitions,
        depth_sum=depth_sum,
    )
    global _last_reap_ts
    now_m = time.monotonic()
    if now_m - _last_reap_ts >= REAP_INTERVAL_SEC:
        _last_reap_ts = now_m
        try:
            reap_dead_events(db)
        except Exception as e:
            _log.debug("%s reap skipped: %s", LOG_PREFIX, e)
    try:
        maybe_flush_metrics(db, graph_version=graph_version)
    except Exception as e:
        _log.debug("%s metrics flush skipped: %s", LOG_PREFIX, e)

    elapsed = round(time.monotonic() - t0, 4)
    if processed:
        _log.info(
            "%s batch processed=%s fires=%s inhib=%s suppress=%s downstream=%s elapsed=%ss",
            LOG_PREFIX,
            processed,
            fires,
            inhibitions,
            suppressions,
            downstream,
            elapsed,
        )
    return {
        "processed": processed,
        "fires": fires,
        "inhibitions": inhibitions,
        "suppressions": suppressions,
        "downstream_enqueued": downstream,
        "elapsed_sec": elapsed,
    }


def run_propagation_dry_run(
    db: Session,
    *,
    source_node_id: Optional[str],
    confidence_delta: float,
    propagation_depth: int,
    correlation_id: Optional[str],
    payload: Optional[dict[str, Any]],
    max_depth: int = DEFAULT_MAX_DEPTH,
    graph_version: int = DEFAULT_GRAPH_VERSION,
) -> dict[str, Any]:
    """Simulate one hop without committing (rollback at caller)."""
    pr = propagate_one_event(
        db,
        source_node_id=source_node_id,
        confidence_delta=confidence_delta,
        propagation_depth=propagation_depth,
        correlation_id=correlation_id,
        payload=payload,
        max_depth=max_depth,
        graph_version=graph_version,
        now=datetime.now(timezone.utc),
    )
    return {
        "targets_touched": pr.targets_touched,
        "fires": pr.fires,
        "downstream_events": pr.downstream_events,
        "inhibitions_applied": pr.inhibitions_applied,
        "suppressions": pr.suppressions,
        "gated_by_signal": pr.gated_by_signal,
        "gated_by_confidence": pr.gated_by_confidence,
    }
