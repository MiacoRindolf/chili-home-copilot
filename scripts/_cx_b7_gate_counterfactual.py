"""B7 — FULL crypto gate counterfactual (last 10d of trading_automation_events).

Reads scripts/_cx_cache/b7_episodes.csv (extracted from DB via psql \\copy):
collapsed blocked/waiting episodes per (session, normalized_reason), 5-min gap rule.

For each episode: fetch Coinbase 1m candles (cached, backoff-aware, chunk-grid
aligned so reruns are ~free), compute forward returns +15/+30/+60min and the
-1R(-2%) stop vs +2R(+4%) target race over a 60-min horizon.

Candle sources (in order):
  1) Coinbase Exchange public:  https://api.exchange.coinbase.com/products/{p}/candles
  2) Advanced Trade public:     https://api.coinbase.com/api/v3/brokerage/market/products/{p}/candles
Products missing from both are reported in the coverage table.

Usage:  cd D:/dev/chili-home-copilot && conda run --no-capture-output -n chili-env \
        python scripts/_cx_b7_gate_counterfactual.py [--fetch-only]
"""
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

CACHE = "scripts/_cx_cache"
CAND_DIR = os.path.join(CACHE, "candles")
MISS_FILE = os.path.join(CACHE, "b7_product_source.json")  # symbol -> exchange|advanced|none
EPISODES = os.path.join(CACHE, "b7_episodes.csv")
RESULTS = os.path.join(CACHE, "b7_results.json")

CHUNK = 300 * 60          # 300 one-minute buckets per request (both APIs allow >=300)
MIN_INTERVAL = 0.34       # ~3 req/s, well under the 10/s public limit
HORIZON = 60              # minutes
STOP_PCT = -0.02          # -1R
TARGET_PCT = 0.04         # +2R (lane is 2:1)
TAKER_RT_R = 0.012 / 0.02 # 1.2% round-trip taker fees expressed in R (1R = 2%)

_last_req = [0.0]
_req_count = [0]
_session = requests.Session()
_session.headers["User-Agent"] = "chili-b7-research/1.0"


def _throttle():
    dt = time.time() - _last_req[0]
    if dt < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - dt)
    _last_req[0] = time.time()
    _req_count[0] += 1


def _get_with_backoff(url, params, ok_404=True):
    """Returns (status, json_or_none). Exponential backoff on 429/5xx."""
    delay = 1.0
    for _ in range(7):
        _throttle()
        try:
            r = _session.get(url, params=params, timeout=20)
        except requests.RequestException:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if r.status_code == 200:
            try:
                return 200, r.json()
            except ValueError:
                return 200, None
        if r.status_code == 404 and ok_404:
            return 404, None
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        return r.status_code, None
    return -1, None


def fetch_chunk_exchange(sym, c0):
    iso = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
    st, js = _get_with_backoff(
        f"https://api.exchange.coinbase.com/products/{sym}/candles",
        {"granularity": 60, "start": iso(c0), "end": iso(c0 + CHUNK - 60)})
    if st == 404:
        return None
    if st != 200 or js is None or not isinstance(js, list):
        return []
    # rows: [time, low, high, open, close, volume]
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in js]


def fetch_chunk_advanced(sym, c0):
    st, js = _get_with_backoff(
        f"https://api.coinbase.com/api/v3/brokerage/market/products/{sym}/candles",
        {"granularity": "ONE_MINUTE", "start": str(c0), "end": str(c0 + CHUNK - 60), "limit": 350})
    if st == 404:
        return None
    if st != 200 or js is None:
        return []
    out = []
    for c in js.get("candles", []):
        out.append([int(c["start"]), float(c["low"]), float(c["high"]),
                    float(c["open"]), float(c["close"]), float(c["volume"])])
    return out


