"""Thoroughly seed and grow intraday (1m / 5m / 15m) pattern coverage.

Run inside the chili container or with `chili-env` activated:
    python -m scripts.seed_intraday_patterns
    python -m scripts.seed_intraday_patterns --skip-backfill
    python -m scripts.seed_intraday_patterns --skip-mine
    python -m scripts.seed_intraday_patterns --intervals 5m,15m

Phases (each idempotent and skippable):
  1. Snapshot before-state: pattern counts by (timeframe, family) and bar counts
     by interval.
  2. Insert curated builtin intraday patterns (`_BUILTIN_INTRADAY_PATTERNS` in
     pattern_engine.py) via `seed_builtin_patterns()` -- safe to re-run.
  3. Backfill `MarketSnapshot` rows at 1m/5m/15m for the same 60-ticker set the
     intraday miner uses, IF current row counts are below the miner threshold
     (>=30 per interval). Shells out to `scripts/backfill_snapshot_bars.py`.
  4. Run `mine_intraday_patterns()` so organic discoveries land at intraday
     timeframes, then `evolve_patterns()` to prune/promote.
  5. Heuristic backfill of `hypothesis_family` on existing intraday rows whose
     name pattern hints at a known family. Leaves unmatched rows NULL.
  6. Snapshot after-state and print a diff.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from typing import Any

from sqlalchemy import func, or_

from app.config import settings
from app.db import SessionLocal
from app.models.trading import MarketSnapshot, ScanPattern
from app.services.trading.learning import (
    evolve_patterns,
    mine_intraday_patterns,
)
from app.services.trading.market_data import (
    DEFAULT_CRYPTO_TICKERS,
    DEFAULT_SCAN_TICKERS,
)
from app.services.trading.pattern_engine import seed_builtin_patterns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("seed_intraday_patterns")

# Miner gate: needs >=30 mined rows per interval before it emits a hypothesis.
MIN_BARS_PER_INTERVAL = 30

# Heuristic family backfill rules. Order matters -- first match wins.
_FAMILY_RULES: list[tuple[str, list[str]]] = [
    ("opening_range", ["%opening%", "%ORB%", "% drive%", "%opening drive%"]),
    ("liquidity_sweep", ["%liquidity sweep%", "%stop run%", "%stop-run%"]),
    ("compression_expansion", [
        "%squeeze%", "%NR4%", "%NR7%", "%narrow range%", "%coil%",
        "%inside bar%", "%tight range%", "%VCP%",
    ]),
    ("mean_reversion", [
        "%VWAP reclaim%", "%failed breakdown%", "%failed break%",
        "%mean reversion%", "%IBS%", "%pullback to vwap%",
    ]),
    ("momentum_continuation", [
        "%RSI%", "%MACD%", "%EMA%", "%momentum%", "%breakout%", "%cross%",
        "%continuation%", "%burst%", "%reclaim%", "%trend%",
    ]),
]


def _snapshot_state(db) -> dict[str, Any]:
    tf_counts = dict(
        db.query(ScanPattern.timeframe, func.count())
        .group_by(ScanPattern.timeframe)
        .all()
    )
    fam_counts = dict(
        db.query(ScanPattern.hypothesis_family, func.count())
        .group_by(ScanPattern.hypothesis_family)
        .all()
    )
    bar_counts = dict(
        db.query(MarketSnapshot.bar_interval, func.count())
        .group_by(MarketSnapshot.bar_interval)
        .all()
    )
    return {"timeframes": tf_counts, "families": fam_counts, "bars": bar_counts}


def _print_state(label: str, state: dict[str, Any]) -> None:
    log.info("=== %s ===", label)
    log.info("  patterns by timeframe: %s", state["timeframes"])
    log.info("  patterns by family:    %s", state["families"])
    log.info("  bars by interval:      %s", state["bars"])


def _intraday_tickers() -> list[str]:
    """Mirror the ticker selection inside `mine_intraday_patterns`."""
    return list(DEFAULT_CRYPTO_TICKERS)[:30] + list(DEFAULT_SCAN_TICKERS)[:30]


def _backfill_intervals(intervals: list[str], tickers: list[str], dry_run: bool) -> None:
    if not intervals:
        log.info("[backfill] no intervals need backfill -- skipping")
        return
    cmd = [
        sys.executable,
        os.path.join("scripts", "backfill_snapshot_bars.py"),
        "--tickers", ",".join(tickers),
        "--intervals", ",".join(intervals),
        "--incremental",
    ]
    if dry_run:
        cmd.append("--dry-run")
    log.info("[backfill] running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _backfill_families(db) -> int:
    """Heuristic UPDATE -- only touches rows where hypothesis_family IS NULL."""
    total_updated = 0
    for family, name_patterns in _FAMILY_RULES:
        clauses = [ScanPattern.name.ilike(p) for p in name_patterns]
        q = (
            db.query(ScanPattern)
            .filter(ScanPattern.hypothesis_family.is_(None))
            .filter(or_(*clauses))
        )
        rows = q.all()
        if not rows:
            continue
        for row in rows:
            row.hypothesis_family = family
        total_updated += len(rows)
        log.info("[family-backfill] %s -> %d rows", family, len(rows))
    if total_updated:
        db.commit()
    return total_updated


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--intervals", default=getattr(settings, "brain_intraday_intervals", "1m,5m,15m"),
        help="Comma-separated intraday intervals to backfill if under the miner threshold.",
    )
    ap.add_argument("--skip-backfill", action="store_true",
                    help="Don't shell out to backfill_snapshot_bars.py.")
    ap.add_argument("--skip-mine", action="store_true",
                    help="Skip the mine_intraday_patterns + evolve_patterns step.")
    ap.add_argument("--skip-family-backfill", action="store_true",
                    help="Skip the heuristic hypothesis_family UPDATE.")
    ap.add_argument("--dry-run-backfill", action="store_true",
                    help="Pass --dry-run to backfill_snapshot_bars.py.")
    args = ap.parse_args()

    intervals = [iv.strip() for iv in args.intervals.split(",") if iv.strip()]
    intervals = [iv for iv in intervals if iv != "1d"]

    db = SessionLocal()
    try:
        before = _snapshot_state(db)
        _print_state("BEFORE", before)

        # Phase 2: curated builtin seeds (idempotent).
        added = seed_builtin_patterns(db)
        log.info("[seed] seed_builtin_patterns added %d new patterns", added)

        # Phase 3: backfill bars only for intervals under the miner threshold.
        if not args.skip_backfill:
            need_backfill: list[str] = []
            for iv in intervals:
                n = before["bars"].get(iv, 0)
                if n < MIN_BARS_PER_INTERVAL:
                    log.info("[backfill] interval %s has %d rows (<%d) -- queue",
                             iv, n, MIN_BARS_PER_INTERVAL)
                    need_backfill.append(iv)
                else:
                    log.info("[backfill] interval %s has %d rows (>=%d) -- skip",
                             iv, n, MIN_BARS_PER_INTERVAL)
            if need_backfill:
                _backfill_intervals(need_backfill, _intraday_tickers(), args.dry_run_backfill)
        else:
            log.info("[backfill] --skip-backfill set")

        # Phase 4: organic mining + evolution.
        if not args.skip_mine:
            log.info("[mine] running mine_intraday_patterns ...")
            result = mine_intraday_patterns(db, user_id=None)
            log.info("[mine] mine_intraday_patterns -> %s", result)
            log.info("[evolve] running evolve_patterns ...")
            evolve_result = evolve_patterns(db)
            log.info("[evolve] evolve_patterns -> %s", evolve_result)
            db.commit()
        else:
            log.info("[mine] --skip-mine set")

        # Phase 5: heuristic family backfill.
        if not args.skip_family_backfill:
            n = _backfill_families(db)
            log.info("[family-backfill] updated %d existing rows", n)
        else:
            log.info("[family-backfill] --skip-family-backfill set")

        after = _snapshot_state(db)
        _print_state("AFTER", after)

        # Diff -- print the deltas that matter.
        log.info("=== DELTAS (timeframe) ===")
        all_tf = set(before["timeframes"]) | set(after["timeframes"])
        for tf in sorted(all_tf, key=lambda x: (x is None, str(x))):
            b = before["timeframes"].get(tf, 0)
            a = after["timeframes"].get(tf, 0)
            log.info("  %-6s  %d -> %d  (%+d)", str(tf), b, a, a - b)

        log.info("=== DELTAS (family) ===")
        all_fam = set(before["families"]) | set(after["families"])
        for fam in sorted(all_fam, key=lambda x: (x is None, str(x))):
            b = before["families"].get(fam, 0)
            a = after["families"].get(fam, 0)
            log.info("  %-25s  %d -> %d  (%+d)", str(fam), b, a, a - b)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
