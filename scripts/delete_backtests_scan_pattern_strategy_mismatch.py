#!/usr/bin/env python3
"""Delete stored backtests whose ``strategy_name`` does not match the linked ``ScanPattern.name``.

After deletion the learning/backtest queue can regenerate clean rows. Child ``PatternTradeRow``
rows for those backtests are removed first.

Usage:

  python scripts/delete_backtests_scan_pattern_strategy_mismatch.py
  python scripts/delete_backtests_scan_pattern_strategy_mismatch.py --apply
  python scripts/delete_backtests_scan_pattern_strategy_mismatch.py --apply --limit 500
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="Max backtests to delete (0 = no cap)")
    args = p.parse_args()

    from app.db import SessionLocal
    from app.models.trading import BacktestResult, PatternTradeRow, ScanPattern
    from app.services.trading.scan_pattern_label_alignment import (
        strategy_label_aligns_scan_pattern_name,
    )

    session = SessionLocal()
    to_delete: list[int] = []
    try:
        q = (
            session.query(BacktestResult, ScanPattern.name)
            .join(ScanPattern, ScanPattern.id == BacktestResult.scan_pattern_id)
            .filter(BacktestResult.scan_pattern_id.isnot(None))
        )
        for bt, sp_name in q.yield_per(500):
            if strategy_label_aligns_scan_pattern_name(bt.strategy_name, sp_name):
                continue
            to_delete.append(int(bt.id))
            if args.limit and len(to_delete) >= args.limit:
                break

        print(f"Mismatched backtests to delete: {len(to_delete)}")
        if len(to_delete) <= 30:
            print(" ids:", to_delete)
        else:
            print(" sample:", to_delete[:15], "...")

        if not args.apply:
            session.rollback()
            print("\n[dry-run] No deletes. Re-run with --apply.")
            return 0

        if not to_delete:
            session.commit()
            print("Nothing to delete.")
            return 0

        session.query(PatternTradeRow).filter(
            PatternTradeRow.backtest_result_id.in_(to_delete)
        ).delete(synchronize_session=False)
        session.query(BacktestResult).filter(BacktestResult.id.in_(to_delete)).delete(
            synchronize_session=False
        )
        session.commit()
        print(f"Deleted {len(to_delete)} backtests (+ pattern_trade rows).")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
