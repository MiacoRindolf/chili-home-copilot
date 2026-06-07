"""One-shot reprocessor for orphaned momentum post-exit shake-out markers.

Background: the post-exit excursion labeler used to select sessions with
``updated_at >= now - 3h``. A terminal (closed/cancelled) session freezes its
``updated_at`` at exit, yet a marker can only be labeled AFTER its ~30min horizon
elapses — so any gap (scheduler restart, backlog) longer than that window left the
marker outside it FOREVER. The labeler now selects on the durable marker state
(``post_exit_excursion_pending.state == 'pending'``), so the in-process scheduler
re-picks orphaned markers on its own. This script forces that catch-up immediately
(e.g. right after deploy, or from the host against the live DB) instead of waiting
for the 2-minute scheduler cadence — and prints a before/after census so the
operator can confirm the orphaned markers were drained.

Usage:
  conda run -n chili-env python scripts/reprocess_post_exit_markers.py            # run + report
  conda run -n chili-env python scripts/reprocess_post_exit_markers.py --dry-run  # census only
  conda run -n chili-env python scripts/reprocess_post_exit_markers.py --passes 4 --sleep 10
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CHILI_APP_NAME", "chili-post-exit-reprocess")

from app.db import SessionLocal  # noqa: E402
from app.services.trading.momentum_neural.post_exit_excursion import (  # noqa: E402
    run_post_exit_excursion_pass,
)

_MARKER_PATH = "risk_snapshot_json->'momentum_live_execution'->'post_exit_excursion_pending'"

_CENSUS_SQL = text(
    f"""
    SELECT coalesce({_MARKER_PATH}->>'state', '(none)') AS marker_state, count(*) AS n
    FROM trading_automation_sessions
    WHERE mode = 'live'
      AND {_MARKER_PATH} IS NOT NULL
    GROUP BY 1
    ORDER BY 2 DESC
    """
)

_PENDING_SQL = text(
    f"SELECT count(*) FROM trading_automation_sessions "
    f"WHERE mode='live' AND {_MARKER_PATH}->>'state' = 'pending'"
)


def _census() -> dict[str, int]:
    with SessionLocal() as db:
        return {str(r.marker_state): int(r.n) for r in db.execute(_CENSUS_SQL).fetchall()}


def _pending_count() -> int:
    with SessionLocal() as db:
        return int(db.execute(_PENDING_SQL).scalar() or 0)


def _print_census(label: str) -> None:
    print(f"\n## {label} marker census (state -> count)")
    rows = _census()
    if not rows:
        print("(no markers)")
        return
    for state, n in rows.items():
        print(f"  {state:<14} {n}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--passes", type=int, default=3, help="max label passes to run (markers may need retries for OHLCV)")
    ap.add_argument("--sleep", type=float, default=5.0, help="seconds between passes")
    ap.add_argument("--dry-run", action="store_true", help="census only, do not label")
    args = ap.parse_args()

    _print_census("BEFORE")
    before = _pending_count()
    print(f"\npending markers before: {before}")

    if args.dry_run:
        print("\n[dry-run] not running the labeler.")
        return 0

    if before == 0:
        print("\nnothing pending — nothing to reprocess.")
        return 0

    for i in range(1, max(1, args.passes) + 1):
        with SessionLocal() as db:
            try:
                summary = run_post_exit_excursion_pass(db)
                db.commit()
            except Exception:  # noqa: BLE001 — surface, don't swallow
                db.rollback()
                raise
        print(f"\npass {i}: {summary}")
        remaining = _pending_count()
        print(f"pass {i}: pending remaining = {remaining}")
        if remaining == 0 or not summary.get("waiting") and not summary.get("errors"):
            # nothing left, or nothing transiently retriable — stop early
            if remaining == 0:
                break
        if i < args.passes:
            time.sleep(max(0.0, args.sleep))

    _print_census("AFTER")
    after = _pending_count()
    print(f"\npending markers after: {after}  (drained {before - after})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