def load_episodes():
    eps = []
    for r in csv.DictReader(open(EPISODES)):
        t = datetime.strptime(r["ep_start"].split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        r["t0"] = int(t.timestamp()) // 60 * 60
        r["first_mid"] = float(r["first_mid"]) if r["first_mid"] else None
        eps.append(r)
    return eps


def needed_chunks(eps):
    """symbol -> sorted set of chunk-grid starts covering [t0-5m, t0+62m]."""
    need = defaultdict(set)
    for e in eps:
        lo, hi = e["t0"] - 5 * 60, e["t0"] + (HORIZON + 2) * 60
        c = lo // CHUNK * CHUNK
        while c <= hi:
            need[e["symbol"]].add(c)
            c += CHUNK
    return need


def main():
    eps = load_episodes()
    need = needed_chunks(eps)
    src_map = json.load(open(MISS_FILE)) if os.path.exists(MISS_FILE) else {}
    os.makedirs(CAND_DIR, exist_ok=True)

    total_chunks = sum(len(v) for v in need.values())
    cached = 0
    print(f"[fetch] {len(eps)} episodes, {len(need)} symbols, {total_chunks} chunk-requests needed (300min each)")

    candles = defaultdict(dict)  # symbol -> {bucket_ts: [t,l,h,o,c,v]}
    for si, (sym, chunks) in enumerate(sorted(need.items())):
        sdir = os.path.join(CAND_DIR, sym)
        os.makedirs(sdir, exist_ok=True)
        for c0 in sorted(chunks):
            fp = os.path.join(sdir, f"{c0}.json")
            if os.path.exists(fp):
                rows = json.load(open(fp))
                cached += 1
            else:
                rows = None
                src = src_map.get(sym)
                if src in (None, "exchange"):
                    rows = fetch_chunk_exchange(sym, c0)
                    if rows is not None:
                        src_map[sym] = "exchange"
                if rows is None and src in (None, "advanced"):
                    rows = fetch_chunk_advanced(sym, c0)
                    if rows is not None:
                        src_map[sym] = "advanced"
                if rows is None:
                    src_map[sym] = "none"
                    rows = []
                json.dump(rows, open(fp, "w"))
                json.dump(src_map, open(MISS_FILE, "w"), indent=1)
            for r in rows:
                candles[sym][r[0]] = r
        print(f"[fetch] {si+1}/{len(need)} {sym}: src={src_map.get(sym)} "
              f"buckets={len(candles[sym])} reqs_so_far={_req_count[0]} cache_hits={cached}")

    if "--fetch-only" in sys.argv:
        return

    # ---- per-episode counterfactual ----
    def last_close_at_or_before(sym, t, lookback_min=5):
        for k in range(lookback_min + 1):
            c = candles[sym].get(t - k * 60)
            if c:
                return c[4]
        return None

    results = []
    for e in eps:
        sym, t0 = e["symbol"], e["t0"]
        out = {"symbol": sym, "mode": e["mode"], "venue": e["venue"], "reason": e["reason"],
               "session_id": e["session_id"], "t0": t0, "covered": False}
        if not candles.get(sym):
            results.append(out)
            continue
        entry = e["first_mid"] or last_close_at_or_before(sym, t0)
        if not entry or entry <= 0:
            results.append(out)
            continue
        # forward closes
        fwd = {}
        for h in (15, 30, 60):
            c = last_close_at_or_before(sym, t0 + h * 60, lookback_min=5)
            fwd[h] = (c / entry - 1.0) if c else None
        # need at least the 60m point AND some bars in the window to count as covered
        bars = [candles[sym][t] for t in range(t0 + 60, t0 + HORIZON * 60 + 60, 60) if t in candles[sym]]
        if fwd[60] is None or len(bars) < 5:
            results.append(out)
            continue
        out["covered"] = True
        out["r15"], out["r30"], out["r60"] = fwd[15], fwd[30], fwd[60]
        stop, target = entry * (1 + STOP_PCT), entry * (1 + TARGET_PCT)
        race, r_out = "neither", None
        for b in bars:
            hit_s, hit_t = b[1] <= stop, b[2] >= target
            if hit_s:                    # conservative: same-candle tie -> stop first
                race, r_out = "stop_first", -1.0
                break
            if hit_t:
                race, r_out = "target_first", 2.0
                break
        if r_out is None:
            r_out = fwd[60] / abs(STOP_PCT)   # timeout exit at 60m close, in R
        out["race"], out["r_units"] = race, r_out
        results.append(out)

    json.dump(results, open(RESULTS, "w"))
    print(f"[done] wrote {RESULTS}: {len(results)} episodes, requests={_req_count[0]}")

    # ---- aggregate ----
    import statistics as st

    def agg(rows):
        cov = [r for r in rows if r["covered"]]
        if not cov:
            return None
        n = len(cov)
        g = lambda k: [r[k] for r in cov if r.get(k) is not None]
        races = [r["race"] for r in cov]
        mean_r = st.mean([r["r_units"] for r in cov])
        return {
            "n_total": len(rows), "n_cov": n,
            "r15": st.mean(g("r15")) if g("r15") else None,
            "r30": st.mean(g("r30")) if g("r30") else None,
            "r60": st.mean(g("r60")), "med60": st.median(g("r60")),
            "win60": sum(1 for x in g("r60") if x > 0) / n,
            "stop": races.count("stop_first") / n,
            "tgt": races.count("target_first") / n,
            "meanR": mean_r, "netR": mean_r - TAKER_RT_R,
        }

    by_reason = defaultdict(list)
    for r in results:
        by_reason[r["reason"]].append(r)
    print("\n=== PER-GATE COUNTERFACTUAL (crypto, last 10d) — if the gate had NOT blocked ===")
    hdr = f"{'gate':<26} {'n':>4} {'cov':>4} {'r15%':>7} {'r30%':>7} {'r60%':>7} {'med60%':>7} {'win60':>6} {'stop1st':>7} {'tgt1st':>6} {'meanR':>6} {'netR':>6}"
    print(hdr)
    for reason, rows in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        a = agg(rows)
        if a is None:
            print(f"{reason:<26} {len(rows):>4}    0  -- no candle coverage --")
            continue
        pc = lambda x: f"{x*100:>6.2f}" if x is not None else "    --"
        print(f"{reason:<26} {a['n_total']:>4} {a['n_cov']:>4} {pc(a['r15'])} {pc(a['r30'])} {pc(a['r60'])} "
              f"{pc(a['med60'])} {a['win60']*100:>5.0f}% {a['stop']*100:>6.0f}% {a['tgt']*100:>5.0f}% "
              f"{a['meanR']:>6.2f} {a['netR']:>6.2f}")

    print("\n=== live vs paper split (gates present in both) ===")
    by_rm = defaultdict(list)
    for r in results:
        by_rm[(r["reason"], r["mode"])].append(r)
    reasons_both = {k[0] for k in by_rm if (k[0], "live") in by_rm and (k[0], "paper") in by_rm}
    for reason in sorted(reasons_both):
        for mode in ("live", "paper"):
            a = agg(by_rm[(reason, mode)])
            if a:
                print(f"{reason:<26} {mode:<6} n={a['n_cov']:>4}  r60={a['r60']*100:+.2f}%  stop1st={a['stop']*100:.0f}%  "
                      f"tgt1st={a['tgt']*100:.0f}%  meanR={a['meanR']:+.2f}")

    print("\n=== PER-SYMBOL COVERAGE ===")
    by_sym = defaultdict(list)
    for r in results:
        by_sym[r["symbol"]].append(r)
    print(f"{'symbol':<14} {'eps':>4} {'covered':>7}  source")
    for sym, rows in sorted(by_sym.items(), key=lambda kv: -len(kv[1])):
        cov = sum(1 for r in rows if r["covered"])
        print(f"{sym:<14} {len(rows):>4} {cov:>7}  {src_map.get(sym, '?')}")


if __name__ == "__main__":
    main()
