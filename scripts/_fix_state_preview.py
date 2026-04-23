"""Print the exact current state and show the rows we'd patch.

No writes; pure read-only preview.
"""
import os
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from app.db import SessionLocal

db = SessionLocal()

def row(q, **p):
    r = db.execute(text(q), p).mappings().all()
    for x in r:
        print("  ", dict(x))

print("=== 1. Zombie RKLX #375 ===")
row("""
    SELECT id, ticker, status, quantity, entry_price,
           exit_price, exit_date, exit_reason, pnl,
           remaining_quantity, broker_status, broker_order_id,
           pending_exit_status, pending_exit_reason, pending_exit_order_id,
           entry_date, user_id, broker_source
    FROM trading_trades WHERE id=375
""")

print("\n=== 2. Bad ETH-USD #370 ===")
row("""
    SELECT id, ticker, status, quantity, entry_price, exit_price, exit_date,
           broker_status, broker_order_id, pending_exit_status, entry_date,
           user_id, broker_source
    FROM trading_trades WHERE id=370
""")

print("\n=== 3. Phantom AAON #369 ===")
row("""
    SELECT id, ticker, status, quantity, entry_price, exit_price, exit_date,
           exit_reason, pnl, broker_status, broker_order_id,
           pending_exit_status, pending_exit_order_id, stop_loss, take_profit,
           entry_date, user_id, broker_source
    FROM trading_trades WHERE id=369
""")

print("\n=== 4. Stale pending_exit on closed 311 (AIXI), 285 (AIFF) ===")
row("""
    SELECT id, ticker, status, exit_price, exit_date, pnl,
           pending_exit_status, pending_exit_reason, pending_exit_order_id,
           pending_exit_requested_at
    FROM trading_trades WHERE id IN (311, 285)
    ORDER BY id
""")

print("\n=== 5. Open DB trades by ticker (user 1, robinhood, status=open) ===")
row("""
    SELECT id, ticker, quantity, remaining_quantity, entry_price, stop_loss,
           take_profit, pending_exit_status, pending_exit_reason, status
    FROM trading_trades
    WHERE status='open' AND user_id=1 AND COALESCE(broker_source,'robinhood')='robinhood'
      AND ticker NOT LIKE '%-USD'
    ORDER BY ticker
""")

db.close()
