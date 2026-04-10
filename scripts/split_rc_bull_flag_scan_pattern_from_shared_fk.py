#!/usr/bin/env python3
"""Split legacy shared ``scan_pattern_id`` (e.g. 93) into a dedicated row for RC BOS-wide.

Some insights were migrated onto the same FK as an older generic bull-flag pattern. This script
creates (or reuses) a ``ScanPattern`` named ``RC Bull Flag Breakout [BOS-wide]``, copies DSL/state
from the source pattern, and moves matching ``TradingInsight`` / ``BacktestResult`` / ``PatternTradeRow``
rows to the new id.

Usage (repo root, ``conda activate chili-env``):

  python scripts/split_rc_bull_flag_scan_pattern_from_shared_fk.py
  python scripts/split_rc_bull_flag_scan_pattern_from_shared_fk.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RC_NAME = "RC Bull Flag Breakout [BOS-wide]"
# Insights whose description is the RC BOS-wide card (not the generic bull-flag line).
INSIGHT_DESC_PREFIX = "RC Bull Flag Breakout [BOS-wide]"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-id", type=int, default=93, help="Shared legacy scan_patterns.id")
    p.add_argument("--apply", action="store_true", help="Commit changes")
    args = p.parse_args()

    from sqlalchemy.orm.attributes import flag_modified

    from app.db import SessionLocal
    from app.models.trading import (
        BacktestResult,
        PatternTradeRow,
        ScanPattern,
        TradingInsight,
    )

    session = SessionLocal()
    try:
        src = session.get(ScanPattern, int(args.source_id))
        if src is None:
            print(f"ERROR: source ScanPattern id={args.source_id} not found")
            return 1

        existing = (
            session.query(ScanPattern).filter(ScanPattern.name == RC_NAME).first()
        )
        if existing:
            new_sp = existing
            print(f"Reusing existing ScanPattern id={new_sp.id} name={RC_NAME!r}")
        else:
            new_sp = ScanPattern(
                name=RC_NAME[:120],
                description=(
                    f"Split from scan_patterns.id={args.source_id} "
                    f"({(src.name or '')[:80]}) — dedicated row for RC BOS-wide insights."
                ),
                rules_json=dict(src.rules_json) if src.rules_json else {},
                origin="split_shared_fk",
                asset_class=src.asset_class,
                timeframe=src.timeframe,
                confidence=src.confidence,
                active=src.active,
                parent_id=int(args.source_id),
                variant_label="BOS-wide",
                generation=max(0, int(src.generation or 0)),
                ticker_scope=src.ticker_scope,
                scope_tickers=src.scope_tickers,
                promotion_status=src.promotion_status,
                lifecycle_stage=src.lifecycle_stage,
                hypothesis_family=src.hypothesis_family,
            )
            session.add(new_sp)
            session.flush()
            print(f"Created ScanPattern id={new_sp.id} name={RC_NAME!r} parent_id={args.source_id}")

        new_id = int(new_sp.id)

        ins_q = session.query(TradingInsight).filter(
            TradingInsight.scan_pattern_id == int(args.source_id),
            TradingInsight.pattern_description.like(f"{INSIGHT_DESC_PREFIX}%"),
        )
        ins_rows = ins_q.all()
        ins_ids = [int(r.id) for r in ins_rows]
        print(f"Insights to move: {len(ins_ids)} {ins_ids[:20]}{'...' if len(ins_ids) > 20 else ''}")

        bt_q = session.query(BacktestResult).filter(
            BacktestResult.scan_pattern_id == int(args.source_id),
            BacktestResult.strategy_name == RC_NAME,
        )
        bt_rows = bt_q.all()
        bt_ids = [int(r.id) for r in bt_rows]
        print(f"Backtests to move (strategy_name match): {len(bt_ids)}")

        if not args.apply:
            session.rollback()
            print("\n[dry-run] No writes. Re-run with --apply.")
            return 0

        for ins in ins_rows:
            ins.scan_pattern_id = new_id
        for bt in bt_rows:
            bt.scan_pattern_id = new_id
            if bt.params and isinstance(bt.params, dict):
                dp = bt.params.get("data_provenance")
                if isinstance(dp, dict):
                    dp = dict(dp)
                    dp["scan_pattern_id"] = new_id
                    bt.params = dict(bt.params)
                    bt.params["data_provenance"] = dp
                    flag_modified(bt, "params")

        session.query(PatternTradeRow).filter(
            PatternTradeRow.backtest_result_id.in_(bt_ids)
        ).update({PatternTradeRow.scan_pattern_id: new_id}, synchronize_session=False)
        session.query(PatternTradeRow).filter(
            PatternTradeRow.related_insight_id.in_(ins_ids)
        ).update({PatternTradeRow.scan_pattern_id: new_id}, synchronize_session=False)

        session.commit()
        print(
            f"Committed: moved {len(ins_ids)} insights, {len(bt_ids)} backtests to scan_pattern_id={new_id}."
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
