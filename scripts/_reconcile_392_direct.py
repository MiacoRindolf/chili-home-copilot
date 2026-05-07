"""Reconcile trade 392 with broker truth (already-known data).

Broker order 69f0dc6c-f181-4402-9971-bacbc48d6332:
  state=filled, side=sell, position_effect=close, fill_price=$2.44, qty=1.

Trade 392:
  entry_price=$4.01, qty=1.0
  pnl = (2.44 - 4.01) * 100 * 1 = -$157.00 (per-contract premium * 100 multiplier)

Idempotent: only writes if status != closed.
"""
from app.db import SessionLocal
from app.models.trading import Trade

SELL_ORDER_ID = "69f0dc6c-f181-4402-9971-bacbc48d6332"
FILL_PRICE = 2.44


def main() -> int:
    sess = SessionLocal()
    try:
        t = sess.get(Trade, 392)
        if not t:
            print("NOT FOUND")
            return 1
        print(f"BEFORE  status={t.status}  pending_exit={t.pending_exit_status}  "
              f"exit_price={t.exit_price}  pnl={t.pnl}  exit_reason={t.exit_reason}")
        if t.status == "closed":
            print("NO-OP: already closed")
            return 0
        entry_px = float(t.entry_price or 0)
        qty = float(t.quantity or 1)
        pnl = (FILL_PRICE - entry_px) * 100.0 * qty
        t.exit_price = FILL_PRICE
        t.pnl = pnl
        t.exit_reason = "manual_close"
        t.status = "closed"
        t.pending_exit_status = "filled"
        t.broker_status = "filled"
        sess.commit()
        print(f"AFTER   status={t.status}  exit_price={t.exit_price}  pnl={t.pnl}  exit_reason={t.exit_reason}")
        return 0
    finally:
        # FIX 46 pattern (rollback before close).
        try:
            sess.rollback()
        except Exception:
            pass
        sess.close()


if __name__ == "__main__":
    raise SystemExit(main())
