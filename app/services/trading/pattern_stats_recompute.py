"""Recompute ScanPattern aggregates and TradingInsight backtest tallies (ops / post-repair).

Mirrors migration ``072_recompute_pattern_stats`` SQL so it can be run after data repairs
without re-applying the whole migration chain.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import inspect, text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def recompute_scan_pattern_stats(engine: Engine) -> None:
    """UPDATE scan_patterns backtest_count, trade_count, win_rate from canonical tables."""
    stmts = []
    stmts.append(
        """
        UPDATE scan_patterns sp
        SET backtest_count = sub.cnt
        FROM (
            SELECT scan_pattern_id, COUNT(*) AS cnt
            FROM trading_backtests
            WHERE scan_pattern_id IS NOT NULL
            GROUP BY scan_pattern_id
        ) sub
        WHERE sp.id = sub.scan_pattern_id
          AND (sp.backtest_count IS NULL OR sp.backtest_count != sub.cnt)
        """
    )
    stmts.append(
        """
        UPDATE scan_patterns
        SET backtest_count = 0
        WHERE id NOT IN (
            SELECT DISTINCT scan_pattern_id FROM trading_backtests
            WHERE scan_pattern_id IS NOT NULL
        ) AND backtest_count > 0
        """
    )
    stmts.append(
        """
        UPDATE scan_patterns sp
        SET trade_count = sub.cnt
        FROM (
            SELECT scan_pattern_id, COUNT(*) AS cnt
            FROM trading_trades
            WHERE scan_pattern_id IS NOT NULL
            GROUP BY scan_pattern_id
        ) sub
        WHERE sp.id = sub.scan_pattern_id
          AND (sp.trade_count IS NULL OR sp.trade_count != sub.cnt)
        """
    )
    stmts.append(
        """
        UPDATE scan_patterns
        SET trade_count = 0
        WHERE id NOT IN (
            SELECT DISTINCT scan_pattern_id FROM trading_trades
            WHERE scan_pattern_id IS NOT NULL
        ) AND trade_count > 0
        """
    )
    stmts.append(
        """
        UPDATE scan_patterns sp
        SET win_rate = sub.wr
        FROM (
            SELECT scan_pattern_id,
                   CASE WHEN COUNT(*) > 0
                        THEN COUNT(*) FILTER (WHERE pnl > 0)::float / COUNT(*)
                        ELSE 0 END AS wr
            FROM trading_trades
            WHERE scan_pattern_id IS NOT NULL
              AND status = 'closed'
            GROUP BY scan_pattern_id
        ) sub
        WHERE sp.id = sub.scan_pattern_id
          AND sp.trade_count >= 5
        """
    )
    tables = set(inspect(engine).get_table_names())
    if "scan_patterns" not in tables:
        logger.warning("[pattern_stats_recompute] scan_patterns missing; skip")
        return
    with engine.begin() as conn:
        for sql in stmts:
            if "trading_backtests" in sql and "trading_backtests" not in tables:
                continue
            if "trading_trades" in sql and "trading_trades" not in tables:
                continue
            conn.execute(text(sql))
    logger.info("[pattern_stats_recompute] scan_patterns aggregates updated")


def refresh_trading_insight_backtest_counts(db: Session) -> int:
    """Set win_count / loss_count from BacktestResult rows per insight (related_insight_id)."""
    from ...models.trading import BacktestResult, TradingInsight

    ids = [
        r[0]
        for r in db.query(BacktestResult.related_insight_id)
        .filter(BacktestResult.related_insight_id.isnot(None))
        .distinct()
        .all()
    ]
    updated = 0
    for iid in ids:
        ins = db.get(TradingInsight, int(iid))
        if not ins:
            continue
        bts = (
            db.query(BacktestResult)
            .filter(BacktestResult.related_insight_id == ins.id)
            .all()
        )
        with_trades = [b for b in bts if (b.trade_count or 0) > 0]
        wins = sum(1 for b in with_trades if (b.return_pct or 0) > 0)
        ins.win_count = wins
        ins.loss_count = len(with_trades) - wins
        updated += 1
    if updated:
        db.commit()
    logger.info("[pattern_stats_recompute] refreshed TradingInsight counts for %d insights", updated)
    return updated
