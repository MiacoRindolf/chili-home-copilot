import json, os
import pandas as pd
from app.services.trading.momentum_neural.ross_momentum import front_side_state

j = json.load(open(os.path.join(os.environ.get("CHILI_REPLAY_RESULTS_DIR", "."), "2026-06-22.json")))
series = j.get("series", {})
trades = {t["sym"]: t for t in j.get("trades", [])}
for sym in ("QXL", "COHH", "NVCT", "UNCY"):
    t = trades.get(sym)
    rows = series.get(sym) or []
    if not t or not rows:
        print(sym, "no trade/series"); continue
    entry_t = t["t"]  # hhmm
    upto = [r for r in rows if r[0] <= entry_t]   # bars up to entry (same day -> no lookahead)
    if len(upto) < 5:
        print(sym, "thin", len(upto)); continue
    df = pd.DataFrame({"Open": [r[1] for r in upto], "High": [r[2] for r in upto],
                       "Low": [r[3] for r in upto], "Close": [r[4] for r in upto],
                       "Volume": [r[5] for r in upto]})
    fs = front_side_state(df)
    print("%-5s entry=%s usd=%+d run_r=%s | is_backside=%s reason=%-14s day_range_pos=%.3f vwap_dist_sigma=%s above_vwap=%s score=%.3f" % (
        sym, entry_t, t["usd"], t.get("run_r"), fs.is_backside, fs.reason,
        fs.day_range_pos or 0, fs.vwap_dist_sigma, fs.above_vwap, fs.front_side_score))
