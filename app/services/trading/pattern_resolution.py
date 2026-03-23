"""Resolve ambiguous pattern_id to ScanPattern.

``TradingInsight`` links to ``ScanPattern`` only via ``scan_pattern_id`` (NOT NULL + FK
after migration 043). Do not derive links from ``pattern_description`` at runtime.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...models.trading import ScanPattern, TradingInsight

_LEGACY_UNLINKED_NAME = "[Unlinked legacy insight]"


def get_legacy_unlinked_scan_pattern_id(db: Session) -> int:
    """PK of the sentinel row (migration 043); creates it if missing (e.g. tests after TRUNCATE)."""
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
    """Resolve ``pattern_id`` (ScanPattern PK or TradingInsight PK) to ``ScanPattern``.

    Rules:
    1. Direct ``ScanPattern`` PK lookup.
    2. Else treat ``pattern_id`` as ``TradingInsight.id`` → follow ``scan_pattern_id`` FK.
    """
    p = db.get(ScanPattern, pattern_id)
    if p:
        return p

    insight = db.get(TradingInsight, pattern_id)
    if not insight:
        return None

    return resolve_to_scan_pattern_from_insight(db, insight)


def resolve_scan_pattern_id_for_insight(db: Session, insight: TradingInsight | None) -> int | None:
    """Return ``ScanPattern.id`` for a ``TradingInsight``, or ``None`` if FK missing/invalid."""
    if not insight:
        return None
    sp = resolve_to_scan_pattern_from_insight(db, insight)
    return int(sp.id) if sp else None
