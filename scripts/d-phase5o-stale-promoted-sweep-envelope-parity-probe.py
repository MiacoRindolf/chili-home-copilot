#!/usr/bin/env python
"""Read-only Phase 5O stale-promoted sweep envelope parity probe.

``stale_promoted_sweep.py`` is lifecycle-sensitive: it uses the latest closed
live management envelope per promoted pattern to decide which promoted patterns
are stale enough to re-check against the realized-EV gate. This probe does not
call the sweep and does not demote anything. It only compares the legacy
``Trade`` compatibility view with the physical ``trading_management_envelopes``
source for the latest-exit/stale-eligibility scope.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
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


LIVE_PROBE_OPT_IN = "PHASE5O_STALE_PROMOTED_ALLOW_LIVE_PROBE"
STALE_PROMOTED_RELATIONS = {
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
        "Phase 5O stale-promoted sweep probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in STALE_PROMOTED_RELATIONS:
        raise ValueError(f"unsupported relation: {relation_name!r}")
    return relation_name


def _relation_kind(db, relation_name: str) -> str | None:
    return db.execute(
        text("SELECT relkind FROM pg_class WHERE relname = :name"),
        {"name": relation_name},
    ).scalar()


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _rows(db, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = db.execute(text(sql), params or {})
    return [
        {str(key): _normalize_scalar(value) for key, value in dict(row).items()}
        for row in result.mappings().all()
    ]


def _promoted_pattern_ids(db) -> list[int]:
    return [
        int(row["id"])
        for row in _rows(
            db,
            """
            SELECT id
              FROM scan_patterns
             WHERE lifecycle_stage = 'promoted'
               AND active IS TRUE
             ORDER BY id
            """,
        )
    ]


def _bind_int_list(prefix: str, values: list[int]) -> tuple[str, dict[str, int]]:
    binds: list[str] = []
    params: dict[str, int] = {}
    for idx, value in enumerate(values):
        key = f"{prefix}_{idx}"
        binds.append(f":{key}")
        params[key] = int(value)
    if not binds:
        return "NULL", {}
    return ", ".join(binds), params


def _latest_exit_rows(
    db,
    *,
    relation_name: str,
    pattern_ids: list[int],
) -> list[dict[str, Any]]:
    if not pattern_ids:
        return []
    relation = _relation_sql(relation_name)
    pattern_binds, params = _bind_int_list("sp", pattern_ids)
    return _rows(
        db,
        f"""
        SELECT scan_pattern_id,
               max(exit_date) AS latest_exit
          FROM {relation}
         WHERE scan_pattern_id IN ({pattern_binds})
         GROUP BY scan_pattern_id
         ORDER BY scan_pattern_id
        """,
        params,
    )


def _latest_exit_map(rows: list[dict[str, Any]]) -> dict[int, str | None]:
    return {
        int(row["scan_pattern_id"]): row["latest_exit"]
        for row in rows
        if row.get("scan_pattern_id") is not None
    }


def _stale_candidates(
    latest_by_pattern: dict[int, str | None],
    *,
    pattern_ids: list[int],
    stale_cutoff: datetime,
) -> list[int]:
    stale: list[int] = []
    cutoff = stale_cutoff.astimezone(timezone.utc).replace(tzinfo=None)
    for pattern_id in sorted(pattern_ids):
        latest_raw = latest_by_pattern.get(pattern_id)
        if latest_raw is None:
            stale.append(pattern_id)
            continue
        latest = datetime.fromisoformat(str(latest_raw).replace("Z", "+00:00"))
        latest_naive = latest.astimezone(timezone.utc).replace(tzinfo=None)
        if latest_naive < cutoff:
            stale.append(pattern_id)
    return stale


def _scope_values(
    db,
    *,
    relation_name: str,
    pattern_ids: list[int],
    stale_cutoff: datetime,
) -> dict[str, Any]:
    latest_rows = _latest_exit_rows(
        db,
        relation_name=relation_name,
        pattern_ids=pattern_ids,
    )
    latest_by_pattern = _latest_exit_map(latest_rows)
    recent = sorted(
        pattern_id
        for pattern_id in pattern_ids
        if pattern_id in latest_by_pattern
        and pattern_id
        not in _stale_candidates(
            latest_by_pattern,
            pattern_ids=[pattern_id],
            stale_cutoff=stale_cutoff,
        )
    )
    stale = _stale_candidates(
        latest_by_pattern,
        pattern_ids=pattern_ids,
        stale_cutoff=stale_cutoff,
    )
    return {
        "latest_exit_by_pattern": sorted(
            (
                {"scan_pattern_id": pattern_id, "latest_exit": latest}
                for pattern_id, latest in latest_by_pattern.items()
            ),
            key=lambda row: row["scan_pattern_id"],
        ),
        "recent_pattern_ids": recent,
        "stale_candidate_pattern_ids": stale,
    }


def run_probe(db, *, now: datetime | None = None) -> dict[str, Any]:
    effective_now = now or datetime.now(timezone.utc)
    stale_cutoff = effective_now - timedelta(days=7)
    pattern_ids = _promoted_pattern_ids(db)
    old_values = _scope_values(
        db,
        relation_name=LEGACY_TRADES_COMPAT_RELATION,
        pattern_ids=pattern_ids,
        stale_cutoff=stale_cutoff,
    )
    new_values = _scope_values(
        db,
        relation_name=MANAGEMENT_ENVELOPES_RELATION,
        pattern_ids=pattern_ids,
        stale_cutoff=stale_cutoff,
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
        f"{len(comparisons)} stale-promoted sweep checks matched"
        if status == "COMPLETE_POSITIVE"
        else "stale-promoted sweep parity drift or relation-kind drift"
    )
    return {
        "status": status,
        "reason": reason,
        "relation_kinds": relation_kinds,
        "checks": len(comparisons),
        "mismatches": mismatches,
        "promoted_pattern_count": len(pattern_ids),
        "stale_cutoff": stale_cutoff.isoformat(),
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
    print(f"PROMOTED_PATTERN_COUNT={result['promoted_pattern_count']}")
    print(f"STALE_CUTOFF={result['stale_cutoff']}")
    print(f"STALE_PROMOTED_CHECKS={result['checks']}")
    print(f"STALE_PROMOTED_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "STALE_PROMOTED_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
