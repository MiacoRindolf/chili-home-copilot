"""One-shot backtest backfill of the pattern x regime ledger."""
from __future__ import annotations
from app.db import SessionLocal
from app.services.trading.pattern_regime_ledger import build_ledger_from_backtests


def main() -> int:
    sess = SessionLocal()
    try:
        out = build_ledger_from_backtests(sess, window_days=365, dry_run=False)
        print(f"result: {out}")
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
