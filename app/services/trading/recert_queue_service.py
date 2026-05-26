"""Phase J - persistence layer for the re-cert proposal queue.

Given a drift-monitor row (or a user-initiated request), this service
writes one row into ``trading_pattern_recert_log`` with status
``proposed``. The scheduler consumer then dispatches open proposals into
the backtest queue and the backtest worker completes or fails the cert row.

Design
------

* **Three entry-points.** :func:`queue_from_drift` for automatic
  proposals triggered by a drift row; :func:`queue_scheduler` for
  system certification sweeps; :func:`queue_manual` for operator-
  initiated requests.
* **Refuses authoritative.** Until full live-authoritative recert opens
  explicitly, the service raises :class:`RuntimeError` on authoritative mode.
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
    propose_scheduler,
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


def _aggregate_oos_backtest_evidence(
    db: Session,
    *,
    scan_pattern_id: int,
    since: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate stored walk-forward/OOS rows for a pattern recert stamp."""
    where = [
        "scan_pattern_id = :pid",
        "oos_trade_count IS NOT NULL",
        "oos_trade_count > 0",
        "oos_win_rate IS NOT NULL",
    ]
    params: dict[str, Any] = {"pid": int(scan_pattern_id)}
    if since is not None:
        where.append("ran_at >= :since")
        params["since"] = since
    row = db.execute(text(f"""
        SELECT
            COUNT(*) AS backtests_run,
            COALESCE(SUM(oos_trade_count), 0) AS total,
            COALESCE(SUM(
                CASE
                    WHEN oos_win_rate > 1.0 THEN oos_win_rate / 100.0
                    ELSE oos_win_rate
                END * oos_trade_count
            ), 0.0) AS wins_float,
            SUM(
                COALESCE(oos_return_pct, 0.0) * oos_trade_count
            ) / NULLIF(SUM(oos_trade_count), 0) AS avg_return
        FROM trading_backtests
        WHERE {" AND ".join(where)}
    """), params).mappings().first()
    if not row:
        return {
            "total": 0,
            "wins": 0,
            "win_rate": None,
            "avg_return": None,
            "backtests_run": 0,
        }
    total = int(row.get("total") or 0)
    wins_float = float(row.get("wins_float") or 0.0)
    wins = int(round(wins_float)) if total > 0 else 0
    return {
        "total": total,
        "wins": wins,
        "win_rate": (wins_float / total) if total > 0 else None,
        "avg_return": (
            float(row.get("avg_return"))
            if row.get("avg_return") is not None
            else None
        ),
        "backtests_run": int(row.get("backtests_run") or 0),
    }


def _already_queued(
    db: Session, *, prop: RecertProposal, mode: str,
) -> RecertQueueResult | None:
    row = db.execute(text(
        """
        SELECT id, recert_id, scan_pattern_id, mode, status
        FROM trading_pattern_recert_log
        WHERE recert_id = :rid
           OR (
               scan_pattern_id = :pid
               AND source = :source
               AND status IN ('proposed', 'dispatched')
               AND (mode = :mode OR mode IS NULL)
           )
        ORDER BY
          CASE WHEN recert_id = :rid THEN 0 ELSE 1 END,
          id DESC
        LIMIT 1
        """
    ), {
        "rid": prop.recert_id,
        "pid": int(prop.scan_pattern_id),
        "source": prop.source,
        "mode": mode,
    }).fetchone()
    if row is None:
        return None
    return RecertQueueResult(
        log_id=int(row[0]),
        recert_id=str(row[1]),
        scan_pattern_id=int(row[2]),
        mode=str(row[3] or mode),
        status=str(row[4]),
    )


def _persist(
    db: Session, prop: RecertProposal, mode: str,
) -> RecertQueueResult:
    existing = _already_queued(db, prop=prop, mode=mode)
    if existing is not None:
        if _ops_log_enabled():
            reason = (
                "duplicate"
                if existing.recert_id == prop.recert_id
                else "open_pattern_source_duplicate"
            )
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
                    reason=reason,
                    existing_recert_id=existing.recert_id,
                    existing_log_id=existing.log_id,
                )
            )
        return existing

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


