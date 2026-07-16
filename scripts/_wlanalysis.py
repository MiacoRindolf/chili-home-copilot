"""Winner-vs-loser correlation analysis over cached replay days (2026-06-22).

Builds a per-trade dataset from /app/data/replays/*.json (the engine's persisted results),
derives setup features (spread, time, hold, R:R, stop%, re-entry, leveraged-ETF, notional),
and reports point-biserial correlations with WIN + win-rate-by-feature-bucket. Data-driven
foundation for an ADAPTIVE selectivity gate (no magic thresholds)."""
import glob
import json
import os

from app.services.trading.momentum_neural.leveraged_etf import symbol_is_leveraged_etf

DIR = "/app/data/replays"
files = sorted(f for f in glob.glob(DIR + "/*.json") if "regression" not in os.path.basename(f))
rows = []
for f in files:
    try:
        d = json.load(open(f))
    except Exception:
        continue
    src = d.get("armed_source", "?")
    date = str(d.get("date") or os.path.basename(f))[:10]
    seen: dict = {}
    for t in (d.get("trades") or []):
        sym = t.get("sym")
        entry = float(t.get("entry") or 0)
        stop = float(t.get("stop") or 0)
        target = float(t.get("target") or 0)
        usd = float(t.get("usd") or 0)
        qty = float(t.get("qty") or 0)
        spread = float(t.get("spread_bps") or 0)
        partial = float(t.get("partial") or 1)
        tt = str(t.get("t") or "00:00")
        ext = str(t.get("exit_t") or tt)
        n = seen.get(sym, 0)
        seen[sym] = n + 1
        try:
            hr = int(tt[:2])
            hold = (int(ext[:2]) * 60 + int(ext[3:5])) - (int(tt[:2]) * 60 + int(tt[3:5]))
        except Exception:
            hr, hold = 0, 0
        try:
            lev = 1 if symbol_is_leveraged_etf(sym) else 0
        except Exception:
            lev = 0
        rows.append({
            "date": date, "src": src, "sym": sym, "usd": usd, "win": 1 if usd > 0 else 0,
            "spread": spread, "hr": hr, "hold": hold, "entry": entry,
            "notional": qty * entry, "partial": partial,
            "stop_pct": (entry - stop) / entry * 100 if entry > 0 and stop > 0 else None,
            "target_pct": (target - entry) / entry * 100 if entry > 0 and target > 0 else None,
            "rr": (target - entry) / (entry - stop) if entry > 0 and (entry - stop) > 1e-9 else None,
            "reentry_n": n, "reentry": 1 if n > 0 else 0,
            "lev_etf": lev, "why": t.get("why"),
        })

N = len(rows)
W = sum(r["win"] for r in rows)
print(f"DATASET: {N} trades / {len(files)} day-files | win_rate={W/max(1,N):.1%} ({W}W/{N-W}L) | net=${sum(r['usd'] for r in rows):+,.0f}")


def corr(key):
    xs = [(r[key], r["win"]) for r in rows if r.get(key) is not None]
    if len(xs) < 15:
        return None
    x = [a for a, _ in xs]
    y = [b for _, b in xs]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = sum((a - mx) ** 2 for a in x) ** 0.5
    sy = sum((b - my) ** 2 for b in y) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in xs) / (sx * sy)


print("\nPOINT-BISERIAL CORRELATION with WIN (sorted by |r|):")
cs = [(k, corr(k)) for k in ["spread", "hr", "hold", "entry", "notional", "partial",
                             "stop_pct", "target_pct", "rr", "reentry_n", "lev_etf"]]
for k, c in sorted([c for c in cs if c[1] is not None], key=lambda z: -abs(z[1])):
    print(f"  {k:12s} r={c:+.3f}")


def wr(label, fn):
    b: dict = {}
    for r in rows:
        k = fn(r)
        if k is None:
            continue
        b.setdefault(k, []).append(r)
    print(f"\n{label}:")
    for k in sorted(b, key=lambda x: str(x)):
        g = b[k]
        wins = sum(x["win"] for x in g)
        net = sum(x["usd"] for x in g)
        print(f"  {str(k):16s} n={len(g):3d}  win={wins/len(g):5.1%}  net=${net:+7.0f}  avg=${net/len(g):+6.1f}")


wr("by RE-ENTRY # (same sym, same day)", lambda r: f"#{r['reentry_n']}" if r['reentry_n'] <= 2 else "#3+")
wr("by LEVERAGED ETF", lambda r: "leveraged" if r['lev_etf'] else "real-co")
wr("by SPREAD (bps)", lambda r: "a<=25" if r['spread'] <= 25 else ("b 26-60" if r['spread'] <= 60 else ("c 61-120" if r['spread'] <= 120 else "d >120")))
wr("by ENTRY HOUR (UTC)", lambda r: f"{r['hr']:02d}h")
wr("by R:R ratio", lambda r: None if r['rr'] is None else ("a<1.5" if r['rr'] < 1.5 else ("b 1.5-2.5" if r['rr'] < 2.5 else "c >=2.5")))
wr("by STOP distance %", lambda r: None if r['stop_pct'] is None else ("a<5%" if r['stop_pct'] < 5 else ("b 5-10%" if r['stop_pct'] < 10 else "c >=10%")))
wr("by HOLD (min)", lambda r: "a<=2" if r['hold'] <= 2 else ("b 3-10" if r['hold'] <= 10 else "c >10"))
wr("by PARTIAL fill", lambda r: "full" if r['partial'] >= 0.99 else "partial")
wr("by SRC", lambda r: r['src'])
