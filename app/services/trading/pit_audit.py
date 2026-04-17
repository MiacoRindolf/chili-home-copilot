"""Phase C: PIT audit service.

Classifies every active ``ScanPattern.rules_json`` against ``pit_contract``
allow/deny lists and records the result to ``trading_pit_audit_log`` when
``brain_pit_audit_mode`` is not ``off``.

Purely advisory this phase — no pattern is ever quarantined, deactivated, or
otherwise mutated. Phase J owns drift-based quarantine.

Failures are swallowed by callers (learning cycle) via try/except; this module
itself does not catch broad exceptions internally so unit tests can observe
real errors. The learning-cycle hook is responsible for defensive wrapping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Iterable, Sequence

from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import PitAuditLog, ScanPattern
from . import pit_contract as _pit

logger = logging.getLogger(__name__)

_DEFAULT_LIFECYCLE_STAGES: tuple[str, ...] = (
    "backtested",
    "validated",
    "challenged",
    "promoted",
    "live",
)


@dataclass(frozen=True)
class PitAuditResult:
    pattern_id: int
    name: str | None
    origin: str | None
    lifecycle_stage: str | None
    pit_fields: list[str] = field(default_factory=list)
    non_pit_fields: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)

    @property
    def violation_count(self) -> int:
        return len(self.non_pit_fields) + len(self.unknown_fields)

    @property
    def agree_bool(self) -> bool:
        return self.violation_count == 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["violation_count"] = self.violation_count
        d["agree_bool"] = self.agree_bool
        return d


def mode_is_active() -> bool:
    """True if the shadow/compare/authoritative audit should run."""
    return str(getattr(settings, "brain_pit_audit_mode", "off")).lower() != "off"


def audit_pattern(pattern: ScanPattern) -> PitAuditResult:
    """Classify a single ``ScanPattern.rules_json`` without DB side-effects."""
    classified = _pit.classify_rules(getattr(pattern, "rules_json", None))
    return PitAuditResult(
        pattern_id=int(getattr(pattern, "id", 0) or 0),
        name=getattr(pattern, "name", None),
        origin=getattr(pattern, "origin", None),
        lifecycle_stage=getattr(pattern, "lifecycle_stage", None),
        pit_fields=list(classified.get("pit", [])),
        non_pit_fields=list(classified.get("non_pit", [])),
        unknown_fields=list(classified.get("unknown", [])),
    )


def audit_active_patterns(
    db: Session,
    *,
    lifecycle_stages: Sequence[str] | None = None,
    only_active: bool = True,
    limit: int | None = None,
) -> list[PitAuditResult]:
    """Audit every matching ``ScanPattern``.

    Default filter: active patterns in the lifecycle stages that can be
    acted on by the trading brain (backtested, validated, challenged,
    promoted, live). Candidates are excluded — they churn rapidly and
    auditing them drowns the log in transient noise.
    """
    stages: tuple[str, ...] = tuple(lifecycle_stages or _DEFAULT_LIFECYCLE_STAGES)

    q = db.query(ScanPattern)
    if only_active:
        q = q.filter(ScanPattern.active.is_(True))
    if stages:
        q = q.filter(ScanPattern.lifecycle_stage.in_(stages))
    q = q.order_by(ScanPattern.id.asc())
    if limit is not None and limit > 0:
        q = q.limit(int(limit))

    return [audit_pattern(p) for p in q.all()]


def record_audit(db: Session, result: PitAuditResult, *, mode: str | None = None) -> int | None:
    """Persist one audit row and return its id. Mode defaults to settings value.

    Caller owns commit semantics — this function ``db.flush()`` but does not
    commit. Safe to call inside an existing transaction.
    """
    effective_mode = (mode or str(getattr(settings, "brain_pit_audit_mode", "off"))).lower()
    row = PitAuditLog(
        pattern_id=result.pattern_id,
        name=(result.name or None),
        origin=(result.origin or None),
        lifecycle_stage=(result.lifecycle_stage or None),
        pit_count=len(result.pit_fields),
        non_pit_count=len(result.non_pit_fields),
        unknown_count=len(result.unknown_fields),
        pit_fields=list(result.pit_fields),
        non_pit_fields=list(result.non_pit_fields),
        unknown_fields=list(result.unknown_fields),
        agree_bool=bool(result.agree_bool),
        mode=effective_mode,
    )
    db.add(row)
    db.flush()

    if bool(getattr(settings, "brain_pit_audit_ops_log_enabled", True)):
        try:
            from ...trading_brain.infrastructure.pit_ops_log import format_pit_ops_line
            logger.info(
                "%s",
                format_pit_ops_line(
                    mode=effective_mode,
                    pattern_id=result.pattern_id,
                    name=result.name,
                    lifecycle=result.lifecycle_stage,
                    pit_count=len(result.pit_fields),
                    non_pit_count=len(result.non_pit_fields),
                    unknown_count=len(result.unknown_fields),
                    agree=result.agree_bool,
                ),
            )
        except Exception:
            logger.debug("[pit_audit] ops log emit failed", exc_info=True)

    return int(row.id) if row.id is not None else None


def audit_and_record_active(
    db: Session,
    *,
    lifecycle_stages: Sequence[str] | None = None,
    only_active: bool = True,
    limit: int | None = None,
) -> list[PitAuditResult]:
    """Convenience: audit + record in one pass. Used by the learning-cycle hook.

    Returns the list of ``PitAuditResult`` that were successfully recorded.
    """
    if not mode_is_active():
        return []

    results = audit_active_patterns(
        db,
        lifecycle_stages=lifecycle_stages,
        only_active=only_active,
        limit=limit,
    )
    recorded: list[PitAuditResult] = []
    for r in results:
        try:
            record_audit(db, r)
            recorded.append(r)
        except Exception:
            logger.debug("[pit_audit] record_audit failed for pattern %s", r.pattern_id, exc_info=True)
    return recorded


def audit_summary(
    db: Session,
    *,
    lookback_hours: int = 24,
) -> dict:
    """Aggregate the most-recent audit per pattern within the lookback window.

    Returns a dict suitable for the diagnostics endpoint.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import func

    if lookback_hours <= 0:
        lookback_hours = 1
    cutoff = datetime.utcnow() - timedelta(hours=int(lookback_hours))

    latest_per_pattern_subq = (
        db.query(
            PitAuditLog.pattern_id.label("pattern_id"),
            func.max(PitAuditLog.created_at).label("max_created"),
        )
        .filter(PitAuditLog.created_at >= cutoff)
        .group_by(PitAuditLog.pattern_id)
        .subquery()
    )

    latest_rows = (
        db.query(PitAuditLog)
        .join(
            latest_per_pattern_subq,
            (PitAuditLog.pattern_id == latest_per_pattern_subq.c.pattern_id)
            & (PitAuditLog.created_at == latest_per_pattern_subq.c.max_created),
        )
        .all()
    )

    total_audits = (
        db.query(func.count(PitAuditLog.id))
        .filter(PitAuditLog.created_at >= cutoff)
        .scalar()
        or 0
    )

    patterns_audited = len(latest_rows)
    patterns_clean = sum(1 for r in latest_rows if r.agree_bool)
    patterns_violating = patterns_audited - patterns_clean

    forbidden_hits: dict[str, int] = {}
    unknown_hits: dict[str, int] = {}
    for r in latest_rows:
        for f in (r.non_pit_fields or []):
            forbidden_hits[f] = forbidden_hits.get(f, 0) + 1
        for f in (r.unknown_fields or []):
            unknown_hits[f] = unknown_hits.get(f, 0) + 1

    top_violators = [
        {
            "pattern_id": r.pattern_id,
            "name": r.name,
            "lifecycle": r.lifecycle_stage,
            "non_pit_fields": list(r.non_pit_fields or []),
            "unknown_fields": list(r.unknown_fields or []),
        }
        for r in sorted(
            latest_rows,
            key=lambda x: (-(int(x.non_pit_count or 0) + int(x.unknown_count or 0)), x.pattern_id),
        )
        if (int(r.non_pit_count or 0) + int(r.unknown_count or 0)) > 0
    ][:20]

    return {
        "mode": str(getattr(settings, "brain_pit_audit_mode", "off")),
        "lookback_hours": int(lookback_hours),
        "audits_total": int(total_audits),
        "patterns_audited": int(patterns_audited),
        "patterns_clean": int(patterns_clean),
        "patterns_violating": int(patterns_violating),
        "top_violators": top_violators,
        "forbidden_hits_by_field": forbidden_hits,
        "unknown_hits_by_field": unknown_hits,
    }
