"""
Report backtest health: win_rate scale, insight↔pattern linkage, 084 preview counts,
strategy_name vs pattern name samples, duplicate (strategy_name, ticker) groups.

Usage (project root, conda env chili-env):
  python scripts/diagnose_backtest_pattern_alignment.py
  python scripts/diagnose_backtest_pattern_alignment.py --limit-mismatch 50
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import and_, or_, text

from app.db import SessionLocal
from app.models.trading import BacktestResult, TradingInsight


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--limit-mismatch",
        type=int,
        default=100,
        help="Max pattern/insight mismatch rows to print",
    )
    p.add_argument(
        "--limit-dupes",
        type=int,
        default=15,
        help="Max duplicate (strategy_name,ticker) groups to print",
    )
    args = p.parse_args()

    db = SessionLocal()
    try:
        n_bad_scale = (
            db.query(BacktestResult)
            .filter(
                or_(
                    and_(BacktestResult.win_rate.isnot(None), BacktestResult.win_rate > 1.0),
                    and_(
                        BacktestResult.oos_win_rate.isnot(None),
                        BacktestResult.oos_win_rate > 1.0,
                    ),
                )
            )
            .count()
        )
        print(f"[scale] win_rate or oos_win_rate > 1.0: {n_bad_scale}")
        if n_bad_scale:
            print("  → Apply migration 083 or re-save rows.")

        n_084 = (
            db.query(BacktestResult)
            .join(TradingInsight, BacktestResult.related_insight_id == TradingInsight.id)
            .filter(
                TradingInsight.scan_pattern_id.isnot(None),
                or_(
                    BacktestResult.scan_pattern_id.is_(None),
                    BacktestResult.scan_pattern_id != TradingInsight.scan_pattern_id,
                ),
            )
            .count()
        )
        print(f"[linkage] rows migration 084 would align (trust insight): {n_084}")

        q_mis = (
            db.query(BacktestResult, TradingInsight)
            .join(TradingInsight, BacktestResult.related_insight_id == TradingInsight.id)
            .filter(
                BacktestResult.scan_pattern_id.isnot(None),
                TradingInsight.scan_pattern_id.isnot(None),
                BacktestResult.scan_pattern_id != TradingInsight.scan_pattern_id,
            )
            .limit(args.limit_mismatch)
        )
        rows = q_mis.all()
        print(
            f"[linkage] sample strict mismatches (both scan_pattern_id set, differ), limit {args.limit_mismatch}: {len(rows)}"
        )
        for bt, ins in rows:
            print(
                f"  bt.id={bt.id} bt.scan_pattern_id={bt.scan_pattern_id} "
                f"ins.id={ins.id} ins.scan_pattern_id={ins.scan_pattern_id} ticker={bt.ticker!r}"
            )

        # Raw SQL avoids ORM loading full ScanPattern (stale DBs may lack columns the model has).
        n_name_mis = db.execute(
            text(
                "SELECT COUNT(*) FROM trading_backtests bt "
                "JOIN scan_patterns sp ON sp.id = bt.scan_pattern_id "
                "WHERE bt.scan_pattern_id IS NOT NULL "
                "AND bt.strategy_name IS DISTINCT FROM sp.name"
            )
        ).scalar()
        print(f"[semantic] backtest.strategy_name != scan_patterns.name (heuristic): {n_name_mis}")
        sm = db.execute(
            text(
                "SELECT bt.id, bt.ticker, bt.strategy_name, sp.name AS pattern_name "
                "FROM trading_backtests bt "
                "JOIN scan_patterns sp ON sp.id = bt.scan_pattern_id "
                "WHERE bt.scan_pattern_id IS NOT NULL "
                "AND bt.strategy_name IS DISTINCT FROM sp.name "
                "LIMIT 10"
            )
        ).fetchall()
        for row in sm:
            print(
                f"  bt.id={row[0]} ticker={row[1]!r} strat={row[2]!r} pattern={row[3]!r}"
            )

        dup_sql = text(
            "SELECT strategy_name, ticker, COUNT(*) AS c "
            "FROM trading_backtests GROUP BY strategy_name, ticker HAVING COUNT(*) > 1 "
            "ORDER BY c DESC LIMIT :lim"
        )
        dup_rows = list(db.execute(dup_sql, {"lim": args.limit_dupes}))
        print(f"[dedupe] duplicate (strategy_name, ticker) groups (top {args.limit_dupes} by count):")
        if not dup_rows:
            print("  (none)")
        for r in dup_rows:
            print(f"  {r[0]!r} / {r[1]!r}: {r[2]} rows")

        print()
        print("Docs: docs/TRADING_BACKTEST_DB_AUDIT.md | 081 risk: docs/MIGRATION_081_BACKTEST_DEDUPE_RISK.md")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
