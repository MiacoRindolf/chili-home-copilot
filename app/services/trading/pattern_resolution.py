"""Single source of truth: resolve ambiguous pattern_id to ScanPattern.

pattern_id may be a ScanPattern primary key or a TradingInsight primary key.
Uses FK links and stored BacktestResult rows — no description string heuristics.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...models.trading import BacktestResult, ScanPattern, TradingInsight


def resolve_to_scan_pattern(db: Session, pattern_id: int) -> ScanPattern | None:
    """Resolve pattern_id (ScanPattern.id or TradingInsight.id) to ScanPattern.

    Rules (in order):
    1. Direct ScanPattern PK lookup.
    2. TradingInsight.id -> insight.scan_pattern_id FK.
    3. Any BacktestResult for this insight with scan_pattern_id set.
    4. Match BacktestResult.strategy_name to ScanPattern.name (case-insensitive);
       on success, back-fill insight.scan_pattern_id for future calls.
    """
    p = db.get(ScanPattern, pattern_id)
    if p:
        return p

    # pattern_id may be a stale ScanPattern PK (row deleted/pruned) still stored on
    # TradingInsight.scan_pattern_id — resolve via any insight that references it.
    ins_linking_sp = (
        db.query(TradingInsight)
        .filter(TradingInsight.scan_pattern_id == pattern_id)
        .order_by(TradingInsight.id.asc())
        .first()
    )
    if ins_linking_sp:
        return resolve_to_scan_pattern(db, int(ins_linking_sp.id))

    insight = db.get(TradingInsight, pattern_id)
    if not insight:
        return None

    sp_id = getattr(insight, "scan_pattern_id", None)
    if sp_id:
        p = db.get(ScanPattern, int(sp_id))
        if p:
            return p

    bt_sp = (
        db.query(BacktestResult.scan_pattern_id)
        .filter(
            BacktestResult.related_insight_id == pattern_id,
            BacktestResult.scan_pattern_id.isnot(None),
        )
        .distinct()
        .first()
    )
    if bt_sp and bt_sp[0]:
        p = db.get(ScanPattern, int(bt_sp[0]))
        if p:
            if not getattr(insight, "scan_pattern_id", None):
                try:
                    insight.scan_pattern_id = p.id
                    db.commit()
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass
            return p

    strategy_rows = (
        db.query(BacktestResult.strategy_name)
        .filter(BacktestResult.related_insight_id == pattern_id)
        .distinct()
        .all()
    )
    for (name,) in strategy_rows:
        if not name or not str(name).strip():
            continue
        p = (
            db.query(ScanPattern)
            .filter(func.lower(ScanPattern.name) == str(name).strip().lower())
            .first()
        )
        if p:
            try:
                insight.scan_pattern_id = p.id
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            return p

    return None


def resolve_scan_pattern_id_for_insight(db: Session, insight: TradingInsight | None) -> int | None:
    """Return ScanPattern.id for a TradingInsight, or None."""
    if not insight:
        return None
    sp = resolve_to_scan_pattern(db, int(insight.id))
    return int(sp.id) if sp else None
