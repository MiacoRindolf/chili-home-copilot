"""Backfill the M.1 pattern x regime performance ledger over a historical
range of ``as_of_date`` values.

The M.2 authoritative consumers (sizing tilt, promotion gate, kill-switch)
need at least ``consecutive_days_negative`` (default 3) prior daily snapshots
of ``trading_pattern_regime_performance_daily`` to make informed decisions.
This script replays the M.1 ledger computation one as-of-date at a time,
so that when M.2 flips into ``shadow`` / ``compare`` / ``authoritative``
the lookup has a non-empty history.

Safety
------
* ``--dry-run`` is the default. Pass ``--commit`` to actually persist.
* Authoritative modes are refused upstream by
  ``pattern_regime_performance_service.compute_and_persist``; this script
  always invokes it with ``mode_override="shadow"`` regardless of settings.
* Runs are append-only; the deterministic ``ledger_run_id`` lets duplicate
  runs be deduped by downstream consumers keyed on ``(as_of_date,
  window_days, ledger_run_id)``.

Run (from repo root, conda chili-env active)::

    python scripts/backfill_pattern_regime_ledger.py \\
        --start 2026-01-01 --end 2026-04-15 --dry-run

    python scripts/backfill_pattern_regime_ledger.py \\
        --start 2026-01-01 --end 2026-04-15 --commit
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.trading.pattern_regime_performance_service import (  # noqa: E402
    compute_and_persist,
)


logger = logging.getLogger("backfill_pattern_regime_ledger")


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {s!r}; expected YYYY-MM-DD"
        ) from exc


def _daterange(start: date, end: date, *, step_days: int) -> List[date]:
    out: List[date] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur = cur + timedelta(days=max(1, int(step_days)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Backfill trading_pattern_regime_performance_daily across a "
            "historical date range. Dry-run by default."
        )
    )
    today = datetime.now(tz=timezone.utc).date()
    ap.add_argument(
        "--start",
        type=_parse_date,
        default=today - timedelta(days=30),
        help="First as_of_date to compute (inclusive, YYYY-MM-DD).",
    )
    ap.add_argument(
        "--end",
        type=_parse_date,
        default=today - timedelta(days=1),
        help="Last as_of_date to compute (inclusive, YYYY-MM-DD).",
    )
    ap.add_argument(
        "--step-days",
        type=int,
        default=1,
        help="Spacing between as_of_dates (default daily).",
    )
    ap.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Actually persist rows. Without this flag the script computes "
            "and reports expected row counts without calling the service."
        ),
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging for the pattern_regime service.",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.start > args.end:
        print(
            f"[backfill] start {args.start} is after end {args.end}; nothing to do",
            file=sys.stderr,
        )
        return 2

    as_of_dates = _daterange(args.start, args.end, step_days=args.step_days)
    mode = "commit" if args.commit else "dry-run"
    print(
        f"[backfill] {mode}: {len(as_of_dates)} as-of-date(s) from "
        f"{args.start} to {args.end} (step={args.step_days}d)"
    )

    if not args.commit:
        print(
            "[backfill] dry-run: not invoking compute_and_persist. Re-run with "
            "--commit to actually backfill."
        )
        for d in as_of_dates:
            print(f"[backfill] dry-run would replay as_of_date={d}")
        return 0

    total_rows = 0
    ok_dates = 0
    skipped_dates = 0
    db = SessionLocal()
    try:
        for d in as_of_dates:
            try:
                ref = compute_and_persist(
                    db,
                    as_of_date=d,
                    mode_override="shadow",
                )
            except Exception as exc:
                logger.warning(
                    "[backfill] compute_and_persist failed for as_of=%s: %s",
                    d,
                    exc,
                )
                skipped_dates += 1
                continue
            if ref is None:
                skipped_dates += 1
                print(
                    f"[backfill] as_of={d}: service returned None "
                    "(mode off or no data)"
                )
                continue
            ok_dates += 1
            total_rows += int(ref.cells_persisted)
            print(
                f"[backfill] as_of={d} ledger_run_id={ref.ledger_run_id} "
                f"cells_persisted={ref.cells_persisted}"
            )
    finally:
        db.close()

    print(
        f"[backfill] done: ok_dates={ok_dates} skipped_dates={skipped_dates} "
        f"total_cells_persisted={total_rows}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
