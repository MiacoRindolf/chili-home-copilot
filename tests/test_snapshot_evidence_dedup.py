"""Snapshot bar upsert + trading_insight_evidence ledger."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.models.trading import MarketSnapshot, ScanPattern, TradingInsight
from app.services.trading.snapshot_bar_ops import (
    count_insight_evidence,
    try_insert_insight_evidence,
    upsert_market_snapshot,
)


@pytest.fixture
def insight_row(db):
    sp = ScanPattern(name="dedup-test", rules_json="{}", origin="test")
    db.add(sp)
    db.commit()
    db.refresh(sp)
    ins = TradingInsight(
        user_id=1,
        scan_pattern_id=sp.id,
        pattern_description="Test pattern for evidence ledger",
        confidence=0.5,
        evidence_count=0,
    )
    db.add(ins)
    db.commit()
    db.refresh(ins)
    return ins


def test_try_insert_insight_evidence_idempotent(db, insight_row):
    ts = datetime(2021, 6, 15, 12, 0, 0)
    assert try_insert_insight_evidence(
        db,
        insight_id=insight_row.id,
        ticker="BTC-USD",
        bar_interval="1d",
        bar_start_utc=ts,
        source="seek",
    ) is True
    db.commit()
    assert try_insert_insight_evidence(
        db,
        insight_id=insight_row.id,
        ticker="BTC-USD",
        bar_interval="1d",
        bar_start_utc=ts,
        source="seek",
    ) is False
    db.commit()
    assert count_insight_evidence(db, insight_row.id) == 1


def test_upsert_market_snapshot_same_bar_updates_not_duplicates(db):
    bar_t = datetime(2022, 3, 1, 0, 0, 0)
    upsert_market_snapshot(
        db,
        ticker="AAPL",
        bar_interval="1d",
        bar_start_at=bar_t,
        close_price=100.0,
        indicator_data=json.dumps({"rsi": {"value": 40}}),
        predicted_score=1.0,
        vix_at_snapshot=None,
        news_sentiment=None,
        news_count=None,
        pe_ratio=None,
        market_cap_b=None,
    )
    db.commit()
    upsert_market_snapshot(
        db,
        ticker="AAPL",
        bar_interval="1d",
        bar_start_at=bar_t,
        close_price=101.0,
        indicator_data=json.dumps({"rsi": {"value": 41}}),
        predicted_score=1.2,
        vix_at_snapshot=None,
        news_sentiment=None,
        news_count=None,
        pe_ratio=None,
        market_cap_b=None,
    )
    db.commit()
    n = (
        db.query(MarketSnapshot)
        .filter(
            MarketSnapshot.ticker == "AAPL",
            MarketSnapshot.bar_interval == "1d",
            MarketSnapshot.bar_start_at == bar_t,
        )
        .count()
    )
    assert n == 1
    row = (
        db.query(MarketSnapshot)
        .filter(
            MarketSnapshot.ticker == "AAPL",
            MarketSnapshot.bar_interval == "1d",
            MarketSnapshot.bar_start_at == bar_t,
        )
        .first()
    )
    assert row is not None
    assert row.close_price == 101.0
    assert row.snapshot_legacy is False
