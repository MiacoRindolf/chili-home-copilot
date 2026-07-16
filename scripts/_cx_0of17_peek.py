"""Quick peek: real commissions vs DB-recorded zero, per product/side."""
import json
import pathlib
from collections import defaultdict

CACHE = pathlib.Path(__file__).resolve().parent / "_cx_cache"
fills = json.loads((CACHE / "cx_0of17_broker_fills.json").read_text())

tot_comm = 0.0
tot_notional = 0.0
liq = defaultdict(int)
by_order = defaultdict(lambda: {"notional": 0.0, "comm": 0.0, "n": 0})
for f in fills:
    sz = float(f["size"] or 0)
    px = float(f["price"] or 0)
    q = float(f["size_in_quote"] or 0) if isinstance(f["size_in_quote"], (int, float)) else 0
    notional = sz if f.get("size_in_quote") in (True, "true") else sz * px
    comm = float(f["commission"] or 0)
    tot_comm += comm
    tot_notional += notional
    liq[f["liquidity_indicator"]] += 1
    o = by_order[(f["product_id"], f["order_id"], f["side"])]
    o["notional"] += notional
    o["comm"] += comm
    o["n"] += 1

print(f"fills={len(fills)} notional=${tot_notional:,.2f} commission=${tot_comm:,.2f} "
      f"avg_fee_bps={10000*tot_comm/max(tot_notional,1e-9):.1f}")
print("liquidity:", dict(liq))
print()
for (p, oid, side), v in sorted(by_order.items(), key=lambda kv: kv[0][0]):
    bps = 10000 * v["comm"] / max(v["notional"], 1e-9)
    print(f"{p:11s} {side:4s} ${v['notional']:9.2f} fee=${v['comm']:7.4f} ({bps:5.1f}bps) n={v['n']} {oid[:8]}")
