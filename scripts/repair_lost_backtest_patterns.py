"""
Repair patterns whose backtest rows were lost (e.g. by migration 081 dedup)
but still carry stale denormalized stats (lifecycle_stage, backtest_count, etc.).

Resets them to candidate stage so the queue re-processes them.

Usage:
    python scripts/repair_lost_backtest_patterns.py            # dry-run
    python scripts/repair_lost_backtest_patterns.py --apply     # execute
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal
from sqlalchemy import text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually write changes")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT sp.id, sp.name, sp.lifecycle_stage, sp.backtest_count,
                   sp.win_rate, sp.last_backtest_at, sp.origin
            FROM scan_patterns sp
            WHERE sp.active = true
              AND sp.lifecycle_stage NOT IN ('candidate')
              AND NOT EXISTS (
                  SELECT 1 FROM trading_backtests tb
                  WHERE tb.scan_pattern_id = sp.id
              )
            ORDER BY sp.id
        """)).fetchall()

        if not rows:
            print("No patterns need repair.")
            return 0

        print(f"Found {len(rows)} active patterns with advanced lifecycle but zero backtest rows:\n")
        for r in rows:
            print(
                f"  id={r[0]:>4}  stage={r[2]:<12}  bt_count={r[3]:>4}  "
                f"wr={r[4] or 0:.1f}%  origin={r[6]:<16}  {r[1][:55]}"
            )

        if not args.apply:
            print(f"\nDry run — pass --apply to reset these {len(rows)} patterns to candidate stage.")
            return 0

        ids = [r[0] for r in rows]
        updated = db.execute(text("""
            UPDATE scan_patterns
            SET lifecycle_stage   = 'candidate',
                lifecycle_changed_at = NOW(),
                backtest_count    = 0,
                win_rate          = NULL,
                avg_return_pct    = NULL,
                oos_win_rate      = NULL,
                oos_avg_return_pct = NULL,
                oos_trade_count   = NULL,
                last_backtest_at  = NULL,
                confidence        = 0,
                evidence_count    = 0,
                trade_count       = 0,
                backtest_priority = 10
            WHERE id = ANY(:ids)
        """), {"ids": ids}).rowcount
        db.commit()
        print(f"\nReset {updated} patterns to candidate (priority=10 so they queue ahead of new candidates).")
        return 0

    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
