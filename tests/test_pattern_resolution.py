"""Regression: TradingInsight.id must not be treated as ScanPattern.id."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.trading import ScanPattern, TradingInsight
from app.services.trading.pattern_resolution import (
    resolve_scan_pattern_id_for_insight,
    resolve_to_scan_pattern,
    resolve_to_scan_pattern_from_insight,
)


def test_resolve_scan_pattern_id_for_insight_no_pk_collision(db):
    """When insight.id equals another ScanPattern row's id, follow insight.scan_pattern_id."""
    sp_wrong = ScanPattern(
        name="Momentum Breakout",
        rules_json="{}",
        origin="test",
    )
    sp_right = ScanPattern(
        name="RSI Overbought",
        rules_json='{"conditions":[{"indicator":"rsi_14","op":">","value":70}]}',
        origin="test",
    )
    db.add(sp_wrong)
    db.add(sp_right)
    db.commit()
    db.refresh(sp_wrong)
    db.refresh(sp_right)

    assert sp_wrong.id == 1
    assert sp_right.id == 2

    ins = TradingInsight(
        scan_pattern_id=sp_right.id,
        pattern_description="RSI overbought — sell signal",
        confidence=0.8,
    )
    db.add(ins)
    db.commit()
    db.refresh(ins)

    assert ins.id == 1
    assert ins.id == sp_wrong.id

    resolved = resolve_scan_pattern_id_for_insight(db, ins)
    assert resolved == sp_right.id

    sp_obj = resolve_to_scan_pattern_from_insight(db, ins)
    assert sp_obj is not None
    assert sp_obj.id == sp_right.id
    assert sp_obj.name == "RSI Overbought"

    by_insight_id = resolve_to_scan_pattern(db, ins.id)
    assert by_insight_id is not None
    assert by_insight_id.id == sp_right.id

    by_sp_id = resolve_to_scan_pattern(db, sp_right.id)
    assert by_sp_id is not None
    assert by_sp_id.id == sp_right.id


def test_trading_insight_fk_rejects_missing_scan_pattern(db):
    db.add(
        TradingInsight(
            scan_pattern_id=9_999_999,
            pattern_description="orphan fk row",
            confidence=0.5,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
