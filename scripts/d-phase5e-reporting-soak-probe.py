"""Phase 5E reporting-reader soak probe.

Read-only gate for the position-identity rename path. The physical rename is
not safe until fresh post-mig-275 entries/closes pass the Phase 5C attribution
comparison without hard linkage issues or attribution drift.

Machine-readable header:

    VERDICT_STATUS=<IN_FLIGHT|READY_FOR_RENAME_BRIEF|BLOCKED_LINKAGE|BLOCKED_DRIFT|ALERT>
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
    print(f"# phase5e reporting soak probe -- {datetime.now(timezone.utc).isoformat()}")
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
             WHERE version_id = '275_position_identity_phase5d_decision_pattern_backfill'
            """,
        )
        applied_at = mig.get("applied_at")
        if applied_at is None:
            print("VERDICT_STATUS=ALERT")
            print("VERDICT_REASON=mig275 not applied")
            return 2

        gate = _fetch_one(
            conn,
            """
            SELECT
                COUNT(*) FILTER (WHERE decision_entry_date > %s)::int AS fresh_decisions,
                COUNT(*) FILTER (WHERE envelope_entry_date > %s)::int AS fresh_envelopes,
                COUNT(*) FILTER (WHERE envelope_exit_date > %s)::int AS fresh_closes,
                COUNT(*) FILTER (
                    WHERE envelope_exit_date > %s
                      AND decision_scan_pattern_id IS DISTINCT FROM envelope_scan_pattern_id
                )::int AS fresh_close_mismatches,
                COUNT(*) FILTER (
                    WHERE linkage_status NOT IN (
                        'linked',
                        'historical_broker_envelope_missing_position'
                    )
                )::int AS hard_linkage_issues
              FROM trading_phase5b_decision_envelope_position
            """,
            (applied_at, applied_at, applied_at, applied_at),
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

        mismatches = _fetch_all(
            conn,
            """
            SELECT
                decision_scan_pattern_id,
                envelope_scan_pattern_id,
                COUNT(*)::int AS closed_envelopes,
                ROUND(SUM(COALESCE(envelope_pnl, 0))::numeric, 4) AS total_pnl
              FROM trading_phase5b_decision_envelope_position
             WHERE envelope_user_id = 1
               AND envelope_status = 'closed'
               AND envelope_exit_date >= NOW() - INTERVAL '30 days'
               AND decision_scan_pattern_id IS DISTINCT FROM envelope_scan_pattern_id
             GROUP BY decision_scan_pattern_id, envelope_scan_pattern_id
             ORDER BY ABS(SUM(COALESCE(envelope_pnl, 0))) DESC
             LIMIT 10
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

    if hard_linkage_issues > 0:
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
            "awaiting fresh post-mig275 entry/close cycle "
            f"(decisions={fresh_decisions}, envelopes={fresh_envelopes}, closes={fresh_closes})"
        )
    else:
        verdict = "READY_FOR_RENAME_BRIEF"
        reason = (
            f"fresh data clean: decisions={fresh_decisions}, "
            f"envelopes={fresh_envelopes}, closes={fresh_closes}"
        )

    print(f"VERDICT_STATUS={verdict}")
    print(f"VERDICT_REASON={reason}")
    print(f"MIG275_APPLIED_AT={applied_at}")
    for key in (
        "fresh_decisions",
        "fresh_envelopes",
        "fresh_closes",
        "fresh_close_mismatches",
        "hard_linkage_issues",
    ):
        print(f"{key.upper()}={gate.get(key)}")
    for key in ("closed_rows", "mismatched_rows", "mismatched_pnl"):
        print(f"{key.upper()}={compare.get(key)}")
    if mismatches:
        print("MISMATCHES:")
        for row in mismatches:
            print(dict(row))
    return 0 if verdict in {"IN_FLIGHT", "READY_FOR_RENAME_BRIEF"} else 1


if __name__ == "__main__":
    sys.exit(_main())
