"""Explain AutoTrader v1 candidate-pool starvation.

The hot tick only processes actionable unprocessed ``pattern_imminent`` alerts.
This report separates scanner supply, already-consumed supply, and stale or
session-deferred backlog so ``candidate_pool=0`` is not a mystery.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

from sqlalchemy import or_, text
from sqlalchemy.orm import aliased

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("CHILI_APP_NAME", "chili-autotrader-candidate-pool-report")

from app.db import SessionLocal
from app.models.trading import AutoTraderRun, BreakoutAlert
from app.services.trading.auto_trader import (
    _candidate_pool_zero_context,
    _non_stock_candidate_actionability_state,
    _stock_candidate_actionability_state,
    _stock_session_defer_state,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--lookback-minutes", type=int, default=120)
    parser.add_argument("--limit", type=int, default=20, help="rows per detail section")
    parser.add_argument("--output", type=Path, help="Optional path to also write the report")
    args = parser.parse_args()
    if args.output is not None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            _run_report(args)
        report = buf.getvalue()
        sys.stdout.write(report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        return
    _run_report(args)


def _run_report(args: argparse.Namespace) -> None:
    db = SessionLocal()
    try:
        limit = max(1, int(args.limit))
        ar = aliased(AutoTraderRun)
        pool = (
            db.query(BreakoutAlert)
            .outerjoin(ar, ar.breakout_alert_id == BreakoutAlert.id)
            .filter(
                BreakoutAlert.alert_tier == "pattern_imminent",
                or_(BreakoutAlert.user_id == args.user_id, BreakoutAlert.user_id.is_(None)),
                ar.id.is_(None),
            )
            .count()
        )
        print(f"candidate_pool_unprocessed={pool}")
        print("candidate_pool_unprocessed_scope=all_time_unprocessed")

        stock_actionability = _stock_candidate_actionability_state()
        non_stock_actionability = _non_stock_candidate_actionability_state()
        stock_defer = _stock_session_defer_state()
        print("\nactionability_settings:")
        print(f"stock_max_age_enabled={stock_actionability.get('enabled')}")
        print(f"stock_max_age_minutes={stock_actionability.get('max_age_minutes')}")
        print(f"stock_cutoff_utc={stock_actionability.get('cutoff')}")
        print(f"non_stock_max_age_enabled={non_stock_actionability.get('enabled')}")
        print(f"non_stock_max_age_minutes={non_stock_actionability.get('max_age_minutes')}")
        print(f"non_stock_cutoff_utc={non_stock_actionability.get('cutoff')}")
        print(f"stock_session_defer_enabled={stock_defer.get('enabled')}")
        print(f"stock_session_defer_active={stock_defer.get('active')}")
        print(f"stock_session_defer_cutoff_utc={stock_defer.get('cutoff')}")

        actionability_params = {
            "uid": int(args.user_id),
            "stock_age_enabled": bool(stock_actionability.get("enabled")),
            "stock_cutoff": stock_actionability.get("cutoff"),
            "non_stock_age_enabled": bool(non_stock_actionability.get("enabled")),
            "non_stock_cutoff": non_stock_actionability.get("cutoff"),
            "stock_defer_enabled": bool(stock_defer.get("enabled")),
            "stock_defer_active": bool(stock_defer.get("active")),
            "stock_defer_cutoff": stock_defer.get("cutoff"),
            "limit": limit,
        }
        actionability_case = """
            CASE
                WHEN COALESCE(ba.asset_type, 'stock') = 'stock'
                     AND :stock_defer_enabled
                     AND :stock_defer_active
                     AND (
                         :stock_defer_cutoff IS NULL
                         OR ba.alerted_at >= :stock_defer_cutoff
                     )
                     THEN 'stock_deferred_until_session'
                WHEN COALESCE(ba.asset_type, 'stock') = 'stock'
                     AND :stock_defer_enabled
                     AND :stock_defer_active
                     THEN 'stale_stock_session_defer_window'
                WHEN COALESCE(ba.asset_type, 'stock') = 'stock'
                     AND :stock_age_enabled
                     AND (
                         ba.alerted_at IS NULL
                         OR ba.alerted_at < :stock_cutoff
                     )
                     THEN 'stale_stock_age_window'
                WHEN COALESCE(ba.asset_type, 'stock') <> 'stock'
                     AND :non_stock_age_enabled
                     AND (
                         ba.alerted_at IS NULL
                         OR ba.alerted_at < :non_stock_cutoff
                     )
                     THEN 'stale_non_stock_age_window'
                ELSE 'actionable_by_age'
            END
        """
        actionability_sql = f"""
            WITH unprocessed AS (
                SELECT
                    ba.id,
                    ba.ticker,
                    ba.asset_type,
                    ba.alerted_at,
                    ba.scan_pattern_id,
                    COALESCE(sp.lifecycle_stage, 'none') AS lifecycle_stage,
                    COALESCE(sp.recert_required, FALSE) AS recert_required,
                    {actionability_case} AS actionability
                FROM trading_breakout_alerts ba
                LEFT JOIN scan_patterns sp ON sp.id = ba.scan_pattern_id
                LEFT JOIN trading_autotrader_runs ar
                  ON ar.breakout_alert_id = ba.id
                WHERE ba.alert_tier = 'pattern_imminent'
                  AND (ba.user_id = :uid OR ba.user_id IS NULL)
                  AND ar.id IS NULL
            )
            SELECT actionability,
                   COALESCE(asset_type, 'stock') AS asset_type,
                   lifecycle_stage,
                   recert_required,
                   COUNT(*) AS n,
                   MIN(alerted_at) AS oldest_alerted_at,
                   MAX(alerted_at) AS latest_alerted_at
            FROM unprocessed
            GROUP BY 1, 2, 3, 4
            ORDER BY n DESC, latest_alerted_at DESC NULLS LAST
            LIMIT :limit
        """
        rows = db.execute(text(actionability_sql), actionability_params).mappings().all()
        print("\nunprocessed_actionability:")
        for row in rows:
            print(dict(row))

        stale_sql = f"""
            WITH unprocessed AS (
                SELECT
                    ba.id,
                    ba.ticker,
                    ba.asset_type,
                    ba.alerted_at,
                    ba.scan_pattern_id,
                    COALESCE(sp.lifecycle_stage, 'none') AS lifecycle_stage,
                    COALESCE(sp.recert_required, FALSE) AS recert_required,
                    {actionability_case} AS actionability
                FROM trading_breakout_alerts ba
                LEFT JOIN scan_patterns sp ON sp.id = ba.scan_pattern_id
                LEFT JOIN trading_autotrader_runs ar
                  ON ar.breakout_alert_id = ba.id
                WHERE ba.alert_tier = 'pattern_imminent'
                  AND (ba.user_id = :uid OR ba.user_id IS NULL)
                  AND ar.id IS NULL
            )
            SELECT id,
                   ticker,
                   COALESCE(asset_type, 'stock') AS asset_type,
                   lifecycle_stage,
                   recert_required,
                   actionability,
                   alerted_at,
                   ROUND(EXTRACT(EPOCH FROM (NOW() - alerted_at)) / 60.0, 1)
                       AS age_minutes
            FROM unprocessed
            WHERE actionability <> 'actionable_by_age'
            ORDER BY alerted_at DESC NULLS LAST, id DESC
            LIMIT :limit
        """
        rows = db.execute(text(stale_sql), actionability_params).mappings().all()
        print("\nstale_or_deferred_unprocessed_examples:")
        for row in rows:
            print(dict(row))

        diag = _candidate_pool_zero_context(
            db, uid=args.user_id, lookback_minutes=args.lookback_minutes,
        )
        for key in ("recent_alerts", "processed", "unprocessed", "lifecycle_counts"):
            print(f"{key}={diag.get(key)}")
        print(f"latest={diag.get('latest')}")
        print(f"latest_unprocessed={diag.get('latest_unprocessed')}")
        rows = db.execute(text("""
            SELECT ar.decision, ar.reason, COUNT(*) AS n, MAX(ar.created_at) AS latest
            FROM trading_autotrader_runs ar
            WHERE ar.created_at >= NOW() - (:mins * INTERVAL '1 minute')
            GROUP BY ar.decision, ar.reason
            ORDER BY n DESC
            LIMIT 20
        """), {"mins": args.lookback_minutes}).mappings().all()
        print("\nrecent_decisions:")
        for row in rows:
            print(dict(row))
    finally:
        db.close()


if __name__ == "__main__":
    main()
