"""Phase K - canonical ops health aggregation (pure functions).

The ops health model takes the frozen-shape ``*_summary`` dicts from
each Phase A-J service, plus the scheduler / governance state, and
produces a single flat snapshot that operators can hit in one HTTP
call (``/api/trading/brain/ops/health``).

**Pure:** no DB, no logging, no config reads. All upstream data is
passed in via :class:`OpsHealthSnapshotInput`.

**Frozen contract:** the ``to_dict()`` output must keep the top-level
key order and sub-key names stable. Tests pin the shape. Adding new
phases is allowed by appending keys; removing or renaming is a
breaking change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# Ordered per-phase keys - must match the diagnostics endpoint wire order.
PHASE_KEYS = (
    "ledger",            # Phase A
    "exit_engine",       # Phase B
    "net_edge",          # Phase E
    "pit",               # Phase C
    "triple_barrier",    # Phase D
    "execution_cost",    # Phase F
    "venue_truth",       # Phase F
    "bracket_intent",    # Phase G
    "bracket_reconciliation",  # Phase G
    "position_sizer",    # Phase H
    "risk_dial",         # Phase I
    "capital_reweight",  # Phase I
    "drift_monitor",     # Phase J
    "recert_queue",      # Phase J
    "divergence",        # Phase K
)


@dataclass(frozen=True)
class OpsHealthSnapshotInput:
    """Inputs for :func:`compute_snapshot`.

    Every ``*_summary`` value is the frozen-shape summary dict that
    each phase's diagnostics endpoint already returns. ``None`` means
    the phase is either disabled or unavailable; the snapshot records
    this as a ``skipped`` phase.

    ``scheduler`` and ``governance`` are optional; ``scheduler`` is
    expected to be the dict returned by
    ``trading_scheduler.get_scheduler_info()``.
    """

    phase_summaries: Mapping[str, Mapping[str, Any] | None]
    scheduler: Mapping[str, Any] | None = None
    governance: Mapping[str, Any] | None = None
    lookback_days: int = 14


@dataclass(frozen=True)
class PhaseHealth:
    """Per-phase health line inside the snapshot."""

    key: str
    present: bool
    mode: str | None
    red_count: int
    yellow_count: int
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OpsHealthSnapshot:
    """Pure snapshot of whole-substrate health."""

    phases: list[PhaseHealth]
    scheduler_running: bool
    scheduler_jobs: int
    kill_switch_engaged: bool
    pending_approvals: int
    overall_severity: str
    lookback_days: int

    def to_dict(self) -> dict[str, Any]:
        """Stable wire shape. Tests pin the exact key set."""
        return {
            "overall_severity": self.overall_severity,
            "lookback_days": int(self.lookback_days),
            "scheduler": {
                "running": bool(self.scheduler_running),
                "job_count": int(self.scheduler_jobs),
            },
            "governance": {
                "kill_switch_engaged": bool(self.kill_switch_engaged),
                "pending_approvals": int(self.pending_approvals),
            },
            "phases": [
                {
                    "key": p.key,
                    "present": bool(p.present),
                    "mode": p.mode,
                    "red_count": int(p.red_count),
                    "yellow_count": int(p.yellow_count),
                    "notes": list(p.notes),
                }
                for p in self.phases
            ],
        }


def _coerce_count(value: Any) -> int:
    """Best-effort int coercion from messy upstream shapes."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def _extract_severity_counts(
    summary: Mapping[str, Any] | None,
) -> tuple[int, int, list[str]]:
    """Pull ``red_count`` / ``yellow_count`` from a summary dict.

    Handles several shapes the various phase summaries use:
    * ``{"by_severity": {"red": 3, "yellow": 1, ...}, ...}``
    * ``{"severity_breakdown": {"red": ..., "yellow": ..., ...}, ...}``
    * ``{"red_count": ..., "yellow_count": ...}``
    * anything else -> (0, 0, [note])
    """
    if not summary:
        return 0, 0, []

    for key in ("by_severity", "severity_breakdown", "severities"):
        candidate = summary.get(key) if isinstance(summary, Mapping) else None
        if isinstance(candidate, Mapping):
            return (
                _coerce_count(candidate.get("red")),
                _coerce_count(candidate.get("yellow")),
                [],
            )

    if isinstance(summary, Mapping) and (
        "red_count" in summary or "yellow_count" in summary
    ):
        return (
            _coerce_count(summary.get("red_count")),
            _coerce_count(summary.get("yellow_count")),
            [],
        )

    return 0, 0, ["no_severity_breakdown"]


