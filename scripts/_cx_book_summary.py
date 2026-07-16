import json
import statistics
from pathlib import Path

C = Path(__file__).resolve().parent / "_cx_cache"
for fn in sorted(C.glob("book_snapshot_*.json")):
    j = json.loads(fn.read_text())
    alts = {k: v for k, v in j["books"].items()
            if k not in ("BTC-USD", "SOL-USD", "DOGE-USD") and "spread_bps" in v}
    spr = sorted(v["spread_bps"] for v in alts.values())
    depth = sorted(min(v["bid_depth_usd"], v["ask_depth_usd"]) for v in alts.values())
    print(f"{fn.name}: alts n={len(spr)}  spread med {statistics.median(spr):.0f} bps  "
          f"p75 {spr[int(0.75 * len(spr))]:.0f}  | min-side L1 depth med ${statistics.median(depth):.0f}  "
          f"p25 ${depth[int(0.25 * len(depth))]:.0f}")
