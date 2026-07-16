"""Tradeable crypto universe scan (Coinbase Exchange public API).

Phases (run as subcommands, all cached to scripts/_cx_cache/):
  meta     -- GET /products (all products, one request)
  stats    -- GET /products/<id>/stats for all online USD pairs (24h volume) [cached]
  books    -- N passes of GET /products/<id>/book?level=2 over the evaluation set
  candles  -- 1m (300 bars) + 5m (2x300 bars ~ 48h) for the evaluation set
  report   -- compute per-pair metrics + tiering, write report JSON + print table

Evaluation set = top-50 USD pairs by 24h dollar volume UNION pairs the momentum
lane has touched (sessions/fills/ledger/quote-unavailable, queried 2026-06-13).

Rate budget: <= 4 req/s, exponential backoff on 429/5xx, everything cached so
re-runs are nearly free.
"""
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://api.exchange.coinbase.com"
CACHE = Path(__file__).resolve().parent / "_cx_cache"
CACHE.mkdir(exist_ok=True)

# Pairs the momentum lane has touched (trading_automation_sessions UNION
# simulated_fills UNION economic_ledger live/paper UNION paper_quote_unavailable),
# pulled from the chili DB 2026-06-13.
TOUCHED = [
    "ALICE-USD", "AST-USD", "BARD-USD", "BAT-USD", "BILL-USD", "BIO-USD",
    "BLUR-USD", "BOBBOB-USD", "BTRST-USD", "CRV-USD", "CTSI-USD", "CTX-USD",
    "CVX-USD", "DOGINME-USD", "DRIFT-USD", "DRV-USD", "EIGEN-USD", "FARM-USD",
    "FET-USD", "FIDA-USD", "FLR-USD", "FLUID-USD", "FOX-USD", "GWEI-USD",
    "HOME-USD", "HYPER-USD", "IDEX-USD", "INX-USD", "IOTX-USD", "JTO-USD",
    "KAIO-USD", "KARRAT-USD", "KMNO-USD", "LAYER-USD", "MAMO-USD", "MEGA-USD",
    "META-USD", "MLN-USD", "MOG-USD", "MSOL-USD", "ONDO-USD", "ORCA-USD",
    "OSMO-USD", "PERP-USD", "PLU-USD", "POLS-USD", "PRL-USD", "PYTH-USD",
    "RAD-USD", "ROBO-USD", "RSC-USD", "SAPIEN-USD", "SENT-USD", "SOL-USD",
    "STG-USD", "TRAC-USD", "VELO-USD", "VOXEL-USD", "XPL-USD", "YB-USD",
    "ZRX-USD", "INJ-USD",
]

_session = requests.Session()
_session.headers["User-Agent"] = "chili-universe-scan/1.0"
_last_req = [0.0]


def _get(path, *, max_tries=6, min_gap=0.5):
    """Rate-limited GET with exponential backoff. Returns (status, json|None)."""
    for attempt in range(max_tries):
        gap = time.monotonic() - _last_req[0]
        if gap < min_gap:
            time.sleep(min_gap - gap)
        _last_req[0] = time.monotonic()
        try:
            r = _session.get(BASE + path, timeout=15)
        except Exception:
            time.sleep(1.5 * (2 ** attempt))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(1.0 * (2 ** attempt))
            continue
        if r.status_code == 404:
            return 404, None
        if r.status_code != 200:
            return r.status_code, None
        try:
            return 200, r.json()
        except Exception:
            return 200, None
    return -1, None


def _load(name):
    p = CACHE / name
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None  # truncated by an interrupted write -- refetch
    return None


