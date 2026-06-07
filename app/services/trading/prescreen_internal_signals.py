"""Brain-memory tickers merged into daily prescreen (capped per kind)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ...config import settings
from ...db import rollback_if_poisoned
from ...models.trading import ScanPattern, TradingInsight, TradingInsightEvidence
from ...models.trading_brain_phase1 import BrainPredictionLine, BrainPredictionSnapshot
from .prescreen_normalize import (
    iter_normalized_prescreen_tickers,
    normalize_prescreen_ticker,
)

logger = logging.getLogger(__name__)

_INTERNAL_MIN_PER_KIND = 5
_INTERNAL_DEFAULT_MAX_PER_KIND = 40
_INSIGHT_EVIDENCE_LOOKBACK_DAYS = 14
_INSIGHT_EVIDENCE_OVERFETCH_MULTIPLIER = 3
_WARMING_PATTERN_LOOKBACK_HOURS = 48
_WARMING_PATTERN_QUERY_LIMIT = 80


def _max_per_kind() -> int:
    return max(
        _INTERNAL_MIN_PER_KIND,
        int(
            getattr(
                settings,
                "brain_prescreen_internal_max_per_kind",
                _INTERNAL_DEFAULT_MAX_PER_KIND,
            )
        ),
    )


def tickers_from_latest_predictions(db: Session, limit: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Top lines from the latest prediction mirror snapshot."""
    lim = limit if limit is not None else _max_per_kind()
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        snap = (
            db.query(BrainPredictionSnapshot.id)
            .order_by(desc(BrainPredictionSnapshot.id))
            .first()
        )
        if not snap:
            return out
        snapshot_id = _row_field(snap, "id", 0)
        lines = (
            db.query(BrainPredictionLine.ticker, BrainPredictionLine.sort_rank, BrainPredictionLine.score)
            .filter(BrainPredictionLine.snapshot_id == snapshot_id)
            .order_by(BrainPredictionLine.sort_rank.asc())
            .limit(lim)
            .all()
        )
        for ln in lines:
            tn = normalize_prescreen_ticker(_row_field(ln, "ticker", 0))
            if not tn:
                continue
            reason = {
                "kind": "brain_prediction",
                "snapshot_id": int(snapshot_id),
                "sort_rank": int(_row_field(ln, "sort_rank", 1)),
                "score": float(_row_field(ln, "score", 2)),
            }
            out.setdefault(tn, []).append(reason)
    except Exception as e:
        logger.warning("[prescreen_internal] predictions: %s", e)
        # A mid-transaction disconnect poisons the shared session; roll back so
        # the next bucket in collect_internal_prescreen_tickers doesn't cascade
        # with PendingRollbackError.
        rollback_if_poisoned(db)
    return out


def tickers_from_insight_evidence(db: Session, limit: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Tickers tied to active insights with recent evidence (continuity)."""
    lim = limit if limit is not None else _max_per_kind()
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        since = datetime.utcnow() - timedelta(days=_INSIGHT_EVIDENCE_LOOKBACK_DAYS)
        q = (
            db.query(TradingInsightEvidence.ticker, TradingInsightEvidence.insight_id)
            .join(TradingInsight, TradingInsight.id == TradingInsightEvidence.insight_id)
            .filter(TradingInsight.active.is_(True))
            .filter(TradingInsightEvidence.created_at >= since)
            .distinct()
            .limit(lim * _INSIGHT_EVIDENCE_OVERFETCH_MULTIPLIER)
        )
        seen: set[str] = set()
        for ticker, iid in q:
            if len(seen) >= lim:
                break
            tn = normalize_prescreen_ticker(ticker)
            if not tn or tn in seen:
                continue
            seen.add(tn)
            out[tn] = [{"kind": "insight_evidence", "insight_id": int(iid)}]
    except Exception as e:
        logger.warning("[prescreen_internal] insight_evidence: %s", e)
        rollback_if_poisoned(db)
    return out


def tickers_from_warming_patterns(db: Session, limit: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Patterns touched recently with explicit scope tickers (not universal)."""
    lim = limit if limit is not None else _max_per_kind()
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        since = datetime.utcnow() - timedelta(hours=_WARMING_PATTERN_LOOKBACK_HOURS)
        rows = (
            db.query(ScanPattern.id, ScanPattern.scope_tickers)
            .filter(ScanPattern.active.is_(True))
            .filter(ScanPattern.updated_at >= since)
            .filter(ScanPattern.ticker_scope != "universal")
            .filter(ScanPattern.scope_tickers.isnot(None))
            .filter(ScanPattern.scope_tickers != "")
            .order_by(desc(ScanPattern.updated_at))
            .limit(_WARMING_PATTERN_QUERY_LIMIT)
            .all()
        )
        seen: set[str] = set()
        for sp in rows:
            if len(seen) >= lim:
                break
            for tn in iter_normalized_prescreen_tickers(_row_field(sp, "scope_tickers", 1)):
                if tn in seen:
                    continue
                seen.add(tn)
                out[tn] = [{"kind": "warming_pattern", "scan_pattern_id": int(_row_field(sp, "id", 0))}]
    except Exception as e:
        logger.warning("[prescreen_internal] warming_patterns: %s", e)
        rollback_if_poisoned(db)
    return out


def collect_internal_prescreen_tickers(db: Session) -> dict[str, list[dict[str, Any]]]:
    """Merge capped internal buckets; later keys do not overwrite earlier reasons."""
    merged: dict[str, list[dict[str, Any]]] = {}
    for bucket in (
        tickers_from_latest_predictions,
        tickers_from_insight_evidence,
        tickers_from_warming_patterns,
    ):
        for t, reasons in bucket(db).items():
            merged.setdefault(t, []).extend(reasons)
    return merged


def _row_field(row: Any, field: str, index: int) -> Any:
    if isinstance(row, (tuple, list)):
        return row[index] if len(row) > index else None
    return getattr(row, field, None)
