"""Spread + depth from already-cached book samples for the touched/exotic pairs
(books.json has all 99 eval pairs even before candles finish)."""
import json
import statistics
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "_cx_cache"
books = json.load(open(CACHE / "books.json"))["samples"]
stats = json.load(open(CACHE / "stats_all.json"))["stats"]
pairs = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "AVAX-USD", "LINK-USD",
         "INX-USD", "ROBO-USD", "XPL-USD", "PRL-USD", "KARRAT-USD", "FIDA-USD",
         "GWEI-USD", "STG-USD", "MOG-USD", "OSMO-USD", "LAYER-USD", "PYTH-USD",
         "CRV-USD", "CVX-USD", "FLR-USD", "DRIFT-USD", "ORCA-USD", "INJ-USD",
         "ONDO-USD", "EIGEN-USD", "JTO-USD", "BAT-USD", "ZRX-USD"]


def dv24(pid):
    d = (stats.get(pid) or {}).get("data") or {}
    try:
        return float(d.get("volume") or 0) * float(d.get("last") or 0)
    except (TypeError, ValueError):
        return 0.0


print("%-12s %8s %8s %12s %12s %10s %5s" % (
    "pair", "sp_med", "sp_p90", "biddep50bps", "askdep50bps", "dv24h_M", "n"))
for pid in pairs:
    spreads, bd, ad = [], [], []
    for s in books.get(pid, []):
        if s.get("status") != 200 or not s.get("bids") or not s.get("asks"):
            continue
        try:
            bb, ba = float(s["bids"][0][0]), float(s["asks"][0][0])
        except (TypeError, ValueError, IndexError):
            continue
        if bb <= 0 or ba <= 0 or ba < bb:
            continue
        mid = (bb + ba) / 2
        spreads.append((ba - bb) / mid * 1e4)
        lo, hi = mid * 0.995, mid * 1.005
        bd.append(sum(float(px) * float(sz) for px, sz, *_ in s["bids"] if float(px) >= lo))
        ad.append(sum(float(px) * float(sz) for px, sz, *_ in s["asks"] if float(px) <= hi))
    if not spreads:
        print("%-12s %8s" % (pid, "NO_BOOK"))
        continue
    spreads.sort()
    print("%-12s %8.1f %8.1f %12.0f %12.0f %10.1f %5d" % (
        pid, statistics.median(spreads),
        spreads[min(len(spreads) - 1, int(len(spreads) * 0.9))],
        statistics.median(bd), statistics.median(ad), dv24(pid) / 1e6, len(spreads)))
