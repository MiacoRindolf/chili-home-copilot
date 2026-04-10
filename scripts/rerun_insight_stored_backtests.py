#!/usr/bin/env python3
"""Re-run every Pattern Evidence–listed stored backtest for a TradingInsight (foreground).

Same logic as Brain **Rerun all listed BTs** but blocks until finished (good for automation).

Usage (repo root, conda env chili-env):
  python scripts/rerun_insight_stored_backtests.py 42
  python scripts/rerun_insight_stored_backtests.py 42 --limit 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.trading.stored_backtest_rerun import (  # noqa: E402
    run_insight_stored_backtests_rerun_job,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("insight_id", type=int, help="TradingInsight.id (Brain card id)")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max rows to rerun (default: all listed)",
    )
    args = p.parse_args()
    run_insight_stored_backtests_rerun_job(int(args.insight_id), limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
