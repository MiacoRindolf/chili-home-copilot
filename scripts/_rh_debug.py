"""Probe RH account/positions to find why SELL is rejected as 'Not enough shares'."""
import json
import robin_stocks.robinhood as rh
from app.services.broker_service import is_connected

print("connected:", is_connected())

print("\n--- all accounts ---")
try:
    accounts = rh.account.load_account_profile(info=None, dataType="results")
    if accounts is None:
        accounts = rh.account.load_account_profile()
    print(json.dumps(accounts, indent=2, default=str)[:4000])
except Exception as e:
    print("err:", e)

print("\n--- get_all_positions (raw, includes held_for_sell) ---")
try:
    positions = rh.account.get_all_positions()
    for p in positions:
        qty = float(p.get("quantity") or 0)
        if qty == 0:
            continue
        sym = rh.stocks.get_symbol_by_url(p.get("instrument"))
        print(f"  {sym:8s} qty={qty} held_for_sell={p.get('shares_held_for_sells')} "
              f"held_for_buy={p.get('shares_held_for_buys')} "
              f"held_for_stock_grants={p.get('shares_held_for_stock_grants')} "
              f"pending_avg_cost={p.get('pending_average_buy_price')} "
              f"account={p.get('account')!s}")
except Exception as e:
    print("err:", e)

print("\n--- open stock orders (working) ---")
try:
    open_orders = rh.orders.get_all_open_stock_orders()
    for o in open_orders[:30]:
        inst_url = o.get("instrument")
        try:
            sym = rh.stocks.get_symbol_by_url(inst_url)
        except Exception:
            sym = "?"
        print(f"  {o.get('created_at')} {sym:8s} side={o.get('side')} qty={o.get('quantity')} "
              f"state={o.get('state')} type={o.get('type')} id={o.get('id')}")
except Exception as e:
    print("err:", e)

print("\n--- Try a DRY-RUN like sell for DHC (we will NOT actually place) ---")
# Inspect the internal call routing robin_stocks uses
import inspect
print("rh.orders.order signature:", inspect.signature(rh.orders.order))
