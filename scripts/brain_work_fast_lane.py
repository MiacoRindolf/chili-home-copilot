"""Fast-lane dispatcher for lightweight brain_work_events handlers.

This process exists to keep promotion/evidence work moving even when the
main brain-worker is busy in mining, backtesting, or market-data fetches.
It deliberately skips the heavy producers:

* backtest_requested
* market_snapshots_batch / mine
* dispatch market-snapshot watchdog

The main brain-worker still owns those. This worker drains CPCV,
promotion, and trade-close evidence rows.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.db import SessionLocal
from app.services.trading.brain_work.dispatcher import run_brain_work_dispatch_round


LOG = logging.getLogger("brain_work_fast_lane")


def _json_default(value: Any) -> str:
    return str(value)


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    db = SessionLocal()
    try:
        return run_brain_work_dispatch_round(
            db,
            user_id=getattr(settings, "brain_default_user_id", None),
            max_backtest=0,
            max_exec_feedback=0,
            max_mine=0,
            max_cpcv_gate=args.max_cpcv_gate,
            max_promote=args.max_promote,
            max_trade_close=args.max_trade_close,
            run_thin_evidence_sweep=False,
            run_market_snapshots_watchdog=False,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-cpcv-gate", type=int, default=8)
    parser.add_argument("--max-promote", type=int, default=8)
    parser.add_argument("--max-trade-close", type=int, default=16)
    parser.add_argument(
        "--log-idle",
        action="store_true",
        help="Log rounds even when no work was claimed.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )

    LOG.info(
        "starting interval=%ss once=%s cpcv=%s promote=%s trade_close=%s",
        args.interval,
        args.once,
        args.max_cpcv_gate,
        args.max_promote,
        args.max_trade_close,
    )

    while True:
        try:
            summary = run_once(args)
            claimed = int(summary.get("claimed") or 0)
            processed = int(summary.get("processed") or 0)
            errors = summary.get("errors") or []
            if claimed or processed or errors or args.log_idle:
                LOG.info(
                    "round claimed=%s processed=%s per_type=%s errors=%s",
                    claimed,
                    processed,
                    json.dumps(summary.get("per_type") or {}, default=_json_default),
                    json.dumps(errors, default=_json_default),
                )
        except Exception:
            LOG.exception("round failed")
            if args.once:
                return 1

        if args.once:
            return 0

        time.sleep(max(1.0, float(args.interval)))


if __name__ == "__main__":
    sys.exit(main())
