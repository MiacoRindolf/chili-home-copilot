"""Canonical OHLCV bar keys and snapshot upserts (dedupe on ticker + interval + bar open UTC)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session


def normalize_bar_start_utc(ts: Any) -> datetime:
    """UTC-naive datetime for DB (matches pandas index normalized to UTC then tz stripped)."""
    if ts is None:
        raise ValueError("bar timestamp required")
    if isinstance(ts, datetime):
        dt = ts
    elif hasattr(ts, "to_pydatetime"):
        dt = ts.to_pydatetime()  # type: ignore[union-attr]
    else:
        dt = pd.Timestamp(ts).to_pydatetime()
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def row_bar_key(row: dict) -> tuple[str, str, datetime] | None:
    t = row.get("ticker")
    iv = row.get("bar_interval")
    bs = row.get("bar_start_utc")
    if t is None or not iv or bs is None:
        return None
    try:
        bsn = normalize_bar_start_utc(bs)
    except Exception:
        return None
    return (str(t).upper(), str(iv), bsn)


def dedupe_sample_rows(rows: list[dict]) -> list[dict]:
    """Keep first occurrence per (ticker, bar_interval, bar_start_utc); pass through rows without keys."""
    seen: set[tuple[str, str, datetime]] = set()
    out: list[dict] = []
    for r in rows:
        k = row_bar_key(r)
        if k is None:
            out.append(r)
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def upsert_market_snapshot(
    db: Session,
    *,
    ticker: str,
    bar_interval: str,
    bar_start_at: datetime,
    close_price: float,
    indicator_data: str | None,
    predicted_score: float | None,
    vix_at_snapshot: float | None,
    news_sentiment: float | None,
    news_count: int | None,
    pe_ratio: float | None,
    market_cap_b: float | None,
) -> None:
    """Insert or update snapshot for one bar. snapshot_date = ingestion time. Preserves future_return_*."""
    from ...models.trading import MarketSnapshot

    ticker_u = ticker.upper()
    biv = bar_interval.strip()
    bs = normalize_bar_start_utc(bar_start_at)
    ingested = datetime.utcnow()

    existing = (
        db.query(MarketSnapshot)
        .filter(
            MarketSnapshot.ticker == ticker_u,
            MarketSnapshot.bar_interval == biv,
            MarketSnapshot.bar_start_at == bs,
        )
        .first()
    )
    if existing:
        existing.snapshot_date = ingested
        existing.close_price = close_price
        existing.indicator_data = indicator_data
        existing.predicted_score = predicted_score
        existing.vix_at_snapshot = vix_at_snapshot
        existing.news_sentiment = news_sentiment
        existing.news_count = news_count
        existing.pe_ratio = pe_ratio
        existing.market_cap_b = market_cap_b
        existing.snapshot_legacy = False
        return

    row = MarketSnapshot(
        ticker=ticker_u,
        snapshot_date=ingested,
        close_price=close_price,
        indicator_data=indicator_data,
        predicted_score=predicted_score,
        vix_at_snapshot=vix_at_snapshot,
        news_sentiment=news_sentiment,
        news_count=news_count,
        pe_ratio=pe_ratio,
        market_cap_b=market_cap_b,
        bar_interval=biv,
        bar_start_at=bs,
        snapshot_legacy=False,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise


def try_insert_insight_evidence(
    db: Session,
    *,
    insight_id: int,
    ticker: str,
    bar_interval: str,
    bar_start_utc: datetime,
    source: str,
) -> bool:
    """Return True if a new evidence row was inserted."""
    from ...models.trading import TradingInsightEvidence

    t_u = ticker.upper()
    biv = bar_interval.strip()
    bs = normalize_bar_start_utc(bar_start_utc)
    src = (source or "")[:24]
    q = (
        db.query(TradingInsightEvidence)
        .filter(
            TradingInsightEvidence.insight_id == insight_id,
            TradingInsightEvidence.ticker == t_u,
            TradingInsightEvidence.bar_interval == biv,
            TradingInsightEvidence.bar_start_utc == bs,
        )
        .first()
    )
    if q is not None:
        return False
    db.add(
        TradingInsightEvidence(
            insight_id=insight_id,
            ticker=t_u,
            bar_interval=biv,
            bar_start_utc=bs,
            source=src,
        )
    )
    db.flush()
    return True


def count_insight_evidence(db: Session, insight_id: int) -> int:
    from ...models.trading import TradingInsightEvidence

    return (
        db.query(TradingInsightEvidence)
        .filter(TradingInsightEvidence.insight_id == insight_id)
        .count()
    )
