"""
Wipe old backtests for active patterns and re-run fresh.
Run from project root: python scripts/refresh_all_backtests.py
"""
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


def main():
    db = SessionLocal()
    
    try:
        # 1. Get all active insights that need refresh
        # Skip insights that already have recent backtests (created in last 2 hours)
        cutoff = dt.utcnow() - timedelta(hours=2)
        
        # Get insight IDs that already have recent backtests
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
        
        logger.info(f"Found {len(active_insights)} active insights to process (skipping {len(recent_insight_ids)} already refreshed)")
        
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
    start = dt.now()
    logger.info(f"Starting backtest refresh at {start}")
    main()
    end = dt.now()
    logger.info(f"Finished at {end} (duration: {end - start})")
