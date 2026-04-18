#!/usr/bin/env python3
"""Batch-backfill trading_pattern_trades.user_id from NULL -> owner (default 1).

Single multi-million-row UPDATE holds one transaction open for a long time and
contends with autovacuum. This script commits every ``--batch`` rows.

Usage (from repo root, app container has DATABASE_URL):

  docker compose exec -T chili python scripts/backfill_pattern_trades_user_id.py --user-id 1

Or locally if DATABASE_URL points at Postgres:

  conda run -n chili-env python scripts/backfill_pattern_trades_user_id.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import psycopg2
except ImportError:
    print("psycopg2 required", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--user-id", type=int, default=1, help="Target users.id (default 1)")
    p.add_argument("--batch", type=int, default=100_000, help="Rows per commit")
    args = p.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    conn = psycopg2.connect(url)
    conn.autocommit = True
    total = 0
    t0 = time.monotonic()
    with conn.cursor() as cur:
        while True:
            cur.execute(
                """
                UPDATE trading_pattern_trades AS t
                SET user_id = %s
                FROM (
                    SELECT id FROM trading_pattern_trades
                    WHERE user_id IS NULL
                    LIMIT %s
                ) AS s
                WHERE t.id = s.id
                """,
                (args.user_id, args.batch),
            )
            n = cur.rowcount
            total += n
            if n == 0:
                break
            elapsed = time.monotonic() - t0
            print(f"updated {n} rows (total {total}, {elapsed:.1f}s)", flush=True)
    print(f"done: {total} rows in {time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
