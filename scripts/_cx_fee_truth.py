"""Realized Coinbase fee/liquidity truth from broker-side fills (cb_fills.json,
fetched by the broker-truth ledger session 2026-06-13). Quantifies what the
crypto lane ACTUALLY pays per side, split by maker/taker and by side."""
import json
from collections import defaultdict
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "_cx_cache"
fills = json.load(open(CACHE / "cb_fills.json"))

agg = defaultdict(lambda: {"n": 0, "value": 0.0, "fees": 0.0})
prod = defaultdict(lambda: {"n": 0, "value": 0.0, "fees": 0.0})
months = defaultdict(lambda: {"n": 0, "value": 0.0, "fees": 0.0})
for f in fills:
    try:
        px = float(f["price"]); sz = float(f["size"])
        val = px * sz if not f.get("size_in_quote") else sz
        fee = float(f.get("commission") or 0.0)
    except (TypeError, ValueError, KeyError):
        continue
    li = f.get("liquidity_indicator") or "?"
    side = f.get("side") or "?"
    for k in (f"{li}", f"{li}/{side}"):
        agg[k]["n"] += 1; agg[k]["value"] += val; agg[k]["fees"] += fee
    p = prod[f.get("product_id") or "?"]
    p["n"] += 1; p["value"] += val; p["fees"] += fee
    m = months[(f.get("trade_time") or "")[:10]]
    m["n"] += 1; m["value"] += val; m["fees"] += fee

print("=== by liquidity/side ===")
for k in sorted(agg):
    a = agg[k]
    print(f"{k:14s} n={a['n']:4d} value=${a['value']:10.2f} fees=${a['fees']:8.2f} "
          f"rate={a['fees']/a['value']*100 if a['value'] else 0:.3f}%")
print("\n=== by product (n>=6) ===")
for k in sorted(prod, key=lambda x: -prod[x]["value"]):
    a = prod[k]
    if a["n"] >= 6:
        print(f"{k:14s} n={a['n']:4d} value=${a['value']:10.2f} "
              f"rate={a['fees']/a['value']*100 if a['value'] else 0:.3f}%")
print("\n=== by day (last 12) ===")
for k in sorted(months)[-12:]:
    a = months[k]
    print(f"{k} n={a['n']:4d} value=${a['value']:10.2f} "
          f"rate={a['fees']/a['value']*100 if a['value'] else 0:.3f}%")
