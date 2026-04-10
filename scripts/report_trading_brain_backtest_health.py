#!/usr/bin/env python3
"""Summarize trading backtest row health: duplicates, label alignment, params completeness.

Run from repo root with ``conda activate chili-env``:

  python scripts/report_trading_brain_backtest_health.py
  python scripts/report_trading_brain_backtest_health.py --scan-pattern-id 205
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.trading.scan_pattern_label_alignment import (
    strategy_label_aligns_scan_pattern_name,
)


def _params_complete(p_raw) -> tuple[bool, bool]:
    """(has_top_window, has_dp_window) — chart_time on top-level vs data_provenance only."""
    if not p_raw:
        return False, False
    try:
        p = json.loads(p_raw) if isinstance(p_raw, str) else dict(p_raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False, False
    if not isinstance(p, dict):
        return False, False
    top = p.get("chart_time_from") is not None and p.get("chart_time_to") is not None
    dp = p.get("data_provenance")
    dpf = (
        isinstance(dp, dict)
        and dp.get("chart_time_from") is not None
        and dp.get("chart_time_to") is not None
    )
    return top, dpf


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan-pattern-id", type=int, default=None)
    args = ap.parse_args()

    from app.db import SessionLocal
    from app.models.trading import BacktestResult, ScanPattern, TradingInsight

    db = SessionLocal()
    try:
        bt_q = db.query(BacktestResult).filter(BacktestResult.scan_pattern_id.isnot(None))
        if args.scan_pattern_id is not None:
            bt_q = bt_q.filter(BacktestResult.scan_pattern_id == int(args.scan_pattern_id))

        by_natural_key: dict[tuple[int, int, str, str], int] = defaultdict(int)
        mislabel = 0
        n = 0
        top_win = 0
        dp_only_win = 0
        neither_win = 0

        for bt in bt_q.yield_per(800):
            n += 1
            spid = int(bt.scan_pattern_id or 0)
            iid = int(bt.related_insight_id or 0)
            tk = (bt.ticker or "").strip()
            strat = bt.strategy_name or ""
            by_natural_key[(spid, iid, tk, strat)] += 1

            sp = db.get(ScanPattern, spid)
            spn = (sp.name if sp else "") or ""
            if spn and not strategy_label_aligns_scan_pattern_name(strat, spn):
                mislabel += 1

            tw, dw = _params_complete(bt.params)
            if tw:
                top_win += 1
            elif dw:
                dp_only_win += 1
            else:
                neither_win += 1

        dup_groups = sum(1 for c in by_natural_key.values() if c > 1)
        dup_rows = sum(c for c in by_natural_key.values() if c > 1)

        print(f"Backtest rows scanned: {n}")
        if args.scan_pattern_id is not None:
            print(f"Filter scan_pattern_id={args.scan_pattern_id}")
        print(f"strategy_name vs ScanPattern.name mismatches: {mislabel}")
        print(
            "params chart window: "
            f"top-level={top_win}, data_provenance_only={dp_only_win}, missing_both={neither_win}"
        )
        print(
            "duplicate keys (scan_pattern_id, related_insight_id, ticker, strategy_name) "
            f"count>1: {dup_groups} keys, {dup_rows} rows"
        )

        if args.scan_pattern_id is not None:
            ins_n = (
                db.query(TradingInsight)
                .filter(TradingInsight.scan_pattern_id == int(args.scan_pattern_id))
                .count()
            )
            print(f"TradingInsight rows with this scan_pattern_id: {ins_n}")

    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
