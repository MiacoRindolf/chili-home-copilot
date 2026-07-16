"""_cx_maker_fill_sim.py — maker-entry fill simulation + fee-aware geometry on real 1m paths.

For every crypto entry the lane actually took (paper sim fills + live ledger fills):
  1. fetch 1m candles around the entry (cached, budgeted, backoff),
  2. simulate posting a post-only buy limit at the decision quote (reference_price)
     vs the taker fill actually recorded — measure fill%, miss%, by wait window,
  3. replay the lane's exit geometry (stop s, target T*R, partial 0.5 @ target,
     BE ratchet, trail) across stop sizes x targets x fee configs,
  4. bucket by 1m-ATR14% at entry -> empirical win rate + expectancy surface.

Candle sources: public Advanced Trade market endpoint (covers AT-only products),
fallback public Exchange endpoint. Honest coverage accounting.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "_cx_cache")
CANDLE_CACHE = os.path.join(CACHE, "candles")
os.makedirs(CANDLE_CACHE, exist_ok=True)

PRE_MIN = 30          # candles before entry (ATR)
POST_MIN = 240        # candles after entry (path sim)
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "chili-research/1.0"
_no_candles: set[str] = set()

# fee configs: (label, entry_bps_taker, exit_bps_taker, entry_bps_maker)
FEE_CONFIGS = [
    ("intro_120/60", 120.0, 120.0, 60.0),
    ("stated_60/35", 60.0, 60.0, 35.0),
    ("actual_adv1_50/25", 50.0, 50.0, 25.0),
    ("adv2_35/15", 35.0, 35.0, 15.0),
]
STOPS = [0.012, 0.02, 0.03]
TARGETS = [2.0, 3.0]
SCALE_OUT_F = 0.5


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.split("+")[0]).replace(tzinfo=timezone.utc)


def fetch_candles(pid: str, start: datetime, end: datetime) -> list[dict] | None:
    """1m candles [start,end) ascending; cached. None = product not covered."""
    if pid in _no_candles:
        return None
    key = f"{pid}_{int(start.timestamp())}_{int(end.timestamp())}.json"
    path = os.path.join(CANDLE_CACHE, key)
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        return data if data else None

    out = None
    # primary: public Advanced Trade market data (covers AT-only listings)
    url = f"https://api.coinbase.com/api/v3/brokerage/market/products/{pid}/candles"
    params = {"start": str(int(start.timestamp())), "end": str(int(end.timestamp())),
              "granularity": "ONE_MINUTE"}
    for attempt in range(5):
        try:
            r = SESSION.get(url, params=params, timeout=15)
        except Exception:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            time.sleep(1.5 * (2 ** attempt))
            continue
        if r.status_code == 404:
            break
        if r.ok:
            rows = (r.json() or {}).get("candles") or []
            out = [{"t": int(c["start"]), "o": float(c["open"]), "h": float(c["high"]),
                    "l": float(c["low"]), "c": float(c["close"]), "v": float(c["volume"])}
                   for c in rows]
            out.sort(key=lambda x: x["t"])
            break
        time.sleep(1.0 * (attempt + 1))
    if out is None:
        # fallback: public Exchange API
        url2 = f"https://api.exchange.coinbase.com/products/{pid}/candles"
        params2 = {"granularity": 60, "start": start.isoformat(), "end": end.isoformat()}
        for attempt in range(5):
            try:
                r = SESSION.get(url2, params=params2, timeout=15)
            except Exception:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                time.sleep(1.5 * (2 ** attempt))
                continue
            if r.status_code == 404:
                break
            if r.ok:
                rows = r.json() or []
                out = [{"t": int(c[0]), "l": float(c[1]), "h": float(c[2]),
                        "o": float(c[3]), "c": float(c[4]), "v": float(c[5])}
                       for c in rows]
                out.sort(key=lambda x: x["t"])
                break
            time.sleep(1.0 * (attempt + 1))
    time.sleep(0.35)  # global pacing
    with open(path, "w") as f:
        json.dump(out if out else [], f)
    if not out:
        _no_candles.add(pid)
        return None
    return out


def atr14_pct(candles: list[dict], entry_idx: int) -> float | None:
    """ATR(14) on 1m bars ending just before entry, as fraction of price."""
    lo = max(0, entry_idx - 14)
    trs = []
    for i in range(lo + 1, entry_idx + 1):
        c, p = candles[i], candles[i - 1]
        tr = max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"]))
        trs.append(tr)
    if len(trs) < 8:
        return None
    px = candles[entry_idx]["c"]
    return (sum(trs) / len(trs)) / px if px > 0 else None


def sim_geometry(candles, entry_idx, entry_px, s, T):
    """Replay ladder: stop s, first target T*s, partial 0.5 @ target -> BE + trail.

    Returns gross return as fraction of notional (before fees), plus tags.
    Conservative: stop checked before target within a bar.
    """
    stop = entry_px * (1.0 - s)
    target = entry_px * (1.0 + T * s)
    qty = 1.0
    realized = 0.0
    partial_done = False
    hi_close = entry_px
    i = entry_idx + 1
    n = len(candles)
    while i < n:
        c = candles[i]
        if not partial_done:
            if c["l"] <= stop:
                return (stop / entry_px - 1.0), "stop", False
            if c["h"] >= target:
                realized += SCALE_OUT_F * (target / entry_px - 1.0)
                qty -= SCALE_OUT_F
                partial_done = True
                stop = entry_px  # breakeven ratchet
                hi_close = target
        else:
            trail = max(stop, hi_close * (1.0 - s))
            if c["l"] <= trail:
                realized += qty * (trail / entry_px - 1.0)
                return realized, "trail", True
            hi_close = max(hi_close, c["c"])
        i += 1
    # timeout: flatten at last close
    last = candles[n - 1]["c"]
    realized += qty * (last / entry_px - 1.0)
    return realized, "timeout", partial_done


def main():
    episodes = []
    for fn in ("paper_entries.json", "live_entries.json"):
        with open(os.path.join(CACHE, fn)) as f:
            rows = json.load(f) or []
        episodes.extend(rows)
    print(f"episodes: {len(episodes)} (paper={sum(1 for e in episodes if e['src']=='paper')}, "
          f"live={sum(1 for e in episodes if e['src']=='live')})")

    covered, no_candles, ts_match = 0, defaultdict(int), 0
    results = []
    for ep in episodes:
        pid = ep["symbol"]
        ts = _parse_ts(ep["ts"])
        start = ts - timedelta(minutes=PRE_MIN)
        end = ts + timedelta(minutes=POST_MIN)
        candles = fetch_candles(pid, start, end)
        if not candles:
            no_candles[pid] += 1
            continue
        # locate entry bar
        t_entry = int(ts.timestamp()) // 60 * 60
        idx = None
        for i, c in enumerate(candles):
            if c["t"] == t_entry:
                idx = i
                break
        if idx is None or idx < 8 or idx >= len(candles) - 10:
            no_candles[pid + ":gap"] += 1
            continue
        covered += 1
        entry_px = float(ep["price"])
        bar = candles[idx]
        if bar["l"] * 0.99 <= entry_px <= bar["h"] * 1.01:
            ts_match += 1
        ref = float(ep.get("reference_price") or entry_px)
        if ref <= 0:
            ref = entry_px
        atr = atr14_pct(candles, idx)
        # maker fill check: post-only limit at ref, windows after signal bar
        fills = {}
        for w in (1, 3, 5, 15):
            touched = any(candles[j]["l"] <= ref for j in range(idx + 1, min(idx + 1 + w, len(candles))))
            through = any(candles[j]["l"] <= ref * (1 - 0.0005) for j in range(idx + 1, min(idx + 1 + w, len(candles))))
            fills[w] = (touched, through)
        geo = {}
        for s in STOPS:
            for T in TARGETS:
                g_taker = sim_geometry(candles, idx, entry_px, s, T)
                g_maker = sim_geometry(candles, idx, ref, s, T)
                geo[(s, T)] = {"taker": g_taker, "maker": g_maker}
        results.append({"src": ep["src"], "pid": pid, "ts": ep["ts"], "entry": entry_px,
                        "ref": ref, "atr": atr, "fills": fills, "geo": geo})

    print(f"covered: {covered}/{len(episodes)}  ts_price_match={ts_match}/{covered}")
    print("no candle coverage:", dict(no_candles))

    if not results:
        return 1

    # ---- maker fill rates ----
    print("\n=== MAKER POST-ONLY FILL RATES (limit at decision quote, after signal bar) ===")
    for w in (1, 3, 5, 15):
        t = sum(1 for r in results if r["fills"][w][0])
        th = sum(1 for r in results if r["fills"][w][1])
        print(f"  wait {w:>2}m: touched={t}/{len(results)} ({t/len(results)*100:.0f}%)  "
              f"filled-through(5bps)={th}/{len(results)} ({th/len(results)*100:.0f}%)")

    # ---- expectancy surface ----
    fill_w = 5  # adopt 5m wait, through-fill rule
    print(f"\n=== FEE-AWARE EXPECTANCY (per signal, n={len(results)}; maker = through-fill @{fill_w}m wait) ===")
    header = f"{'geometry':14} {'win%':>5} {'gross%':>7} | " + " | ".join(f"{fc[0]:>20}" for fc in FEE_CONFIGS)
    print(header)
    print(" " * 30 + "| " + " | ".join(f"{'taker':>9} {'maker':>10}" for _ in FEE_CONFIGS))
    surface = {}
    for s in STOPS:
        for T in TARGETS:
            wins = sum(1 for r in results if r["geo"][(s, T)]["taker"][2])
            gross_taker = [r["geo"][(s, T)]["taker"][0] for r in results]
            row = f"s={s*100:.1f}% T={T:.0f}R   {wins/len(results)*100:>4.0f} {sum(gross_taker)/len(results)*100:>6.2f}% | "
            cells = []
            for (label, tk_in, tk_out, mk_in) in FEE_CONFIGS:
                # taker-all: entry+exit taker on ~2x notional (exit notional approx 1+ret)
                net_taker = [g - (tk_in + tk_out * (1 + g)) / 1e4 for g in gross_taker]
                e_taker = sum(net_taker) / len(net_taker)
                # maker entry: only filled signals trade; entry at ref; exit taker
                net_maker_sum = 0.0
                for r in results:
                    if r["fills"][fill_w][1]:
                        g = r["geo"][(s, T)]["maker"][0]
                        net_maker_sum += g - (mk_in + tk_out * (1 + g)) / 1e4
                e_maker = net_maker_sum / len(results)  # per SIGNAL (misses = 0)
                cells.append(f"{e_taker*100:>8.3f}% {e_maker*100:>9.3f}%")
            print(row + " | ".join(cells))
            surface[(s, T)] = (wins / len(results), sum(gross_taker) / len(results))

    # ---- missed-fill opportunity cost ----
    print(f"\n=== WHAT THE MAKER MISSES (signals with no through-fill in {fill_w}m) ===")
    for s, T in [(0.012, 2.0), (0.02, 2.0)]:
        missed = [r for r in results if not r["fills"][fill_w][1]]
        if missed:
            mg = [r["geo"][(s, T)]["taker"][0] for r in missed]
            mw = sum(1 for r in missed if r["geo"][(s, T)]["taker"][2])
            print(f"  s={s*100:.1f}% T={T:.0f}R: missed n={len(missed)}  "
                  f"their taker win%={mw/len(missed)*100:.0f}%  avg gross={sum(mg)/len(mg)*100:.2f}%")

    # ---- ATR buckets ----
    print("\n=== BY 1m-ATR14%% BUCKET (taker fills, s=1.2%% T=2R and s=2%% T=2R) ===")
    buckets = [(0, 0.003), (0.003, 0.006), (0.006, 0.012), (0.012, 9)]
    for blo, bhi in buckets:
        sel = [r for r in results if r["atr"] is not None and blo <= r["atr"] < bhi]
        if not sel:
            continue
        line = f"  ATR {blo*100:.1f}-{bhi*100:.1f}% n={len(sel)}: "
        for s, T in [(0.012, 2.0), (0.02, 2.0), (0.03, 2.0)]:
            wins = sum(1 for r in sel if r["geo"][(s, T)]["taker"][2])
            gross = sum(r["geo"][(s, T)]["taker"][0] for r in sel) / len(sel)
            # net at actual tier taker
            net = gross - 0.0100 * 1.0  # ~50bps x2
            line += f" [s={s*100:.0f}.{int(s*1000)%10}%: win {wins/len(sel)*100:.0f}% gross {gross*100:+.2f}% netT50 {net*100:+.2f}%]"
        print(line)

    with open(os.path.join(CACHE, "maker_sim_results.json"), "w") as f:
        json.dump([{**r, "geo": {f"{k[0]}_{k[1]}": v for k, v in r["geo"].items()},
                    "fills": {str(k): v for k, v in r["fills"].items()}} for r in results], f)
    print("\nsaved -> _cx_cache/maker_sim_results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
