"""B7: robustness slices over b7_results.json — symbol concentration per gate,
overall take-all aggregate, and per-gate verdicts excluding the dominant symbol."""
import json
import statistics as st
from collections import Counter, defaultdict

R = json.load(open("scripts/_cx_cache/b7_results.json"))
cov = [r for r in R if r["covered"]]
print(f"covered {len(cov)}/{len(R)}")

meanR = st.mean(r["r_units"] for r in cov)
print(f"\nALL blocked episodes taken: meanR={meanR:+.3f} gross, {meanR-0.6:+.3f} net taker | "
      f"r60 mean {st.mean(r['r60'] for r in cov)*100:+.2f}% med {st.median(r['r60'] for r in cov)*100:+.2f}%")

by = defaultdict(list)
for r in cov:
    by[r["reason"]].append(r)
print(f"\n{'gate':<26} {'n':>4} {'topsym':>12} {'share':>6} {'meanR':>7} {'meanR_ex_top':>12} {'n_ex':>5}")
for reason, rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
    if len(rows) < 10:
        continue
    c = Counter(r["symbol"] for r in rows)
    top, ntop = c.most_common(1)[0]
    ex = [r for r in rows if r["symbol"] != top]
    m = st.mean(r["r_units"] for r in rows)
    mex = st.mean(r["r_units"] for r in ex) if ex else float("nan")
    print(f"{reason:<26} {len(rows):>4} {top:>12} {ntop/len(rows)*100:>5.0f}% {m:>+7.2f} {mex:>+12.2f} {len(ex):>5}")

# uncovered breakdown by reason
unc = Counter(r["reason"] for r in R if not r["covered"])
print("\nuncovered episodes by gate:", dict(unc.most_common()))
