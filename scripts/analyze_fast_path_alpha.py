"""Fast-path alpha and live-readiness report.

Read-only diagnostic for the ultra-short-horizon Coinbase fast lane. It
answers the operator question that matters before flipping live:

* Are paper exits actually profitable by ticker/signal class?
* Which decay buckets clear realistic round-trip cost?
* Are maker fills suffering adverse selection?
* What is blocking execution right now?

Usage:
    python scripts/analyze_fast_path_alpha.py --days 7 --min-samples 30
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("CHILI_APP_NAME", "chili-fast-path-alpha-report")

from app.db import SessionLocal


@dataclass(frozen=True)
class Params:
    days: int
    min_samples: int
    limit: int


def _rows(db, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(text(sql), params).mappings().all()]


def _print_table(title: str, rows: list[dict[str, Any]], *, limit: int | None = None) -> None:
    print(f"\n== {title} ==")
    if not rows:
        print("(none)")
        return
    view = rows[:limit] if limit else rows
    cols = list(view[0].keys())
    widths = {
        c: min(max(len(c), *(len(str(r.get(c, ""))) for r in view)), 36)
        for c in cols
    }
    print(" | ".join(c.ljust(widths[c]) for c in cols))
    print("-+-".join("-" * widths[c] for c in cols))
    for r in view:
        print(" | ".join(str(r.get(c, ""))[: widths[c]].ljust(widths[c]) for c in cols))


def _load_cost_context(db) -> dict[str, float | str]:
    try:
        from app.services.trading.fast_path.settings import load as load_fp_settings

        fp = load_fp_settings()
        execution_mode = str(getattr(fp, "execution_mode", "taker") or "taker")
        if execution_mode in ("maker_only", "maker_first_then_taker"):
            fee_bps = float(getattr(fp, "cost_aware_maker_fee_bps", 0.0) or 0.0)
        else:
            fee_bps = float(getattr(fp, "cost_aware_taker_fee_bps", 0.0) or 0.0)
    except Exception:
        execution_mode = "unknown"
        fee_bps = 0.0
    spread_row = db.execute(text("""
        SELECT AVG(spread_at_placement_bps) AS spread_bps
        FROM fast_path_maker_attempts
        WHERE placed_at >= NOW() - INTERVAL '7 days'
          AND spread_at_placement_bps IS NOT NULL
    """)).mappings().first()
    spread_bps = float((spread_row or {}).get("spread_bps") or 0.0)
    return {
        "execution_mode": execution_mode,
        "fee_bps": fee_bps,
        "spread_bps": spread_bps,
        "round_trip_cost_bps": 2.0 * (fee_bps + spread_bps),
    }


def build_report(params: Params) -> dict[str, Any]:
    db = SessionLocal()
    try:
        cost = _load_cost_context(db)
        decay_table = (
            "fast_signal_decay_maker_filled"
            if str(cost["execution_mode"]) in ("maker_only", "maker_first_then_taker")
            else "fast_signal_decay"
        )
        decay = _rows(db, f"""
            SELECT ticker, alert_type, score_bucket, horizon_s,
                   sample_count,
                   ROUND((mean_return * 10000.0)::numeric, 4) AS mean_bps,
                   ROUND(((mean_return * 10000.0) - :cost_bps)::numeric, 4) AS net_bps
            FROM {decay_table}
            WHERE sample_count >= :min_samples
            ORDER BY net_bps DESC, sample_count DESC
            LIMIT :limit
        """, {
            "min_samples": params.min_samples,
            "cost_bps": float(cost["round_trip_cost_bps"]),
            "limit": params.limit,
        })
        exits = _rows(db, """
            SELECT e.ticker, e.alert_type, COUNT(*) AS exits,
                   ROUND(SUM(x.realized_pnl_usd)::numeric, 4) AS pnl_usd,
                   ROUND(AVG(x.realized_return_pct)::numeric, 4) AS avg_ret_pct,
                   ROUND((100.0 * COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0)
                          / NULLIF(COUNT(*), 0))::numeric, 2) AS win_rate_pct
            FROM fast_exits x
            JOIN fast_executions e ON e.id = x.entry_execution_id
            WHERE x.exited_at >= NOW() - (:days * INTERVAL '1 day')
            GROUP BY e.ticker, e.alert_type
            ORDER BY pnl_usd DESC NULLS LAST, exits DESC
            LIMIT :limit
        """, {"days": params.days, "limit": params.limit})
        rejects = _rows(db, """
            SELECT reject_reason, COUNT(*) AS n, MAX(decided_at) AS latest
            FROM fast_executions
            WHERE decided_at >= NOW() - (:days * INTERVAL '1 day')
              AND decision = 'rejected'
            GROUP BY reject_reason
            ORDER BY n DESC
            LIMIT :limit
        """, {"days": params.days, "limit": params.limit})
        maker = _rows(db, """
            SELECT ticker, fill_outcome, COUNT(*) AS attempts,
                   ROUND(AVG(mid_drift_bps)::numeric, 4) AS avg_mid_drift_bps,
                   ROUND(AVG(spread_at_placement_bps)::numeric, 4) AS avg_spread_bps
            FROM fast_path_maker_attempts
            WHERE placed_at >= NOW() - (:days * INTERVAL '1 day')
            GROUP BY ticker, fill_outcome
            ORDER BY ticker, fill_outcome
        """, {"days": params.days})
        viable = [
            r for r in decay
            if float(r.get("net_bps") or 0.0) > 0.0
            and not str(r.get("alert_type") or "").endswith("_short")
        ]
        realized_positive = [
            r for r in exits
            if float(r.get("pnl_usd") or 0.0) > 0.0 and int(r.get("exits") or 0) >= 5
        ]
        verdict = "do_not_flip_live"
        if viable and realized_positive:
            verdict = "paper_canary_only"
        return {
            "params": params,
            "cost": cost,
            "decay_table": decay_table,
            "decay": decay,
            "exits": exits,
            "rejects": rejects,
            "maker": maker,
            "verdict": verdict,
            "viable_decay_rows": len(viable),
            "positive_realized_groups": len(realized_positive),
        }
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    params = Params(days=args.days, min_samples=args.min_samples, limit=args.limit)
    report = build_report(params)
    cost = report["cost"]
    print("Fast-path alpha report")
    print(
        "execution_mode={execution_mode} fee_bps={fee_bps:.4f} "
        "avg_spread_bps={spread_bps:.4f} round_trip_cost_bps={round_trip_cost_bps:.4f}".format(
            **cost
        )
    )
    print(
        "verdict={verdict} viable_decay_rows={viable_decay_rows} "
        "positive_realized_groups={positive_realized_groups} decay_table={decay_table}".format(
            **report
        )
    )
    _print_table("Realized fast exits", report["exits"])
    _print_table("Decay buckets after estimated cost", report["decay"])
    _print_table("Maker adverse-selection audit", report["maker"])
    _print_table("Recent fast-path rejects", report["rejects"])


if __name__ == "__main__":
    main()
