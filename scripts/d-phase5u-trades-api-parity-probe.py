"""Phase 5U /trades API read-shape parity probe.

Read-only guard before any public /trades route cutover. It compares the base
fields used by the current Trade ORM route against the management-envelope
table for the latest rows. Broker-truth display overlays are intentionally out
of scope; this probe validates the stable database-backed row shape.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path[:0] = [str(REPO_ROOT)]


DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://chili:chili@localhost:5433/chili",
)

OLD_RELATION = "trading_trades"
NEW_RELATION = "trading_management_envelopes"
FIELDS = (
    "id",
    "ticker",
    "direction",
    "entry_price",
    "exit_price",
    "quantity",
    "entry_date",
    "exit_date",
    "status",
    "pnl",
    "tags",
    "notes",
    "broker_source",
    "broker_status",
    "broker_order_id",
    "filled_at",
    "avg_fill_price",
    "tca_reference_entry_price",
    "tca_entry_slippage_bps",
    "tca_reference_exit_price",
    "tca_exit_slippage_bps",
    "strategy_proposal_id",
    "scan_pattern_id",
    "position_id",
)


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k): _normalize_scalar(v) for k, v in row.items()}


def _fetch_all(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [_normalize_row(dict(row)) for row in cur.fetchall()]


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


def _rows_for(conn, relation: str, status: str | None) -> list[dict[str, Any]]:
    if relation not in {OLD_RELATION, NEW_RELATION}:
        raise ValueError(f"unexpected relation: {relation!r}")
    status_clause = ""
    params: tuple[Any, ...] = ()
    if status is not None:
        status_clause = "WHERE status = %s"
        params = (status,)
    field_sql = ", ".join(FIELDS)
    return _fetch_all(
        conn,
        f"""
        SELECT {field_sql}
          FROM {relation}
          {status_clause}
         ORDER BY entry_date DESC NULLS LAST, id DESC
         LIMIT 50
        """,
        params,
    )


def _json_rows(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def main() -> int:
    mismatches: list[dict[str, Any]] = []
    statuses = (None, "open", "closed")
    with psycopg2.connect(DSN) as conn:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        relation_kinds = _relation_kinds(conn)

        for status in statuses:
            old_rows = _rows_for(conn, OLD_RELATION, status)
            new_rows = _rows_for(conn, NEW_RELATION, status)
            if _json_rows(old_rows) != _json_rows(new_rows):
                mismatches.append({
                    "status": status or "all",
                    "old_count": len(old_rows),
                    "new_count": len(new_rows),
                    "old_head": old_rows[:3],
                    "new_head": new_rows[:3],
                })

    if relation_kinds.get(OLD_RELATION) != "v" or relation_kinds.get(NEW_RELATION) != "r":
        print("VERDICT_STATUS=REGRESSION_SCHEMA")
        print(f"VERDICT_REASON=unexpected relation kinds: {relation_kinds}")
        return 2
    if mismatches:
        print("VERDICT_STATUS=REGRESSION_PARITY")
        print(f"VERDICT_REASON={len(mismatches)} /trades row-shape parity mismatches")
        print("MISMATCHES=" + json.dumps(mismatches, sort_keys=True))
        return 3

    print("VERDICT_STATUS=COMPLETE_POSITIVE")
    print("VERDICT_REASON=/trades base row shape matches management envelopes")
    print(f"RELATION_KINDS={relation_kinds}")
    print(f"CHECKS={len(statuses)}")
    print("MISMATCHES=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
