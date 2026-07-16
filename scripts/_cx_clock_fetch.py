"""Fetch Coinbase Exchange public 1m candles for the crypto-clock study.

- Caches per (product, UTC day) to scripts/_cx_cache/<PRODUCT>_<YYYY-MM-DD>.json
  so re-runs cost zero API calls for already-complete days.
- Products with no public candles (Advanced-Trade-only listings) get a
  <PRODUCT>_UNAVAILABLE.marker file and are skipped forever.
- Rate budget: ~2.5 req/s with exponential backoff on 429/5xx.

Usage:  python scripts/_cx_clock_fetch.py [--days 15] [--book-snapshot]
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CACHE = Path(__file__).resolve().parent / "_cx_cache"
CACHE.mkdir(exist_ok=True)

BASE = "https://api.exchange.coinbase.com"
HEADERS = {"User-Agent": "chili-cx-clock/1.0"}

# Reference majors + the lane's top-traded pairs (sessions since 2026-06-06)
# + every live-traded symbol from the 17-trade live record.
PRODUCTS = [
    # majors (clock reference)
    "BTC-USD", "SOL-USD", "DOGE-USD",
    # lane top session names
    "OSMO-USD", "LAYER-USD", "YB-USD", "MOG-USD", "STG-USD", "FIDA-USD",
    "META-USD", "DOGINME-USD", "MAMO-USD", "CTSI-USD", "IDEX-USD",
    "BTRST-USD", "MEGA-USD", "FOX-USD", "EIGEN-USD", "DRV-USD",
    # live-traded extras
    "KAIO-USD", "GWEI-USD", "RSC-USD", "DRIFT-USD", "BILL-USD", "POLS-USD",
    # fresh paper names (likely Advanced-Trade-only; probe anyway)
    "INX-USD", "XPL-USD", "ROBO-USD",
]

_tls = threading.local()
REQ_COUNT = 0
_count_lock = threading.Lock()
_rate_lock = threading.Lock()
_last_req = [0.0]
MIN_INTERVAL = 0.18  # ~5.5 req/s global ceiling (public limit is 10/s)


def _sess():
    if not hasattr(_tls, "s"):
        _tls.s = requests.Session()
        _tls.s.headers.update(HEADERS)
    return _tls.s


def _throttle():
    with _rate_lock:
        wait = _last_req[0] + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_req[0] = time.time()


def _get(url, params=None, max_tries=7):
    global REQ_COUNT
    delay = 1.0
    for attempt in range(max_tries):
        with _count_lock:
            REQ_COUNT += 1
        _throttle()
        try:
            r = _sess().get(url, params=params, timeout=20)
        except requests.RequestException as e:
            print(f"    net-err {e}; sleep {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if r.status_code == 200:
            return r
        if r.status_code in (404, 400):
            return r  # caller decides (product unavailable)
        if r.status_code == 429 or r.status_code >= 500:
            print(f"    {r.status_code}; backoff {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 90)
            continue
        r.raise_for_status()
    return None


def probe(product: str) -> bool:
    """True if the product has public candles."""
    marker = CACHE / f"{product}_UNAVAILABLE.marker"
    if marker.exists():
        return False
    # any cached day file proves availability
    if list(CACHE.glob(f"{product}_2*.json")):
        return True
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=200)
    r = _get(f"{BASE}/products/{product}/candles",
             {"granularity": 60, "start": start.isoformat(), "end": end.isoformat()})
    if r is None:
        return False
    if r.status_code in (404, 400):
        marker.write_text(r.text[:200])
        print(f"  {product}: UNAVAILABLE ({r.status_code})", flush=True)
        return False
    rows = r.json()
    if not isinstance(rows, list):
        marker.write_text(json.dumps(rows)[:200])
        return False
    print(f"  {product}: ok ({len(rows)} probe bars)", flush=True)
    return True


def fetch_day(product: str, day: datetime) -> None:
    """Fetch one UTC day of 1m candles (5 windows of 300 bars) into cache."""
    day_str = day.strftime("%Y-%m-%d")
    out = CACHE / f"{product}_{day_str}.json"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if out.exists() and day_str < today:
        return  # complete past day cached
    rows_all = {}
    t0 = day.replace(hour=0, minute=0, second=0, microsecond=0)
    for w in range(5):  # 5 x 300min = 1500 >= 1440
        ws = t0 + timedelta(minutes=300 * w)
        we = min(ws + timedelta(minutes=300), t0 + timedelta(days=1))
        if ws >= datetime.now(timezone.utc):
            break
        r = _get(f"{BASE}/products/{product}/candles",
                 {"granularity": 60, "start": ws.isoformat(), "end": we.isoformat()})
        if r is None or r.status_code != 200:
            continue
        for row in r.json():
            rows_all[row[0]] = row  # ts -> [ts, low, high, open, close, vol]
    rows = sorted(rows_all.values(), key=lambda x: x[0])
    out.write_text(json.dumps(rows))
    print(f"  {product} {day_str}: {len(rows)} bars", flush=True)


def book_snapshot(products):
    snap = {"ts": datetime.now(timezone.utc).isoformat(), "books": {}}
    for p in products:
        r = _get(f"{BASE}/products/{p}/book", {"level": 1})
        if r is not None and r.status_code == 200:
            j = r.json()
            try:
                bid = float(j["bids"][0][0]); ask = float(j["asks"][0][0])
                bid_sz = float(j["bids"][0][1]); ask_sz = float(j["asks"][0][1])
                mid = (bid + ask) / 2
                snap["books"][p] = {
                    "bid": bid, "ask": ask, "bid_sz": bid_sz, "ask_sz": ask_sz,
                    "spread_bps": (ask - bid) / mid * 1e4,
                    "bid_depth_usd": bid_sz * bid, "ask_depth_usd": ask_sz * ask,
                }
            except (KeyError, IndexError, ValueError):
                snap["books"][p] = {"error": j if isinstance(j, dict) else str(j)[:100]}
    fn = CACHE / f"book_snapshot_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
    fn.write_text(json.dumps(snap, indent=1))
    print(f"book snapshot -> {fn.name}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--book-snapshot", action="store_true")
    args = ap.parse_args()

    print("probing products...", flush=True)
    avail = [p for p in PRODUCTS if probe(p)]
    unavail = [p for p in PRODUCTS if p not in avail]
    print(f"available: {len(avail)}  unavailable: {unavail}", flush=True)

    if args.book_snapshot:
        book_snapshot(avail)

    end_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    days = [end_day - timedelta(days=i) for i in range(args.days, -1, -1)]
    tasks = [(p, d) for p in avail for d in days]
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(lambda t: fetch_day(*t), tasks))
    print(f"DONE: {REQ_COUNT} requests in {time.time()-t_start:.0f}s", flush=True)


if __name__ == "__main__":
    main()
