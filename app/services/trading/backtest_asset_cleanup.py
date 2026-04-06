"""Startup maintenance: backfill ScanPattern.asset_class from text hints and purge cross-asset backtests."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def run_cross_asset_backtest_cleanup(db: Session) -> dict[str, Any]:
    """Backfill ``asset_class`` when still ``all``, delete mismatched ``BacktestResult`` rows,
    recompute affected ``TradingInsight`` win/loss counts, then refresh ``ticker_scope``.

    Returns a small stats dict for logging.
    """
    from ...models.trading import BacktestResult, ScanPattern, TradingInsight
    from .backtest_engine import _extract_context
    from .learning import recompute_ticker_scope
    from .market_data import is_crypto

    stats: dict[str, Any] = {
        "patterns_asset_backfilled": 0,
        "backtests_deleted": 0,
        "insights_recomputed": 0,
        "scopes_recomputed": 0,
    }
    patterns_for_scope: set[int] = set()

    # 1) Backfill asset_class from name + description
    for p in db.query(ScanPattern).all():
        ac = (p.asset_class or "all").strip().lower()
        if ac not in ("all", ""):
            continue
        blob = f"{p.name or ''} {p.description or ''}"
        ctx = _extract_context(blob, db=None, insight_id=None)
        if ctx.get("crypto_only"):
            p.asset_class = "crypto"
            stats["patterns_asset_backfilled"] += 1
            patterns_for_scope.add(p.id)
        elif ctx.get("stock_only"):
            p.asset_class = "stocks"
            stats["patterns_asset_backfilled"] += 1
            patterns_for_scope.add(p.id)
    if stats["patterns_asset_backfilled"]:
        db.commit()

    crypto_pat_ids = [
        r[0]
        for r in db.query(ScanPattern.id).filter(ScanPattern.asset_class == "crypto").all()
    ]
    stock_pat_ids = [
        r[0]
        for r in db.query(ScanPattern.id).filter(ScanPattern.asset_class == "stocks").all()
    ]

    affected_insights: set[int] = set()
    deleted = 0

    def _purge_for_patterns(pattern_ids: list[int], require_crypto_ticker: bool) -> None:
        nonlocal deleted
        if not pattern_ids:
            return
        clauses = [BacktestResult.scan_pattern_id.in_(pattern_ids)]
        ins_ids = [
            r[0]
            for r in db.query(TradingInsight.id)
            .filter(TradingInsight.scan_pattern_id.in_(pattern_ids))
            .all()
        ]
        if ins_ids:
            clauses.append(BacktestResult.related_insight_id.in_(ins_ids))
        rows = db.query(BacktestResult).filter(or_(*clauses)).all()
        for bt in rows:
            t = bt.ticker or ""
            if require_crypto_ticker:
                if is_crypto(t):
                    continue
            else:
                if not is_crypto(t):
                    continue
            if bt.scan_pattern_id:
                patterns_for_scope.add(bt.scan_pattern_id)
            if bt.related_insight_id:
                affected_insights.add(bt.related_insight_id)
                ins_row = db.get(TradingInsight, bt.related_insight_id)
                if ins_row and ins_row.scan_pattern_id:
                    patterns_for_scope.add(ins_row.scan_pattern_id)
            db.delete(bt)
            deleted += 1

    _purge_for_patterns(crypto_pat_ids, require_crypto_ticker=True)
    _purge_for_patterns(stock_pat_ids, require_crypto_ticker=False)
    db.flush()
    stats["backtests_deleted"] = deleted

    # Recompute win/loss for touched insights (panel definition: deduped, trade-weighted).
    from .insight_backtest_panel_sync import sync_insight_backtest_tallies_from_evidence_panel

    for ins_id in affected_insights:
        ins = db.get(TradingInsight, ins_id)
        if not ins:
            continue
        try:
            sync_insight_backtest_tallies_from_evidence_panel(db, ins)
        except Exception:
            logger.exception(
                "[asset_cleanup] insight_backtest_panel_sync failed for insight %s", ins_id
            )
        stats["insights_recomputed"] += 1

    if affected_insights or deleted:
        db.commit()

    # Refresh ticker_scope only for patterns we backfilled or had backtests removed
    for pid in sorted(patterns_for_scope):
        try:
            recompute_ticker_scope(db, pid)
            stats["scopes_recomputed"] += 1
        except Exception:
            logger.exception("[asset_cleanup] recompute_ticker_scope failed for pattern %s", pid)
    if patterns_for_scope:
        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("[asset_cleanup] commit after scope recompute failed")

    return stats