def _save(name, obj):
    tmp = CACHE / (name + ".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(CACHE / name)


# ---------------------------------------------------------------- meta
def phase_meta():
    cached = _load("products.json")
    if cached:
        print(f"products.json cached ({len(cached['products'])} products, ts={cached['ts']})")
        return cached
    st, data = _get("/products")
    if st != 200 or not isinstance(data, list):
        print(f"FATAL products fetch status={st}")
        sys.exit(1)
    out = {"ts": datetime.now(timezone.utc).isoformat(), "products": data}
    _save("products.json", out)
    usd = [p for p in data if p.get("quote_currency") == "USD"]
    print(f"fetched {len(data)} products, {len(usd)} USD-quoted")
    return out


def usd_products(meta):
    return {
        p["id"]: p for p in meta["products"]
        if p.get("quote_currency") == "USD"
    }


# ---------------------------------------------------------------- stats
def phase_stats():
    meta = phase_meta()
    usd = usd_products(meta)
    online = [pid for pid, p in usd.items()
              if p.get("status") == "online" and not p.get("trading_disabled")]
    cached = _load("stats_all.json") or {"ts": None, "stats": {}}
    todo = [pid for pid in online if pid not in cached["stats"]]
    print(f"{len(online)} online USD pairs, {len(todo)} stats to fetch")
    for i, pid in enumerate(todo):
        st, data = _get(f"/products/{pid}/stats")
        cached["stats"][pid] = {"status": st, "data": data}
        if (i + 1) % 40 == 0:
            cached["ts"] = datetime.now(timezone.utc).isoformat()
            _save("stats_all.json", cached)
            print(f"  {i+1}/{len(todo)}")
    cached["ts"] = datetime.now(timezone.utc).isoformat()
    _save("stats_all.json", cached)
    print("stats done")
    return cached


def eval_set():
    """Top-50 by 24h USD volume UNION touched pairs. Returns (list, dollarvol map, missing_meta)."""
    meta = phase_meta()
    usd = usd_products(meta)
    stats = (_load("stats_all.json") or {"stats": {}})["stats"]
    dv = {}
    for pid, rec in stats.items():
        d = rec.get("data") or {}
        try:
            vol = float(d.get("volume") or 0.0)
            last = float(d.get("last") or 0.0)
            dv[pid] = vol * last
        except (TypeError, ValueError):
            dv[pid] = 0.0
    top50 = sorted(dv, key=dv.get, reverse=True)[:50]
    missing_meta = [t for t in TOUCHED if t not in usd]
    ev = sorted(set(top50) | (set(TOUCHED) & set(usd)))
    return ev, dv, missing_meta


# ---------------------------------------------------------------- books
def phase_books(passes=4, gap_s=45):
    ev, _, missing = eval_set()
    print(f"eval set {len(ev)} pairs; {len(missing)} touched pairs NOT on exchange API: {missing}")
    books = _load("books.json") or {"samples": {}}
    done_passes = len(books.get("pass_ts", []))
    books.setdefault("pass_ts", [])
    for pn in range(done_passes, passes):
        t0 = time.monotonic()
        for pid in ev:
            st, data = _get(f"/products/{pid}/book?level=2")
            rec = {"ts": datetime.now(timezone.utc).isoformat(), "status": st}
            if st == 200 and data:
                # keep top 25 levels each side -- enough for depth@50bps
                rec["bids"] = data.get("bids", [])[:25]
                rec["asks"] = data.get("asks", [])[:25]
            books["samples"].setdefault(pid, []).append(rec)
        books["pass_ts"].append(datetime.now(timezone.utc).isoformat())
        _save("books.json", books)
        el = time.monotonic() - t0
        print(f"pass {pn+1}/{passes} done in {el:.0f}s")
        if pn + 1 < passes and el < gap_s:
            time.sleep(gap_s - el)
    print("books done")


# ---------------------------------------------------------------- candles
def phase_candles():
    ev, _, _ = eval_set()
    c1 = _load("candles_1m.json") or {}
    c5 = _load("candles_5m.json") or {}
    now = int(time.time())
    for pid in ev:
        if pid not in c1:
            st, data = _get(f"/products/{pid}/candles?granularity=60")
            c1[pid] = {"status": st, "bars": data if isinstance(data, list) else None}
            _save("candles_1m.json", c1)
        if pid not in c5:
            bars = []
            ok = True
            for w in range(1):  # 1 window x 300 5m-bars = 25h (rate-budget cut
                # under contention with the sibling session's fetches)
                end = now - w * 300 * 300
                start = end - 300 * 300
                iso_s = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()
                iso_e = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
                st, data = _get(
                    f"/products/{pid}/candles?granularity=300&start={iso_s}&end={iso_e}")
                if st != 200 or not isinstance(data, list):
                    ok = False
                    break
                bars.extend(data)
            c5[pid] = {"status": 200 if ok else st, "bars": bars if ok else None}
            _save("candles_5m.json", c5)
    print(f"candles done: 1m for {sum(1 for v in c1.values() if v['bars'])}/{len(ev)}, "
          f"5m for {sum(1 for v in c5.values() if v['bars'])}/{len(ev)}")


# ---------------------------------------------------------------- report
def _pct(vals, q):
    if not vals:
        return None
    vs = sorted(vals)
    k = (len(vs) - 1) * q
    f = math.floor(k)
    c = min(f + 1, len(vs) - 1)
    return vs[f] + (vs[c] - vs[f]) * (k - f)


def phase_report():
    meta = phase_meta()
    usd = usd_products(meta)
    ev, dv, missing_meta = eval_set()
    books = (_load("books.json") or {}).get("samples", {})
    c1 = _load("candles_1m.json") or {}
    c5 = _load("candles_5m.json") or {}

    rows = []
    for pid in ev:
        p = usd.get(pid, {})
        r = {
            "pair": pid,
            "touched": pid in TOUCHED,
            "status": p.get("status"),
            "post_only": p.get("post_only"),
            "limit_only": p.get("limit_only"),
            "trading_disabled": p.get("trading_disabled"),
            "base_min_size": p.get("base_min_size"),
            "min_market_funds": p.get("min_market_funds"),
            "quote_increment": p.get("quote_increment"),
            "dollar_vol_24h": round(dv.get(pid, 0.0)),
        }
        # ---- spreads + depth from book samples
        spreads, bid_depth50, ask_depth50, touch_bid = [], [], [], []
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
            lo = mid * (1 - 0.005)
            d = sum(float(px) * float(sz) for px, sz, *_ in s["bids"] if float(px) >= lo)
            bid_depth50.append(d)
            hi = mid * (1 + 0.005)
            d = sum(float(px) * float(sz) for px, sz, *_ in s["asks"] if float(px) <= hi)
            ask_depth50.append(d)
            touch_bid.append(float(s["bids"][0][0]) * float(s["bids"][0][1]))
        r["n_book_samples"] = len(spreads)
        r["spread_med_bps"] = round(_pct(spreads, 0.5), 1) if spreads else None
        r["spread_p90_bps"] = round(_pct(spreads, 0.9), 1) if spreads else None
        r["bid_depth50bps_med"] = round(statistics.median(bid_depth50)) if bid_depth50 else None
        r["touch_bid_usd_med"] = round(statistics.median(touch_bid)) if touch_bid else None
        # tick-implied spread floor
        try:
            qi = float(p.get("quote_increment") or 0)
            last_px = None
            if c1.get(pid, {}).get("bars"):
                last_px = float(c1[pid]["bars"][0][4])
            if qi and last_px:
                r["tick_floor_bps"] = round(qi / last_px * 1e4, 1)
        except (TypeError, ValueError, IndexError):
            pass
        # ---- 1m candles: typical 1m dollar volume + tape density (Fri-night window)
        bars1 = c1.get(pid, {}).get("bars")
        r["has_candles"] = bool(bars1)
        if bars1:
            # bar: [time, low, high, open, close, volume]
            dvols = [float(b[5]) * (float(b[4]) + float(b[3])) / 2 for b in bars1]
            r["n_1m_bars"] = len(bars1)
            ts = [b[0] for b in bars1]
            span_min = (max(ts) - min(ts)) / 60 + 1 if len(ts) > 1 else 1
            r["tape_density_pct"] = round(len(bars1) / span_min * 100, 1)
            r["dv1m_med"] = round(_pct(dvols, 0.5))
            r["dv1m_p90"] = round(_pct(dvols, 0.9))
            # gap behavior: bar-to-bar jump (prev close -> open), the crypto
            # analogue of a halt/gap — big values = quotes teleport between prints
            srt = sorted(bars1, key=lambda b: b[0])
            gaps = []
            for a, b in zip(srt, srt[1:]):
                pc, o = float(a[4]), float(b[3])
                if pc > 0 and o > 0:
                    gaps.append(abs(o / pc - 1) * 1e4)
            if gaps:
                r["gap_p99_bps"] = round(_pct(gaps, 0.99), 1)
                r["gap_max_bps"] = round(max(gaps), 1)
        # ---- 5m candles (~50h): burst frequency + gappiness
        bars5 = c5.get(pid, {}).get("bars")
        if bars5:
            seen, uniq = set(), []
            for b in bars5:
                if b[0] not in seen:
                    seen.add(b[0])
                    uniq.append(b)
            uniq.sort(key=lambda b: b[0])
            d5 = [float(b[5]) * (float(b[4]) + float(b[3])) / 2 for b in uniq]
            med_d5 = _pct(d5, 0.5) or 0.0
            bursts = 0
            for b, dvol in zip(uniq, d5):
                o, h = float(b[3]), float(b[2])
                if o > 0 and (h / o - 1) >= 0.02 and med_d5 > 0 and dvol >= 3 * med_d5:
                    bursts += 1
            days = (uniq[-1][0] - uniq[0][0]) / 86400 if len(uniq) > 1 else 1
            r["n_5m_bars"] = len(uniq)
            r["bursts_per_day"] = round(bursts / max(days, 0.01), 2)
            # max single-bar range as gappiness proxy
            rngs = [(float(b[2]) - float(b[1])) / float(b[3]) * 100 for b in uniq if float(b[3]) > 0]
            r["max_5m_range_pct"] = round(max(rngs), 2) if rngs else None
        rows.append(r)

    # products the lane touched but the exchange API doesn't list (no public candles)
    for pid in missing_meta:
        rows.append({"pair": pid, "touched": True, "status": "NOT_ON_EXCHANGE_API",
                     "has_candles": False, "n_book_samples": 0})

    # ---------------- tiering (anchored to fee/stop geometry: limit fee ~40bps/side
    # observed, lane stop ~120bps; A requires spread <= ~1/8 stop and exit depth
    # >= the $15k max notional; B = marginal; C = untouchable)
    for r in rows:
        sp, p90 = r.get("spread_med_bps"), r.get("spread_p90_bps")
        dv1, depth = r.get("dv1m_med"), r.get("bid_depth50bps_med")
        dens = r.get("tape_density_pct") or 0
        if (r.get("status") != "online" or not r.get("has_candles")
                or sp is None or r.get("trading_disabled")):
            r["tier"] = "C"
            r["tier_reason"] = "no_public_data_or_not_online"
            continue
        if r.get("post_only") or r.get("limit_only"):
            r["tier"] = "C"
            r["tier_reason"] = "restricted_mode"
            continue
        if sp <= 15 and (p90 or 99) <= 40 and (dv1 or 0) >= 20000 and (depth or 0) >= 15000 and dens >= 90:
            r["tier"] = "A"
            r["tier_reason"] = "tight_deep"
        elif sp <= 50 and (dv1 or 0) >= 3000 and (depth or 0) >= 4000 and dens >= 60:
            r["tier"] = "B"
            r["tier_reason"] = "marginal"
        else:
            r["tier"] = "C"
            why = []
            if sp > 50:
                why.append(f"spread{sp}")
            if (dv1 or 0) < 3000:
                why.append(f"thin1m{dv1}")
            if (depth or 0) < 4000:
                why.append(f"shallow{depth}")
            if dens < 60:
                why.append(f"sparse{dens}")
            r["tier_reason"] = ",".join(why) or "fails_b"
        # adaptive per-pair max notional: exit-side bounded
        if depth and dv1:
            r["max_notional_usd"] = round(min(0.25 * depth, 0.5 * dv1))

    out = {"ts": datetime.now(timezone.utc).isoformat(), "rows": rows}
    _save("report.json", out)

    rows.sort(key=lambda r: (r.get("tier", "Z"), -(r.get("dollar_vol_24h") or 0)))
    hdr = ("pair", "tier", "touched", "spread_med", "spread_p90", "dv1m_med",
           "depth50", "density", "bursts/d", "maxnotional", "dv24h_m")
    print(("%-14s %-4s %-7s %10s %10s %10s %10s %8s %8s %11s %8s") % hdr)
    for r in rows:
        print("%-14s %-4s %-7s %10s %10s %10s %10s %8s %8s %11s %8s" % (
            r["pair"], r.get("tier", "?"), "T" if r.get("touched") else "",
            r.get("spread_med_bps", ""), r.get("spread_p90_bps", ""),
            r.get("dv1m_med", ""), r.get("bid_depth50bps_med", ""),
            r.get("tape_density_pct", ""), r.get("bursts_per_day", ""),
            r.get("max_notional_usd", ""),
            round((r.get("dollar_vol_24h") or 0) / 1e6, 1)))
    n = {"A": 0, "B": 0, "C": 0}
    for r in rows:
        n[r.get("tier", "C")] = n.get(r.get("tier", "C"), 0) + 1
    print(f"\ntiers: {n}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "meta":
        phase_meta()
    elif cmd == "stats":
        phase_stats()
    elif cmd == "books":
        passes = int(sys.argv[2]) if len(sys.argv) > 2 else 4
        phase_books(passes=passes)
    elif cmd == "candles":
        phase_candles()
    elif cmd == "report":
        phase_report()
    else:
        print("usage: meta|stats|books [passes]|candles|report")
