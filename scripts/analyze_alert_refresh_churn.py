"""Read-only report for recert-rescue and exit-variant alert refresh churn.

The cash-deployment and edge-reliability surfaces can recommend
``recert_rescue_refresh`` or ``exit_variant_refresh`` even when the queue has
recently learned that a specific refresh is a no-op. This report separates the
visible recommendation mix from actual queued work and diagnostic outcomes.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CHILI_APP_NAME", "chili-alert-refresh-churn-audit")

from app.db import SessionLocal  # noqa: E402


TARGET_WORK = ("recert_rescue_refresh", "exit_variant_refresh")
TARGET_DIAGNOSTICS = ("recert_rescue_diagnostic", "exit_variant_diagnostic")


def _rows(sql: str, params: dict) -> list[dict]:
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        rows = db.execute(text(sql), params).fetchall()
        db.rollback()
        return [dict(row._mapping) for row in rows]


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
        GROUP BY event_type, status, source
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
          event_type, source, skip_reason, recert_status, next_action,
          fast_skipped, created_count
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
          t.scan_pattern_id, pattern_name, lifecycle_stage, recert_reason,
          asset_class, source
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
           OR d.skip_reason IN (
             'duplicate_learned_exit_label',
             'missing_parent_payoff_geometry',
             'non_positive_quality_evidence_no_exit_variant_birth',
             'no_loss_report',
             'no_parent_returns'
           )
           OR d.skip_reason LIKE 'edge_debt_too_negative_for_exit_child:%%'
           OR d.skip_reason LIKE 'insufficient_parent_payoff_samples:%%'
           OR d.skip_reason LIKE 'reward_risk_below_floor:%%'
         )
        LEFT JOIN scan_patterns sp ON sp.id = w.scan_pattern_id
        WHERE w.scan_pattern_id IS NOT NULL
        ORDER BY w.id, d.created_at DESC, d.id DESC
        LIMIT :limit
        """,
        {"hours": int(hours), "limit": int(limit)},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, help="lookback window")
    parser.add_argument("--limit", type=int, default=20, help="rows per top-pattern section")
    args = parser.parse_args()
    hours = max(1, int(args.hours))
    limit = max(1, int(args.limit))

    print(f"# alert-refresh-churn hours={hours} limit={limit}")
    _print_table("Work Counts", _work_counts(hours))
    _print_table("Diagnostic Outcomes", _diagnostic_counts(hours))
    _print_table("Top Work-Producing Patterns", _top_patterns(hours, limit))
    _print_table("Top No-Op Exit Variant Diagnostics", _top_noop_exit_patterns(hours, limit))
    _print_table(
        "Open Exit Variant Work With Recent No-Op Evidence",
        _open_exit_work_with_recent_noop(hours, limit),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
