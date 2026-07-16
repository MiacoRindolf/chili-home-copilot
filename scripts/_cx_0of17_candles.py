"""0/17 forensics — step 2: fetch Coinbase Exchange public 1m candles per trade.

For each live coinbase session in cx_0of17_db.json, fetch 1m candles covering
[entry_fill - 90min, exit + 240min]. Hard-cached to scripts/_cx_cache/candles/
(one json per product+window); exponential backoff on 429; ~1 req per 300 bars.

Coverage is reported honestly: Advanced-Trade-only products have no public
candles and are flagged, not guessed.
"""
import json
import pathlib
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

CACHE = pathlib.Path(__file__).resolve().parent / "_cx_cache"
CDIR = CACHE / "candles"
CDIR.mkdir(parents=True, exist_ok=True)
DB = json.loads((CACHE / "cx_0of17_db.json").read_text())

BASE = "https://api.exchange.coinbase.com/products/{p}/candles?granularity=60&start={s}&end={e}"
UA = {"User-Agent": "chili-forensics/1.0"}


def fetch_window(product: str, start: datetime, end: datetime):
    """Fetch [start,end) 1m candles with per-chunk caching. Returns (rows, note)."""
    rows: list = []
    note = "ok"
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(minutes=300), end)
        key = f"{product}_{cur:%Y%m%d%H%M}_{chunk_end:%Y%m%d%H%M}.json"
        fp = CDIR / key
        if fp.exists():
            rows.extend(json.loads(fp.read_text()))
            cur = chunk_end
            continue
        url = BASE.format(p=product, s=cur.isoformat(), e=chunk_end.isoformat())
        delay = 1.0
        for attempt in range(7):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                    data = json.loads(r.read().decode())
                fp.write_text(json.dumps(data))
                rows.extend(data)
                time.sleep(0.4)  # stay friendly
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                if e.code == 404:
                    return rows, "no_public_candles"
                time.sleep(delay)
                delay = min(delay * 2, 30)
            except Exception:
                time.sleep(delay)
                delay = min(delay * 2, 30)
        else:
            note = "fetch_failed"
            break
        cur = chunk_end
    return rows, note


def main() -> None:
    # entry fill ts per session from events; terminal ts from outcomes.
    entry_ts: dict[int, str] = {}
    for ev in DB["events"]:
        if ev["event_type"] == "live_entry_filled" and ev["session_id"] not in entry_ts:
            entry_ts[ev["session_id"]] = ev["ts"]
    coverage = {}
    for o in DB["outcomes"]:
        sid = o["session_id"]
        sym = o["symbol"]
        et = entry_ts.get(sid) or o["started_at"]
        t0 = datetime.fromisoformat(et).replace(tzinfo=timezone.utc) - timedelta(minutes=300)
        t1 = datetime.fromisoformat(o["terminal_at"] or o["ended_at"] or et).replace(
            tzinfo=timezone.utc) + timedelta(minutes=240)
        rows, note = fetch_window(sym, t0, t1)
        # dedupe by epoch, ascending
        seen = {}
        for r in rows:
            seen[r[0]] = r
        ordered = [seen[k] for k in sorted(seen)]
        out = CDIR / f"session_{sid}_{sym}.json"
        out.write_text(json.dumps(ordered))
        coverage[sid] = {"symbol": sym, "bars": len(ordered), "note": note,
                         "window": [t0.isoformat(), t1.isoformat()]}
        print(f"session {sid} {sym}: {len(ordered)} bars ({note})")
    (CACHE / "cx_0of17_candle_coverage.json").write_text(json.dumps(coverage, indent=1))


if __name__ == "__main__":
    main()
