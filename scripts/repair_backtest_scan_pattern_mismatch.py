#!/usr/bin/env python3
"""Fix ``trading_backtests.scan_pattern_id`` when it disagrees with ``strategy_name``.

Queue/engine saves ``strategy_name`` from the run's pattern label; if ``scan_pattern_id`` was
wrongly aligned (repair bug, merged rows), Evidence UI shows mixed strategies under one card.
Correct the **data**: point each row at the ``ScanPattern`` whose ``name`` matches the stored
strategy (optionally create that pattern).

Does not change API filtering — run this after backups in production.

Usage (repo root, ``conda activate chili-env``):

  python scripts/repair_backtest_scan_pattern_mismatch.py --dry-run
  python scripts/repair_backtest_scan_pattern_mismatch.py --apply
  python scripts/repair_backtest_scan_pattern_mismatch.py --apply --create-missing

``--create-missing`` inserts a minimal ``scan_patterns`` row when no name match exists (empty
``rules_json``); you must edit rules or re-run mining later.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_BT_STRATEGY_LEN = 100
_SP_NAME_LEN = 120


def _row_matches_pattern_name(strategy_name: str | None, pattern_name: str | None) -> bool:
    if not pattern_name or not strategy_name:
        return False
    spn = str(pattern_name).strip()
    rsn = str(strategy_name).strip()
    if not spn or not rsn:
        return False
    if rsn == spn:
        return True
    if len(spn) > _BT_STRATEGY_LEN and rsn == spn[:_BT_STRATEGY_LEN]:
        return True
    if len(rsn) > _BT_STRATEGY_LEN and spn == rsn[:_BT_STRATEGY_LEN]:
        return True
    return False


def _resolve_target_pattern_id(db, strategy_name: str, *, create_missing: bool) -> tuple[int | None, str]:
    """Return (scan_pattern_id, reason). reason: ok_skip | updated | ambiguous | no_match | created."""
    from app.models.trading import ScanPattern

    rsn = (strategy_name or "").strip()
    if not rsn:
        return None, "empty_strategy"

    exact = db.query(ScanPattern).filter(ScanPattern.name == rsn).all()
    if len(exact) == 1:
        return int(exact[0].id), "exact_name"

    if len(exact) > 1:
        return None, f"ambiguous:{len(exact)}_rows_for_name"

    # Truncation: stored strategy may be first 100 chars of a longer pattern name
    if len(rsn) == _BT_STRATEGY_LEN:
        prefix_hits = (
            db.query(ScanPattern)
            .filter(ScanPattern.name.like(f"{rsn}%"))
            .all()
        )
        if len(prefix_hits) == 1:
            return int(prefix_hits[0].id), "prefix_100"
        if len(prefix_hits) > 1:
            return None, f"ambiguous_prefix:{len(prefix_hits)}"

    if not create_missing:
        return None, "no_match"

    name_store = rsn[:_SP_NAME_LEN]
    sp = ScanPattern(
        name=name_store,
        description=None,
        rules_json={},
        origin="repair_scan_pattern_mismatch",
        active=False,
        promotion_status="legacy",
        lifecycle_stage="candidate",
    )
    db.add(sp)
    db.flush()
    return int(sp.id), "created_stub"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Report only (default if neither flag)")
    p.add_argument("--apply", action="store_true", help="Commit updates")
    p.add_argument(
        "--create-missing",
        action="store_true",
        help="Create inactive ScanPattern stub when name not found",
    )
    args = p.parse_args()
    dry = args.dry_run or not args.apply

    from app.db import SessionLocal
    from app.models.trading import BacktestResult, PatternTradeRow, ScanPattern

    session = SessionLocal()
    try:
        rows = (
            session.query(BacktestResult)
            .filter(BacktestResult.scan_pattern_id.isnot(None))
            .order_by(BacktestResult.id)
            .all()
        )
        mismatches: list[BacktestResult] = []
        for bt in rows:
            sp = session.get(ScanPattern, int(bt.scan_pattern_id))
            if sp is None:
                continue
            if _row_matches_pattern_name(bt.strategy_name, sp.name):
                continue
            mismatches.append(bt)

        print(f"Backtest rows with scan_pattern_id set: {len(rows)}")
        print(f"Mismatched (strategy_name vs linked ScanPattern.name): {len(mismatches)}")
        if not mismatches:
            print("OK — no FK/strategy label mismatches.")
            return 0

        plan: list[tuple[BacktestResult, int, str]] = []
        skip = 0
        for bt in mismatches:
            tid, reason = _resolve_target_pattern_id(
                session, bt.strategy_name or "", create_missing=args.create_missing and not dry
            )
            if tid is None or reason.startswith("ambiguous"):
                print(f"  skip bt id={bt.id} ticker={bt.ticker!r} strategy={bt.strategy_name!r} -> {reason}")
                skip += 1
                continue
            if int(bt.scan_pattern_id) == tid:
                continue
            plan.append((bt, tid, reason))

        print(f"Planned reassignments: {len(plan)}  (skipped ambiguous/empty: {skip})")
        for bt, tid, reason in plan[:50]:
            print(f"  bt id={bt.id} {bt.ticker!r} {bt.strategy_name!r}  fk {bt.scan_pattern_id} -> {tid} ({reason})")
        if len(plan) > 50:
            print(f"  ... and {len(plan) - 50} more")

        if dry:
            print("\n[dry-run] No writes. Re-run with --apply to update.")
            session.rollback()
            return 0

        for bt, tid, _ in plan:
            bt.scan_pattern_id = tid
            session.query(PatternTradeRow).filter(
                PatternTradeRow.backtest_result_id == bt.id
            ).update({PatternTradeRow.scan_pattern_id: tid}, synchronize_session=False)
        session.commit()
        print(f"\nCommitted {len(plan)} trading_backtests.scan_pattern_id fixes (+ pattern_trade rows).")
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
