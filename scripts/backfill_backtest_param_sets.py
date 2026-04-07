#!/usr/bin/env python3
"""Backfill ``trading_backtests.param_set_id`` from existing ``params`` JSON.

Run from repo root (conda env ``chili-env``):

  python scripts/backfill_backtest_param_sets.py
  python scripts/backfill_backtest_param_sets.py --dry-run
  python scripts/backfill_backtest_param_sets.py --limit 2000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Count only; no commits")
    ap.add_argument("--limit", type=int, default=None, help="Max backtest rows to process")
    ap.add_argument("--commit-every", type=int, default=200, help="Commit after this many updates")
    args = ap.parse_args()

    from app.db import SessionLocal
    from app.models.trading import BacktestResult
    from app.services.trading.backtest_param_sets import get_or_create_backtest_param_set

    db = SessionLocal()
    processed = 0
    linked = 0
    skipped = 0
    since_commit = 0
    try:
        q = (
            db.query(BacktestResult)
            .filter(BacktestResult.param_set_id.is_(None))
            .filter(BacktestResult.params.isnot(None))
            .order_by(BacktestResult.id.asc())
        )
        if args.limit is not None:
            q = q.limit(int(args.limit))

        for bt in q.yield_per(100):
            processed += 1
            raw = bt.params
            try:
                if isinstance(raw, str):
                    obj = json.loads(raw)
                elif isinstance(raw, dict):
                    obj = dict(raw)
                else:
                    skipped += 1
                    continue
            except (json.JSONDecodeError, TypeError, ValueError):
                skipped += 1
                continue
            if not isinstance(obj, dict) or not obj:
                skipped += 1
                continue
            sid = get_or_create_backtest_param_set(db, obj)
            if sid is None:
                skipped += 1
                continue
            if not args.dry_run:
                bt.param_set_id = int(sid)
                since_commit += 1
                linked += 1
                if since_commit >= args.commit_every:
                    db.commit()
                    since_commit = 0
            else:
                linked += 1

        if not args.dry_run and since_commit:
            db.commit()

        print(
            f"processed_rows={processed} param_sets_linked={linked} skipped={skipped} dry_run={args.dry_run}",
        )
        return 0
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
