"""Post-hoc checks on the burst log: per-day concentration, per-symbol skew,
and cost-adjusted expectancy of the +2%/-1% follow-through bet."""
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "_cx_cache"
log = json.loads((CACHE / "cx_clock_burst_log.json").read_text())
MAJORS = {"BTC-USD", "SOL-USD", "DOGE-USD"}
alts = [e for e in log if e["prod"] not in MAJORS]

by_day = defaultdict(lambda: [0, 0, 0])  # bursts, ftW, ftL
by_sym = defaultdict(lambda: [0, 0, 0])
for e in alts:
    d = datetime.fromtimestamp(e["ts"], tz=timezone.utc)
    k = d.strftime("%Y-%m-%d %a")
    by_day[k][0] += 1
    by_sym[e["prod"]][0] += 1
    if e["outcome"] == "win":
        by_day[k][1] += 1; by_sym[e["prod"]][1] += 1
    elif e["outcome"] == "loss":
        by_day[k][2] += 1; by_sym[e["prod"]][2] += 1

print("=== alt bursts by day ===")
for k in sorted(by_day):
    b, w, l = by_day[k]
    ft = f"{100*w/(w+l):.0f}%" if w + l else "-"
    print(f"  {k}: {b:>4} bursts  ft {ft} ({w}/{w+l})")

print("\n=== alt bursts by symbol ===")
for k, (b, w, l) in sorted(by_sym.items(), key=lambda x: -x[1][0]):
    ft = f"{100*w/(w+l):.0f}%" if w + l else "-"
    print(f"  {k:14} {b:>4} bursts  ft {ft} ({w}/{w+l})")

# cost-adjusted expectancy of the +2/-1 chase at observed ft rates
print("\n=== breakeven ft% under cost scenarios (target +2%, stop -1%) ===")
for name, rt in (("real taker RT (measured 1.53%)", 1.53),
                 ("assumed taker RT (config 1.20%)", 1.20),
                 ("real maker RT (measured 0.82%)", 0.82),
                 ("alpaca-style 0.50% RT", 0.50),
                 ("zero fee", 0.0)):
    win = 2.0 - rt
    loss = -1.0 - rt
    be = -loss / (win - loss) * 100 if win > 0 else float("inf")
    print(f"  {name:34} net win {win:+.2f}%  net loss {loss:+.2f}%  breakeven ft {be:.0f}%")
