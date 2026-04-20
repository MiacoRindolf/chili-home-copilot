"""P0.6 — execution-event lag telemetry.

Measures the gap between ``event_at`` (broker-side timestamp on the
venue payload) and ``recorded_at`` (DB-side insert time). A sustained
lag means audit data is stale by the time the reconciler reads it —
which is exactly when the reconciler would miss a partial fill or a
cancelled stop, because the ``broker_status`` on the Trade row hasn't
caught up yet.

This module is read-only: it queries ``trading_execution_events`` and
produces a summary + warning log. It does not block the pipeline and
doesn't write back. Wire it on an interval via the scheduler; a single
warning line per threshold crossing is enough — the operator's job is
to decide whether to pause trading.

Thresholds (all from settings, with safe defaults):

* ``chili_execution_event_lag_warn_p95_ms`` — P95 lag that triggers a
  warning. Default 15_000 ms (15s) — well above normal broker
  roundtrip + DB commit under healthy load.
* ``chili_execution_event_lag_error_p95_ms`` — P95 lag that escalates
  to ``logger.error`` and flips the summary's ``breach`` field to
  ``error``. Default 60_000 ms (1 minute).

Exposed entry points:

* :func:`measure_execution_event_lag(db, *, lookback_seconds=300)` —
  compute the summary; never raises.
* :func:`run_execution_event_lag_tick(db)` — measure + log; used by the
  scheduler job wrapper.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings

logger = logging.getLogger(__name__)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    rows = sorted(float(v) for v in values)
    idx = max(0, min(len(rows) - 1, int(round((len(rows) - 1) * q))))
    return rows[idx]


def _warn_p95_ms() -> float:
    return float(getattr(settings, "chili_execution_event_lag_warn_p95_ms", 15_000.0))


def _error_p95_ms() -> float:
    return float(getattr(settings, "chili_execution_event_lag_error_p95_ms", 60_000.0))


@dataclass(frozen=True)
class EventLagSummary:
    """Frozen-shape gauge output.

    ``breach`` is ``"ok"`` / ``"warn"`` / ``"error"`` — telemetry
    consumers (dashboards, oncall pagers) key off this single field.
    """

    checked_at: str
    lookback_seconds: int
    sample_size: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None
    warn_threshold_ms: float
    error_threshold_ms: float
    breach: str = "ok"
    per_venue: dict[str, dict[str, float | int | None]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "lookback_seconds": self.lookback_seconds,
            "sample_size": self.sample_size,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "max_ms": self.max_ms,
            "warn_threshold_ms": self.warn_threshold_ms,
            "error_threshold_ms": self.error_threshold_ms,
            "breach": self.breach,
            "per_venue": self.per_venue,
        }


def measure_execution_event_lag(
    db: Session,
    *,
    lookback_seconds: int = 300,
) -> EventLagSummary:
    """Compute the lag gauge over the most recent window.

    Lag is ``recorded_at - event_at`` in milliseconds; negative or zero
    lags (clock skew / event_at unset) are excluded.
    """
    checked_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    warn_ms = _warn_p95_ms()
    error_ms = _error_p95_ms()

    lb = max(30, int(lookback_seconds))

    try:
        rows = db.execute(
            text(
                """
                SELECT venue,
                       EXTRACT(EPOCH FROM (recorded_at - event_at)) * 1000.0 AS lag_ms
                FROM trading_execution_events
                WHERE event_at IS NOT NULL
                  AND recorded_at >= NOW() - make_interval(secs => :lb)
                  AND recorded_at > event_at
                """
            ),
            {"lb": lb},
        ).fetchall()
    except Exception:
        logger.warning(
            "[execution_event_lag] query failed; returning empty gauge",
            exc_info=True,
        )
        rows = []

    all_lags: list[float] = []
    per_venue_lags: dict[str, list[float]] = {}
    for venue, lag_ms in rows:
        try:
            v = float(lag_ms)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        all_lags.append(v)
        key = venue or "unknown"
        per_venue_lags.setdefault(key, []).append(v)

    p50 = _percentile(all_lags, 0.50)
    p95 = _percentile(all_lags, 0.95)
    p99 = _percentile(all_lags, 0.99)
    mx = max(all_lags) if all_lags else None

    breach = "ok"
    if p95 is not None:
        if p95 >= error_ms:
            breach = "error"
        elif p95 >= warn_ms:
            breach = "warn"

    per_venue: dict[str, dict[str, float | int | None]] = {}
    for v, vals in per_venue_lags.items():
        per_venue[v] = {
            "sample_size": len(vals),
            "p50_ms": _percentile(vals, 0.50),
            "p95_ms": _percentile(vals, 0.95),
            "max_ms": max(vals) if vals else None,
        }

    return EventLagSummary(
        checked_at=checked_at,
        lookback_seconds=lb,
        sample_size=len(all_lags),
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
        max_ms=mx,
        warn_threshold_ms=warn_ms,
        error_threshold_ms=error_ms,
        breach=breach,
        per_venue=per_venue,
    )


def run_execution_event_lag_tick(db: Session) -> dict[str, Any]:
    """Scheduler entrypoint. Measures and logs the gauge; never raises."""
    if not bool(getattr(settings, "chili_execution_event_lag_enabled", True)):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    lookback = int(getattr(settings, "chili_execution_event_lag_lookback_seconds", 300))
    try:
        summary = measure_execution_event_lag(db, lookback_seconds=lookback)
    except Exception:
        logger.exception("[execution_event_lag] measure failed")
        return {"ok": False, "error": "measure_failed"}

    if summary.breach == "error":
        logger.error(
            "[execution_event_lag] lag breach=ERROR p95=%.0fms threshold=%.0fms "
            "samples=%s lookback=%ss max=%s",
            summary.p95_ms or 0.0, summary.error_threshold_ms,
            summary.sample_size, summary.lookback_seconds, summary.max_ms,
        )
    elif summary.breach == "warn":
        logger.warning(
            "[execution_event_lag] lag breach=WARN p95=%.0fms threshold=%.0fms "
            "samples=%s lookback=%ss",
            summary.p95_ms or 0.0, summary.warn_threshold_ms,
            summary.sample_size, summary.lookback_seconds,
        )
    else:
        logger.debug(
            "[execution_event_lag] ok p50=%s p95=%s samples=%s",
            summary.p50_ms, summary.p95_ms, summary.sample_size,
        )

    return {"ok": True, "summary": summary.to_dict()}


__all__ = [
    "EventLagSummary",
    "measure_execution_event_lag",
    "run_execution_event_lag_tick",
]
