#!/usr/bin/env python3
"""Remove redundant trading_snapshots rows (same logical bar or same legacy calendar day).

- Rows with (ticker, bar_interval, bar_start_at) set: keep highest id per key.
- Legacy rows (bar_start_at IS NULL): keep highest id per (ticker, calendar day of snapshot_date).

Default is dry-run. Pass --execute to delete.

Dry-run counts **rows** that would be removed (not pair-counts: k duplicate rows
share k(k-1)/2 ordered pairs but only k-1 rows are deleted).

Usage (conda env chili-env):
  python scripts/cleanup_redundant_snapshots.py
  python scripts/cleanup_redundant_snapshots.py --execute
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Repo root on path (for optional app.config fallback)
sys.path.insert(0, str(_REPO_ROOT))


def _resolve_database_url() -> str:
    """Load .env from repo root, then env var or app settings (same as other scripts)."""
    try:
        from dotenv import load_dotenv

        load_dotenv(_REPO_ROOT / ".env")
    except ImportError:
        pass
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        return url
    try:
        from app.config import settings

        return (settings.database_url or "").strip()
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Actually delete (default dry-run)")
    args = ap.parse_args()

    url = _resolve_database_url()
    if not url:
        print(
            "DATABASE_URL not set. Add it to .env in the repo root "
            f"({_REPO_ROOT / '.env'}) or export DATABASE_URL (PostgreSQL URL). "
            "See .env.example.",
            file=sys.stderr,
        )
        return 1

    engine = create_engine(url)
    dry = not args.execute

    stmts_count = [
        (
            "bar_key_rows_to_delete",
            """
            SELECT COUNT(*) FROM trading_snapshots a
            WHERE a.bar_start_at IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM trading_snapshots b
                WHERE b.bar_start_at IS NOT NULL
                  AND a.ticker = b.ticker
                  AND a.bar_interval = b.bar_interval
                  AND a.bar_start_at = b.bar_start_at
                  AND b.id > a.id
              )
            """,
        ),
        (
            "legacy_same_day_rows_to_delete",
            """
            SELECT COUNT(*) FROM trading_snapshots a
            WHERE a.bar_start_at IS NULL
              AND EXISTS (
                SELECT 1 FROM trading_snapshots b
                WHERE b.bar_start_at IS NULL
                  AND a.ticker = b.ticker
                  AND (a.snapshot_date::date) = (b.snapshot_date::date)
                  AND b.id > a.id
              )
            """,
        ),
    ]

    with engine.connect() as conn:
        for label, sql in stmts_count:
            n = conn.execute(text(sql)).scalar() or 0
            print(f"{label}: {n}")

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
