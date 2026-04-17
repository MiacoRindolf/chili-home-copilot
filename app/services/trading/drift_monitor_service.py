"""Phase J - persistence layer for the drift monitor.

Runs the pure drift model against the recent closed-trade sample per
scan pattern and writes one row into ``trading_pattern_drift_log`` per
(pattern, sweep). Shadow-safe: never transitions lifecycle state,
never touches ``scan_patterns``.

Design
------

* **Single public entry-point.** The APScheduler daily job and the
  diagnostics endpoint both call :func:`run_sweep` (scheduler) and
  :func:`drift_summary` (endpoint).
* **Refuses authoritative.** Until Phase J.2 opens explicitly the
  service raises :class:`RuntimeError` on
  ``mode_override="authoritative"`` or
  ``brain_drift_monitor_mode="authoritative"``.
* **Append-only.** Every sweep appends a new row; the deterministic
  ``drift_id`` (from :mod:`drift_monitor_model`) lets callers dedupe.
* **Off-mode short-circuit.** When
  ``brain_drift_monitor_mode == "off"`` :func:`run_sweep` is a no-op
  and returns an empty list.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.drift_monitor_ops_log import (
    format_drift_monitor_ops_line,
)
from .drift_monitor_model import (
    DriftMonitorConfig,
    DriftMonitorInput,
    DriftMonitorOutput,
    compute_drift,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


def _effective_mode(override: str | None = None) -> str:
    m = (override or getattr(settings, "brain_drift_monitor_mode", "off") or "off").lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_drift_monitor_ops_log_enabled", True))


def _config_from_settings() -> DriftMonitorConfig:
    return DriftMonitorConfig(
        min_red_sample=int(
            getattr(settings, "brain_drift_monitor_min_red_sample", 20)
        ),
        min_yellow_sample=int(
            getattr(settings, "brain_drift_monitor_min_yellow_sample", 10)
        ),
        yellow_brier_abs=float(
            getattr(settings, "brain_drift_monitor_yellow_brier_abs", 0.10)
        ),
        red_brier_abs=float(
            getattr(settings, "brain_drift_monitor_red_brier_abs", 0.20)
        ),
        cusum_k=float(
            getattr(settings, "brain_drift_monitor_cusum_k", 0.05)
        ),
        cusum_threshold_mult=float(
            getattr(settings, "brain_drift_monitor_cusum_threshold_mult", 0.6)
        ),
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftSweepRow:
    log_id: int
    drift_id: str
    scan_pattern_id: int
    severity: str
    mode: str


@dataclass(frozen=True)
class DriftInputBundle:
    """One pattern's inputs for a single sweep.

    ``outcomes`` is ordered oldest-first. ``baseline_win_prob`` is the
    backtest baseline (e.g. pulled from ``scan_patterns``); ``None``
    makes the monitor a no-op for that pattern.
    """

    scan_pattern_id: int
    pattern_name: str | None
    baseline_win_prob: float | None
    outcomes: Sequence[int]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_one(
    db: Session,
    *,
    bundle: DriftInputBundle,
    as_of_key: str | None,
    mode_override: str | None = None,
    config: DriftMonitorConfig | None = None,
) -> DriftSweepRow | None:
    """Evaluate a single pattern and persist the row.

    Returns ``None`` in ``off`` mode. Raises ``RuntimeError`` in
    ``authoritative`` mode until Phase J.2 opens explicitly.
    """
    mode = _effective_mode(mode_override)
    if mode == "off":
        return None
    if mode == "authoritative":
        if _ops_log_enabled():
            logger.warning(
                format_drift_monitor_ops_line(
                    event="drift_refused_authoritative",
                    mode=mode,
                    scan_pattern_id=bundle.scan_pattern_id,
                    reason="phase_j_2_not_opened",
                )
            )
        raise RuntimeError(
            "drift_monitor authoritative mode is not permitted "
            "until Phase J.2 is explicitly opened",
        )

    cfg = config or _config_from_settings()
    inp = DriftMonitorInput(
        scan_pattern_id=bundle.scan_pattern_id,
        pattern_name=bundle.pattern_name,
        baseline_win_prob=bundle.baseline_win_prob,
        outcomes=tuple(bundle.outcomes),
        as_of_key=as_of_key,
    )
    out: DriftMonitorOutput = compute_drift(inp, config=cfg)

    now = datetime.utcnow()
    row = db.execute(text("""
        INSERT INTO trading_pattern_drift_log (
            drift_id, scan_pattern_id, pattern_name,
            baseline_win_prob, observed_win_prob,
            brier_delta, cusum_statistic, cusum_threshold,
            sample_size, severity, payload_json, mode,
            sweep_at, observed_at
        ) VALUES (
            :drift_id, :scan_pattern_id, :pattern_name,
            :baseline_p, :observed_p,
            :brier_delta, :cusum_stat, :cusum_thresh,
            :sample_size, :severity, CAST(:payload AS JSONB), :mode,
            :now, :now
        )
        RETURNING id
    """), {
        "drift_id": out.drift_id,
        "scan_pattern_id": out.scan_pattern_id,
        "pattern_name": out.pattern_name,
        "baseline_p": out.baseline_win_prob,
        "observed_p": out.observed_win_prob,
        "brier_delta": out.brier_delta,
        "cusum_stat": out.cusum_statistic,
        "cusum_thresh": out.cusum_threshold,
        "sample_size": int(out.sample_size),
        "severity": out.severity,
        "payload": json.dumps(out.payload, default=str, separators=(",", ":")),
        "mode": mode,
        "now": now,
    })
    new_id = int(row.scalar_one())
    db.commit()

    if _ops_log_enabled():
        logger.info(
            format_drift_monitor_ops_line(
                event="drift_persisted",
                mode=mode,
                drift_id=out.drift_id,
                scan_pattern_id=out.scan_pattern_id,
                pattern_name=out.pattern_name,
                severity=out.severity,
                sample_size=int(out.sample_size),
                baseline_win_prob=out.baseline_win_prob,
                observed_win_prob=out.observed_win_prob,
                brier_delta=out.brier_delta,
                cusum_statistic=out.cusum_statistic,
                cusum_threshold=out.cusum_threshold,
            )
        )

    return DriftSweepRow(
        log_id=new_id,
        drift_id=out.drift_id,
        scan_pattern_id=out.scan_pattern_id,
        severity=out.severity,
        mode=mode,
    )


def run_sweep(
    db: Session,
    *,
    bundles: Sequence[DriftInputBundle],
    as_of_date: date | str | None = None,
    mode_override: str | None = None,
    config: DriftMonitorConfig | None = None,
) -> list[DriftSweepRow]:
    """Iterate ``bundles`` and persist one row per pattern.

    Returns the full list of written rows. ``as_of_date`` seeds the
    deterministic ``drift_id`` salt used for dedupe.
    """
    mode = _effective_mode(mode_override)
    if mode == "off":
        return []

    as_of_key = (
        as_of_date.isoformat()
        if isinstance(as_of_date, date)
        else (str(as_of_date) if as_of_date else datetime.utcnow().date().isoformat())
    )

    rows: list[DriftSweepRow] = []
    for bundle in bundles:
        res = evaluate_one(
            db,
            bundle=bundle,
            as_of_key=as_of_key,
            mode_override=mode_override,
            config=config,
        )
        if res is not None:
            rows.append(res)
    return rows


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def drift_summary(
    db: Session,
    *,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary for drift sweeps.

    Keys (stable, order-preserving):
      * mode
      * lookback_days
      * drift_events_total
      * by_severity {green, yellow, red}
      * patterns_red
      * patterns_yellow
      * mean_brier_delta
      * mean_cusum_statistic
      * latest_drift {drift_id, scan_pattern_id, pattern_name,
                      severity, sample_size, observed_at}
    """
    mode = _effective_mode()

    total = int(db.execute(text("""
        SELECT COUNT(*) FROM trading_pattern_drift_log
        WHERE sweep_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    by_sev_rows = db.execute(text("""
        SELECT severity, COUNT(*) FROM trading_pattern_drift_log
        WHERE sweep_at >= (NOW() - (:ld || ' days')::INTERVAL)
        GROUP BY severity
    """), {"ld": int(lookback_days)}).fetchall()
    by_sev = {"green": 0, "yellow": 0, "red": 0}
    for sev, cnt in by_sev_rows:
        if sev in by_sev:
            by_sev[sev] = int(cnt or 0)

    patterns_red = int(db.execute(text("""
        SELECT COUNT(DISTINCT scan_pattern_id) FROM trading_pattern_drift_log
        WHERE sweep_at >= (NOW() - (:ld || ' days')::INTERVAL) AND severity = 'red'
    """), {"ld": int(lookback_days)}).scalar_one() or 0)
    patterns_yellow = int(db.execute(text("""
        SELECT COUNT(DISTINCT scan_pattern_id) FROM trading_pattern_drift_log
        WHERE sweep_at >= (NOW() - (:ld || ' days')::INTERVAL) AND severity = 'yellow'
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    agg = db.execute(text("""
        SELECT AVG(brier_delta), AVG(cusum_statistic)
        FROM trading_pattern_drift_log
        WHERE sweep_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).fetchone()
    mean_brier = float(agg[0]) if agg and agg[0] is not None else 0.0
    mean_cusum = float(agg[1]) if agg and agg[1] is not None else 0.0

    latest = db.execute(text("""
        SELECT drift_id, scan_pattern_id, pattern_name, severity,
               sample_size, observed_at
        FROM trading_pattern_drift_log
        ORDER BY observed_at DESC
        LIMIT 1
    """)).fetchone()
    latest_payload: dict[str, Any] | None = None
    if latest:
        latest_payload = {
            "drift_id": latest[0],
            "scan_pattern_id": latest[1],
            "pattern_name": latest[2],
            "severity": latest[3],
            "sample_size": int(latest[4]) if latest[4] is not None else None,
            "observed_at": latest[5].isoformat() if latest[5] else None,
        }

    return {
        "mode": mode,
        "lookback_days": int(lookback_days),
        "drift_events_total": total,
        "by_severity": by_sev,
        "patterns_red": patterns_red,
        "patterns_yellow": patterns_yellow,
        "mean_brier_delta": round(mean_brier, 6),
        "mean_cusum_statistic": round(mean_cusum, 6),
        "latest_drift": latest_payload,
    }


__all__ = [
    "DriftInputBundle",
    "DriftSweepRow",
    "drift_summary",
    "evaluate_one",
    "mode_is_active",
    "mode_is_authoritative",
    "run_sweep",
]
