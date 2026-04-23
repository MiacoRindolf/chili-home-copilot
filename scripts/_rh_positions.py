"""Dump Robinhood positions from inside the running container's client cache.

We run this via `docker exec` against scheduler-worker so we use the same
authenticated robin-stocks session the monitor uses.
"""
from app.services.broker_service import get_positions, is_connected

print("connected:", is_connected())
positions = get_positions() or []
print(f"positions: {len(positions)}")
watchlist = {"RKLX", "DHC", "GENI", "ACHC", "PFSI", "JOB", "GEO", "INTC",
             "EKSO", "ABM", "AAON", "ACMR", "VFS", "HAFN", "AIDX", "CRDL",
             "ACHR", "AMUU", "ELTX", "IMTX", "SOFX", "TLS", "PED", "MRNA",
             "CCCC", "ETH-USD"}
found = {}
for p in positions:
    tkr = str(p.get("ticker") or "").upper()
    found[tkr] = p
    if tkr in watchlist:
        print(f"  HOLD {tkr:8s} qty={p.get('quantity')} "
              f"avg={p.get('average_buy_price')} equity={p.get('equity')} "
              f"px={p.get('current_price')}")

print("\n--- Tickers in DB but NOT held in RH ---")
for tkr in sorted(watchlist):
    if tkr not in found:
        print(f"  MISSING {tkr}")

print("\n--- Tickers held in RH but NOT in our watchlist ---")
for tkr, p in found.items():
    if tkr not in watchlist:
        print(f"  EXTRA {tkr:8s} qty={p.get('quantity')} "
              f"avg={p.get('average_buy_price')} px={p.get('current_price')}")
