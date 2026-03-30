"""Idempotent: tag legacy trading_backtests rows missing data_provenance in params JSON.

Run: conda activate chili-env && python scripts/backfill_backtest_provenance.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Repo root on path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.models.trading import BacktestResult  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Max rows to update (0=all)")
    args = ap.parse_args()

    db = SessionLocal()
    updated = 0
    try:
        q = db.query(BacktestResult).order_by(BacktestResult.id.asc())
        if args.limit > 0:
            q = q.limit(args.limit)
        for row in q.yield_per(200):
            raw = row.params
            if not raw:
                blob: dict = {}
            else:
                try:
                    blob = json.loads(raw)
                except json.JSONDecodeError:
                    blob = {}
            dp = blob.get("data_provenance")
            if isinstance(dp, dict) and dp.get("status") != "unknown" and dp.get("ohlc_bars"):
                continue
            blob["data_provenance"] = {
                "status": "unknown",
                "ticker": row.ticker,
                "strategy_name": row.strategy_name,
                "note": "backfilled_by_script",
            }
            if args.dry_run:
                updated += 1
                continue
            row.params = json.dumps(blob)
            updated += 1
            if updated % 50 == 0:
                db.commit()
        if not args.dry_run:
            db.commit()
    finally:
        db.close()

    print(f"{'Would update' if args.dry_run else 'Updated'} {updated} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
