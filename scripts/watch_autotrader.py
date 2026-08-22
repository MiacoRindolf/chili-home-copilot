"""Live AutoTrader watcher — tails new placements + a periodic decision-funnel summary.

Read-only monitor over ``trading_autotrader_runs``: prints each new ``placed`` /
``scaled_in`` row as it happens, plus a compact 1h funnel summary every
``--funnel-every`` seconds so you can see WHY entries are (or aren't) firing
without guessing at the live state.

Usage:
    python scripts/watch_autotrader.py                 # run until Ctrl-C
    python scripts/watch_autotrader.py --interval 15   # poll every 15s
    python scripts/watch_autotrader.py --max-seconds 60  # bounded session

DB target: $DATABASE_URL, else postgresql://chili:chili@localhost:5433/chili.
"""
from __future__ import annotations

import argparse
import os
import time

import psycopg2

_DEFAULT_URL = "postgresql://chili:chili@localhost:5433/chili"


def _connect(url: str):
    conn = psycopg2.connect(url)
    conn.autocommit = True
    return conn


def _print_funnel(cur) -> None:
    cur.execute(
        "SELECT COUNT(*) FROM trading_autotrader_runs "
        "WHERE decision IN ('placed','scaled_in') AND created_at > now() - interval '1 hour'"
    )
    placed_1h = cur.fetchone()[0]
    cur.execute(
        "SELECT decision, reason, COUNT(*) n FROM trading_autotrader_runs "
        "WHERE created_at > now() - interval '1 hour' AND decision NOT IN ('placed','scaled_in') "
        "GROUP BY decision, reason ORDER BY n DESC LIMIT 5"
    )
    top = " | ".join(f"{reason}={n}" for _d, reason, n in cur.fetchall())
    print(f"[funnel 1h] placed/scaled={placed_1h} | top blocks: {top}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Live AutoTrader placement watcher")
    ap.add_argument("--interval", type=int, default=30, help="poll seconds (default 30)")
    ap.add_argument("--funnel-every", type=int, default=300, help="funnel summary seconds (default 300)")
    ap.add_argument("--max-seconds", type=int, default=0, help="auto-stop after N seconds (0 = forever)")
    ap.add_argument("--db-url", default=None, help="override DATABASE_URL")
    args = ap.parse_args()

    url = args.db_url or os.environ.get("DATABASE_URL", _DEFAULT_URL)
    conn = _connect(url)
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(id), 0) FROM trading_autotrader_runs")
    last_id = cur.fetchone()[0]
    started = time.time()
    print(
        f"[watch] live from run id {last_id} | poll {args.interval}s | "
        f"funnel {args.funnel_every}s | Ctrl-C to stop",
        flush=True,
    )
    _print_funnel(cur)
    last_funnel = time.time()

    while True:
        try:
            cur.execute(
                "SELECT id, created_at, ticker, decision, reason FROM trading_autotrader_runs "
                "WHERE id > %s AND decision IN ('placed','scaled_in') ORDER BY id",
                (last_id,),
            )
            for _rid, created, ticker, decision, reason in cur.fetchall():
                print(
                    f"[TRADE {str(created)[:19]}] {decision:10s} {str(ticker):12s} {reason}",
                    flush=True,
                )
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM trading_autotrader_runs")
            last_id = cur.fetchone()[0]
            if time.time() - last_funnel >= args.funnel_every:
                _print_funnel(cur)
                last_funnel = time.time()
        except Exception as exc:  # transient DB blips — reconnect and continue
            print(f"[watch] query error: {exc}; reconnecting in 5s...", flush=True)
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(5)
            conn = _connect(url)
            cur = conn.cursor()
        if args.max_seconds and (time.time() - started) >= args.max_seconds:
            print("[watch] max-seconds reached; stopping.", flush=True)
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