def queue_scheduler(
    db: Session,
    *,
    scan_pattern_id: int,
    pattern_name: str | None,
    as_of_date: date | str,
    reason: str,
    severity: str | None = "red",
    payload: dict | None = None,
    mode_override: str | None = None,
) -> RecertQueueResult | None:
    """Queue a system-owned re-cert proposal from a scheduled sweep."""
    mode = _effective_mode(mode_override)
    if mode == "off":
        return None
    _check_mode_or_raise(mode, scan_pattern_id=scan_pattern_id)

    prop = propose_scheduler(
        scan_pattern_id=scan_pattern_id,
        pattern_name=pattern_name,
        as_of_date=as_of_date,
        reason=reason,
        severity=severity,
        payload=payload,
    )
    return _persist(db, prop, mode)


def complete_open_recerts_from_backtest(
    db: Session,
    *,
    scan_pattern_id: int,
    total: int | None = None,
    wins: int | None = None,
    win_rate: float | None = None,
    avg_return: float | None = None,
    backtests_run: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark open recert work complete after a queue backtest certifies OOS.

    The recert queue's job is not only to boost priority; once the worker
    finishes, it must stamp the pattern with fresh OOS evidence so the alpha
    gate can clear certification debt without an operator touching it.
    """
    pid = int(scan_pattern_id)
    open_row = db.execute(text("""
        SELECT COUNT(*) AS open_count, MIN(observed_at) AS first_observed_at
        FROM trading_pattern_recert_log
        WHERE scan_pattern_id = :pid
          AND status IN ('proposed', 'dispatched')
    """), {"pid": pid}).mappings().first()
    open_count = int((open_row or {}).get("open_count") or 0)
    if open_count <= 0:
        return {"ok": True, "completed": 0, "scan_pattern_id": pid}

    now = now or datetime.utcnow()
    first_observed_at = (open_row or {}).get("first_observed_at")
    oos_evidence = _aggregate_oos_backtest_evidence(
        db,
        scan_pattern_id=pid,
        since=first_observed_at if isinstance(first_observed_at, datetime) else None,
    )
    if int(oos_evidence.get("total") or 0) > 0:
        total = int(oos_evidence["total"])
        wins = int(oos_evidence["wins"])
        win_rate = oos_evidence.get("win_rate")
        avg_return = oos_evidence.get("avg_return")
        backtests_run = int(oos_evidence.get("backtests_run") or 0)
    evidence_total = int(total or 0)
    if evidence_total <= 0:
        no_evidence_payload = {
            "completion": {
                "attempted_at": now.isoformat(),
                "certifier": "backtest_queue_worker",
                "status": "cert_failed_no_oos_evidence",
                "total": total,
                "wins": wins,
                "win_rate": win_rate,
                "avg_return": avg_return,
                "backtests_run": backtests_run,
            }
        }
        failed = int(db.execute(text("""
            UPDATE trading_pattern_recert_log
            SET status = 'cert_failed',
                payload_json = COALESCE(payload_json, '{}'::jsonb)
                    || CAST(:payload AS JSONB)
            WHERE scan_pattern_id = :pid
              AND status IN ('proposed', 'dispatched')
        """), {
            "pid": pid,
            "payload": json.dumps(no_evidence_payload, default=str),
        }).rowcount or 0)
        db.commit()
        return {
            "ok": False,
            "completed": 0,
            "failed": failed,
            "scan_pattern_id": pid,
            "reason": "cert_failed_no_oos_evidence",
        }

    remaining_reasons: list[str] = []
    try:
        from ...models.trading import ScanPattern
        from .alpha_portfolio_gate import (
            config_from_settings,
            recert_reasons_for_pattern,
        )

        pattern = db.get(ScanPattern, pid)
        if pattern is not None:
            normalized_wr = None
            if win_rate is not None:
                try:
                    from .backtest_metrics import normalize_win_rate_for_db

                    normalized_wr = normalize_win_rate_for_db(float(win_rate))
                except Exception:
                    normalized_wr = None
            pattern.oos_evaluated_at = now
            if total is not None:
                pattern.oos_trade_count = int(total)
            if normalized_wr is not None:
                pattern.oos_win_rate = float(normalized_wr)
            if avg_return is not None:
                pattern.oos_avg_return_pct = float(avg_return)
            remaining_reasons = recert_reasons_for_pattern(
                pattern,
                now=now,
                config=config_from_settings(settings),
            )
            pattern.recert_required = bool(remaining_reasons)
            pattern.recert_reason = (
                ",".join(remaining_reasons) if remaining_reasons else None
            )
    except Exception:
        logger.debug(
            "[recert_queue] pattern certification stamp failed",
            exc_info=True,
        )

    completion_payload = {
        "completion": {
            "completed_at": now.isoformat(),
            "certifier": "backtest_queue_worker",
            "total": total,
            "wins": wins,
            "win_rate": win_rate,
            "avg_return": avg_return,
            "backtests_run": backtests_run,
            "remaining_recert_reasons": remaining_reasons,
        }
    }
    updated = int(db.execute(text("""
        UPDATE trading_pattern_recert_log
        SET status = 'completed',
            payload_json = COALESCE(payload_json, '{}'::jsonb)
                || CAST(:payload AS JSONB)
        WHERE scan_pattern_id = :pid
          AND status IN ('proposed', 'dispatched')
    """), {
        "pid": pid,
        "payload": json.dumps(completion_payload, default=str),
    }).rowcount or 0)
    db.commit()
    return {
        "ok": True,
        "completed": updated,
        "scan_pattern_id": pid,
        "remaining_recert_reasons": remaining_reasons,
    }


def reconcile_dispatched_recerts_from_backtests(
    db: Session,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    """Complete dispatched recerts when their boosted backtests already ran.

    This repairs the common operational gap where a recert row was dispatched,
    the pattern was retested, but the completion hook did not stamp the pattern's
    OOS certification columns. Without this reconciliation, signal-rich patterns
    can keep blocking live orders on stale ``missing_oos_recert`` debt.
    """
    rows = db.execute(text("""
        SELECT id, scan_pattern_id, observed_at
        FROM trading_pattern_recert_log
        WHERE status = 'dispatched'
        ORDER BY observed_at ASC, id ASC
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    """), {"limit": int(limit)}).mappings().all()
    checked = 0
    completed = 0
    skipped = 0
    repaired: list[dict[str, Any]] = []
    for row in rows:
        checked += 1
        pid = int(row["scan_pattern_id"])
        since = row.get("observed_at")
        evidence = _aggregate_oos_backtest_evidence(
            db,
            scan_pattern_id=pid,
            since=since if isinstance(since, datetime) else None,
        )
        if int(evidence.get("total") or 0) <= 0:
            skipped += 1
            continue
        result = complete_open_recerts_from_backtest(
            db,
            scan_pattern_id=pid,
            total=int(evidence["total"]),
            wins=int(evidence["wins"]),
            win_rate=evidence.get("win_rate"),
            avg_return=evidence.get("avg_return"),
            backtests_run=int(evidence.get("backtests_run") or 0),
        )
        completed += int(result.get("completed") or 0)
        repaired.append({
            "scan_pattern_id": pid,
            "recert_log_id": int(row["id"]),
            "oos_trade_count": int(evidence["total"]),
            "backtests_run": int(evidence.get("backtests_run") or 0),
            "remaining_recert_reasons": result.get("remaining_recert_reasons") or [],
        })
    return {
        "ok": True,
        "checked": checked,
        "completed": completed,
        "skipped_no_oos": skipped,
        "repaired": repaired,
    }


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
    "queue_scheduler",
    "complete_open_recerts_from_backtest",
    "reconcile_dispatched_recerts_from_backtests",
    "recert_summary",
]
