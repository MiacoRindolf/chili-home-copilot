#!/usr/bin/env python
"""Read-only Phase 5O learning.py envelope parity probe.

``learning.py`` is not a passive report reader. It consumes closed management
envelopes to reinforce pattern insights, write journals, aggregate corrected
ScanPattern evidence, and summarize setup-vitals degradation outcomes. This
probe does not call any learning writer. It compares the evidence rows that
would feed those paths through the legacy ``trading_trades`` compatibility view
and the physical ``trading_management_envelopes`` source.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault(
    "DATABASE_URL",
    os.getenv("TEST_DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili_test"),
)

from app.db import SessionLocal  # noqa: E402
from app.models.trade_relation_symbols import (  # noqa: E402
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
)


LIVE_PROBE_OPT_IN = "PHASE5O_LEARNING_ALLOW_LIVE_PROBE"
LEARNING_RELATIONS = {
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
}


def _live_probe_enabled() -> bool:
    return str(os.getenv(LIVE_PROBE_OPT_IN, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def _is_test_database_url(url: str | None) -> bool:
    return "_test" in str(url or "").split("?", 1)[0].lower()


def _assert_probe_database_allowed(database_url: str | None) -> None:
    if _is_test_database_url(database_url) or _live_probe_enabled():
        return
    raise RuntimeError(
        "Phase 5O learning.py probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in LEARNING_RELATIONS:
        raise ValueError(f"unsupported relation: {relation_name!r}")
    return relation_name


def _relation_kind(db, relation_name: str) -> str | None:
    return db.execute(
        text("SELECT relkind FROM pg_class WHERE relname = :name"),
        {"name": relation_name},
    ).scalar()


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _rows(db, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = db.execute(text(sql), params or {})
    return [
        {str(key): _normalize_scalar(value) for key, value in dict(row).items()}
        for row in result.mappings().all()
    ]


def _coverage_pct(closed_trades: int, closed_with_scan_pattern_id: int) -> float | None:
    if closed_trades <= 0:
        return None
    return round(100.0 * closed_with_scan_pattern_id / closed_trades, 2)


def _attribution_coverage_by_user(db, *, relation_name: str) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    rows = _rows(
        db,
        f"""
        SELECT user_id,
               COUNT(*) FILTER (WHERE status = 'closed') AS closed_trades,
               COUNT(*) FILTER (
                   WHERE status = 'closed' AND scan_pattern_id IS NOT NULL
               ) AS closed_with_scan_pattern_id
          FROM {relation}
         GROUP BY user_id
         ORDER BY user_id NULLS FIRST
        """,
    )
    return [
        {
            "user_id": row["user_id"],
            "closed_trades": int(row["closed_trades"]),
            "closed_with_scan_pattern_id": int(row["closed_with_scan_pattern_id"]),
            "coverage_pct": _coverage_pct(
                int(row["closed_trades"]),
                int(row["closed_with_scan_pattern_id"]),
            ),
        }
        for row in rows
        if int(row["closed_trades"]) > 0
    ]


def _closed_trade_analysis_rows(db, *, relation_name: str) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    return _rows(
        db,
        f"""
        SELECT id,
               user_id,
               ticker,
               direction,
               entry_price,
               exit_price,
               quantity,
               entry_date,
               exit_date,
               pnl,
               scan_pattern_id,
               exit_reason,
               indicator_snapshot IS NOT NULL AS has_indicator_snapshot
          FROM {relation}
         WHERE status = 'closed'
         ORDER BY exit_date NULLS LAST, id
        """,
    )


def _evidence_correction_closed_rows(
    db,
    *,
    relation_name: str,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    return _rows(
        db,
        f"""
        SELECT scan_pattern_id,
               id,
               entry_price,
               exit_price,
               entry_date,
               exit_date,
               direction,
               ticker,
               pnl
          FROM {relation}
         WHERE status = 'closed'
           AND scan_pattern_id IS NOT NULL
           AND entry_date IS NOT NULL
           AND exit_date IS NOT NULL
           AND exit_date >= :cutoff
         ORDER BY scan_pattern_id, exit_date, id
        """,
        {"cutoff": cutoff},
    )


def _evidence_pattern_buckets(
    db,
    *,
    relation_name: str,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    return _rows(
        db,
        f"""
        SELECT scan_pattern_id,
               COUNT(*) AS closed_count,
               COUNT(*) FILTER (
                   WHERE entry_price IS NOT NULL
                     AND exit_price IS NOT NULL
                     AND entry_price > 0
               ) AS correction_candidate_count,
               SUM(COALESCE(pnl, 0)) AS pnl_sum,
               MIN(exit_date) AS first_exit,
               MAX(exit_date) AS latest_exit
          FROM {relation}
         WHERE status = 'closed'
           AND scan_pattern_id IS NOT NULL
           AND entry_date IS NOT NULL
           AND exit_date IS NOT NULL
           AND exit_date >= :cutoff
         GROUP BY scan_pattern_id
         ORDER BY scan_pattern_id
        """,
        {"cutoff": cutoff},
    )


def _actual_trade_count_by_pattern(db, *, relation_name: str) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    return _rows(
        db,
        f"""
        SELECT scan_pattern_id,
               COUNT(*) AS trade_count
          FROM {relation}
         WHERE scan_pattern_id IS NOT NULL
         GROUP BY scan_pattern_id
         ORDER BY scan_pattern_id
        """,
    )


def _setup_vitals_closed_join(
    db,
    *,
    relation_name: str,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    return _rows(
        db,
        f"""
        SELECT h.id AS history_id,
               h.trade_id,
               h.created_at,
               t.id AS envelope_id,
               t.status,
               t.pnl,
               h.degradation_flags ->> 'degraded_3plus' AS degraded_3plus
          FROM trading_setup_vitals_history h
          JOIN {relation} t
            ON h.trade_id = t.id
         WHERE h.created_at >= :cutoff
           AND t.status = 'closed'
         ORDER BY h.created_at, h.id
         LIMIT 5000
        """,
        {"cutoff": cutoff},
    )


def _scope_values(
    db,
    *,
    relation_name: str,
    cutoff: datetime,
) -> dict[str, Any]:
    return {
        "actual_trade_count_by_pattern": _actual_trade_count_by_pattern(
            db,
            relation_name=relation_name,
        ),
        "attribution_coverage_by_user": _attribution_coverage_by_user(
            db,
            relation_name=relation_name,
        ),
        "closed_trade_analysis_rows": _closed_trade_analysis_rows(
            db,
            relation_name=relation_name,
        ),
        "evidence_correction_closed_rows": _evidence_correction_closed_rows(
            db,
            relation_name=relation_name,
            cutoff=cutoff,
        ),
        "evidence_pattern_buckets": _evidence_pattern_buckets(
            db,
            relation_name=relation_name,
            cutoff=cutoff,
        ),
        "setup_vitals_closed_join": _setup_vitals_closed_join(
            db,
            relation_name=relation_name,
            cutoff=cutoff,
        ),
    }


def run_probe(db, *, now: datetime | None = None) -> dict[str, Any]:
    effective_now = now or datetime.now(timezone.utc)
    cutoff = effective_now - timedelta(days=180)
    old_values = _scope_values(
        db,
        relation_name=LEGACY_TRADES_COMPAT_RELATION,
        cutoff=cutoff,
    )
    new_values = _scope_values(
        db,
        relation_name=MANAGEMENT_ENVELOPES_RELATION,
        cutoff=cutoff,
    )

    comparisons: list[dict[str, Any]] = []
    mismatches = 0
    for scope in sorted(old_values):
        old = old_values[scope]
        new = new_values[scope]
        match = old == new
        if not match:
            mismatches += 1
        comparisons.append(
            {
                "scope": scope,
                "match": match,
                "old_count": len(old),
                "new_count": len(new),
                "old": old,
                "new": new,
            }
        )

    relation_kinds = {
        MANAGEMENT_ENVELOPES_RELATION: _relation_kind(db, MANAGEMENT_ENVELOPES_RELATION),
        LEGACY_TRADES_COMPAT_RELATION: _relation_kind(db, LEGACY_TRADES_COMPAT_RELATION),
    }
    expected_relations = (
        relation_kinds.get(MANAGEMENT_ENVELOPES_RELATION) == "r"
        and relation_kinds.get(LEGACY_TRADES_COMPAT_RELATION) == "v"
    )
    status = "COMPLETE_POSITIVE" if mismatches == 0 and expected_relations else "ALERT"
    reason = (
        f"{len(comparisons)} learning.py evidence checks matched"
        if status == "COMPLETE_POSITIVE"
        else "learning.py evidence parity drift or relation-kind drift"
    )
    return {
        "status": status,
        "reason": reason,
        "relation_kinds": relation_kinds,
        "checks": len(comparisons),
        "mismatches": mismatches,
        "cutoff": cutoff.isoformat(),
        "comparisons": comparisons,
    }


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    _assert_probe_database_allowed(database_url)
    db = SessionLocal()
    try:
        result = run_probe(db)
    finally:
        db.rollback()
        db.close()

    print(f"VERDICT_STATUS={result['status']}")
    print(f"VERDICT_REASON={result['reason']}")
    print(f"RELATION_KINDS={result['relation_kinds']}")
    print(f"LEARNING_CUTOFF={result['cutoff']}")
    print(f"LEARNING_CHECKS={result['checks']}")
    print(f"LEARNING_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "LEARNING_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
