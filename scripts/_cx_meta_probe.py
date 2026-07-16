import json
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "_cx_cache"
m = json.load(open(CACHE / "products.json"))
ids = {p["id"]: p for p in m["products"]}
pairs = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "AVAX-USD",
         "LINK-USD", "INX-USD", "ROBO-USD", "XPL-USD", "PRL-USD", "KARRAT-USD",
         "FIDA-USD", "GWEI-USD", "STG-USD", "MOG-USD", "OSMO-USD", "LAYER-USD",
         "PYTH-USD", "CRV-USD", "CVX-USD", "FLR-USD", "DRIFT-USD", "ORCA-USD", "INJ-USD"]
hdr = ("pair", "status", "postO", "limO", "disab", "base_min", "min_mkt_funds", "quote_inc")
print("%-13s %-7s %-6s %-6s %-6s %-14s %-13s %-12s" % hdr)
for s in pairs:
    p = ids.get(s, {})
    print("%-13s %-7s %-6s %-6s %-6s %-14s %-13s %-12s" % (
        s, p.get("status", "ABSENT"), str(p.get("post_only", "")),
        str(p.get("limit_only", "")), str(p.get("trading_disabled", "")),
        str(p.get("base_min_size", "")), str(p.get("min_market_funds", "")),
        str(p.get("quote_increment", ""))))
