#!/usr/bin/env python
"""Read-only Phase 5AE alpha-decay envelope parity probe.

``alpha_decay.py`` is lifecycle-sensitive: it reads recent closed live
management envelopes, blends them with paper trades, and may demote promoted
patterns. This probe does not change decay behavior. It only compares the live
closed-envelope evidence currently read through the ``Trade`` ORM compatibility
view with the physical ``trading_management_envelopes`` source.
"""
from __future__ import annotations

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


LIVE_PROBE_OPT_IN = "PHASE5AE_ALLOW_LIVE_PROBE"
DEFAULT_WINDOW_DAYS = 30
ALPHA_DECAY_RELATIONS = {
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
        "Phase 5AE alpha-decay parity probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in ALPHA_DECAY_RELATIONS:
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
    out: list[dict[str, Any]] = []
    for row in result.mappings().all():
        out.append({str(key): _normalize_scalar(value) for key, value in dict(row).items()})
    return out


def _active_decay_pattern_ids(db, *, user_id: int | None = None) -> list[int]:
    params: dict[str, Any] = {}
    sql = """
        SELECT id
          FROM scan_patterns
         WHERE active IS TRUE
           AND lifecycle_stage IN ('live', 'promoted')
    """
    if user_id is not None:
        sql += " AND user_id = :uid"
        params["uid"] = int(user_id)
    sql += " ORDER BY id"
    return [int(row["id"]) for row in _rows(db, sql, params)]


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


def _decay_live_evidence_rows(
    db,
    *,
    relation_name: str,
    pattern_ids: list[int],
    cutoff: datetime,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    if not pattern_ids:
        return []
    relation = _relation_sql(relation_name)
    pattern_binds, params = _bind_int_list("sp", pattern_ids)
    params["cutoff"] = cutoff
    sql = f"""
        SELECT id,
               scan_pattern_id,
               user_id,
               exit_date,
               pnl,
               entry_price,
               exit_price,
               quantity,
               direction,
               status
          FROM {relation}
         WHERE status = 'closed'
           AND scan_pattern_id IN ({pattern_binds})
           AND exit_date >= :cutoff
    """
    if user_id is not None:
        sql += " AND user_id = :uid"
        params["uid"] = int(user_id)
    sql += " ORDER BY scan_pattern_id, exit_date, id"
    return _rows(db, sql, params)


def _half_life_live_evidence_rows(
    db,
    *,
    relation_name: str,
    pattern_ids: list[int],
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    if not pattern_ids:
        return []
    relation = _relation_sql(relation_name)
    pattern_binds, params = _bind_int_list("sp", pattern_ids)
    sql = f"""
        SELECT id,
               scan_pattern_id,
               user_id,
               exit_date,
               pnl,
               entry_price,
               exit_price,
               quantity,
               direction,
               status
          FROM {relation}
         WHERE scan_pattern_id IN ({pattern_binds})
           AND status = 'closed'
           AND exit_date IS NOT NULL
    """
    if user_id is not None:
        sql += " AND user_id = :uid"
        params["uid"] = int(user_id)
    sql += " ORDER BY scan_pattern_id, exit_date, id"
    return _rows(db, sql, params)


def _counts_by_pattern(rows: list[dict[str, Any]]) -> list[dict[str, int]]:
    counts: dict[int, int] = {}
    for row in rows:
        spid = row.get("scan_pattern_id")
        if spid is None:
            continue
        key = int(spid)
        counts[key] = counts.get(key, 0) + 1
    return [
        {"scan_pattern_id": spid, "n": n}
        for spid, n in sorted(counts.items())
    ]


def _scope_values(
    db,
    *,
    relation_name: str,
    pattern_ids: list[int],
    cutoff: datetime,
    user_id: int | None,
) -> dict[str, Any]:
    decay_rows = _decay_live_evidence_rows(
        db,
        relation_name=relation_name,
        pattern_ids=pattern_ids,
        cutoff=cutoff,
        user_id=user_id,
    )
    half_life_rows = _half_life_live_evidence_rows(
        db,
        relation_name=relation_name,
        pattern_ids=pattern_ids,
        user_id=user_id,
    )
    return {
        "active_decay_pattern_ids": pattern_ids,
        "decay_live_evidence_ids": [int(row["id"]) for row in decay_rows],
        "decay_live_counts_by_pattern": _counts_by_pattern(decay_rows),
        "half_life_live_evidence_ids": [int(row["id"]) for row in half_life_rows],
        "half_life_live_counts_by_pattern": _counts_by_pattern(half_life_rows),
    }


def run_probe(
    db,
    *,
    user_id: int | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    anchor = now or datetime.now(timezone.utc)
    cutoff = anchor - timedelta(days=max(1, int(window_days)))
    pattern_ids = _active_decay_pattern_ids(db, user_id=user_id)
    old_values = _scope_values(
        db,
        relation_name=LEGACY_TRADES_COMPAT_RELATION,
        pattern_ids=pattern_ids,
        cutoff=cutoff,
        user_id=user_id,
    )
    new_values = _scope_values(
        db,
        relation_name=MANAGEMENT_ENVELOPES_RELATION,
        pattern_ids=pattern_ids,
        cutoff=cutoff,
        user_id=user_id,
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
        f"{len(comparisons)} alpha-decay evidence checks matched"
        if status == "COMPLETE_POSITIVE"
        else "alpha-decay evidence parity drift or relation-kind drift"
    )
    return {
        "status": status,
        "reason": reason,
        "relation_kinds": relation_kinds,
        "checks": len(comparisons),
        "mismatches": mismatches,
        "comparisons": comparisons,
        "window_days": int(window_days),
        "user_id": user_id,
    }


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    _assert_probe_database_allowed(database_url)
    user_raw = os.getenv("PHASE5AE_USER_ID", "").strip()
    user_id = int(user_raw) if user_raw else None
    window_days = int(os.getenv("PHASE5AE_WINDOW_DAYS", str(DEFAULT_WINDOW_DAYS)) or DEFAULT_WINDOW_DAYS)
    db = SessionLocal()
    try:
        result = run_probe(db, user_id=user_id, window_days=window_days)
    finally:
        db.rollback()
        db.close()

    print(f"VERDICT_STATUS={result['status']}")
    print(f"VERDICT_REASON={result['reason']}")
    print(f"RELATION_KINDS={result['relation_kinds']}")
    print(f"WINDOW_DAYS={result['window_days']}")
    print(f"USER_ID={result['user_id']}")
    print(f"ALPHA_DECAY_CHECKS={result['checks']}")
    print(f"ALPHA_DECAY_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "ALPHA_DECAY_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
