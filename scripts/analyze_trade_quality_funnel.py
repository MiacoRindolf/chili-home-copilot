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
from app.config import settings  # noqa: E402

FAST_BACKTEST_BATCH_ENV = "CHILI_BRAIN_FAST_BACKTEST_BATCH"
UNKNOWN_BATCH = "unset"


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


def _queue_health_rows() -> list[dict]:
    with SessionLocal() as db:
        from app.services.trading.backtest_queue import get_queue_status

        queue = get_queue_status(db, use_cache=False)
    batch_raw = os.environ.get(FAST_BACKTEST_BATCH_ENV, UNKNOWN_BATCH)
    try:
        batch_size = int(batch_raw)
    except (TypeError, ValueError):
        batch_size = None
    pending = int(queue.get("pending") or 0)
    return [{
        **queue,
        "fast_backtest_batch_env": batch_raw,
        "disabled_with_pending": bool(
            pending > 0 and batch_size is not None and batch_size <= 0
        ),
        "backtest_mode_default_batch": getattr(
            settings,
            "brain_fast_backtest_batch_backtest",
            None,
        ),
        "lean_cycle_default_batch": getattr(
            settings,
            "brain_fast_backtest_batch_lean_cycle",
            None,
        ),
    }]


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
        f"AutoTrader decisions by asset/lifecycle/lane, last {params['days']}d",
        _rows(
            """
            SELECT COALESCE(a.asset_type, 'unknown') AS asset_type,
                   COALESCE(sp.lifecycle_stage, 'none') AS lifecycle_stage,
                   COALESCE(
                       a.indicator_snapshot->'imminent_scorecard'->>'signal_lane',
                       'none'
                   ) AS signal_lane,
                   ar.decision,
                   COALESCE(ar.reason, 'none') AS reason,
                   COUNT(*) AS n
            FROM trading_autotrader_runs ar
            LEFT JOIN trading_breakout_alerts a ON a.id = ar.breakout_alert_id
            LEFT JOIN scan_patterns sp ON sp.id = COALESCE(ar.scan_pattern_id, a.scan_pattern_id)
            WHERE ar.created_at >= NOW() - (:days * INTERVAL '1 day')
            GROUP BY 1, 2, 3, 4, 5
            ORDER BY n DESC
            LIMIT :limit
            """,
            params,
        ),
    )
    _print_table(
        f"Stock managed-exit overlay on edge skips, last {params['days']}d",
        _rows(
            """
            SELECT COALESCE(
                       ar.rule_snapshot->'entry_edge'->'managed_exit_edge'
                           ->'geometry'->>'reason',
                       ar.rule_snapshot->'entry_edge'->'managed_exit_edge'
                           ->>'selection_reason',
                       'missing_managed_snapshot'
                   ) AS managed_reason,
                   COUNT(*) AS n,
                   ROUND(MAX(
                       NULLIF(
                           ar.rule_snapshot->'entry_edge'->>'expected_net_pct',
                           ''
                       )::numeric
                   ), 4) AS max_full_bracket_expected_net_pct,
                   ROUND(MAX(
                       NULLIF(
                           ar.rule_snapshot->'entry_edge'->'managed_exit_edge'
                               ->>'expected_net_pct',
                           ''
                       )::numeric
                   ), 4) AS max_managed_expected_net_pct,
                   MAX(ar.created_at) AS latest_created_at
            FROM trading_autotrader_runs ar
            LEFT JOIN trading_breakout_alerts a ON a.id = ar.breakout_alert_id
            WHERE ar.created_at >= NOW() - (:days * INTERVAL '1 day')
              AND ar.reason = 'non_positive_expected_edge'
              AND COALESCE(a.asset_type, 'stock') = 'stock'
            GROUP BY 1
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
        f"Positive-edge stock blocks by legacy price cap, last {params['days']}d",
        _rows(
            """
            WITH cap_blocks AS (
                SELECT ar.ticker,
                       COALESCE(sp.lifecycle_stage, 'none') AS lifecycle_stage,
                       COALESCE(
                           a.indicator_snapshot->'imminent_scorecard'->>'signal_lane',
                           'none'
                       ) AS signal_lane,
                       CASE
                           WHEN COALESCE(ar.rule_snapshot->>'current_price', '')
                                ~ '^-?[0-9]+([.][0-9]+)?$'
                           THEN (ar.rule_snapshot->>'current_price')::numeric
                       END AS current_price,
                       CASE
                           WHEN COALESCE(
                                   ar.rule_snapshot->'entry_edge'->>'expected_net_pct',
                                   ar.rule_snapshot->>'entry_edge_expected_net_pct',
                                   ''
                                ) ~ '^-?[0-9]+([.][0-9]+)?$'
                           THEN COALESCE(
                                   ar.rule_snapshot->'entry_edge'->>'expected_net_pct',
                                   ar.rule_snapshot->>'entry_edge_expected_net_pct'
                                )::numeric
                       END AS expected_net_pct,
                       ar.created_at
                FROM trading_autotrader_runs ar
                LEFT JOIN trading_breakout_alerts a ON a.id = ar.breakout_alert_id
                LEFT JOIN scan_patterns sp ON sp.id = COALESCE(ar.scan_pattern_id, a.scan_pattern_id)
                WHERE ar.created_at >= NOW() - (:days * INTERVAL '1 day')
                  AND COALESCE(a.asset_type, '') = 'stock'
                  AND ar.reason = 'symbol_price_above_cap'
            )
            SELECT ticker,
                   lifecycle_stage,
                   signal_lane,
                   COUNT(*) AS n,
                   ROUND(MAX(current_price), 2) AS max_px,
                   ROUND(MAX(expected_net_pct), 4) AS max_expected_net_pct,
                   MAX(created_at) AS latest_created_at
            FROM cap_blocks
            WHERE expected_net_pct > 0
            GROUP BY 1, 2, 3
            ORDER BY n DESC
            LIMIT :limit
            """,
            params,
        ),
    )
    _print_table(
        f"Shadow stock fastlane boosts, last {params['days']}d",
        _rows(
            """
            SELECT ar.ticker,
                   COALESCE(sp.lifecycle_stage, 'none') AS lifecycle_stage,
                   COALESCE(
                       a.indicator_snapshot->'imminent_scorecard'->>'signal_lane',
                       'none'
                   ) AS signal_lane,
                   COALESCE(
                       ar.rule_snapshot->'shadow_stock_fastlane'->>'reason',
                       'none'
                   ) AS fastlane_reason,
                   COUNT(*) AS n,
                   COUNT(*) FILTER (
                       WHERE COALESCE(
                           ar.rule_snapshot->'shadow_stock_fastlane'->>'queued',
                           'false'
                       ) = 'true'
                   ) AS queued,
                   MAX(
                       NULLIF(
                           ar.rule_snapshot->'shadow_stock_fastlane'->>'priority',
                           ''
                       )::numeric
                   ) AS max_priority,
                   ROUND(
                       MAX(
                           NULLIF(
                               ar.rule_snapshot->'shadow_stock_fastlane'
                               ->>'expected_net_pct',
                               ''
                           )::numeric
                       ),
                       4
                   ) AS max_expected_net_pct,
                   MAX(ar.created_at) AS latest_created_at
            FROM trading_autotrader_runs ar
            LEFT JOIN trading_breakout_alerts a ON a.id = ar.breakout_alert_id
            LEFT JOIN scan_patterns sp ON sp.id = COALESCE(ar.scan_pattern_id, a.scan_pattern_id)
            WHERE ar.created_at >= NOW() - (:days * INTERVAL '1 day')
              AND ar.rule_snapshot ? 'shadow_stock_fastlane'
            GROUP BY 1, 2, 3, 4
            ORDER BY queued DESC, n DESC
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
    _print_table("Backtest queue health", _queue_health_rows())
    _print_table(
        "Recert backlog by status/source",
        _rows(
            """
            SELECT COALESCE(status, 'none') AS status,
                   COALESCE(source, 'none') AS source,
                   COUNT(*) AS n,
                   MIN(observed_at) AS oldest_observed_at,
                   MAX(observed_at) AS newest_observed_at
            FROM trading_pattern_recert_log
            WHERE observed_at >= NOW() - (:trade_days * INTERVAL '1 day')
               OR status IN ('proposed', 'dispatched')
            GROUP BY 1, 2
            ORDER BY n DESC
            LIMIT :limit
            """,
            params,
        ),
    )
    _print_table(
        "Paper shadow capacity",
        _rows(
            """
            SELECT COUNT(*) FILTER (WHERE status = 'open') AS open_total,
                   COUNT(*) FILTER (
                       WHERE status = 'open'
                         AND (
                             paper_shadow_of_alert_id IS NOT NULL
                             OR COALESCE(signal_json, '{}'::jsonb)
                                @> '{"paper_shadow": true}'::jsonb
                         )
                   ) AS open_shadow,
                   :shadow_max_open AS configured_shadow_max_open
            FROM trading_paper_trades
            """,
            {
                **params,
                "shadow_max_open": int(
                    getattr(settings, "chili_autotrader_paper_shadow_max_open", 0)
                    or 0
                ),
            },
        ),
    )
    _print_table(
        f"Coinbase cap blocks, last {params['days']}d",
        _rows(
            """
            SELECT reason,
                   COUNT(*) AS n,
                   MAX(created_at) AS latest_created_at
            FROM trading_autotrader_runs
            WHERE created_at >= NOW() - (:days * INTERVAL '1 day')
              AND reason LIKE 'coinbase_cap:%'
            GROUP BY reason
            ORDER BY n DESC
            LIMIT :limit
            """,
            params,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
