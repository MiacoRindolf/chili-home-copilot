"""P0.8 — escalate persistent reconciliation drift.

The reconciliation sweep writes one row per (trade, broker) pair on
every tick. ``run_missing_stop_watchdog`` already alerts on stale
``missing_stop`` / ``orphan_stop`` based on age; this watchdog covers
the other failure mode: *persistent* drift that never ages out because
every new sweep refreshes ``last_observed_at``.

If the same bracket intent classifies as the same non-agree kind (e.g.
``qty_drift`` or ``price_drift``) for N consecutive sweeps, that's a
signal the underlying fault is not self-healing — operator attention is
needed. Without this, a partial fill that leaves a qty_drift in place
could silently repeat for hours while the sweep keeps logging it and
nothing fires.

Thresholds (settings):

* ``chili_drift_escalation_enabled`` — feature flag (default False;
  this is new telemetry, opt-in to avoid alert-fatigue surprises).
* ``chili_drift_escalation_min_count`` — minimum consecutive sweeps
  classifying the same kind before we alert. Default 5.
* ``chili_drift_escalation_lookback_minutes`` — how far back to count.
  Default 60 minutes.

The watchdog is read-only: it reads
``trading_bracket_reconciliation_log`` and calls the alert dispatcher
for flagged intents. Throttling of repeated alerts for the same intent
is delegated to ``alerts.dispatch_alert``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings

logger = logging.getLogger(__name__)


# Kinds we escalate on. ``missing_stop`` / ``orphan_stop`` are covered
# by the age-based watchdog in bracket_reconciliation_service, but we
# still include them here for count-based escalation (they can escalate
# even before the age threshold fires, if the sweep is very frequent).
_ESCALATABLE_KINDS = frozenset({
    "qty_drift",
    "price_drift",
    "state_drift",
    "missing_stop",
    "orphan_stop",
})


def _enabled() -> bool:
    return bool(getattr(settings, "chili_drift_escalation_enabled", False))


def _min_count() -> int:
    raw = getattr(settings, "chili_drift_escalation_min_count", 5) or 5
    try:
        return max(2, int(raw))
    except (TypeError, ValueError):
        return 5


def _lookback_minutes() -> int:
    raw = getattr(settings, "chili_drift_escalation_lookback_minutes", 60) or 60
    try:
        return max(5, int(raw))
    except (TypeError, ValueError):
        return 60


@dataclass(frozen=True)
class DriftEscalationHit:
    bracket_intent_id: int | None
    trade_id: int | None
    ticker: str | None
    broker_source: str | None
    kind: str
    consecutive_count: int
    first_observed_at: str | None
    last_observed_at: str | None
    alert_sent: bool
    alert_skip_reason: str | None = None


@dataclass(frozen=True)
class DriftEscalationSummary:
    checked_at: str
    enabled: bool
    min_count: int
    lookback_minutes: int
    rows_inspected: int
    hits: list[DriftEscalationHit] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "enabled": self.enabled,
            "min_count": self.min_count,
            "lookback_minutes": self.lookback_minutes,
            "rows_inspected": self.rows_inspected,
            "hits": [h.__dict__ for h in self.hits],
        }


def run_drift_escalation_watchdog(
    db: Session,
    *,
    enabled_override: bool | None = None,
    min_count_override: int | None = None,
    lookback_minutes_override: int | None = None,
    alert_dispatcher: Any = None,
) -> DriftEscalationSummary:
    """Scan recent sweep logs and alert on persistent same-kind drift.

    Algorithm:

    1. For each (bracket_intent_id, kind) group in the lookback window,
       count consecutive rows from NEWEST to OLDEST that have the same
       kind (breaking at the first different kind).
    2. If the streak length >= min_count AND the kind is escalatable,
       emit an alert.

    A bracket intent that flipped from ``missing_stop`` → ``agree`` →
    ``missing_stop`` will NOT escalate until it has been missing for
    min_count sweeps again.
    """
    checked_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    enabled = enabled_override if enabled_override is not None else _enabled()
    min_n = int(min_count_override) if min_count_override is not None else _min_count()
    lookback_m = (
        int(lookback_minutes_override)
        if lookback_minutes_override is not None else _lookback_minutes()
    )

    if not enabled:
        return DriftEscalationSummary(
            checked_at=checked_at,
            enabled=False,
            min_count=min_n,
            lookback_minutes=lookback_m,
            rows_inspected=0,
        )

    try:
        rows = db.execute(
            text(
                """
                SELECT
                    bracket_intent_id,
                    trade_id,
                    ticker,
                    broker_source,
                    kind,
                    observed_at
                FROM trading_bracket_reconciliation_log
                WHERE bracket_intent_id IS NOT NULL
                  AND observed_at >= NOW() - make_interval(mins => :lb)
                ORDER BY bracket_intent_id, observed_at DESC, id DESC
                """
            ),
            {"lb": lookback_m},
        ).fetchall()
    except Exception:
        logger.warning(
            "[drift_escalation] log query failed; returning empty summary",
            exc_info=True,
        )
        rows = []

    # Group by bracket_intent_id preserving newest-first order.
    by_intent: dict[int, list[Any]] = {}
    for r in rows:
        intent_id = int(r[0])
        by_intent.setdefault(intent_id, []).append(r)

    hits: list[DriftEscalationHit] = []
    for intent_id, seq in by_intent.items():
        if not seq:
            continue
        newest = seq[0]
        kind = (newest[4] or "").lower()
        if kind not in _ESCALATABLE_KINDS:
            continue

        # Count the leading streak of same-kind sweeps.
        streak = 0
        for row in seq:
            if (row[4] or "").lower() != kind:
                break
            streak += 1
        if streak < min_n:
            continue

        first_in_streak = seq[streak - 1]
        trade_id = int(newest[1]) if newest[1] is not None else None
        ticker = newest[2]
        broker_source = newest[3]

        alert_sent = False
        alert_skip_reason: str | None = None
        try:
            dispatcher = alert_dispatcher
            if dispatcher is None:
                from .alerts import dispatch_alert as dispatcher  # type: ignore
            message = (
                f"[drift_escalation] {kind} on {ticker or '?'} "
                f"(intent_id={intent_id} trade_id={trade_id}) "
                f"persisted for {streak} consecutive sweeps"
            )
            alert_sent = bool(
                dispatcher(
                    db=db,
                    user_id=None,
                    alert_type=f"drift_escalation_{kind}",
                    ticker=ticker,
                    message=message,
                    skip_throttle=False,
                )
            )
            if not alert_sent:
                alert_skip_reason = "throttled_or_log_only"
        except Exception as exc:
            alert_skip_reason = f"dispatch_error:{type(exc).__name__}"

        hits.append(DriftEscalationHit(
            bracket_intent_id=intent_id,
            trade_id=trade_id,
            ticker=ticker,
            broker_source=broker_source,
            kind=kind,
            consecutive_count=streak,
            first_observed_at=(
                first_in_streak[5].isoformat()
                if hasattr(first_in_streak[5], "isoformat") else None
            ),
            last_observed_at=(
                newest[5].isoformat()
                if hasattr(newest[5], "isoformat") else None
            ),
            alert_sent=alert_sent,
            alert_skip_reason=alert_skip_reason,
        ))

    return DriftEscalationSummary(
        checked_at=checked_at,
        enabled=True,
        min_count=min_n,
        lookback_minutes=lookback_m,
        rows_inspected=len(rows),
        hits=hits,
    )


__all__ = [
    "DriftEscalationHit",
    "DriftEscalationSummary",
    "run_drift_escalation_watchdog",
]
