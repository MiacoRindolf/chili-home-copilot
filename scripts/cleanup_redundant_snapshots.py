#!/usr/bin/env python3
"""Remove redundant trading_snapshots rows (same logical bar or same legacy calendar day).

- Rows with (ticker, bar_interval, bar_start_at) set: keep highest id per key.
- Legacy rows (bar_start_at IS NULL): keep highest id per (ticker, calendar day of snapshot_date).

Default is dry-run. Pass --execute to delete.

Usage (conda env chili-env):
  python scripts/cleanup_redundant_snapshots.py
  python scripts/cleanup_redundant_snapshots.py --execute
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text

# Repo root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Actually delete (default dry-run)")
    args = ap.parse_args()

    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        print("DATABASE_URL required", file=sys.stderr)
        return 1

    engine = create_engine(url)
    dry = not args.execute

    stmts_count = [
        (
            "bar_key_duplicates",
            """
            SELECT COUNT(*) FROM trading_snapshots a
            INNER JOIN trading_snapshots b
              ON a.ticker = b.ticker
             AND a.bar_interval = b.bar_interval
             AND a.bar_start_at = b.bar_start_at
             AND a.bar_start_at IS NOT NULL
             AND b.bar_start_at IS NOT NULL
             AND a.id < b.id
            """,
        ),
        (
            "legacy_day_duplicates",
            """
            SELECT COUNT(*) FROM trading_snapshots a
            INNER JOIN trading_snapshots b
              ON a.ticker = b.ticker
             AND a.bar_start_at IS NULL AND b.bar_start_at IS NULL
             AND (a.snapshot_date::date) = (b.snapshot_date::date)
             AND a.id < b.id
            """,
        ),
    ]

    with engine.connect() as conn:
        for label, sql in stmts_count:
            n = conn.execute(text(sql)).scalar() or 0
            print(f"{label}: rows that would lose (lower id): {n}")

        if dry:
            print("Dry-run only. Use --execute to delete.")
            return 0

        del_bar = text(
            """
            DELETE FROM trading_snapshots a
            USING trading_snapshots b
            WHERE a.bar_start_at IS NOT NULL AND b.bar_start_at IS NOT NULL
              AND a.ticker = b.ticker
              AND a.bar_interval = b.bar_interval
              AND a.bar_start_at = b.bar_start_at
              AND a.id < b.id
            """
        )
        del_legacy = text(
            """
            DELETE FROM trading_snapshots a
            USING trading_snapshots b
            WHERE a.bar_start_at IS NULL AND b.bar_start_at IS NULL
              AND a.ticker = b.ticker
              AND (a.snapshot_date::date) = (b.snapshot_date::date)
              AND a.id < b.id
            """
        )
        r1 = conn.execute(del_bar)
        r2 = conn.execute(del_legacy)
        conn.commit()
        print(f"Deleted bar-key duplicates: {r1.rowcount}")
        print(f"Deleted legacy same-day duplicates: {r2.rowcount}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
