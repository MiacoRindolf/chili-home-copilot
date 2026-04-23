"""Apply the reconciliation data fixes.

Writes:
  * RKLX  #375 -> status='closed' (zombie; exit data already correct)
  * ETH-USD #370 -> status='cancelled' (never filled, bad entry)
  * AAON  #369 -> status='closed', exit_price=96.32, exit_date=2026-04-22 08:00:17Z,
                  pnl=-3.48, exit_reason='broker_reconcile_external_sell'
  * AIFF  #285 -> pending_exit_status=NULL, pending_exit_reason=NULL
  * AIXI  #311 -> pending_exit_status=NULL, pending_exit_reason=NULL

Dry-run by default; pass --apply to commit.
"""
import sys
import datetime as _dt
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from app.db import SessionLocal

APPLY = "--apply" in sys.argv

PATCHES: list[tuple[str, str, dict]] = [
    (
        "RKLX #375 zombie -> closed",
        """
        UPDATE trading_trades
        SET status = 'closed'
        WHERE id = 375 AND status = 'open' AND exit_price IS NOT NULL
        """,
        {},
    ),
    (
        "ETH-USD #370 bad fill -> cancelled",
        """
        UPDATE trading_trades
        SET status = 'cancelled',
            exit_reason = COALESCE(exit_reason, 'bad_entry_price_0')
        WHERE id = 370 AND status = 'open' AND entry_price = 0
        """,
        {},
    ),
    (
        "AAON #369 phantom -> closed (external sell filled 04-22 08:00 UTC)",
        """
        UPDATE trading_trades
        SET status = 'closed',
            exit_price = 96.32,
            exit_date = :exit_date,
            pnl = -3.48,
            exit_reason = 'broker_reconcile_external_sell',
            pending_exit_status = NULL,
            pending_exit_reason = NULL
        WHERE id = 369 AND status = 'open'
        """,
        {"exit_date": _dt.datetime(2026, 4, 22, 8, 0, 17)},
    ),
    (
        "AIFF #285 clear stale pending_exit_status",
        """
        UPDATE trading_trades
        SET pending_exit_status = NULL,
            pending_exit_reason = NULL,
            pending_exit_order_id = NULL,
            pending_exit_requested_at = NULL
        WHERE id = 285 AND status = 'closed'
        """,
        {},
    ),
    (
        "AIXI #311 clear stale pending_exit_status",
        """
        UPDATE trading_trades
        SET pending_exit_status = NULL,
            pending_exit_reason = NULL,
            pending_exit_order_id = NULL,
            pending_exit_requested_at = NULL
        WHERE id = 311 AND status = 'closed'
        """,
        {},
    ),
]

db = SessionLocal()
try:
    print(f"Mode: {'APPLY' if APPLY else 'DRY RUN (add --apply to commit)'}")
    print()
    for label, sql, params in PATCHES:
        res = db.execute(text(sql), params)
        rc = res.rowcount if res.rowcount is not None else 0
        print(f"  [{'WRITE' if APPLY else 'would write'}] {label}: rows={rc}")
    if APPLY:
        db.commit()
        print("\nCommitted.")
    else:
        db.rollback()
        print("\nRolled back (dry run). Re-run with --apply to commit.")
finally:
    db.close()
