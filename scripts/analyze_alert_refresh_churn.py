"""Read-only report for recert-rescue and exit-variant alert refresh churn.

The cash-deployment and edge-reliability surfaces can recommend
``recert_rescue_refresh`` or ``exit_variant_refresh`` even when the queue has
recently learned that a specific refresh is a no-op. This report separates the
visible recommendation mix from actual queued work and diagnostic outcomes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CHILI_APP_NAME", "chili-alert-refresh-churn-audit")

from app.db import SessionLocal  # noqa: E402
from app.services.trading.exit_variant_policy import (  # noqa: E402
    NON_POSITIVE_EXIT_NOOP_REASONS,
    STRUCTURAL_EXIT_NOOP_PREFIXES,
    STRUCTURAL_EXIT_NOOP_REASONS,
)
from app.services.trading.recert_rescue_policy import (  # noqa: E402
    CONDITIONAL_RECERT_RESCUE_BACKTEST_ACTION,
    recert_rescue_blocker_actions,
    recert_rescue_blocker_reasons,
)


TARGET_WORK = ("recert_rescue_refresh", "exit_variant_refresh")
TARGET_DIAGNOSTICS = ("recert_rescue_diagnostic", "exit_variant_diagnostic")
OPEN_WORK_STATUSES = frozenset({"pending", "retry_wait", "processing"})
RECERT_BLOCKER_ACTIONS = tuple(recert_rescue_blocker_actions())
RECERT_BLOCKER_REASONS = tuple(recert_rescue_blocker_reasons())
RECERT_CONDITIONAL_BACKTEST_ACTION = CONDITIONAL_RECERT_RESCUE_BACKTEST_ACTION
EXIT_STRUCTURAL_NOOP_REASONS = tuple(sorted(STRUCTURAL_EXIT_NOOP_REASONS))
EXIT_STRUCTURAL_NOOP_PREFIX_PATTERNS = tuple(
    f"{prefix}%" for prefix in STRUCTURAL_EXIT_NOOP_PREFIXES
)
EXIT_NON_POSITIVE_NOOP_REASONS = tuple(sorted(NON_POSITIVE_EXIT_NOOP_REASONS))
APPLICATION_NAME = "chili-alert-refresh-churn-audit"
DEFAULT_STATEMENT_TIMEOUT_MS = 5000
DEFAULT_LOCK_TIMEOUT_MS = 1000


class DatabaseUnavailable(RuntimeError):
    pass


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return int(default)
    return value if value > 0 else int(default)


def _read_only_guardrails() -> dict[str, int | str | bool]:
    return {
        "read_only": True,
        "statement_timeout_ms": _positive_int_env(
            "CHILI_ALERT_REFRESH_CHURN_STATEMENT_TIMEOUT_MS",
            DEFAULT_STATEMENT_TIMEOUT_MS,
        ),
        "lock_timeout_ms": _positive_int_env(
            "CHILI_ALERT_REFRESH_CHURN_LOCK_TIMEOUT_MS",
            DEFAULT_LOCK_TIMEOUT_MS,
        ),
        "application_name": APPLICATION_NAME,
    }


def _configure_read_only_session(db) -> None:
    guardrails = _read_only_guardrails()
    db.execute(text("SET TRANSACTION READ ONLY"))
    db.execute(
        text(f"SET LOCAL statement_timeout = {guardrails['statement_timeout_ms']}")
    )
    db.execute(text(f"SET LOCAL lock_timeout = {guardrails['lock_timeout_ms']}"))
    db.execute(text(f"SET LOCAL application_name = '{APPLICATION_NAME}'"))


def _rows(sql: str, params: dict) -> list[dict]:
    try:
        with SessionLocal() as db:
            try:
                _configure_read_only_session(db)
                rows = db.execute(text(sql), params).fetchall()
                return [dict(row._mapping) for row in rows]
            finally:
                db.rollback()
    except (OperationalError, DBAPIError) as exc:
        raise DatabaseUnavailable(str(exc)) from exc


def _print_table(title: str, rows: Iterable[dict]) -> None:
    rows = list(rows)
    print(f"\n## {title}")
    if not rows:
        print("(no rows)")
        return
    keys = list(rows[0].keys())
    widths = {key: max(len(key), *(len(str(row.get(key, ""))) for row in rows)) for key in keys}
    print(" | ".join(key.ljust(widths[key]) for key in keys))
    print("-+-".join("-" * widths[key] for key in keys))
    for row in rows:
        print(" | ".join(str(row.get(key, "")).ljust(widths[key]) for key in keys))


def _work_counts(hours: int) -> list[dict]:
    return _rows(
        """
        SELECT
          event_type,
          status,
          COALESCE(payload->>'source', '<none>') AS source,
          count(*) AS events,
          min(created_at) AS first_seen,
          max(created_at) AS last_seen
        FROM brain_work_events
        WHERE event_kind = 'work'
          AND event_type = ANY(:event_types)
          AND created_at >= now() - (:hours * interval '1 hour')
        GROUP BY event_type, status, COALESCE(payload->>'source', '<none>')
        ORDER BY events DESC, event_type, status, source
        """,
        {"event_types": list(TARGET_WORK), "hours": int(hours)},
    )


def _diagnostic_counts(hours: int) -> list[dict]:
    return _rows(
        """
        SELECT
          event_type,
          COALESCE(payload->>'source', '<none>') AS source,
          COALESCE(payload->>'skip_reason', '<none>') AS skip_reason,
          COALESCE(payload->>'recert_rescue_status', '<none>') AS recert_status,
          COALESCE(payload->>'recommended_next_action', '<none>') AS next_action,
          COALESCE(payload->>'fast_skipped', 'false') AS fast_skipped,
          COALESCE(payload->>'created_count', '<none>') AS created_count,
          count(*) AS events,
          max(created_at) AS last_seen
        FROM brain_work_events
        WHERE event_kind = 'outcome'
          AND event_type = ANY(:event_types)
          AND created_at >= now() - (:hours * interval '1 hour')
        GROUP BY
          event_type,
          COALESCE(payload->>'source', '<none>'),
          COALESCE(payload->>'skip_reason', '<none>'),
          COALESCE(payload->>'recert_rescue_status', '<none>'),
          COALESCE(payload->>'recommended_next_action', '<none>'),
          COALESCE(payload->>'fast_skipped', 'false'),
          COALESCE(payload->>'created_count', '<none>')
        ORDER BY events DESC, event_type
        LIMIT 40
        """,
        {"event_types": list(TARGET_DIAGNOSTICS), "hours": int(hours)},
    )


def _top_patterns(hours: int, limit: int) -> list[dict]:
    return _rows(
        """
        WITH target AS (
          SELECT
            event_type,
            status,
            payload,
            created_at,
            CASE
              WHEN COALESCE(payload->>'scan_pattern_id', '') ~ '^[0-9]+$'
              THEN (payload->>'scan_pattern_id')::bigint
              ELSE NULL
            END AS scan_pattern_id
          FROM brain_work_events
          WHERE event_kind = 'work'
            AND event_type = ANY(:event_types)
            AND created_at >= now() - (:hours * interval '1 hour')
        )
        SELECT
          t.scan_pattern_id,
          COALESCE(sp.name, '<missing>') AS pattern_name,
          COALESCE(sp.lifecycle_stage, '<null>') AS lifecycle_stage,
          COALESCE(sp.recert_reason, '<none>') AS recert_reason,
          COALESCE(t.payload->>'asset_class', '<none>') AS asset_class,
          COALESCE(t.payload->>'source', '<none>') AS source,
          count(*) FILTER (WHERE t.event_type = 'recert_rescue_refresh') AS recert_work,
          count(*) FILTER (WHERE t.event_type = 'exit_variant_refresh') AS exit_work,
          count(*) FILTER (WHERE t.status IN ('pending', 'retry_wait', 'processing')) AS open_work,
          count(*) FILTER (WHERE t.status = 'done') AS done_work,
          max(t.created_at) AS last_seen
        FROM target t
        LEFT JOIN scan_patterns sp ON sp.id = t.scan_pattern_id
        WHERE t.scan_pattern_id IS NOT NULL
        GROUP BY
          t.scan_pattern_id,
          COALESCE(sp.name, '<missing>'),
          COALESCE(sp.lifecycle_stage, '<null>'),
          COALESCE(sp.recert_reason, '<none>'),
          COALESCE(t.payload->>'asset_class', '<none>'),
          COALESCE(t.payload->>'source', '<none>')
        ORDER BY (count(*) FILTER (WHERE t.status IN ('pending', 'retry_wait', 'processing'))) DESC,
          (count(*) FILTER (WHERE t.event_type = 'recert_rescue_refresh')
           + count(*) FILTER (WHERE t.event_type = 'exit_variant_refresh')) DESC,
          last_seen DESC
        LIMIT :limit
        """,
        {"event_types": list(TARGET_WORK), "hours": int(hours), "limit": int(limit)},
    )


def _top_noop_exit_patterns(hours: int, limit: int) -> list[dict]:
    return _rows(
        """
        SELECT
          (e.payload->>'scan_pattern_id')::bigint AS scan_pattern_id,
          COALESCE(sp.name, '<missing>') AS pattern_name,
          COALESCE(e.payload->>'skip_reason', '<none>') AS skip_reason,
          COALESCE(e.payload->>'evidence_fingerprint', '<none>') AS evidence_fingerprint,
          count(*) AS noop_diagnostics,
          max(e.created_at) AS last_seen
        FROM brain_work_events e
        LEFT JOIN scan_patterns sp
          ON sp.id = (e.payload->>'scan_pattern_id')::bigint
        WHERE e.event_kind = 'outcome'
          AND e.event_type = 'exit_variant_diagnostic'
          AND e.created_at >= now() - (:hours * interval '1 hour')
          AND COALESCE(e.payload->>'scan_pattern_id', '') ~ '^[0-9]+$'
          AND (
            COALESCE(e.payload->>'created_count', '') = ''
            OR (
              COALESCE(e.payload->>'created_count', '') ~ '^-?[0-9]+$'
              AND (e.payload->>'created_count')::int = 0
            )
          )
        GROUP BY scan_pattern_id, pattern_name, skip_reason, evidence_fingerprint
        ORDER BY noop_diagnostics DESC, last_seen DESC
        LIMIT :limit
        """,
        {"hours": int(hours), "limit": int(limit)},
    )


def _top_noop_exit_pattern_rollups(hours: int, limit: int) -> list[dict]:
    return _rows(
        """
        WITH noop AS (
          SELECT
            (e.payload->>'scan_pattern_id')::bigint AS scan_pattern_id,
            COALESCE(e.payload->>'skip_reason', '<none>') AS skip_reason,
            NULLIF(COALESCE(e.payload->>'evidence_fingerprint', ''), '') AS evidence_fingerprint,
            e.created_at
          FROM brain_work_events e
          WHERE e.event_kind = 'outcome'
            AND e.event_type = 'exit_variant_diagnostic'
            AND e.created_at >= now() - (:hours * interval '1 hour')
            AND COALESCE(e.payload->>'scan_pattern_id', '') ~ '^[0-9]+$'
            AND (
              COALESCE(e.payload->>'created_count', '') = ''
              OR (
                COALESCE(e.payload->>'created_count', '') ~ '^-?[0-9]+$'
                AND (e.payload->>'created_count')::int = 0
              )
            )
        )
        SELECT
          n.scan_pattern_id,
          COALESCE(sp.name, '<missing>') AS pattern_name,
          COALESCE(sp.lifecycle_stage, '<null>') AS lifecycle_stage,
          n.skip_reason,
          count(*) AS noop_diagnostics,
          count(DISTINCT n.evidence_fingerprint) FILTER (
            WHERE n.evidence_fingerprint IS NOT NULL
          ) AS distinct_fingerprints,
          min(n.created_at) AS first_seen,
          max(n.created_at) AS last_seen
        FROM noop n
        LEFT JOIN scan_patterns sp ON sp.id = n.scan_pattern_id
        GROUP BY
          n.scan_pattern_id,
          COALESCE(sp.name, '<missing>'),
          COALESCE(sp.lifecycle_stage, '<null>'),
          n.skip_reason
        ORDER BY noop_diagnostics DESC, distinct_fingerprints DESC, last_seen DESC
        LIMIT :limit
        """,
        {"hours": int(hours), "limit": int(limit)},
    )


def _top_recert_rescue_blocker_rollups(hours: int, limit: int) -> list[dict]:
    return _rows(
        """
        WITH recert_blockers AS (
          SELECT
            (e.payload->>'scan_pattern_id')::bigint AS scan_pattern_id,
            COALESCE(e.payload->>'recert_rescue_status', '<none>') AS recert_status,
            COALESCE(e.payload->>'recommended_next_action', '<none>') AS next_action,
            COALESCE(e.payload->>'source', '<none>') AS source,
            e.created_at
          FROM brain_work_events e
          WHERE e.event_kind = 'outcome'
            AND e.event_type = 'recert_rescue_diagnostic'
            AND e.created_at >= now() - (:hours * interval '1 hour')
            AND COALESCE(e.payload->>'scan_pattern_id', '') ~ '^[0-9]+$'
            AND (
              COALESCE(e.payload->>'recommended_next_action', '') = ANY(:recert_actions)
              OR COALESCE(e.payload #>> '{recert_backtest_refresh,reason}', '')
                = ANY(:recert_reasons)
              OR (
                COALESCE(e.payload->>'recommended_next_action', '') = :conditional_action
                AND COALESCE(e.payload #>> '{recert_backtest_refresh,requested}', 'false') <> 'true'
              )
            )
        )
        SELECT
          r.scan_pattern_id,
          COALESCE(sp.name, '<missing>') AS pattern_name,
          COALESCE(sp.lifecycle_stage, '<null>') AS lifecycle_stage,
          COALESCE(sp.recert_reason, '<none>') AS recert_reason,
          r.recert_status,
          r.next_action,
          r.source,
          count(*) AS blocker_diagnostics,
          min(r.created_at) AS first_seen,
          max(r.created_at) AS last_seen
        FROM recert_blockers r
        LEFT JOIN scan_patterns sp ON sp.id = r.scan_pattern_id
        GROUP BY
          r.scan_pattern_id,
          COALESCE(sp.name, '<missing>'),
          COALESCE(sp.lifecycle_stage, '<null>'),
          COALESCE(sp.recert_reason, '<none>'),
          r.recert_status,
          r.next_action,
          r.source
        ORDER BY blocker_diagnostics DESC, last_seen DESC
        LIMIT :limit
        """,
        {
            "hours": int(hours),
            "limit": int(limit),
            "recert_actions": list(RECERT_BLOCKER_ACTIONS),
            "recert_reasons": list(RECERT_BLOCKER_REASONS),
            "conditional_action": RECERT_CONDITIONAL_BACKTEST_ACTION,
        },
    )


def _top_recert_rescue_action_rollups(hours: int, limit: int) -> list[dict]:
    return _rows(
        """
        WITH recert_actions AS (
          SELECT
            (e.payload->>'scan_pattern_id')::bigint AS scan_pattern_id,
            COALESCE(e.payload->>'recert_rescue_status', '<none>') AS recert_status,
            COALESCE(e.payload->>'recommended_next_action', '<none>') AS next_action,
            COALESCE(e.payload->>'source', '<none>') AS source,
            e.created_at
          FROM brain_work_events e
          WHERE e.event_kind = 'outcome'
            AND e.event_type = 'recert_rescue_diagnostic'
            AND e.created_at >= now() - (:hours * interval '1 hour')
            AND COALESCE(e.payload->>'scan_pattern_id', '') ~ '^[0-9]+$'
        )
        SELECT
          r.scan_pattern_id,
          COALESCE(sp.name, '<missing>') AS pattern_name,
          COALESCE(sp.lifecycle_stage, '<null>') AS lifecycle_stage,
          COALESCE(sp.recert_reason, '<none>') AS recert_reason,
          r.recert_status,
          r.next_action,
          r.source,
          count(*) AS action_diagnostics,
          min(r.created_at) AS first_seen,
          max(r.created_at) AS last_seen
        FROM recert_actions r
        LEFT JOIN scan_patterns sp ON sp.id = r.scan_pattern_id
        GROUP BY
          r.scan_pattern_id,
          COALESCE(sp.name, '<missing>'),
          COALESCE(sp.lifecycle_stage, '<null>'),
          COALESCE(sp.recert_reason, '<none>'),
          r.recert_status,
          r.next_action,
          r.source
        ORDER BY action_diagnostics DESC, last_seen DESC
        LIMIT :limit
        """,
        {"hours": int(hours), "limit": int(limit)},
    )


def _open_exit_work_with_recent_noop(hours: int, limit: int) -> list[dict]:
    return _rows(
        """
        WITH open_work AS (
          SELECT
            id,
            status,
            created_at,
            payload,
            CASE
              WHEN COALESCE(payload->>'scan_pattern_id', '') ~ '^[0-9]+$'
              THEN (payload->>'scan_pattern_id')::bigint
              ELSE NULL
            END AS scan_pattern_id,
            COALESCE(payload->>'evidence_fingerprint', '') AS evidence_fingerprint
          FROM brain_work_events
          WHERE event_kind = 'work'
            AND event_type = 'exit_variant_refresh'
            AND status IN ('pending', 'retry_wait', 'processing')
            AND created_at >= now() - (:hours * interval '1 hour')
        ),
        noop_diag AS (
          SELECT
            id,
            created_at,
            payload,
            (payload->>'scan_pattern_id')::bigint AS scan_pattern_id,
            COALESCE(payload->>'evidence_fingerprint', '') AS evidence_fingerprint,
            COALESCE(payload->>'skip_reason', '<none>') AS skip_reason
          FROM brain_work_events
          WHERE event_kind = 'outcome'
            AND event_type = 'exit_variant_diagnostic'
            AND created_at >= now() - (:hours * interval '1 hour')
            AND COALESCE(payload->>'scan_pattern_id', '') ~ '^[0-9]+$'
            AND (
              COALESCE(payload->>'created_count', '') = ''
              OR (
                COALESCE(payload->>'created_count', '') ~ '^-?[0-9]+$'
                AND (payload->>'created_count')::int = 0
              )
            )
        )
        SELECT DISTINCT ON (w.id)
          w.id AS work_id,
          w.status,
          w.scan_pattern_id,
          COALESCE(sp.name, '<missing>') AS pattern_name,
          COALESCE(w.payload->>'asset_class', '<none>') AS asset_class,
          COALESCE(w.payload->>'source', '<none>') AS work_source,
          COALESCE(NULLIF(w.evidence_fingerprint, ''), '<none>') AS work_fingerprint,
          d.id AS diagnostic_id,
          d.skip_reason,
          d.created_at AS diagnostic_seen,
          w.created_at AS work_created
        FROM open_work w
        JOIN noop_diag d
          ON d.scan_pattern_id = w.scan_pattern_id
         AND (
           d.evidence_fingerprint = w.evidence_fingerprint
           OR d.skip_reason = ANY(:structural_exit_noop_reasons)
           OR d.skip_reason LIKE ANY(:structural_exit_noop_prefixes)
           OR (
             d.skip_reason = ANY(:non_positive_exit_noop_reasons)
             AND NOT (
               (
                 COALESCE(w.payload->>'expected_evidence_value', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                 AND (w.payload->>'expected_evidence_value')::double precision > 0.0
               )
               OR (
                 COALESCE(w.payload->>'calibrated_ev_after_cost_pct', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                 AND (w.payload->>'calibrated_ev_after_cost_pct')::double precision > 0.0
               )
               OR (
                 COALESCE(w.payload->>'calibrated_ev_pct', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                 AND (w.payload->>'calibrated_ev_pct')::double precision > 0.0
               )
               OR (
                 COALESCE(w.payload->>'expected_net_pct', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                 AND (w.payload->>'expected_net_pct')::double precision > 0.0
               )
             )
           )
         )
        LEFT JOIN scan_patterns sp ON sp.id = w.scan_pattern_id
        WHERE w.scan_pattern_id IS NOT NULL
        ORDER BY w.id, d.created_at DESC, d.id DESC
        LIMIT :limit
        """,
        {
            "hours": int(hours),
            "limit": int(limit),
            "structural_exit_noop_reasons": list(EXIT_STRUCTURAL_NOOP_REASONS),
            "structural_exit_noop_prefixes": list(EXIT_STRUCTURAL_NOOP_PREFIX_PATTERNS),
            "non_positive_exit_noop_reasons": list(EXIT_NON_POSITIVE_NOOP_REASONS),
        },
    )


def _open_recert_work_with_recent_blocker_diagnostic(hours: int, limit: int) -> list[dict]:
    return _rows(
        """
        WITH open_work AS (
          SELECT
            id,
            status,
            created_at,
            payload,
            CASE
              WHEN COALESCE(payload->>'scan_pattern_id', '') ~ '^[0-9]+$'
              THEN (payload->>'scan_pattern_id')::bigint
              ELSE NULL
            END AS scan_pattern_id
          FROM brain_work_events
          WHERE event_kind = 'work'
            AND event_type = 'recert_rescue_refresh'
            AND status IN ('pending', 'retry_wait', 'processing')
            AND created_at >= now() - (:hours * interval '1 hour')
        ),
        recert_diag AS (
          SELECT
            id,
            created_at,
            payload,
            (payload->>'scan_pattern_id')::bigint AS scan_pattern_id,
            COALESCE(payload->>'recert_rescue_status', '<none>') AS recert_status,
            COALESCE(payload->>'recommended_next_action', '<none>') AS next_action
          FROM brain_work_events
          WHERE event_kind = 'outcome'
            AND event_type = 'recert_rescue_diagnostic'
            AND created_at >= now() - (:hours * interval '1 hour')
            AND COALESCE(payload->>'scan_pattern_id', '') ~ '^[0-9]+$'
            AND (
              COALESCE(payload->>'recommended_next_action', '') = ANY(:recert_actions)
              OR COALESCE(payload #>> '{recert_backtest_refresh,reason}', '')
                = ANY(:recert_reasons)
              OR (
                COALESCE(payload->>'recommended_next_action', '') = :conditional_action
                AND COALESCE(payload #>> '{recert_backtest_refresh,requested}', 'false') <> 'true'
              )
            )
        )
        SELECT DISTINCT ON (w.id)
          w.id AS work_id,
          w.status,
          w.scan_pattern_id,
          COALESCE(sp.name, '<missing>') AS pattern_name,
          COALESCE(sp.recert_reason, '<none>') AS recert_reason,
          COALESCE(w.payload->>'asset_class', '<none>') AS asset_class,
          COALESCE(w.payload->>'source', '<none>') AS work_source,
          d.id AS diagnostic_id,
          d.recert_status,
          d.next_action,
          d.created_at AS diagnostic_seen,
          w.created_at AS work_created
        FROM open_work w
        JOIN recert_diag d ON d.scan_pattern_id = w.scan_pattern_id
        LEFT JOIN scan_patterns sp ON sp.id = w.scan_pattern_id
        WHERE w.scan_pattern_id IS NOT NULL
        ORDER BY w.id, d.created_at DESC, d.id DESC
        LIMIT :limit
        """,
        {
            "hours": int(hours),
            "limit": int(limit),
            "recert_actions": list(RECERT_BLOCKER_ACTIONS),
            "recert_reasons": list(RECERT_BLOCKER_REASONS),
            "conditional_action": RECERT_CONDITIONAL_BACKTEST_ACTION,
        },
    )


def _duplicate_open_refresh_work(hours: int, limit: int) -> list[dict]:
    return _rows(
        """
        WITH open_work AS (
          SELECT
            event_type,
            status,
            created_at,
            dedupe_key,
            payload,
            CASE
              WHEN COALESCE(payload->>'scan_pattern_id', '') ~ '^[0-9]+$'
              THEN (payload->>'scan_pattern_id')::bigint
              ELSE NULL
            END AS scan_pattern_id
          FROM brain_work_events
          WHERE event_kind = 'work'
            AND event_type = ANY(:event_types)
            AND status IN ('pending', 'retry_wait', 'processing')
            AND created_at >= now() - (:hours * interval '1 hour')
        )
        SELECT
          open_work.event_type,
          open_work.status,
          open_work.scan_pattern_id,
          COALESCE(sp.name, '<missing>') AS pattern_name,
          COALESCE(open_work.payload->>'asset_class', '<none>') AS asset_class,
          COALESCE(open_work.payload->>'source', '<none>') AS source,
          count(*) AS open_work,
          count(DISTINCT open_work.dedupe_key) AS distinct_dedupe_keys,
          min(open_work.created_at) AS oldest_open,
          max(open_work.created_at) AS newest_open
        FROM open_work
        LEFT JOIN scan_patterns sp ON sp.id = open_work.scan_pattern_id
        WHERE open_work.scan_pattern_id IS NOT NULL
        GROUP BY
          open_work.event_type,
          open_work.status,
          open_work.scan_pattern_id,
          COALESCE(sp.name, '<missing>'),
          COALESCE(open_work.payload->>'asset_class', '<none>'),
          COALESCE(open_work.payload->>'source', '<none>')
        HAVING count(*) > 1
        ORDER BY open_work DESC, newest_open DESC
        LIMIT :limit
        """,
        {
            "event_types": list(TARGET_WORK),
            "hours": int(hours),
            "limit": int(limit),
        },
    )


def _recent_duplicate_suppressions(hours: int, limit: int) -> list[dict]:
    return _rows(
        """
        SELECT
          event_type,
          COALESCE(payload->>'duplicate_open_work_suppressed_reason', '<none>') AS reason,
          count(*) AS suppressed,
          count(DISTINCT payload->>'duplicate_open_work_kept_event_id') AS kept_events,
          max(updated_at) AS last_suppressed
        FROM brain_work_events
        WHERE event_kind = 'work'
          AND status = 'done'
          AND event_type = ANY(:event_types)
          AND updated_at >= now() - (:hours * interval '1 hour')
          AND COALESCE(payload->>'duplicate_open_work_suppressed', 'false') = 'true'
        GROUP BY
          event_type,
          COALESCE(payload->>'duplicate_open_work_suppressed_reason', '<none>')
        ORDER BY suppressed DESC, last_suppressed DESC
        LIMIT :limit
        """,
        {
            "event_types": list(TARGET_WORK),
            "hours": int(hours),
            "limit": int(limit),
        },
    )


def _safe_count(row: dict, key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _sum_rows(rows: Iterable[dict], key: str) -> int:
    return sum(_safe_count(row, key) for row in rows)


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        out = value
    elif value:
        try:
            out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if out.tzinfo is not None:
        out = out.astimezone(timezone.utc).replace(tzinfo=None)
    return out


def _oldest_age_seconds(
    values: Iterable[object],
    *,
    now: datetime | None = None,
) -> int | None:
    timestamps = [_coerce_datetime(value) for value in values]
    timestamps = [ts for ts in timestamps if ts is not None]
    if not timestamps:
        return None
    ref = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0, int((ref - min(timestamps)).total_seconds()))


def _alert_pressure_summary(report: dict[str, object]) -> dict[str, int | str | None]:
    work_counts = list(report.get("work_counts") or [])
    diagnostic_outcomes = list(report.get("diagnostic_outcomes") or [])
    noop_exit_rollups = list(report.get("top_noop_exit_variant_pattern_rollups") or [])
    recert_blocker_rollups = list(report.get("top_recert_rescue_blocker_rollups") or [])
    duplicate_suppressions = list(report.get("recent_duplicate_suppressions") or [])
    open_conflicts = (
        list(report.get("open_exit_variant_work_with_recent_noop") or [])
        + list(report.get("open_recert_work_with_recent_blocker_diagnostic") or [])
        + list(report.get("duplicate_open_refresh_work") or [])
    )

    open_work_events = 0
    done_work_events = 0
    recert_open_work_events = 0
    exit_open_work_events = 0
    open_first_seen_values: list[object] = []
    for row in work_counts:
        events = _safe_count(row, "events")
        status = str(row.get("status") or "")
        event_type = str(row.get("event_type") or "")
        if status in OPEN_WORK_STATUSES:
            open_work_events += events
            open_first_seen_values.append(row.get("first_seen"))
            if event_type == "recert_rescue_refresh":
                recert_open_work_events += events
            elif event_type == "exit_variant_refresh":
                exit_open_work_events += events
        elif status == "done":
            done_work_events += events

    open_conflict_rows = len(open_conflicts)
    diagnostic_events = _sum_rows(diagnostic_outcomes, "events")
    duplicate_suppressions_count = _sum_rows(duplicate_suppressions, "suppressed")
    historical_noise_events = (
        done_work_events
        + diagnostic_events
        + duplicate_suppressions_count
    )
    if open_conflict_rows:
        pressure_mode = "actionable_conflict"
    elif open_work_events:
        pressure_mode = "open_work"
    elif historical_noise_events:
        pressure_mode = "historical_noise"
    else:
        pressure_mode = "quiet"

    return {
        "status": "clear" if open_conflict_rows == 0 else "attention",
        "pressure_mode": pressure_mode,
        "open_work_events": open_work_events,
        "recert_open_work_events": recert_open_work_events,
        "exit_open_work_events": exit_open_work_events,
        "open_conflict_rows": open_conflict_rows,
        "completed_work_events": done_work_events,
        "diagnostic_events": diagnostic_events,
        "noop_exit_diagnostics": _sum_rows(noop_exit_rollups, "noop_diagnostics"),
        "recert_blocker_diagnostics": _sum_rows(
            recert_blocker_rollups,
            "blocker_diagnostics",
        ),
        "duplicate_suppressions": duplicate_suppressions_count,
        "historical_noise_events": historical_noise_events,
        "oldest_open_work_age_seconds": _oldest_age_seconds(open_first_seen_values),
        "oldest_open_conflict_age_seconds": _oldest_age_seconds(
            row.get("work_created") or row.get("first_seen")
            for row in open_conflicts
            if isinstance(row, dict)
        ),
    }


def _build_report(hours: int, limit: int) -> dict[str, object]:
    report: dict[str, object] = {
        "hours": int(hours),
        "limit": int(limit),
        "read_only_guardrails": _read_only_guardrails(),
        "work_counts": _work_counts(hours),
        "diagnostic_outcomes": _diagnostic_counts(hours),
        "top_work_producing_patterns": _top_patterns(hours, limit),
        "top_noop_exit_variant_diagnostics": _top_noop_exit_patterns(hours, limit),
        "top_noop_exit_variant_pattern_rollups": _top_noop_exit_pattern_rollups(
            hours,
            limit,
        ),
        "top_recert_rescue_blocker_rollups": _top_recert_rescue_blocker_rollups(
            hours,
            limit,
        ),
        "top_recert_rescue_action_rollups": _top_recert_rescue_action_rollups(
            hours,
            limit,
        ),
        "open_exit_variant_work_with_recent_noop": _open_exit_work_with_recent_noop(
            hours,
            limit,
        ),
        "open_recert_work_with_recent_blocker_diagnostic": (
            _open_recert_work_with_recent_blocker_diagnostic(hours, limit)
        ),
        "duplicate_open_refresh_work": _duplicate_open_refresh_work(hours, limit),
        "recent_duplicate_suppressions": _recent_duplicate_suppressions(hours, limit),
    }
    report["alert_pressure_summary"] = _alert_pressure_summary(report)
    return report


def _report_ok(report: dict[str, object]) -> bool:
    return not any(
        bool(report.get(key))
        for key in (
            "open_exit_variant_work_with_recent_noop",
            "open_recert_work_with_recent_blocker_diagnostic",
            "duplicate_open_refresh_work",
        )
    )


def _print_report(report: dict[str, object]) -> None:
    hours = int(report["hours"])
    limit = int(report["limit"])
    print(f"# alert-refresh-churn hours={hours} limit={limit}")
    _print_table("Alert Pressure Summary", [report["alert_pressure_summary"]])
    _print_table("Work Counts", report["work_counts"])
    _print_table("Diagnostic Outcomes", report["diagnostic_outcomes"])
    _print_table("Top Work-Producing Patterns", report["top_work_producing_patterns"])
    _print_table(
        "Top No-Op Exit Variant Diagnostics",
        report["top_noop_exit_variant_diagnostics"],
    )
    _print_table(
        "Top No-Op Exit Variant Pattern Rollups",
        report["top_noop_exit_variant_pattern_rollups"],
    )
    _print_table(
        "Top Recert Rescue Blocker Rollups",
        report["top_recert_rescue_blocker_rollups"],
    )
    _print_table(
        "Top Recert Rescue Action Rollups",
        report["top_recert_rescue_action_rollups"],
    )
    _print_table(
        "Open Exit Variant Work With Recent No-Op Evidence",
        report["open_exit_variant_work_with_recent_noop"],
    )
    _print_table(
        "Open Recert Work With Recent Blocker Diagnostic",
        report["open_recert_work_with_recent_blocker_diagnostic"],
    )
    _print_table("Duplicate Open Refresh Work", report["duplicate_open_refresh_work"])
    _print_table("Recent Duplicate Suppressions", report["recent_duplicate_suppressions"])


def _database_unavailable_payload(
    *,
    detail: str,
    hours: int,
    limit: int,
    wait_seconds: int,
) -> dict[str, object]:
    return {
        "ok": False,
        "error": "database_unavailable",
        "detail": detail,
        "hours": int(hours),
        "limit": int(limit),
        "wait_seconds": int(wait_seconds),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, help="lookback window")
    parser.add_argument("--limit", type=int, default=20, help="rows per top-pattern section")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help="retry read-only connection attempts for this many seconds",
    )
    args = parser.parse_args()
    hours = max(1, int(args.hours))
    limit = max(1, int(args.limit))
    wait_seconds = max(0, int(args.wait_seconds))

    deadline = time.monotonic() + wait_seconds
    last_unavailable: DatabaseUnavailable | None = None
    try:
        while True:
            try:
                report = _build_report(hours, limit)
                break
            except DatabaseUnavailable as exc:
                last_unavailable = exc
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(5.0, max(0.25, deadline - time.monotonic())))
    except DatabaseUnavailable as exc:
        if last_unavailable is not None:
            exc = last_unavailable
        detail = str(exc).splitlines()[0] if str(exc) else "connection unavailable"
        if args.json:
            payload = _database_unavailable_payload(
                detail=detail,
                hours=hours,
                limit=limit,
                wait_seconds=wait_seconds,
            )
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"# alert-refresh-churn hours={hours} limit={limit}")
            print(
                "Database is not accepting read-only connections yet; "
                "retry after Postgres health is healthy.",
                file=sys.stderr,
            )
            print(f"Detail: {detail}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {"ok": _report_ok(report), "wait_seconds": wait_seconds, **report},
                default=str,
                sort_keys=True,
            )
        )
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
