"""On-demand three-way ledger integrity check for the live momentum lane.

Compares momentum_automation_outcomes x momentum_mfe_realized events x
momentum_fill_outcomes legs x Alpaca broker order history, and exits non-zero when
they disagree — so it is usable as a pre-deploy / pre-analysis gate, not just a
report.

READ-ONLY. It never writes to the database and never places, replaces or cancels
a broker order.

    conda run -n chili-env python scripts/ledger_integrity_check.py --days 21
    conda run -n chili-env python scripts/ledger_integrity_check.py --days 21 --json > census.json
    conda run -n chili-env python scripts/ledger_integrity_check.py --no-broker   # partial coverage

Exit codes: 0 clean, 1 violations found or broker unreadable, 2 harness error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=3.0)
    parser.add_argument("--execution-family", default="alpaca_spot")
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument(
        "--no-broker",
        action="store_true",
        help="Skip the broker read. WARNING: cannot see sessions the broker filled "
             "that left no DB trace — the exact class this check exists to find.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the full result as JSON")
    args = parser.parse_args()

    try:
        from app.db import SessionLocal
        from app.services.trading.momentum_neural.ledger_integrity import (
            check_live_ledger_integrity,
        )
    except Exception as exc:  # pragma: no cover - harness only
        print(f"import failed: {exc}", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        result = check_live_ledger_integrity(
            db,
            days=args.days,
            execution_family=args.execution_family,
            user_id=args.user_id,
            include_broker=not args.no_broker,
        )
    finally:
        db.rollback()
        db.close()

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1

    print(f"ledger integrity: {'OK' if result['ok'] else 'FAILED'}")
    print(f"  coverage          {result['coverage']}")
    if result.get("broker_read_error"):
        print(f"  broker read error {result['broker_read_error']}")
    print(f"  window            {result['window_start_utc']} .. {result['window_end_utc']}")
    print(f"  sessions scanned  {result['sessions_scanned']} (traded {result['sessions_traded']})")
    print(f"  broker P&L        {result['broker_realized_pnl_usd']:>12.2f}")
    print(f"  booked P&L        {result['booked_pnl_usd']:>12.2f}")
    print(f"  UNBOOKED          {result['unbooked_pnl_usd']:>12.2f}")
    print("  source coverage   " + ", ".join(
        f"{k}={v}" for k, v in (result.get("source_coverage") or {}).items()
    ))
    print("  counts            " + ", ".join(
        f"{k}={v}" for k, v in sorted((result.get("counts") or {}).items())
    ))
    violations = result.get("violations") or []
    if violations:
        print(f"\n  {len(violations)} VIOLATION(S):")
        print(f"    {'session':>8} {'sym':<6} {'state':<18} {'broker':>10} {'booked':>10} {'delta':>10}  status")
        for row in violations:
            def _n(v):
                return f"{v:10.2f}" if isinstance(v, (int, float)) else f"{'-':>10}"
            print(
                f"    {row['session_id']:>8} {str(row['symbol'] or '')[:6]:<6} "
                f"{str(row['terminal_state'] or '')[:18]:<18} "
                f"{_n(row['broker_realized_pnl_usd'])} {_n(row['booked_pnl_usd'])} "
                f"{_n(row['delta_usd'])}  {row['status']}"
            )
    orphans = result.get("unattributed_broker_episodes") or []
    if orphans:
        print(f"\n  {len(orphans)} broker episode(s) belonging to NO session:")
        for ep in orphans:
            print(f"    {ep.get('symbol')} pnl={ep.get('realized_pnl_usd')} closed={ep.get('closed')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
