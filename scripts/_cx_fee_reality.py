"""What fee does CHILI actually pay on Coinbase? From cached real fills
(scripts/_cx_cache/cb_fills.json, fetched from the Coinbase Advanced Trade
fills API by a prior pass) -- commission / notional per fill."""
import json
from collections import defaultdict
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "_cx_cache"
fills = json.loads((CACHE / "cb_fills.json").read_text())

rows = []
for f in fills:
    px = float(f["price"]); sz = float(f["size"])
    notional = px * sz if not f.get("size_in_quote") else sz
    comm = float(f.get("commission") or 0)
    rows.append({
        "t": f["trade_time"][:19], "product": f["product_id"], "side": f["side"],
        "liq": f.get("liquidity_indicator", "?"), "src": f.get("fillSource", "?"),
        "notional": notional, "comm": comm,
        "rate": comm / notional if notional else None,
    })

print(f"total fills: {len(rows)}, span {min(r['t'] for r in rows)} .. {max(r['t'] for r in rows)}")
by = defaultdict(lambda: {"n": 0, "noti": 0.0, "comm": 0.0})
for r in rows:
    k = (r["liq"], r["src"])
    by[k]["n"] += 1; by[k]["noti"] += r["notional"]; by[k]["comm"] += r["comm"]

print(f"{'liquidity':10} {'source':18} {'n':>4} {'notional':>12} {'fee$':>10} {'fee%/side':>9}")
for k, v in sorted(by.items(), key=lambda x: -x[1]["noti"]):
    print(f"{k[0]:10} {k[1]:18} {v['n']:>4} {v['noti']:>12.2f} {v['comm']:>10.4f} {100*v['comm']/v['noti']:>8.3f}%")

tot_n = sum(v["noti"] for v in by.values()); tot_c = sum(v["comm"] for v in by.values())
print(f"\nALL: n={len(rows)} notional=${tot_n:,.0f} fees=${tot_c:,.2f} -> {100*tot_c/tot_n:.3f}%/side "
      f"(~{200*tot_c/tot_n:.2f}% round trip)")

# rate distribution
rates = sorted(r["rate"] for r in rows if r["rate"] is not None)
import statistics
print(f"per-fill fee rate: min {100*rates[0]:.3f}%  median {100*statistics.median(rates):.3f}%  max {100*rates[-1]:.3f}%")
