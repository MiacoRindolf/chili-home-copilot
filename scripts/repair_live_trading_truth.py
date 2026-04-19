from __future__ import annotations

import argparse
import json

from app.db import SessionLocal
from app.services.trading.backtest_provenance import repair_backtest_provenance
from app.services.trading.broker_account_repair import repair_broker_account_truth


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair canonical broker ownership and backtest provenance.")
    parser.add_argument("--apply", action="store_true", help="Write changes instead of previewing.")
    parser.add_argument(
        "--evaluate-open-positions",
        action="store_true",
        help="After broker repair, run existing AI Evaluate All logic for the canonical open book.",
    )
    parser.add_argument(
        "--repair-backtests",
        action="store_true",
        help="Also normalize historical backtest provenance metadata.",
    )
    parser.add_argument(
        "--canonical-user-id",
        type=int,
        default=None,
        help="Explicit canonical user id for the broker repair.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        out = {
            "broker_repair": repair_broker_account_truth(
                db,
                broker="robinhood",
                canonical_user_id=args.canonical_user_id,
                preview_only=not args.apply,
                evaluate_open_positions=bool(args.apply and args.evaluate_open_positions),
            )
        }
        if args.repair_backtests:
            out["backtest_repair"] = repair_backtest_provenance(
                db,
                apply=bool(args.apply),
            )
        print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
