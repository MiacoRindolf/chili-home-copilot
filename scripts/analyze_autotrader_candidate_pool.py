"""Explain AutoTrader v1 candidate-pool starvation.

The hot tick only processes unprocessed ``pattern_imminent`` alerts. This
report separates scanner supply from already-consumed supply so
``candidate_pool=0`` is not a mystery.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import or_, text
from sqlalchemy.orm import aliased

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("CHILI_APP_NAME", "chili-autotrader-candidate-pool-report")

from app.db import SessionLocal
from app.models.trading import AutoTraderRun, BreakoutAlert
from app.services.trading.auto_trader import _candidate_pool_zero_context


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--lookback-minutes", type=int, default=120)
    args = parser.parse_args()
    db = SessionLocal()
    try:
        ar = aliased(AutoTraderRun)
        pool = (
            db.query(BreakoutAlert)
            .outerjoin(ar, ar.breakout_alert_id == BreakoutAlert.id)
            .filter(
                BreakoutAlert.alert_tier == "pattern_imminent",
                or_(BreakoutAlert.user_id == args.user_id, BreakoutAlert.user_id.is_(None)),
                ar.id.is_(None),
            )
            .count()
        )
        print(f"candidate_pool_unprocessed={pool}")
        diag = _candidate_pool_zero_context(
            db, uid=args.user_id, lookback_minutes=args.lookback_minutes,
        )
        for key in ("recent_alerts", "processed", "unprocessed", "lifecycle_counts"):
            print(f"{key}={diag.get(key)}")
        print(f"latest={diag.get('latest')}")
        print(f"latest_unprocessed={diag.get('latest_unprocessed')}")
        rows = db.execute(text("""
            SELECT ar.decision, ar.reason, COUNT(*) AS n, MAX(ar.created_at) AS latest
            FROM trading_autotrader_runs ar
            WHERE ar.created_at >= NOW() - (:mins * INTERVAL '1 minute')
            GROUP BY ar.decision, ar.reason
            ORDER BY n DESC
            LIMIT 20
        """), {"mins": args.lookback_minutes}).mappings().all()
        print("\nrecent_decisions:")
        for row in rows:
            print(dict(row))
    finally:
        db.close()


if __name__ == "__main__":
    main()
