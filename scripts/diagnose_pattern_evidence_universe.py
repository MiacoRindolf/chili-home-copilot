"""
Diagnose why a Brain pattern card may show stock-only backtests despite a
crypto-themed title: mirrors ``_evidence_backtest_asset_universe`` +
``_compute_deduped_backtest_win_stats`` (sibling insights + asset filter).

Usage (project root, conda env chili-env):
  python scripts/diagnose_pattern_evidence_universe.py --insight-id 123
  python scripts/diagnose_pattern_evidence_universe.py --search "crypto rsi"
  python scripts/diagnose_pattern_evidence_universe.py --search "crypto rsi" --limit 5
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal
from app.models.trading import BacktestResult, ScanPattern, TradingInsight
from app.services.trading.backtest_engine import (
    _extract_context,
    effective_backtest_asset_universe,
)
from app.services.trading.market_data import is_crypto
from app.services.trading.pattern_resolution import resolve_scan_pattern_id_for_insight


def _sibling_insight_ids(
    db,
    primary_insight_id: int,
    scan_pattern_id: int | None,
    sp_resolved_id: int | None,
) -> list[int]:
    sid = scan_pattern_id or sp_resolved_id
    if not sid:
        return [primary_insight_id]
    ids = [
        r[0]
        for r in db.query(TradingInsight.id)
        .filter(TradingInsight.scan_pattern_id == sid)
        .all()
    ]
    if primary_insight_id not in ids:
        ids = [*ids, primary_insight_id]
    return ids


def _diagnose_one(db, ins: TradingInsight) -> None:
    desc = ins.pattern_description or ""
    scan_pid = getattr(ins, "scan_pattern_id", None)
    sp_resolved = resolve_scan_pattern_id_for_insight(db, ins)
    sibs = _sibling_insight_ids(db, ins.id, scan_pid, sp_resolved)

    ac = None
    sp_name = None
    if sp_resolved:
        sp = db.get(ScanPattern, int(sp_resolved))
        if sp:
            ac = getattr(sp, "asset_class", None)
            sp_name = sp.name

    ctx = _extract_context(desc, db=db, insight_id=ins.id)
    univ = effective_backtest_asset_universe(ac, ctx)

    print("---")
    print(f"TradingInsight.id={ins.id} scan_pattern_id={scan_pid} sp_resolved={sp_resolved}")
    print(f"ScanPattern.name={sp_name!r} asset_class(raw)={ac!r}")
    print(
        "context:",
        {
            "wants_crypto": ctx.get("wants_crypto"),
            "crypto_only": ctx.get("crypto_only"),
            "stock_only": ctx.get("stock_only"),
            "mentioned_tickers": ctx.get("mentioned_tickers"),
        },
    )
    print(f"effective_backtest_asset_universe (API evidence filter) -> {univ!r}")

    q = db.query(BacktestResult).filter(
        BacktestResult.related_insight_id.in_(sibs)
    )
    total = q.count()
    rows = q.all()
    n_crypto = sum(1 for bt in rows if is_crypto(bt.ticker or ""))
    n_stock = sum(1 for bt in rows if (bt.ticker or "") and not is_crypto(bt.ticker or ""))
    n_crypto_tr = sum(
        1 for bt in rows if is_crypto(bt.ticker or "") and (bt.trade_count or 0) > 0
    )
    n_stock_tr = sum(
        1
        for bt in rows
        if (bt.ticker or "") and not is_crypto(bt.ticker or "") and (bt.trade_count or 0) > 0
    )
    print(
        f"BacktestResult: total={total} sibling_insights={len(sibs)} "
        f"crypto_rows={n_crypto} stock_rows={n_stock} "
        f"with_trades_crypto={n_crypto_tr} with_trades_stock={n_stock_tr}"
    )
    if univ == "stocks" and n_crypto:
        print(
            f"  NOTE: {n_crypto} crypto row(s) exist but are HIDDEN in evidence (universe=stocks)."
        )
    snippet = desc.replace("\n", " ")[:140]
    print(f"description_snippet: {snippet!r}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--insight-id", type=int, default=None, help="TradingInsight PK")
    p.add_argument(
        "--search",
        type=str,
        default=None,
        help="ILIKE %%term%% on pattern_description (case-insensitive)",
    )
    p.add_argument("--limit", type=int, default=10, help="Max rows for --search")
    args = p.parse_args()

    if not args.insight_id and not args.search:
        p.error("Provide --insight-id and/or --search")

    db = SessionLocal()
    try:
        try:
            db.execute(text("SELECT 1"))
        except OperationalError as e:
            err = str(e).lower()
            if "too many clients" in err:
                print(
                    "PostgreSQL refused the connection (too many clients). "
                    "Stop other app/worker processes or raise max_connections, then retry.",
                    file=sys.stderr,
                )
            else:
                print(f"Database connection failed: {e}", file=sys.stderr)
            return 2

        if args.insight_id:
            ins = db.get(TradingInsight, args.insight_id)
            if not ins:
                print(f"No TradingInsight id={args.insight_id}")
                return 1
            _diagnose_one(db, ins)

        if args.search:
            term = f"%{args.search.strip()}%"
            found = (
                db.query(TradingInsight)
                .filter(TradingInsight.pattern_description.ilike(term))
                .order_by(TradingInsight.id.desc())
                .limit(max(1, args.limit))
                .all()
            )
            print(f"search {term!r}: {len(found)} insight(s)")
            for ins in found:
                if args.insight_id and ins.id == args.insight_id:
                    continue
                _diagnose_one(db, ins)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