def _extract_mode(summary: Mapping[str, Any] | None) -> str | None:
    if not isinstance(summary, Mapping):
        return None
    mode = summary.get("mode")
    if isinstance(mode, str):
        return mode
    return None


def _overall_severity(phases: list[PhaseHealth]) -> str:
    """Worst-of across phases with special-cases.

    * ``red`` - any phase has ``red_count > 0`` **or** is in
      ``authoritative`` mode with any red/yellow breach (tighter
      guard on authoritative).
    * ``yellow`` - any phase has ``yellow_count > 0`` or is
      ``present=False`` with known key (missing summary is a
      degraded signal, not a total fail).
    * ``green`` - otherwise.
    """
    has_red = False
    has_yellow = False
    for p in phases:
        if p.red_count > 0:
            has_red = True
        if p.yellow_count > 0:
            has_yellow = True
        if p.mode == "authoritative" and (p.red_count or p.yellow_count):
            has_red = True
        if p.key in PHASE_KEYS and not p.present:
            has_yellow = True

    if has_red:
        return "red"
    if has_yellow:
        return "yellow"
    return "green"


def _extract_scheduler(
    scheduler: Mapping[str, Any] | None,
) -> tuple[bool, int]:
    if not isinstance(scheduler, Mapping):
        return False, 0
    running = bool(scheduler.get("running", False))
    jobs = scheduler.get("jobs")
    if isinstance(jobs, list):
        return running, len(jobs)
    return running, _coerce_count(scheduler.get("job_count"))


def _extract_governance(
    governance: Mapping[str, Any] | None,
) -> tuple[bool, int]:
    if not isinstance(governance, Mapping):
        return False, 0
    kill_switch = governance.get("kill_switch")
    engaged = False
    if isinstance(kill_switch, Mapping):
        engaged = bool(kill_switch.get("engaged"))
    elif isinstance(kill_switch, bool):
        engaged = kill_switch

    pending = governance.get("pending_approvals")
    if isinstance(pending, list):
        return engaged, len(pending)
    return engaged, _coerce_count(pending)


def compute_snapshot(inputs: OpsHealthSnapshotInput) -> OpsHealthSnapshot:
    """Build a pure :class:`OpsHealthSnapshot` from upstream summaries.

    Unknown phase keys in ``inputs.phase_summaries`` are silently
    ignored to keep the wire shape stable across versions. Missing
    keys from :data:`PHASE_KEYS` are recorded with ``present=False``.
    """
    phases: list[PhaseHealth] = []
    for key in PHASE_KEYS:
        summary = inputs.phase_summaries.get(key)
        present = isinstance(summary, Mapping) and bool(summary)
        mode = _extract_mode(summary)
        red, yellow, notes = _extract_severity_counts(summary)
        phases.append(
            PhaseHealth(
                key=key,
                present=present,
                mode=mode,
                red_count=red,
                yellow_count=yellow,
                notes=notes,
            )
        )

    sched_running, sched_jobs = _extract_scheduler(inputs.scheduler)
    kill_switch, pending = _extract_governance(inputs.governance)
    overall = _overall_severity(phases)

    return OpsHealthSnapshot(
        phases=phases,
        scheduler_running=sched_running,
        scheduler_jobs=sched_jobs,
        kill_switch_engaged=kill_switch,
        pending_approvals=pending,
        overall_severity=overall,
        lookback_days=int(inputs.lookback_days),
    )


__all__ = [
    "OpsHealthSnapshot",
    "OpsHealthSnapshotInput",
    "PHASE_KEYS",
    "PhaseHealth",
    "compute_snapshot",
]
