"""Phase J - canonical re-certification proposal (pure functions).

The re-cert queue is a small pure helper on top of the drift monitor.
Given a ``DriftMonitorOutput`` (or a user-initiated request), it
decides whether a re-cert proposal should be created and, if so,
returns a ``RecertProposal`` with a deterministic id.

Phase J.1 uses the proposal **only** to write a row into
``trading_pattern_recert_log``. No downstream consumer reads this
table in J.1; Phase J.2 will wire it into the backtest queue and the
lifecycle FSM.

This module is 100% pure - no DB, no logging, no config reads.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date

from .drift_monitor_model import DriftMonitorOutput

_VALID_STATUSES = (
    "proposed",
    "dispatched",
    "completed",
    "cancelled",
)

_VALID_SOURCES = (
    "drift_monitor",
    "manual",
    "scheduler",
)


@dataclass(frozen=True)
class RecertQueueConfig:
    """Tuning knobs for the re-cert proposal."""

    # Minimum severity that auto-creates a proposal.
    trigger_severity: str = "red"
    # When True, ``yellow`` severities also propose (louder observability
    # during soak). Default False: keep the queue lean.
    include_yellow: bool = False


@dataclass(frozen=True)
class RecertProposal:
    """Pure result of :func:`propose_recert`.

    Shadow-safe: nothing in this dataclass triggers a side effect.
    """

    recert_id: str
    scan_pattern_id: int
    pattern_name: str | None
    as_of_date: date
    source: str
    severity: str | None
    status: str
    reason: str | None
    drift_log_id: int | None
    payload: dict = field(default_factory=dict)


def compute_recert_id(
    *,
    scan_pattern_id: int,
    as_of_date: date | str,
    source: str,
) -> str:
    """Deterministic hash for ``(pattern, as_of, source)`` dedupe."""
    as_of_str = (
        as_of_date.isoformat()
        if isinstance(as_of_date, date)
        else str(as_of_date)
    )
    basis = f"{int(scan_pattern_id)}|{as_of_str}|{source}"
    return hashlib.blake2b(
        basis.encode("utf-8"), digest_size=16,
    ).hexdigest()


def propose_from_drift(
    drift: DriftMonitorOutput,
    *,
    as_of_date: date | str,
    source: str = "drift_monitor",
    drift_log_id: int | None = None,
    config: RecertQueueConfig | None = None,
) -> RecertProposal | None:
    """Turn a drift decision into a proposal (or ``None``).

    Returns ``None`` when the severity does not warrant a proposal
    under the given config. Returning ``None`` is the normal path for
    healthy patterns.
    """
    cfg = config or RecertQueueConfig()
    severity = drift.severity or "green"
    should_propose = severity == cfg.trigger_severity or (
        cfg.include_yellow and severity == "yellow"
    )
    if not should_propose:
        return None

    if source not in _VALID_SOURCES:
        raise ValueError(
            f"recert_queue.propose_from_drift: invalid source {source!r}"
        )

    recert_id = compute_recert_id(
        scan_pattern_id=drift.scan_pattern_id,
        as_of_date=as_of_date,
        source=source,
    )

    reason = (
        f"drift_severity={severity} "
        f"brier_delta={drift.brier_delta if drift.brier_delta is not None else 'na'} "
        f"cusum={drift.cusum_statistic if drift.cusum_statistic is not None else 'na'}"
    )

    payload = {
        "baseline_win_prob": drift.baseline_win_prob,
        "observed_win_prob": drift.observed_win_prob,
        "brier_delta": drift.brier_delta,
        "cusum_statistic": drift.cusum_statistic,
        "cusum_threshold": drift.cusum_threshold,
        "sample_size": drift.sample_size,
    }

    as_of_value = (
        as_of_date
        if isinstance(as_of_date, date)
        else date.fromisoformat(str(as_of_date))
    )

    return RecertProposal(
        recert_id=recert_id,
        scan_pattern_id=int(drift.scan_pattern_id),
        pattern_name=drift.pattern_name,
        as_of_date=as_of_value,
        source=source,
        severity=severity,
        status="proposed",
        reason=reason,
        drift_log_id=drift_log_id,
        payload=payload,
    )


def propose_manual(
    *,
    scan_pattern_id: int,
    pattern_name: str | None,
    as_of_date: date | str,
    reason: str,
) -> RecertProposal:
    """Construct a manual re-cert proposal (user-initiated)."""
    recert_id = compute_recert_id(
        scan_pattern_id=scan_pattern_id,
        as_of_date=as_of_date,
        source="manual",
    )
    as_of_value = (
        as_of_date
        if isinstance(as_of_date, date)
        else date.fromisoformat(str(as_of_date))
    )
    return RecertProposal(
        recert_id=recert_id,
        scan_pattern_id=int(scan_pattern_id),
        pattern_name=pattern_name,
        as_of_date=as_of_value,
        source="manual",
        severity=None,
        status="proposed",
        reason=reason,
        drift_log_id=None,
        payload={"origin": "manual"},
    )


__all__ = [
    "RecertQueueConfig",
    "RecertProposal",
    "compute_recert_id",
    "propose_from_drift",
    "propose_manual",
]
