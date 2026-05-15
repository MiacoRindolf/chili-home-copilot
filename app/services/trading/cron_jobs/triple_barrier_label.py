"""4-hourly cron: triple-barrier labeling of recent MarketSnapshots.

Phase C of f-evidence-fidelity-architecture (2026-05-14). The
:func:`triple_barrier_labeler.label_snapshots` function is fully
implemented but had no production caller until this cron job was added
-- ``trading_triple_barrier_labels`` was at 0 rows. Once populated this
unlocks a per-pattern meta-classifier ("take this signal vs skip") that
filters false positives from existing alpha without inventing new alpha
(Lopez de Prado, *Advances in Financial Machine Learning*).

Why cron, not event-handler: labeling needs ``min_lookback_days`` of
forward bars to resolve TP/SL/timeout barriers. The trigger is "enough
time has passed" rather than "this thing happened" -- a periodic batch
is the natural fit.

Cadence: 4h. The labeler's own ``min_lookback_days=10`` gate means each
run only processes snapshots old enough to have resolvable barriers;
faster cadence wouldn't increase coverage.

Mode is governed by ``settings.brain_triple_barrier_mode``. Default is
``shadow`` -- labels are written but downstream gates do not consume
them. Operator flips to ``authoritative`` after soak (see runbook
``docs/runbooks/TRIPLE_BARRIER_LABELING.md``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Per-cycle ceiling. Matches the brief default. The labeler internally
# skips snapshots that already have a row for the active barrier tuple,
# so re-runs across batches are idempotent.
DEFAULT_LIMIT = 500
DEFAULT_SIDE = "long"
DEFAULT_MIN_LOOKBACK_DAYS = 10


def run_triple_barrier_label_cycle(
    db: "Session",
    *,
    limit: int = DEFAULT_LIMIT,
    side: str = DEFAULT_SIDE,
    min_lookback_days: int = DEFAULT_MIN_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """Label the most recent ``limit`` snapshots with sufficient lookback.

    Wraps :func:`triple_barrier_labeler.label_snapshots` and returns a
    plain-dict summary for ops logging. The labeler is mode-gated on
    ``brain_triple_barrier_mode``; when mode is ``off`` it returns an
    empty report without DB writes.
    """
    from app.services.trading.triple_barrier_labeler import label_snapshots

    report = label_snapshots(
        db,
        limit=limit,
        side=side,
        min_lookback_days=min_lookback_days,
    )
    return {
        "mode": report.mode,
        "requested": report.requested,
        "written": report.written,
        "skipped_existing": report.skipped_existing,
        "missing_data": report.missing_data,
        "labels_tp": report.labels_tp,
        "labels_sl": report.labels_sl,
        "labels_timeout": report.labels_timeout,
        "errors": report.errors,
    }
