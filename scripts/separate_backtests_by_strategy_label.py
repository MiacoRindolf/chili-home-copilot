#!/usr/bin/env python3
"""Re-home backtests so Pattern Evidence only sees rows for the pattern the run actually used.

Problem: migration 084 aligned ``trading_backtests.scan_pattern_id`` to ``TradingInsight.scan_pattern_id``
without updating ``strategy_name``. The UI filters by ``scan_pattern_id``, so mixed **Strategy** labels
appear under one card. This script moves each conflicting row to the ``ScanPattern`` whose **name**
matches the stored ``strategy_name`` (with truncation / prefix matching), sets ``strategy_name`` to
that canonical name (100 chars), patches ``params.data_provenance.scan_pattern_id``, and updates
``trading_pattern_trades``. If no pattern exists, ``--create-missing`` inserts an inactive stub row.

Usage (repo root, ``conda activate chili-env``):

  python scripts/separate_backtests_by_strategy_label.py
  python scripts/separate_backtests_by_strategy_label.py --apply
  python scripts/separate_backtests_by_strategy_label.py --apply --create-missing

Optional: ``--only-scan-pattern-id N`` limits to rows whose **current** ``scan_pattern_id`` is N
(useful to clean one card after backup).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_BT_STRATEGY_LEN = 100
_SP_NAME_LEN = 120
_MIN_PREFIX_LEN = 14  # avoid matching "BB Squeeze" to many rows


def _names_align(row_strategy: str | None, pattern_name: str | None) -> bool:
    if not pattern_name or not row_strategy:
        return False
    spn = str(pattern_name).strip()
    rsn = str(row_strategy).strip()
    if not spn or not rsn:
        return False
    if rsn == spn:
        return True
    if len(spn) > _BT_STRATEGY_LEN and rsn == spn[:_BT_STRATEGY_LEN]:
        return True
    if len(rsn) > _BT_STRATEGY_LEN and spn == rsn[:_BT_STRATEGY_LEN]:
        return True
    return False


def _resolve_target_pattern_id(session, strategy_name: str, *, create_missing: bool) -> tuple[Any, str | None]:
    """Return (ScanPattern or None, reason)."""
    from app.models.trading import ScanPattern

    rsn = (strategy_name or "").strip()
    if not rsn:
        return None, "empty_strategy"

    exact = session.query(ScanPattern).filter(ScanPattern.name == rsn).order_by(ScanPattern.id).all()
    if len(exact) >= 1:
        return exact[0], "exact_name" if len(exact) == 1 else f"exact_name_dup_pick_min:{len(exact)}"

    if len(rsn) == _BT_STRATEGY_LEN:
        hits = session.query(ScanPattern).filter(ScanPattern.name.like(f"{rsn}%")).all()
        if len(hits) == 1:
            return hits[0], "prefix_100"
        if len(hits) > 1:
            return None, f"ambiguous_prefix_100:{len(hits)}"

    if len(rsn) >= _MIN_PREFIX_LEN:
        hits = list(
            session.query(ScanPattern).filter(ScanPattern.name.like(f"{rsn}%")).all()
        )
        if len(hits) == 1:
            return hits[0], "prefix_unique"
        if len(hits) > 1:
            return None, f"ambiguous_prefix:{len(hits)}"

    if not create_missing:
        return None, "no_match"

    name_store = rsn[:_SP_NAME_LEN]
    sp = ScanPattern(
        name=name_store,
        description="Auto-created: backtest label had no matching ScanPattern row.",
        rules_json={},
        origin="separate_backtests_by_strategy_label",
        active=False,
        promotion_status="legacy",
        lifecycle_stage="candidate",
    )
    session.add(sp)
    session.flush()
    return sp, "created_stub"


def _patch_params_provenance(bt_params: Any, new_sp_id: int) -> dict:
    if bt_params is None:
        return {"data_provenance": {"scan_pattern_id": int(new_sp_id)}}
    if isinstance(bt_params, str):
        try:
            raw = json.loads(bt_params) if bt_params.strip() else {}
        except (json.JSONDecodeError, TypeError):
            raw = {}
        d = raw if isinstance(raw, dict) else {}
    elif isinstance(bt_params, dict):
        d = dict(bt_params)
    else:
        d = {}
    dp = d.get("data_provenance")
    if not isinstance(dp, dict):
        dp = {}
    dp = dict(dp)
    dp["scan_pattern_id"] = int(new_sp_id)
    d["data_provenance"] = dp
    return d


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Commit (default dry-run)")
    p.add_argument(
        "--create-missing",
        action="store_true",
        help="Insert inactive ScanPattern stub when label matches nothing",
    )
    p.add_argument(
        "--only-scan-pattern-id",
        type=int,
        default=None,
        help="Only process backtests whose current scan_pattern_id equals this",
    )
    p.add_argument("--limit", type=int, default=0, help="Max rows to change (0 = no limit)")
    args = p.parse_args()

    from app.db import SessionLocal
    from app.models.trading import BacktestResult, PatternTradeRow, ScanPattern

    session = SessionLocal()
    n_seen = 0
    n_skip_aligned = 0
    n_skip_resolve = 0
    n_plan = 0
    n_applied = 0
    samples: list[str] = []
    try:
        q = session.query(BacktestResult).filter(BacktestResult.scan_pattern_id.isnot(None))
        if args.only_scan_pattern_id is not None:
            q = q.filter(BacktestResult.scan_pattern_id == int(args.only_scan_pattern_id))
        for bt in q.yield_per(400):
            n_seen += 1
            cur = session.get(ScanPattern, int(bt.scan_pattern_id))
            if cur is None:
                continue
            if _names_align(bt.strategy_name, cur.name):
                n_skip_aligned += 1
                continue

            tgt, reason = _resolve_target_pattern_id(
                session,
                bt.strategy_name or "",
                create_missing=bool(args.create_missing and args.apply),
            )
            if tgt is None or reason and str(reason).startswith("ambiguous"):
                n_skip_resolve += 1
                if len(samples) < 5 and reason:
                    samples.append(f"skip bt={bt.id} strat={str(bt.strategy_name)[:50]!r} -> {reason}")
                continue
            new_id = int(tgt.id)
            if new_id == int(bt.scan_pattern_id):
                n_skip_aligned += 1
                continue

            canon = (tgt.name or "").strip()[:_BT_STRATEGY_LEN]
            n_plan += 1
            if len(samples) < 25:
                samples.append(
                    f"bt id={bt.id} {bt.ticker!r} fk {bt.scan_pattern_id}->{new_id} ({reason}) "
                    f"strat {str(bt.strategy_name)[:40]!r} => {canon[:40]!r}"
                )

            if args.apply:
                if args.limit and n_applied >= args.limit:
                    continue
                n_applied += 1
                bt.scan_pattern_id = new_id
                bt.strategy_name = canon
                bt.params = _patch_params_provenance(bt.params, new_id)
                session.query(PatternTradeRow).filter(
                    PatternTradeRow.backtest_result_id == bt.id
                ).update({PatternTradeRow.scan_pattern_id: new_id}, synchronize_session=False)

        print(f"Scanned backtest rows: {n_seen}")
        print(f"Already aligned (strategy matches current ScanPattern.name): {n_skip_aligned}")
        print(f"Unresolved (ambiguous / no match / empty): {n_skip_resolve}")
        print(f"Planned moves: {n_plan}")
        for s in samples[:20]:
            print(f"  {s}")
        if len(samples) > 20:
            print(f"  ... ({len(samples) - 20} more sample lines truncated)")
        if n_skip_resolve and not samples:
            print("  (no samples; increase logic or use --create-missing)")

        if not args.apply:
            print("\n[dry-run] No writes. Re-run with --apply [--create-missing].")
            session.rollback()
            return 0

        if args.limit and n_applied >= args.limit and n_plan > n_applied:
            print(f"\nStopped applying after --limit {args.limit} (planned conflicts total: {n_plan}).")
        session.commit()
        print(
            f"\nCommitted: relabeled/moved {n_applied} backtests "
            f"(+ pattern_trade rows, + stubs if any). Planned: {n_plan}."
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
