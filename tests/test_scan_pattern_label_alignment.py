"""``strategy_name`` vs ``ScanPattern.name`` alignment for evidence backtests."""
from __future__ import annotations

import pytest

from app.models.trading import ScanPattern, TradingInsight
from app.routers.trading_sub.ai import (
    _evidence_representative_backtest,
    _sibling_insight_ids_for_pattern,
)
from app.services.trading.scan_pattern_label_alignment import (
    strategy_label_aligns_scan_pattern_name,
)


def test_strategy_label_aligns_exact() -> None:
    assert strategy_label_aligns_scan_pattern_name("Alpha", "Alpha") is True


def test_strategy_label_aligns_truncated_pattern_name() -> None:
    long_p = "x" * 120
    assert strategy_label_aligns_scan_pattern_name(long_p[:100], long_p) is True


def test_strategy_label_mismatch() -> None:
    assert (
        strategy_label_aligns_scan_pattern_name(
            "RC Bull Flag Breakout [BOS-wide]",
            "Bull flag/pennant continuation breakout (post-impulse consolidation)",
        )
        is False
    )


def test_evidence_representative_prefers_trades() -> None:
    from datetime import datetime
    from types import SimpleNamespace

    newer_empty = SimpleNamespace(
        trade_count=0, ran_at=datetime(2026, 4, 3, 12, 0, 0), id="new"
    )
    older_bt = SimpleNamespace(
        trade_count=1, ran_at=datetime(2026, 3, 18, 12, 0, 0), id="old"
    )
    pick = _evidence_representative_backtest([newer_empty, older_bt])
    assert pick.id == "old"


def test_sibling_insight_ids_by_scan_pattern_only(db) -> None:
    sp = ScanPattern(name="shared-fk", rules_json={}, origin="test")
    db.add(sp)
    db.commit()
    db.refresh(sp)
    a = TradingInsight(
        user_id=1,
        scan_pattern_id=sp.id,
        pattern_description="Alpha Strategy — body",
        confidence=0.5,
        evidence_count=0,
    )
    b = TradingInsight(
        user_id=1,
        scan_pattern_id=sp.id,
        pattern_description="Beta Other — other body",
        confidence=0.5,
        evidence_count=0,
    )
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    s_a = _sibling_insight_ids_for_pattern(db, a.id, sp.id, sp.id)
    assert sorted(s_a) == sorted([a.id, b.id])
