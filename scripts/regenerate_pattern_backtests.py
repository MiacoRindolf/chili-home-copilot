#!/usr/bin/env python3
"""Run queue-style backtests for explicit scan_pattern ids (repopulate evidence after repair/deletes).

Uses ``execute_queue_backtest_for_pattern`` (same as brain worker): creates/links a
TradingInsight if needed, runs ``smart_backtest_insight``, updates queue markers.

**Costs:** market-data API usage and time. Prefer off-peak batches.

Usage (repo root, conda env chili-env):
  python scripts/regenerate_pattern_backtests.py --pattern-ids 12,34 --dry-run
  python scripts/regenerate_pattern_backtests.py --pattern-ids 12,34 --user-id 1
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--pattern-ids",
        type=str,
        required=True,
        help="Comma-separated scan_patterns.id values",
    )
    p.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Optional user id for learning_event attribution (worker often uses None)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print ids only; do not run backtests",
    )
    args = p.parse_args()

    raw = [x.strip() for x in args.pattern_ids.split(",") if x.strip()]
    ids: list[int] = []
    for x in raw:
        try:
            ids.append(int(x))
        except ValueError:
            print(f"Invalid pattern id (not int): {x!r}", file=sys.stderr)
            return 2

    if not ids:
        print("No pattern ids parsed.", file=sys.stderr)
        return 2

    if args.dry_run:
        for pid in ids:
            print(f"dry-run: would execute_queue_backtest_for_pattern({pid}, user_id={args.user_id!r})")
        return 0

    from app.services.trading.backtest_queue_worker import execute_queue_backtest_for_pattern

    for pid in ids:
        ran, _proc = execute_queue_backtest_for_pattern(pid, args.user_id)
        print(f"pattern_id={pid} backtests_run≈{ran}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
