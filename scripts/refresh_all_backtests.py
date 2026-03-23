"""
Re-run ``smart_backtest_insight`` for active trading insights (refreshes stored
``BacktestResult`` rows with current pattern engine + market data).

Run from project root (``conda activate chili-env``):

  python scripts/refresh_all_backtests.py
  python scripts/refresh_all_backtests.py --force          # ignore 2h skip
  python scripts/refresh_all_backtests.py --force --limit 5

Pair with param alignment (usually a no-op if already synced):

  python scripts/backfill_backtest_metadata.py --apply --brain
"""
import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from datetime import datetime as dt, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("backtest_refresh.log"),
    ],
)
logger = logging.getLogger(__name__)

from app.db import SessionLocal
from app.models.trading import TradingInsight, BacktestResult
from app.services.trading.backtest_engine import smart_backtest_insight


def main(*, force: bool = False, limit: int | None = None) -> None:
    db = SessionLocal()
    
    try:
        # Skip insights that already have a backtest row touched in the last 2 hours
        # unless --force (e.g. after market-data or engine fixes).
        recent_insight_ids: set[int | None] = set()
        if not force:
            cutoff = dt.utcnow() - timedelta(hours=2)
            recent_insight_ids = {
                r[0] for r in db.query(BacktestResult.related_insight_id).filter(
                    BacktestResult.ran_at >= cutoff,
                    BacktestResult.related_insight_id.isnot(None)
                ).distinct().all()
            }
        
        active_insights = db.query(TradingInsight).filter(
            TradingInsight.active == True,
            ~TradingInsight.id.in_(recent_insight_ids) if recent_insight_ids else True
        ).all()
        if limit is not None and limit > 0:
            active_insights = active_insights[:limit]
        
        logger.info(
            "Found %s active insights to process (force=%s, skipping %s recently refreshed)",
            len(active_insights),
            force,
            len(recent_insight_ids),
        )
        
        # 3. Re-run backtests for each insight
        total_wins = 0
        total_losses = 0
        total_backtests = 0
        
        for i, insight in enumerate(active_insights, 1):
            logger.info(f"[{i}/{len(active_insights)}] Processing insight {insight.id}: {(insight.pattern_description or '')[:50]}...")
            
            try:
                result = smart_backtest_insight(
                    db,
                    insight,
                    target_tickers=40,
                    update_confidence=True,
                )
                
                wins = result.get("wins", 0)
                losses = result.get("losses", 0)
                bt_count = result.get("backtests_run", 0)
                
                total_wins += wins
                total_losses += losses
                total_backtests += bt_count
                
                logger.info(f"  -> Completed: {bt_count} backtests, {wins} wins, {losses} losses")
                
            except Exception as e:
                logger.error(f"  -> Error: {e}")
                db.rollback()
                continue
        
        logger.info("=" * 60)
        logger.info(f"COMPLETED: {total_backtests} total backtests")
        logger.info(f"  Wins: {total_wins}, Losses: {total_losses}")
        logger.info(f"  Win rate: {total_wins / max(1, total_wins + total_losses) * 100:.1f}%")
        
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Refresh smart backtests for active insights")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Process all active insights (do not skip those with backtests in the last 2 hours)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process at most N insights (after filters)",
    )
    args = ap.parse_args()
    start = dt.now()
    logger.info(f"Starting backtest refresh at {start}")
    main(force=args.force, limit=args.limit)
    end = dt.now()
    logger.info(f"Finished at {end} (duration: {end - start})")
