"""Phase 5K-A live-path parity probe.

Read-only evidence gate before any live capital, risk, promotion, broker, or
reconciliation path is moved away from the ``trading_trades`` compatibility
view. The probe compares live-decision input aggregates through:

* ``trading_trades`` -- legacy compatibility view
* ``trading_management_envelopes`` -- semantic physical base table

Machine-readable header:

    VERDICT_STATUS=<COMPLETE_POSITIVE|REGRESSION_SCHEMA|REGRESSION_PARITY|ALERT>
    VERDICT_REASON=<short reason>
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path[:0] = [str(REPO_ROOT)]

from app.services.trading.realized_pnl_sql import trade_return_fraction_sql


DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://chili:chili@localhost:5433/chili",
)

OLD_RELATION = "trading_trades"
NEW_RELATION = "trading_management_envelopes"

RECONCILE_ARTIFACT_EXIT_REASONS = (
    "broker_reconcile_position_gone",
    "coinbase_position_sync_gone",
    "zombie_reconcile_orphan",
    "broker_reconcile_no_exit_price",
    "coinbase_phantom_cycle_cleanup_2026_05_19",
)


def _earliest_business_day_in_window(
    now: datetime,
    *,
    window_business_days: int = 5,
) -> datetime:
    d = now.date()
    days_found = 0
    while True:
        if d.weekday() < 5:
            days_found += 1
            if days_found >= window_business_days:
                break
        d = d - timedelta(days=1)
    return datetime(d.year, d.month, d.day)


def _fetch_all(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return [_normalize_row(dict(row)) for row in cur.fetchall()]


def _enforce_read_only(conn) -> None:
    try:
        conn.set_session(readonly=True, autocommit=False)
    except Exception as exc:
        raise RuntimeError(f"could not set read-only session: {exc}") from exc

    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SHOW transaction_read_only")
        row = cur.fetchone()
    value = row[0] if row else None
    if str(value).strip().lower() not in {"on", "true", "1"}:
        raise RuntimeError(f"read-only transaction not confirmed: {value!r}")


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k): _normalize_scalar(v) for k, v in row.items()}


def _json_rows(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def _relation_kinds(conn) -> dict[str, str]:
    rows = _fetch_all(
        conn,
        """
        SELECT c.relname, c.relkind
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = ANY(current_schemas(false))
           AND c.relname IN (%s, %s)
         ORDER BY c.relname
        """,
        (OLD_RELATION, NEW_RELATION),
    )
    return {str(row["relname"]): str(row["relkind"]) for row in rows}


def _query_for_check(name: str, relation: str) -> tuple[str, tuple[Any, ...]]:
    if relation not in {OLD_RELATION, NEW_RELATION}:
        raise ValueError(f"unexpected relation: {relation!r}")
    return_fraction = trade_return_fraction_sql("e")

    if name == "coinbase_cap":
        return (
            f"""
            SELECT COUNT(*)::int AS open_count,
                   ROUND(COALESCE(SUM(quantity * entry_price), 0)::numeric, 8) AS open_notional
              FROM {relation}
             WHERE status = 'open'
               AND LOWER(COALESCE(broker_source, '')) = 'coinbase'
               AND (
                    LOWER(COALESCE(auto_trader_version, '')) = 'v1'
                    OR LOWER(COALESCE(management_scope, '')) = 'auto_trader_v1'
               )
            """,
            (),
        )

    if name == "pdt_day_trades":
        cutoff = _earliest_business_day_in_window(datetime.now(timezone.utc))
        return (
            f"""
            WITH eligible AS (
                SELECT user_id
                  FROM {relation}
                 WHERE status = 'closed'
                   AND entry_date IS NOT NULL
                   AND exit_date IS NOT NULL
                   AND DATE(entry_date) = DATE(exit_date)
                   AND exit_date > %s
                   AND ticker NOT LIKE '%%-USD'
                   AND broker_order_id IS NOT NULL
                   AND last_fill_at IS NOT NULL
                   AND NOT (COALESCE(exit_reason, '') = ANY(%s))
            )
            SELECT 'all' AS scope,
                   NULL::bigint AS user_id,
                   COUNT(*)::int AS day_trades_5bd
              FROM eligible
            UNION ALL
            SELECT 'user' AS scope,
                   user_id,
                   COUNT(*)::int AS day_trades_5bd
              FROM eligible
             GROUP BY user_id
             ORDER BY scope, user_id NULLS FIRST
            """,
            (cutoff, list(RECONCILE_ARTIFACT_EXIT_REASONS)),
        )

    if name == "promotion_realized":
        return (
            f"""
            SELECT scan_pattern_id,
                   COUNT(*)::int AS n,
                   ROUND(COALESCE(SUM(pnl), 0)::numeric, 8) AS total_pnl,
                   ROUND(AVG({return_fraction})::numeric, 10) AS avg_return_fraction
              FROM {relation} e
             WHERE scan_pattern_id IS NOT NULL
               AND scan_pattern_id != -1
               AND status = 'closed'
               AND pnl IS NOT NULL
               AND entry_price > 0
               AND quantity > 0
               AND exit_date > NOW() - INTERVAL '90 days'
             GROUP BY scan_pattern_id
             ORDER BY scan_pattern_id
            """,
            (),
        )

    if name == "pattern_quality":
        return (
            f"""
            SELECT scan_pattern_id,
                   COUNT(*)::int AS n,
                   COUNT(*) FILTER (WHERE pnl > 0)::int AS winners,
                   COUNT(*) FILTER (WHERE pnl <= 0)::int AS losers,
                   ROUND(AVG({return_fraction})::numeric, 10) AS avg_return_fraction,
                   ROUND(COALESCE(SUM(pnl), 0)::numeric, 8) AS total_pnl
              FROM {relation} e
             WHERE scan_pattern_id IS NOT NULL
               AND scan_pattern_id != -1
               AND status = 'closed'
               AND pnl IS NOT NULL
               AND entry_price > 0
               AND quantity > 0
               AND exit_date > NOW() - INTERVAL '90 days'
             GROUP BY scan_pattern_id
             ORDER BY scan_pattern_id
            """,
            (),
        )

    if name == "portfolio_risk_open":
        return (
            f"""
            SELECT LOWER(COALESCE(broker_source, 'unknown')) AS broker_source,
                   LOWER(COALESCE(asset_kind, 'unknown')) AS asset_kind,
                   COUNT(*)::int AS open_count,
                   ROUND(COALESCE(SUM(quantity * entry_price), 0)::numeric, 8) AS open_notional
              FROM {relation}
             WHERE status = 'open'
             GROUP BY 1, 2
             ORDER BY 1, 2
            """,
            (),
        )

    if name == "position_integrity_open":
        return (
            f"""
            WITH matches AS (
                SELECT p.id AS position_id,
                       COUNT(*) AS open_trade_count
                  FROM trading_positions p
                  JOIN {relation} e
                    ON e.status = 'open'
                   AND e.user_id = p.user_id
                   AND LOWER(COALESCE(e.broker_source, '')) = p.broker_source
                   AND e.ticker = p.ticker
                   AND LOWER(COALESCE(e.direction, 'long')) = p.direction
                 WHERE p.state = 'open'
                   AND p.current_envelope_id IS NULL
                 GROUP BY p.id
            )
            SELECT 'open_positions_without_open_trade' AS invariant,
                   COUNT(*)::int AS n
              FROM trading_positions p
             WHERE p.state = 'open'
               AND NOT EXISTS (
                   SELECT 1
                     FROM {relation} e
                    WHERE e.status = 'open'
                      AND e.user_id = p.user_id
                      AND LOWER(COALESCE(e.broker_source, '')) = p.broker_source
                      AND e.ticker = p.ticker
                      AND LOWER(COALESCE(e.direction, 'long')) = p.direction
               )
            UNION ALL
            SELECT 'open_trades_without_open_position' AS invariant,
                   COUNT(*)::int AS n
              FROM {relation} e
             WHERE e.status = 'open'
               AND NOT EXISTS (
                   SELECT 1
                     FROM trading_positions p
                    WHERE p.state = 'open'
                      AND p.user_id = e.user_id
                      AND p.broker_source = LOWER(COALESCE(e.broker_source, ''))
                      AND p.ticker = e.ticker
                      AND p.direction = LOWER(COALESCE(e.direction, 'long'))
               )
            UNION ALL
            SELECT 'open_positions_missing_current_envelope' AS invariant,
                   COUNT(*)::int AS n
              FROM trading_positions p
             WHERE p.state = 'open'
               AND p.current_envelope_id IS NULL
            UNION ALL
            SELECT 'current_envelope_mismatches' AS invariant,
                   COUNT(*)::int AS n
              FROM trading_positions p
              LEFT JOIN {relation} e ON e.id = p.current_envelope_id
             WHERE p.state = 'open'
               AND p.current_envelope_id IS NOT NULL
               AND (
                   e.id IS NULL
                   OR e.status <> 'open'
                   OR e.user_id <> p.user_id
                   OR LOWER(COALESCE(e.broker_source, '')) <> p.broker_source
                   OR e.ticker <> p.ticker
                   OR LOWER(COALESCE(e.direction, 'long')) <> p.direction
               )
            UNION ALL
            SELECT 'repairable_current_envelope_links' AS invariant,
                   COUNT(*)::int AS n
              FROM matches
             WHERE open_trade_count = 1
             ORDER BY invariant
            """,
            (),
        )

    raise ValueError(f"unknown check: {name!r}")


CHECKS = (
    "coinbase_cap",
    "pdt_day_trades",
    "promotion_realized",
    "pattern_quality",
    "portfolio_risk_open",
    "position_integrity_open",
)


def _run_check(conn, name: str) -> dict[str, Any]:
    old_sql, old_params = _query_for_check(name, OLD_RELATION)
    new_sql, new_params = _query_for_check(name, NEW_RELATION)
    old_rows = _fetch_all(conn, old_sql, old_params)
    new_rows = _fetch_all(conn, new_sql, new_params)
    matched = _json_rows(old_rows) == _json_rows(new_rows)
    return {
        "name": name,
        "matched": matched,
        "old_rows": old_rows,
        "new_rows": new_rows,
        "old_count": len(old_rows),
        "new_count": len(new_rows),
    }


def _main() -> int:
    print(f"# phase5k live-path parity probe -- {datetime.now(timezone.utc).isoformat()}")
    try:
        conn = psycopg2.connect(DSN)
    except Exception as exc:
        print("VERDICT_STATUS=ALERT")
        print(f"VERDICT_REASON=DB connect failed: {exc}")
        return 2

    try:
        _enforce_read_only(conn)
        kinds = _relation_kinds(conn)
        schema_ok = kinds.get(OLD_RELATION) == "v" and kinds.get(NEW_RELATION) == "r"
        if not schema_ok:
            print("VERDICT_STATUS=REGRESSION_SCHEMA")
            print(f"VERDICT_REASON=unexpected relation kinds: {kinds}")
            print(f"RELATION_KINDS={kinds}")
            return 1

        results = [_run_check(conn, name) for name in CHECKS]
    except Exception as exc:
        print("VERDICT_STATUS=ALERT")
        print(f"VERDICT_REASON=query failed: {exc}")
        return 2
    finally:
        conn.close()

    mismatches = [r for r in results if not r["matched"]]
    if mismatches:
        verdict = "REGRESSION_PARITY"
        reason = f"{len(mismatches)} live-path aggregate mismatch(es)"
    else:
        verdict = "COMPLETE_POSITIVE"
        reason = f"{len(results)} live-path aggregate checks matched"

    print(f"VERDICT_STATUS={verdict}")
    print(f"VERDICT_REASON={reason}")
    print(f"RELATION_KINDS={kinds}")
    print(f"PARITY_GROUPS={len(results)}")
    print(f"MISMATCH_GROUPS={len(mismatches)}")
    print(f"PARITY_CHECKS={len(results)}")
    print(f"PARITY_MISMATCHES={len(mismatches)}")
    for result in results:
        status = "OK" if result["matched"] else "MISMATCH"
        print(
            f"CHECK_{result['name'].upper()}={status} "
            f"old_rows={result['old_count']} new_rows={result['new_count']}"
        )
        if not result["matched"]:
            print(f"CHECK_{result['name'].upper()}_OLD={_json_rows(result['old_rows'])}")
            print(f"CHECK_{result['name'].upper()}_NEW={_json_rows(result['new_rows'])}")

    return 0 if not mismatches else 1


if __name__ == "__main__":
    sys.exit(_main())
