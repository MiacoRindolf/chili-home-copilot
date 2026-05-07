"""One-shot run of the ticker_scope autotune.

Run from inside the chili container::

    cd /app && PYTHONPATH=/app python /workspace/scripts/_run_ticker_autotune.py

Prints per-pattern decisions and any committed changes.
"""
from __future__ import annotations
from app.db import SessionLocal
from app.services.trading.ticker_scope_autotune import run_autotune


def main() -> int:
    sess = SessionLocal()
    try:
        actions = run_autotune(sess, dry_run=False)
        if not actions:
            print("no eligible patterns (or autotune disabled)")
            return 0
        print(f"actions={len(actions)}")
        for a in actions:
            payload = a.to_payload()
            print(f"  decision={payload['decision']:>20}  pattern_id={payload['pattern_id']}  "
                  f"name={payload['pattern_name'][:50]}  net_pnl={payload['net_pnl']:.2f}")
            print(f"    edge_tickers={payload['edge_tickers']}")
            print(f"    bleed_tickers={payload['bleed_tickers']}")
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
