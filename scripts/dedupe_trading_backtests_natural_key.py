#!/usr/bin/env python3
"""Keep one ``trading_backtests`` row per (related_insight_id, ticker, strategy_name).

Chooses the row with highest ``trade_count``, then latest ``ran_at``. Deletes others and their
``trading_pattern_trades``. Run after identifying duplicates via
``scripts/report_trading_brain_backtest_health.py``.

  python scripts/dedupe_trading_backtests_natural_key.py
  python scripts/dedupe_trading_backtests_natural_key.py --apply
  python scripts/dedupe_trading_backtests_natural_key.py --apply --scan-pattern-id 205
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--scan-pattern-id", type=int, default=None)
    args = ap.parse_args()

    from app.db import SessionLocal
    from app.models.trading import BacktestResult, PatternTradeRow

    session = SessionLocal()
    try:
        q = session.query(BacktestResult).filter(
            BacktestResult.related_insight_id.isnot(None),
        )
        if args.scan_pattern_id is not None:
            q = q.filter(BacktestResult.scan_pattern_id == int(args.scan_pattern_id))

        groups: dict[tuple[int, str, str], list[BacktestResult]] = defaultdict(list)
        for bt in q.yield_per(500):
            iid = int(bt.related_insight_id or 0)
            tk = (bt.ticker or "").strip()
            st = (bt.strategy_name or "").strip()
            if not tk or not st:
                continue
            groups[(iid, tk, st)].append(bt)

        to_drop: list[int] = []
        for _k, rows in groups.items():
            if len(rows) < 2:
                continue
            rows.sort(
                key=lambda b: (
                    int(b.trade_count or 0),
                    b.ran_at.timestamp() if b.ran_at else 0.0,
                    int(b.id),
                ),
                reverse=True,
            )
            for loser in rows[1:]:
                to_drop.append(int(loser.id))

        print(f"Rows to delete (duplicate natural keys): {len(to_drop)}")
        if len(to_drop) <= 40:
            print(" ids:", to_drop)

        if not args.apply:
            session.rollback()
            print("\n[dry-run] Re-run with --apply.")
            return 0

        if to_drop:
            session.query(PatternTradeRow).filter(
                PatternTradeRow.backtest_result_id.in_(to_drop)
            ).delete(synchronize_session=False)
            session.query(BacktestResult).filter(BacktestResult.id.in_(to_drop)).delete(
                synchronize_session=False
            )
            session.commit()
            print(f"Deleted {len(to_drop)} duplicate backtests.")
        else:
            session.commit()
            print("No duplicates.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
