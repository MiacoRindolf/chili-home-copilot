"""Resolve ambiguous pattern ids to ``ScanPattern`` rows.

``TradingInsight`` links to ``ScanPattern`` only via ``scan_pattern_id`` (NOT NULL
and FK after migration 043). Do not derive links from ``pattern_description`` at
runtime.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...models.trading import ScanPattern, TradingInsight

_LEGACY_UNLINKED_NAME = "[Unlinked legacy insight]"


def is_legacy_unlinked_scan_pattern(pattern: ScanPattern | None) -> bool:
    """True for the shared sentinel row; never overwrite shared ``rules_json``."""
    if pattern is None:
        return False
    return (
        (getattr(pattern, "origin", "") or "").strip() == "legacy_unlinked"
        and (getattr(pattern, "name", "") or "").strip() == _LEGACY_UNLINKED_NAME
    )


def get_legacy_unlinked_scan_pattern_id(db: Session) -> int:
    """PK of the sentinel row; creates it if missing after test truncation."""
    sp = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.name == _LEGACY_UNLINKED_NAME,
            ScanPattern.origin == "legacy_unlinked",
        )
        .first()
    )
    if sp:
        return int(sp.id)
    sp = ScanPattern(
        name=_LEGACY_UNLINKED_NAME,
        description="Placeholder for insights that could not be linked to a real ScanPattern.",
        rules_json="{}",
        origin="legacy_unlinked",
        asset_class="all",
        timeframe="1d",
        confidence=0.0,
        active=False,
        promotion_status="legacy",
        lifecycle_stage="retired",
    )
    db.add(sp)
    db.commit()
    db.refresh(sp)
    return int(sp.id)


def resolve_to_scan_pattern_from_insight(
    db: Session, insight: TradingInsight
) -> ScanPattern | None:
    """Return ``ScanPattern`` for ``insight.scan_pattern_id`` if the row exists."""
    sid = getattr(insight, "scan_pattern_id", None)
    if sid is None:
        return None
    return db.get(ScanPattern, int(sid))


def resolve_to_scan_pattern(db: Session, pattern_id: int) -> ScanPattern | None:
    """Resolve ``pattern_id`` (TradingInsight PK or ScanPattern PK) to ``ScanPattern``.

    ``TradingInsight`` ids can collide with unrelated ``ScanPattern`` ids in test
    and restored databases, so insight foreign-key truth wins when both rows exist.
    If no insight resolves to a real pattern, fall back to a direct ScanPattern PK.
    """
    insight = db.get(TradingInsight, pattern_id)
    if insight:
        p = resolve_to_scan_pattern_from_insight(db, insight)
        if p:
            return p

    return db.get(ScanPattern, pattern_id)


def resolve_scan_pattern_id_for_insight(
    db: Session, insight: TradingInsight | None
) -> int | None:
    """Return ``ScanPattern.id`` for a ``TradingInsight``, or ``None`` if invalid."""
    if not insight:
        return None
    sp = resolve_to_scan_pattern_from_insight(db, insight)
    return int(sp.id) if sp else None
