"""Inspect the recorded print window used by entry gates at UTC decision instants.

Reads PostgreSQL through the production helper, including receive/publication
eligibility BEFORE latest-N selection and its exact settings. The publication
marker is not exact commit/consumer visibility; this is a reconstruction, not a
sealed ReplayV3 result or a claim about fillable P&L. No IQFeed lookup/backfill.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def probe(db, symbol, at, *, prints=None, window_s=None):
    """Use the same selector and computation as production, without a second parser."""
    from app.services.trading.momentum_neural.entry_gates import signed_tape_accel_features
    kwargs = {"window_s": window_s} if window_s is not None else {"window_prints": prints}
    return signed_tape_accel_features(symbol, db=db, as_of=at, **kwargs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--date", required=True, help="UTC date YYYY-MM-DD")
    ap.add_argument("--at", action="append", required=True, help="UTC HH:MM:SS")
    ap.add_argument("--prints", type=int, default=None)
    ap.add_argument("--window-s", type=float, default=None, help="Explicit legacy seconds comparison")
    args = ap.parse_args()
    if args.prints is not None and args.prints < 4:
        ap.error("--prints requires at least 4 prints")
    if args.window_s is not None and args.window_s <= 0:
        ap.error("--window-s must be positive")
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from app.config import settings
    url = os.environ.get("DATABASE_URL") or settings.database_url
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            # Read-only enforced by PostgreSQL, not only by this tool's intent.
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout='20s'"))
            with Session(bind=conn) as db:
                for clock in args.at:
                    at = datetime.fromisoformat(args.date + "T" + clock).replace(tzinfo=timezone.utc)
                    feature = probe(db, args.symbol, at, prints=args.prints, window_s=args.window_s)
                    print(json.dumps({"symbol": args.symbol.upper(), "as_of": at.isoformat(),
                                      "feature": feature, "status": "readable" if feature else "unreadable",
                                      "visibility": "recorded_publication_reconstruction"},
                                     sort_keys=True, default=str))
            conn.rollback()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
