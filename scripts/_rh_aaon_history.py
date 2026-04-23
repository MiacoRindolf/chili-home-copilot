"""Find AAON sell history on RH so we know what happened to the position."""
import robin_stocks.robinhood as rh
from datetime import datetime, timezone, timedelta
from app.services.broker_service import is_connected

assert is_connected(), "RH not connected"
since = datetime.now(timezone.utc) - timedelta(days=10)

orders = rh.orders.get_all_stock_orders()
for o in orders:
    try:
        sym = rh.stocks.get_symbol_by_url(o.get("instrument") or "")
    except Exception:
        continue
    if sym != "AAON":
        continue
    print(f"  {o.get('created_at')} {sym} side={o.get('side')} qty={o.get('quantity')} "
          f"state={o.get('state')} type={o.get('type')} "
          f"avg_px={o.get('average_price')} id={o.get('id')}")
