import argparse
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.trading.momentum_neural.ross_feed_health import (
    FeedHealth,
    evaluate_feed_health,
    check_feed_health as _check_feed_health,
)


def check_feed_health(*, max_iqfeed_age_hot_s: float = 60.0) -> FeedHealth:
    db = SessionLocal()
    try:
        return _check_feed_health(db, max_iqfeed_age_hot_s=max_iqfeed_age_hot_s)
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Ross lane market-data feed health.")
    parser.add_argument("--max-iqfeed-age-hot-s", type=float, default=60.0)
    args = parser.parse_args(argv)
    health = check_feed_health(max_iqfeed_age_hot_s=args.max_iqfeed_age_hot_s)
    print(health.reason)
    for key, value in health.details.items():
        print(f"{key}={value}")
    return 0 if health.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
