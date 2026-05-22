"""Read-only AutoTrader quality funnel report.

Use this before changing scan cadence, caps, sizing, or promotion policy. It
answers the practical question: are we short on signals, short on certified
patterns, or losing qualified signals to execution/risk gates?
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CHILI_APP_NAME", "chili-trade-quality-funnel")

from app.db import SessionLocal  # noqa: E402


def _rows(sql: str, params: dict) -> list[dict]:
    with SessionLocal() as db:
        return [dict(row._mapping) for row in db.execute(text(sql), params).fetchall()]


def _print_table(title: str, rows: Iterable[dict]) -> None:
    rows = list(rows)
    print(f"\n## {title}")
    if not rows:
        print("(no rows)")
        return
    keys = list(rows[0].keys())
    widths = {k: max(len(k), *(len(str(r.get(k, ""))) for r in rows)) for k in keys}
    print(" | ".join(k.ljust(widths[k]) for k in keys))
    print("-+-".join("-" * widths[k] for k in keys))
    for row in rows:
        print(" | ".join(str(row.get(k, "")).ljust(widths[k]) for k in keys))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="lookback window for alerts/runs")
    parser.add_argument("--trade-days", type=int, default=30, help="lookback window for realized trades")
    parser.add_argument("--limit", type=int, default=30, help="rows per section")
    args = parser.parse_args()
    params = {
        "days": max(1, int(args.days)),
        "trade_days": max(1, int(args.trade_days)),
        "limit": max(1, int(args.limit)),
    }

    _print_table(
        f"AutoTrader decisions, last {params['days']}d",
        _rows(
            """
            SELECT decision, reason, COUNT(*) AS n
            FROM trading_autotrader_runs
            WHERE created_at >= NOW() - (:days * INTERVAL '1 day')
            GROUP BY decision, reason
            ORDER BY n DESC
            LIMIT :limit
            """,
            params,
        ),
    )
    _print_table(
        f"Pattern-imminent alert supply by lifecycle, last {params['days']}d",
        _rows(
            """
            SELECT COALESCE(sp.lifecycle_stage, 'none') AS lifecycle_stage,
                   COALESCE(sp.active, FALSE) AS active,
                   COALESCE(sp.recert_required, FALSE) AS recert_required,
                   COUNT(*) AS alerts,
                   ROUND(AVG(a.score_at_alert)::numeric, 4) AS avg_score
            FROM trading_breakout_alerts a
            LEFT JOIN scan_patterns sp ON sp.id = a.scan_pattern_id
            WHERE a.alerted_at >= NOW() - (:days * INTERVAL '1 day')
              AND a.alert_tier = 'pattern_imminent'
            GROUP BY 1, 2, 3
            ORDER BY alerts DESC
            LIMIT :limit
            """,
            params,
        ),
    )
    _print_table(
        f"Live AutoTrader outcomes by pattern, last {params['trade_days']}d",
        _rows(
            """
            SELECT t.scan_pattern_id,
                   LEFT(COALESCE(sp.name, 'none'), 56) AS pattern_name,
                   COALESCE(sp.lifecycle_stage, 'none') AS lifecycle_stage,
                   COALESCE(sp.recert_required, FALSE) AS recert_required,
                   COUNT(*) AS trades,
                   COUNT(*) FILTER (WHERE t.status = 'closed') AS closed,
                   COUNT(*) FILTER (WHERE t.status = 'open') AS open,
                   ROUND(COALESCE(SUM(t.pnl) FILTER (WHERE t.status = 'closed'), 0)::numeric, 2) AS closed_pnl,
                   ROUND(AVG(t.pnl) FILTER (WHERE t.status = 'closed')::numeric, 2) AS avg_closed_pnl
            FROM trading_trades t
            LEFT JOIN scan_patterns sp ON sp.id = t.scan_pattern_id
            WHERE t.entry_date >= NOW() - (:trade_days * INTERVAL '1 day')
              AND COALESCE(t.auto_trader_version, '') = 'v1'
            GROUP BY 1, 2, 3, 4
            ORDER BY trades DESC
            LIMIT :limit
            """,
            params,
        ),
    )
    _print_table(
        "Pattern supply and evidence readiness",
        _rows(
            """
            SELECT lifecycle_stage,
                   active,
                   recert_required,
                   COUNT(*) AS patterns,
                   COUNT(*) FILTER (WHERE promotion_gate_passed IS TRUE) AS gate_passed,
                   COUNT(*) FILTER (WHERE quality_composite_score IS NOT NULL) AS quality_scored,
                   COUNT(*) FILTER (WHERE last_backtest_at < NOW() - INTERVAL '7 days') AS stale_7d
            FROM scan_patterns
            GROUP BY 1, 2, 3
            ORDER BY patterns DESC
            LIMIT :limit
            """,
            params,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
