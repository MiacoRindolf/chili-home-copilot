"""Brain-memory tickers merged into daily prescreen (capped per kind)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import ScanPattern, TradingInsight, TradingInsightEvidence
from ...models.trading_brain_phase1 import BrainPredictionLine, BrainPredictionSnapshot
from .prescreen_normalize import normalize_prescreen_ticker

logger = logging.getLogger(__name__)


def _max_per_kind() -> int:
    return max(5, int(getattr(settings, "brain_prescreen_internal_max_per_kind", 40)))


def tickers_from_latest_predictions(db: Session, limit: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Top lines from the latest prediction mirror snapshot."""
    lim = limit if limit is not None else _max_per_kind()
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        snap = (
            db.query(BrainPredictionSnapshot)
            .order_by(desc(BrainPredictionSnapshot.id))
            .first()
        )
        if not snap:
            return out
        lines = (
            db.query(BrainPredictionLine)
            .filter(BrainPredictionLine.snapshot_id == snap.id)
            .order_by(BrainPredictionLine.sort_rank.asc())
            .limit(lim)
            .all()
        )
        for ln in lines:
            tn = normalize_prescreen_ticker(ln.ticker)
            if not tn:
                continue
            reason = {
                "kind": "brain_prediction",
                "snapshot_id": int(snap.id),
                "sort_rank": int(ln.sort_rank),
                "score": float(ln.score),
            }
            out.setdefault(tn, []).append(reason)
    except Exception as e:
        logger.warning("[prescreen_internal] predictions: %s", e)
    return out


def tickers_from_insight_evidence(db: Session, limit: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Tickers tied to active insights with recent evidence (continuity)."""
    lim = limit if limit is not None else _max_per_kind()
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        since = datetime.utcnow() - timedelta(days=14)
        q = (
            db.query(TradingInsightEvidence.ticker, TradingInsightEvidence.insight_id)
            .join(TradingInsight, TradingInsight.id == TradingInsightEvidence.insight_id)
            .filter(TradingInsight.active.is_(True))
            .filter(TradingInsightEvidence.created_at >= since)
            .distinct()
            .limit(lim * 3)
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
    return out


def tickers_from_warming_patterns(db: Session, limit: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Patterns touched recently with explicit scope tickers (not universal)."""
    lim = limit if limit is not None else _max_per_kind()
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        since = datetime.utcnow() - timedelta(hours=48)
        rows = (
            db.query(ScanPattern)
            .filter(ScanPattern.active.is_(True))
            .filter(ScanPattern.updated_at >= since)
            .filter(ScanPattern.ticker_scope != "universal")
            .filter(ScanPattern.scope_tickers.isnot(None))
            .filter(ScanPattern.scope_tickers != "")
            .order_by(desc(ScanPattern.updated_at))
            .limit(80)
            .all()
        )
        seen: set[str] = set()
        for sp in rows:
            if len(seen) >= lim:
                break
            raw = (sp.scope_tickers or "").replace("\n", ",")
            for part in raw.split(","):
                tn = normalize_prescreen_ticker(part)
                if not tn or tn in seen:
                    continue
                seen.add(tn)
                out[tn] = [{"kind": "warming_pattern", "scan_pattern_id": int(sp.id)}]
    except Exception as e:
        logger.warning("[prescreen_internal] warming_patterns: %s", e)
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
