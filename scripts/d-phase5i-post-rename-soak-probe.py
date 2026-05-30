"""Phase 5I post-rename soak probe.

Read-only gate after mig 283 physically renamed ``trading_trades`` to the
``trading_management_envelopes`` base table and recreated ``trading_trades`` as
a compatibility view.

Machine-readable header:

    VERDICT_STATUS=<IN_FLIGHT|COMPLETE_POSITIVE|REGRESSION_SCHEMA|BLOCKED_LINKAGE|BLOCKED_DRIFT|ALERT>
    VERDICT_REASON=<short reason>
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.extras


DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://chili:chili@localhost:5433/chili",
)


def _fetch_one(conn, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return dict(row or {})


def _fetch_all(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _main() -> int:
    print(f"# phase5i post-rename soak probe -- {datetime.now(timezone.utc).isoformat()}")
    try:
        conn = psycopg2.connect(DSN)
    except Exception as exc:
        print("VERDICT_STATUS=ALERT")
        print(f"VERDICT_REASON=DB connect failed: {exc}")
        return 2

    try:
        mig = _fetch_one(
            conn,
            """
            SELECT applied_at
              FROM schema_version
             WHERE version_id = '283_position_identity_phase5h_physical_rename'
            """,
        )
        applied_at = mig.get("applied_at")
        if applied_at is None:
            print("VERDICT_STATUS=ALERT")
            print("VERDICT_REASON=mig283 not applied")
            return 2

        rel_rows = _fetch_all(
            conn,
            """
            SELECT c.relname, c.relkind
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = ANY(current_schemas(false))
               AND c.relname IN (
                    'trading_trades',
                    'trading_management_envelopes',
                    'trading_phase5b_decision_envelope_position'
               )
             ORDER BY c.relname
            """,
        )
        rels = {str(row["relname"]): str(row["relkind"]) for row in rel_rows}

        gate = _fetch_one(
            conn,
            """
            SELECT
                COUNT(*) FILTER (WHERE decision_entry_date > %s)::int AS fresh_decisions,
                COUNT(*) FILTER (WHERE envelope_entry_date > %s)::int AS fresh_envelopes,
                COUNT(*) FILTER (
                    WHERE envelope_exit_date > %s
                      AND envelope_status = 'closed'
                )::int AS fresh_closes,
                COUNT(*) FILTER (
                    WHERE envelope_exit_date > %s
                      AND envelope_status = 'closed'
                      AND decision_scan_pattern_id IS DISTINCT FROM envelope_scan_pattern_id
                )::int AS fresh_close_mismatches,
                COUNT(*) FILTER (
                    WHERE envelope_exit_date > %s
                      AND envelope_status IS DISTINCT FROM 'closed'
                )::int AS fresh_terminal_non_closed,
                COUNT(*) FILTER (
                    WHERE envelope_exit_date > %s
                      AND envelope_status = 'cancelled'
                      AND decision_scan_pattern_id IS DISTINCT FROM envelope_scan_pattern_id
                )::int AS fresh_cancelled_mismatches,
                COUNT(*) FILTER (
                    WHERE linkage_status NOT IN (
                        'linked',
                        'historical_broker_envelope_missing_position'
                    )
                )::int AS hard_linkage_issues
              FROM trading_phase5b_decision_envelope_position
            """,
            (
                applied_at,
                applied_at,
                applied_at,
                applied_at,
                applied_at,
                applied_at,
            ),
        )

        compare = _fetch_one(
            conn,
            """
            SELECT
                COUNT(*)::int AS closed_rows,
                COUNT(*) FILTER (
                    WHERE decision_scan_pattern_id IS DISTINCT FROM envelope_scan_pattern_id
                )::int AS mismatched_rows,
                ROUND(COALESCE(SUM(COALESCE(envelope_pnl, 0)) FILTER (
                    WHERE decision_scan_pattern_id IS DISTINCT FROM envelope_scan_pattern_id
                ), 0)::numeric, 4) AS mismatched_pnl
              FROM trading_phase5b_decision_envelope_position
             WHERE envelope_user_id = 1
               AND envelope_status = 'closed'
               AND envelope_exit_date >= NOW() - INTERVAL '30 days'
            """,
        )
    except Exception as exc:
        print("VERDICT_STATUS=ALERT")
        print(f"VERDICT_REASON=query failed: {exc}")
        return 2
    finally:
        conn.close()

    fresh_decisions = int(gate.get("fresh_decisions") or 0)
    fresh_envelopes = int(gate.get("fresh_envelopes") or 0)
    fresh_closes = int(gate.get("fresh_closes") or 0)
    fresh_close_mismatches = int(gate.get("fresh_close_mismatches") or 0)
    hard_linkage_issues = int(gate.get("hard_linkage_issues") or 0)
    mismatched_rows = int(compare.get("mismatched_rows") or 0)
    mismatched_pnl = _num(compare.get("mismatched_pnl"))

    schema_ok = (
        rels.get("trading_management_envelopes") == "r"
        and rels.get("trading_trades") == "v"
        and rels.get("trading_phase5b_decision_envelope_position") == "v"
    )

    if not schema_ok:
        verdict = "REGRESSION_SCHEMA"
        reason = f"unexpected relation kinds: {rels}"
    elif hard_linkage_issues > 0:
        verdict = "BLOCKED_LINKAGE"
        reason = f"{hard_linkage_issues} hard linkage issue(s) in Phase 5B view"
    elif fresh_close_mismatches > 0 or mismatched_rows > 0 or abs(mismatched_pnl) > 0.0001:
        verdict = "BLOCKED_DRIFT"
        reason = (
            f"attribution drift rows={mismatched_rows} "
            f"fresh_close_mismatches={fresh_close_mismatches} pnl={mismatched_pnl:.4f}"
        )
    elif fresh_decisions <= 0 or fresh_envelopes <= 0 or fresh_closes <= 0:
        verdict = "IN_FLIGHT"
        reason = (
            "awaiting fresh post-mig283 entry/close cycle "
            f"(decisions={fresh_decisions}, envelopes={fresh_envelopes}, closes={fresh_closes})"
        )
    else:
        verdict = "COMPLETE_POSITIVE"
        reason = (
            f"fresh post-rename data clean: decisions={fresh_decisions}, "
            f"envelopes={fresh_envelopes}, closes={fresh_closes}"
        )

    print(f"VERDICT_STATUS={verdict}")
    print(f"VERDICT_REASON={reason}")
    print(f"MIG283_APPLIED_AT={applied_at}")
    print(f"RELATION_KINDS={rels}")
    for key in (
        "fresh_decisions",
        "fresh_envelopes",
        "fresh_closes",
        "fresh_close_mismatches",
        "fresh_terminal_non_closed",
        "fresh_cancelled_mismatches",
        "hard_linkage_issues",
    ):
        print(f"{key.upper()}={gate.get(key)}")
    for key in ("closed_rows", "mismatched_rows", "mismatched_pnl"):
        print(f"{key.upper()}={compare.get(key)}")
    return 0 if verdict in {"IN_FLIGHT", "COMPLETE_POSITIVE"} else 1


if __name__ == "__main__":
    sys.exit(_main())
