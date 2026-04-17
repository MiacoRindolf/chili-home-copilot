"""Phase J - persistence layer for the re-cert proposal queue.

Given a drift-monitor row (or a user-initiated request), this service
writes one row into ``trading_pattern_recert_log`` with status
``proposed``. In Phase J.1 there is no consumer of this table; Phase
J.2 will wire it into the backtest queue + lifecycle FSM.

Design
------

* **Two entry-points.** :func:`queue_from_drift` for automatic
  proposals triggered by a drift row; :func:`queue_manual` for
  operator-initiated requests.
* **Refuses authoritative.** Until Phase J.2 opens explicitly the
  service raises :class:`RuntimeError` on authoritative mode.
* **Idempotent dedupe.** The pure ``recert_id`` stays stable for
  ``(pattern, as_of_date, source)`` so repeated calls don't
  double-insert.
* **Off-mode short-circuit.** ``brain_recert_queue_mode == "off"``
  is a no-op returning ``None``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.recert_queue_ops_log import (
    format_recert_queue_ops_line,
)
from .drift_monitor_model import DriftMonitorOutput
from .recert_queue_model import (
    RecertProposal,
    RecertQueueConfig,
    propose_from_drift,
    propose_manual,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


def _effective_mode(override: str | None = None) -> str:
    m = (override or getattr(settings, "brain_recert_queue_mode", "off") or "off").lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_recert_queue_ops_log_enabled", True))


def _config_from_settings() -> RecertQueueConfig:
    return RecertQueueConfig(
        trigger_severity="red",
        include_yellow=bool(
            getattr(settings, "brain_recert_queue_include_yellow", False)
        ),
    )


@dataclass(frozen=True)
class RecertQueueResult:
    log_id: int
    recert_id: str
    scan_pattern_id: int
    mode: str
    status: str


def _already_queued(
    db: Session, *, recert_id: str,
) -> int | None:
    row = db.execute(text(
        "SELECT id FROM trading_pattern_recert_log "
        "WHERE recert_id = :rid ORDER BY id DESC LIMIT 1"
    ), {"rid": recert_id}).fetchone()
    if row is None:
        return None
    return int(row[0])


def _persist(
    db: Session, prop: RecertProposal, mode: str,
) -> RecertQueueResult:
    existing = _already_queued(db, recert_id=prop.recert_id)
    if existing is not None:
        if _ops_log_enabled():
            logger.info(
                format_recert_queue_ops_line(
                    event="recert_skipped",
                    mode=mode,
                    recert_id=prop.recert_id,
                    scan_pattern_id=prop.scan_pattern_id,
                    pattern_name=prop.pattern_name,
                    severity=prop.severity,
                    source=prop.source,
                    status=prop.status,
                    drift_log_id=prop.drift_log_id,
                    reason="duplicate",
                )
            )
        return RecertQueueResult(
            log_id=existing,
            recert_id=prop.recert_id,
            scan_pattern_id=prop.scan_pattern_id,
            mode=mode,
            status=prop.status,
        )

    now = datetime.utcnow()
    as_of_str = prop.as_of_date.isoformat()
    row = db.execute(text("""
        INSERT INTO trading_pattern_recert_log (
            recert_id, scan_pattern_id, pattern_name,
            as_of_date, source, severity, status, reason,
            drift_log_id, payload_json, mode, observed_at
        ) VALUES (
            :recert_id, :scan_pattern_id, :pattern_name,
            CAST(:as_of_date AS DATE), :source, :severity,
            :status, :reason, :drift_log_id,
            CAST(:payload AS JSONB), :mode, :now
        )
        RETURNING id
    """), {
        "recert_id": prop.recert_id,
        "scan_pattern_id": prop.scan_pattern_id,
        "pattern_name": prop.pattern_name,
        "as_of_date": as_of_str,
        "source": prop.source,
        "severity": prop.severity,
        "status": prop.status,
        "reason": prop.reason,
        "drift_log_id": prop.drift_log_id,
        "payload": json.dumps(prop.payload, default=str, separators=(",", ":")),
        "mode": mode,
        "now": now,
    })
    new_id = int(row.scalar_one())
    db.commit()

    if _ops_log_enabled():
        logger.info(
            format_recert_queue_ops_line(
                event="recert_persisted",
                mode=mode,
                recert_id=prop.recert_id,
                scan_pattern_id=prop.scan_pattern_id,
                pattern_name=prop.pattern_name,
                severity=prop.severity,
                source=prop.source,
                status=prop.status,
                drift_log_id=prop.drift_log_id,
            )
        )

    return RecertQueueResult(
        log_id=new_id,
        recert_id=prop.recert_id,
        scan_pattern_id=prop.scan_pattern_id,
        mode=mode,
        status=prop.status,
    )


def _check_mode_or_raise(
    mode: str, *, scan_pattern_id: int | None,
) -> None:
    if mode == "authoritative":
        if _ops_log_enabled():
            logger.warning(
                format_recert_queue_ops_line(
                    event="recert_refused_authoritative",
                    mode=mode,
                    scan_pattern_id=scan_pattern_id,
                    reason="phase_j_2_not_opened",
                )
            )
        raise RuntimeError(
            "recert_queue authoritative mode is not permitted "
            "until Phase J.2 is explicitly opened",
        )


def queue_from_drift(
    db: Session,
    drift: DriftMonitorOutput,
    *,
    as_of_date: date | str,
    drift_log_id: int | None = None,
    mode_override: str | None = None,
    config: RecertQueueConfig | None = None,
) -> RecertQueueResult | None:
    """Turn a drift row into a proposal when severity warrants."""
    mode = _effective_mode(mode_override)
    if mode == "off":
        return None
    _check_mode_or_raise(mode, scan_pattern_id=drift.scan_pattern_id)

    cfg = config or _config_from_settings()
    prop = propose_from_drift(
        drift,
        as_of_date=as_of_date,
        drift_log_id=drift_log_id,
        config=cfg,
    )
    if prop is None:
        return None
    return _persist(db, prop, mode)


def queue_manual(
    db: Session,
    *,
    scan_pattern_id: int,
    pattern_name: str | None,
    as_of_date: date | str,
    reason: str,
    mode_override: str | None = None,
) -> RecertQueueResult | None:
    """Queue a user-initiated re-cert proposal."""
    mode = _effective_mode(mode_override)
    if mode == "off":
        return None
    _check_mode_or_raise(mode, scan_pattern_id=scan_pattern_id)

    prop = propose_manual(
        scan_pattern_id=scan_pattern_id,
        pattern_name=pattern_name,
        as_of_date=as_of_date,
        reason=reason,
    )
    return _persist(db, prop, mode)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def recert_summary(
    db: Session,
    *,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary for re-cert proposals.

    Keys (stable, order-preserving):
      * mode
      * lookback_days
      * recert_events_total
      * by_source {drift_monitor, manual, scheduler, other}
      * by_severity {red, yellow, green, null}
      * by_status {proposed, dispatched, completed, cancelled, other}
      * patterns_queued_distinct
      * latest_recert {recert_id, scan_pattern_id, pattern_name,
                       severity, source, status, observed_at}
    """
    mode = _effective_mode()

    total = int(db.execute(text("""
        SELECT COUNT(*) FROM trading_pattern_recert_log
        WHERE observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    by_source = {"drift_monitor": 0, "manual": 0, "scheduler": 0, "other": 0}
    for src, cnt in db.execute(text("""
        SELECT source, COUNT(*) FROM trading_pattern_recert_log
        WHERE observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
        GROUP BY source
    """), {"ld": int(lookback_days)}).fetchall():
        key = src if src in ("drift_monitor", "manual", "scheduler") else "other"
        by_source[key] = by_source.get(key, 0) + int(cnt or 0)

    by_severity = {"red": 0, "yellow": 0, "green": 0, "null": 0}
    for sev, cnt in db.execute(text("""
        SELECT severity, COUNT(*) FROM trading_pattern_recert_log
        WHERE observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
        GROUP BY severity
    """), {"ld": int(lookback_days)}).fetchall():
        key = sev if sev in ("red", "yellow", "green") else "null"
        by_severity[key] = by_severity.get(key, 0) + int(cnt or 0)

    by_status = {
        "proposed": 0, "dispatched": 0,
        "completed": 0, "cancelled": 0, "other": 0,
    }
    for stat, cnt in db.execute(text("""
        SELECT status, COUNT(*) FROM trading_pattern_recert_log
        WHERE observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
        GROUP BY status
    """), {"ld": int(lookback_days)}).fetchall():
        key = stat if stat in (
            "proposed", "dispatched", "completed", "cancelled",
        ) else "other"
        by_status[key] = by_status.get(key, 0) + int(cnt or 0)

    patterns_queued_distinct = int(db.execute(text("""
        SELECT COUNT(DISTINCT scan_pattern_id) FROM trading_pattern_recert_log
        WHERE observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    latest = db.execute(text("""
        SELECT recert_id, scan_pattern_id, pattern_name, severity,
               source, status, observed_at
        FROM trading_pattern_recert_log
        ORDER BY observed_at DESC
        LIMIT 1
    """)).fetchone()
    latest_payload: dict[str, Any] | None = None
    if latest:
        latest_payload = {
            "recert_id": latest[0],
            "scan_pattern_id": latest[1],
            "pattern_name": latest[2],
            "severity": latest[3],
            "source": latest[4],
            "status": latest[5],
            "observed_at": latest[6].isoformat() if latest[6] else None,
        }

    return {
        "mode": mode,
        "lookback_days": int(lookback_days),
        "recert_events_total": total,
        "by_source": by_source,
        "by_severity": by_severity,
        "by_status": by_status,
        "patterns_queued_distinct": patterns_queued_distinct,
        "latest_recert": latest_payload,
    }


__all__ = [
    "RecertQueueResult",
    "mode_is_active",
    "mode_is_authoritative",
    "queue_from_drift",
    "queue_manual",
    "recert_summary",
]
