"""One-shot realized stats sync."""
from __future__ import annotations
from app.db import SessionLocal
from app.services.trading.realized_stats_sync import sync_realized_stats


def main() -> int:
    sess = SessionLocal()
    try:
        result = sync_realized_stats(sess, dry_run=False)
        print(f"updated={result['updated']}  skipped={result['skipped']}  no_trades={result['no_trades']}")
    finally:
        # FIX 46 pattern (rollback before close).
        try:
            sess.rollback()
        except Exception:
            pass
        sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
