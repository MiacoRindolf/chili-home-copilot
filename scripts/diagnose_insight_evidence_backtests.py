#!/usr/bin/env python3
"""Fast consistency check: Pattern Evidence panel vs DB (no learning cycle).

Prints duplicate (insight, ticker, strategy) groups, panel row ids, and sample mismatches.
Run time: a few seconds (DB only).

Usage (repo root, conda env chili-env):
  python scripts/diagnose_insight_evidence_backtests.py 123
  python scripts/diagnose_insight_evidence_backtests.py 123 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models.trading import BacktestResult, TradingInsight  # noqa: E402
from app.services.trading.stored_backtest_rerun import (  # noqa: E402
    collect_evidence_listed_backtest_ids,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("insight_id", type=int, help="TradingInsight.id (Brain card)")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    args = p.parse_args()
    iid = int(args.insight_id)

    db = SessionLocal()
    try:
        ins = db.get(TradingInsight, iid)
        if not ins:
            print("ERROR: insight not found", file=sys.stderr)
            return 1

        panel_ids, err = collect_evidence_listed_backtest_ids(db, iid, limit=None)
        if err:
            print("ERROR:", err, file=sys.stderr)
            return 1

        rows_by_id = {
            r.id: r
            for r in db.query(BacktestResult).filter(BacktestResult.id.in_(panel_ids)).all()
        }
        missing = [x for x in panel_ids if x not in rows_by_id]

        dup_q = (
            db.query(
                BacktestResult.ticker,
                BacktestResult.strategy_name,
                func.count(BacktestResult.id),
            )
            .filter(BacktestResult.related_insight_id == iid)
            .group_by(BacktestResult.ticker, BacktestResult.strategy_name)
            .having(func.count(BacktestResult.id) > 1)
            .all()
        )
        dup_detail: list[dict] = []
        for t, s, n in dup_q[:25]:
            rows = (
                db.query(BacktestResult)
                .filter(
                    BacktestResult.related_insight_id == iid,
                    BacktestResult.ticker == t,
                    BacktestResult.strategy_name == s,
                )
                .order_by(BacktestResult.ran_at.desc().nullslast())
                .all()
            )
            dup_detail.append(
                {
                    "ticker": t,
                    "strategy_name": s,
                    "count": int(n),
                    "rows": [
                        {
                            "id": r.id,
                            "trade_count": r.trade_count,
                            "ran_at": r.ran_at.isoformat() if r.ran_at else None,
                            "return_pct": r.return_pct,
                        }
                        for r in rows
                    ],
                }
            )

        out = {
            "insight_id": iid,
            "scan_pattern_id": ins.scan_pattern_id,
            "panel_listed_count": len(panel_ids),
            "panel_ids_missing_from_db": missing,
            "duplicate_ticker_strategy_groups": [
                {"ticker": t, "strategy_name": s, "rows": int(n)} for t, s, n in dup_q
            ],
            "duplicate_row_details": dup_detail,
            "sample_panel_rows": [],
        }
        for pid in panel_ids[:15]:
            r = rows_by_id.get(pid)
            if not r:
                continue
            sn = r.strategy_name or ""
            out["sample_panel_rows"].append(
                {
                    "id": r.id,
                    "ticker": r.ticker,
                    "strategy_name": sn,
                    "strategy_len": len(sn),
                    "trade_count": r.trade_count,
                    "ran_at": r.ran_at.isoformat() if r.ran_at else None,
                }
            )

        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print(f"Insight {iid} scan_pattern_id={ins.scan_pattern_id}")
            print(f"Evidence panel lists {len(panel_ids)} deduped backtest row id(s)")
            if missing:
                print(f"  WARNING: {len(missing)} id(s) not in DB: {missing[:20]}")
            if dup_q:
                print(f"  WARNING: {len(dup_q)} duplicate (ticker, strategy) group(s) — run dedupe:")
                for t, s, n in dup_q[:20]:
                    print(f"    {n}x {t!r} / {s!r}")
                for block in dup_detail[:8]:
                    print(f"    --- {block['ticker']}: ids/trades/ran ---")
                    for r in block["rows"]:
                        print(f"        id={r['id']} trades={r['trade_count']} ran={r['ran_at']}")
            else:
                print("  No duplicate (ticker, strategy) rows for this insight.")
            print("Sample panel rows (first 15):")
            for line in out["sample_panel_rows"]:
                print(
                    f"  id={line['id']} {line['ticker']} trades={line['trade_count']} "
                    f"ran={line['ran_at']} strat_len={line['strategy_len']}"
                )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
