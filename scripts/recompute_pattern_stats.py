#!/usr/bin/env python3
"""Recompute ScanPattern.backtest_count / trade_count / win_rate from DB facts (same logic as migration 072).

Optionally refresh TradingInsight.win_count / loss_count from linked BacktestResult rows.

Usage (repo root, conda env chili-env):
  python scripts/recompute_pattern_stats.py
  python scripts/recompute_pattern_stats.py --insights-too
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal, engine
from app.services.trading.pattern_stats_recompute import (
    refresh_trading_insight_backtest_counts,
    recompute_scan_pattern_stats,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--insights-too",
        action="store_true",
        help="Also recompute TradingInsight win_count/loss_count from backtests",
    )
    args = p.parse_args()

    recompute_scan_pattern_stats(engine)
    print("recompute_scan_pattern_stats: done")

    if args.insights_too:
        db = SessionLocal()
        try:
            n = refresh_trading_insight_backtest_counts(db)
            print(f"refresh_trading_insight_backtest_counts: {n} insights updated")
        finally:
            db.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
