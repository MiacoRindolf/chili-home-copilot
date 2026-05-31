"""Phase 5V monitor/router read parity probe.

Read-only evidence gate before converting the remaining monitor read candidates
away from ``Trade`` ORM joins. The probe compares old-vs-new result sets using:

* ``trading_trades`` -- legacy compatibility view
* ``trading_management_envelopes`` -- semantic physical base table

Machine-readable header:

    VERDICT_STATUS=<COMPLETE_POSITIVE|MISMATCH|ALERT>
    VERDICT_REASON=<short reason>
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
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
ALLOWED_RELATIONS = {OLD_RELATION, NEW_RELATION}


@dataclass(frozen=True)
class ParityCheck:
    name: str
    old_rows: list[dict[str, Any]]
    new_rows: list[dict[str, Any]]

    @property
    def matched(self) -> bool:
        return _json_rows(self.old_rows) == _json_rows(self.new_rows)

    @property
    def old_count(self) -> int:
        return len(self.old_rows)

    @property
    def new_count(self) -> int:
        return len(self.new_rows)


def _relation_sql(relation: str) -> str:
    if relation not in ALLOWED_RELATIONS:
        raise ValueError(f"unexpected relation: {relation!r}")
    return relation


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k): _normalize_scalar(v) for k, v in row.items()}


def _json_rows(rows: list[dict[str, Any]]) -> str:
    normalized = [_normalize_row(row) for row in rows]
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _fetch_all(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [_normalize_row(dict(row)) for row in cur.fetchall()]


def _enforce_read_only(conn) -> None:
    conn.set_session(readonly=True, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SET LOCAL statement_timeout = '25000ms'")
        cur.execute("SHOW transaction_read_only")
        row = cur.fetchone()
    value = row[0] if row else None
    if str(value).strip().lower() not in {"on", "true", "1"}:
        raise RuntimeError(f"read-only transaction not confirmed: {value!r}")


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


def monitor_decisions_sql(
    relation: str,
    *,
    user_id: int | None,
    action: str | None,
    limit: int,
    offset: int = 0,
) -> tuple[str, tuple[Any, ...]]:
    rel = _relation_sql(relation)
    return (
        f"""
        WITH scoped AS (
            SELECT
                d.id,
                d.trade_id,
                t.ticker,
                t.direction,
                d.action,
                d.created_at
              FROM trading_pattern_monitor_decisions d
              JOIN {rel} t ON t.id = d.trade_id
             WHERE t.user_id IS NOT DISTINCT FROM %s
               AND (%s IS NULL OR d.action = %s)
        )
        SELECT
            COUNT(*) OVER()::int AS total_count,
            id,
            trade_id,
            ticker,
            direction,
            action,
            created_at
          FROM scoped
         ORDER BY created_at DESC NULLS LAST, id DESC
         LIMIT %s OFFSET %s
        """,
        (user_id, action, action, int(limit), int(offset)),
    )


def imminent_alerts_sql(
    relation: str,
    *,
    user_id: int | None,
    hours: int,
    limit: int,
) -> tuple[str, tuple[Any, ...]]:
    rel = _relation_sql(relation)
    return (
        f"""
        WITH actioned AS (
            SELECT DISTINCT related_alert_id
              FROM {rel}
             WHERE related_alert_id IS NOT NULL
               AND status IN ('open', 'closed')
               AND user_id IS NOT DISTINCT FROM %s
        )
        SELECT ba.id,
               ba.ticker,
               ba.user_id,
               ba.alerted_at
          FROM trading_breakout_alerts ba
         WHERE ba.alert_tier = 'pattern_imminent'
           AND ba.outcome = 'pending'
           AND ba.alerted_at >= NOW() - (%s * INTERVAL '1 hour')
           AND NOT EXISTS (
               SELECT 1
                 FROM actioned a
                WHERE a.related_alert_id = ba.id
           )
           AND (%s IS NULL OR ba.user_id = %s OR ba.user_id IS NULL)
         ORDER BY ba.alerted_at DESC NULLS LAST, ba.id DESC
         LIMIT %s
        """,
        (user_id, int(hours), user_id, user_id, int(limit)),
    )


def stop_decisions_sql(
    relation: str,
    *,
    user_id: int | None,
    trade_id: int | None,
    limit: int,
) -> tuple[str, tuple[Any, ...]]:
    rel = _relation_sql(relation)
    return (
        f"""
        WITH recent AS MATERIALIZED (
            SELECT id, trade_id, as_of_ts, state, trigger, executed
              FROM trading_stop_decisions
             WHERE (%s IS NULL OR trade_id = %s)
             ORDER BY as_of_ts DESC NULLS LAST, id DESC
             LIMIT 1000
        )
        SELECT
            d.id,
            d.trade_id,
            d.as_of_ts,
            d.state,
            d.trigger,
            d.executed
          FROM recent d
          JOIN {rel} t ON t.id = d.trade_id
         WHERE t.user_id IS NOT DISTINCT FROM %s
         ORDER BY d.as_of_ts DESC NULLS LAST, d.id DESC
         LIMIT %s
        """,
        (trade_id, trade_id, user_id, int(limit)),
    )


def _user_ids(conn, *, limit: int = 3) -> list[int | None]:
    rows = _fetch_all(
        conn,
        f"""
        SELECT DISTINCT user_id
          FROM (
                SELECT user_id FROM {OLD_RELATION}
                UNION
                SELECT user_id FROM {NEW_RELATION}
          ) u
         WHERE user_id IS NOT NULL
         ORDER BY user_id
         LIMIT %s
        """,
        (int(limit),),
    )
    return [None] + [int(row["user_id"]) for row in rows]


def _actions(conn, *, limit: int = 3) -> list[str | None]:
    rows = _fetch_all(
        conn,
        """
        SELECT action, COUNT(*) AS n
          FROM trading_pattern_monitor_decisions
         WHERE action IS NOT NULL
         GROUP BY action
         ORDER BY n DESC, action
         LIMIT %s
        """,
        (int(limit),),
    )
    return [None] + [str(row["action"]) for row in rows]


def _recent_trade_ids_with_stop_decisions(conn, *, limit: int = 2) -> list[int | None]:
    rows = _fetch_all(
        conn,
        """
        SELECT DISTINCT trade_id
          FROM trading_stop_decisions
         WHERE trade_id IS NOT NULL
         ORDER BY trade_id DESC
         LIMIT %s
        """,
        (int(limit),),
    )
    return [None] + [int(row["trade_id"]) for row in rows]


def _run_pair(
    conn,
    name: str,
    old_query: tuple[str, tuple[Any, ...]],
    new_query: tuple[str, tuple[Any, ...]],
) -> ParityCheck:
    old_sql, old_params = old_query
    new_sql, new_params = new_query
    return ParityCheck(
        name=name,
        old_rows=_fetch_all(conn, old_sql, old_params),
        new_rows=_fetch_all(conn, new_sql, new_params),
    )


def run_checks(conn) -> list[ParityCheck]:
    checks: list[ParityCheck] = []
    users = _user_ids(conn)
    actions = _actions(conn)

    for uid in users:
        for action in actions:
            suffix = f"user={uid if uid is not None else 'NULL'}:action={action or 'ALL'}"
            checks.append(
                _run_pair(
                    conn,
                    f"monitor_decisions:{suffix}",
                    monitor_decisions_sql(
                        OLD_RELATION,
                        user_id=uid,
                        action=action,
                        limit=50,
                    ),
                    monitor_decisions_sql(
                        NEW_RELATION,
                        user_id=uid,
                        action=action,
                        limit=50,
                    ),
                )
            )

        checks.append(
            _run_pair(
                conn,
                f"imminent_alerts:user={uid if uid is not None else 'NULL'}",
                imminent_alerts_sql(
                    OLD_RELATION,
                    user_id=uid,
                    hours=72,
                    limit=200,
                ),
                imminent_alerts_sql(
                    NEW_RELATION,
                    user_id=uid,
                    hours=72,
                    limit=200,
                ),
            )
        )

    if os.environ.get("PHASE5V_INCLUDE_STOP_DECISIONS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        for uid in users:
            for tid in _recent_trade_ids_with_stop_decisions(conn):
                suffix = (
                    f"user={uid if uid is not None else 'NULL'}:"
                    f"trade={tid if tid is not None else 'ALL'}"
                )
                checks.append(
                    _run_pair(
                        conn,
                        f"stop_decisions:{suffix}",
                        stop_decisions_sql(
                            OLD_RELATION,
                            user_id=uid,
                            trade_id=tid,
                            limit=50,
                        ),
                        stop_decisions_sql(
                            NEW_RELATION,
                            user_id=uid,
                            trade_id=tid,
                            limit=50,
                        ),
                    )
                )
    return checks


def main() -> int:
    print(f"# phase5v monitor-read parity probe -- {datetime.now(timezone.utc).isoformat()}")
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
            print("VERDICT_STATUS=ALERT")
            print(f"VERDICT_REASON=unexpected relation kinds: {kinds}")
            print(f"RELATION_KINDS={kinds}")
            return 2
        checks = run_checks(conn)
    except Exception as exc:
        print("VERDICT_STATUS=ALERT")
        print(f"VERDICT_REASON=query failed: {exc}")
        return 2
    finally:
        conn.close()

    mismatches = [check for check in checks if not check.matched]
    verdict = "COMPLETE_POSITIVE" if not mismatches else "MISMATCH"
    reason = (
        f"{len(checks)} monitor read checks matched"
        if not mismatches
        else f"{len(mismatches)} of {len(checks)} monitor read checks mismatched"
    )

    print(f"VERDICT_STATUS={verdict}")
    print(f"VERDICT_REASON={reason}")
    print(f"RELATION_KINDS={kinds}")
    print(f"PARITY_CHECKS={len(checks)}")
    print(f"PARITY_MISMATCHES={len(mismatches)}")
    print(
        "STOP_DECISIONS_INCLUDED="
        f"{bool(os.environ.get('PHASE5V_INCLUDE_STOP_DECISIONS', '').strip())}"
    )
    for check in checks:
        status = "OK" if check.matched else "MISMATCH"
        print(
            f"CHECK {status} {check.name} "
            f"old_rows={check.old_count} new_rows={check.new_count}"
        )
        if not check.matched:
            print(f"CHECK_OLD {check.name} {_json_rows(check.old_rows)}")
            print(f"CHECK_NEW {check.name} {_json_rows(check.new_rows)}")

    return 0 if not mismatches else 1


if __name__ == "__main__":
    sys.exit(main())
