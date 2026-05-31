#!/usr/bin/env python
"""Read-only Phase 5O live_drift envelope parity probe.

``live_drift.py`` compares runtime live/paper outcomes against research
baselines and may mark promoted/live patterns as challenged when drift is
critical. This probe does not call the drift refresh or mutate ScanPattern
validation contracts. It only compares the live closed-envelope inputs read
through the legacy ``trading_trades`` compatibility view and the physical
``trading_management_envelopes`` source.
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

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.trade_relation_symbols import (  # noqa: E402
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
)


LIVE_PROBE_OPT_IN = "PHASE5O_LIVE_DRIFT_ALLOW_LIVE_PROBE"
PROBE_USER_ID_ENV = "PHASE5O_LIVE_DRIFT_USER_ID"
LIVE_DRIFT_RELATIONS = {
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
}
REPEATABLE_EDGE_ORIGINS = ("web_discovered", "brain_discovered")


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
        "Phase 5O live_drift probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in LIVE_DRIFT_RELATIONS:
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


def _probe_user_id() -> int | None:
    override = os.getenv(PROBE_USER_ID_ENV)
    if override:
        try:
            return int(override)
        except (TypeError, ValueError):
            return None
    value = getattr(settings, "brain_default_user_id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _active_repeatable_pattern_ids(db) -> list[int]:
    rows = _rows(
        db,
        """
        SELECT id
          FROM scan_patterns
         WHERE active IS TRUE
           AND origin IN ('web_discovered', 'brain_discovered')
           AND lifecycle_stage IN ('promoted', 'live')
         ORDER BY id
        """,
    )
    return [int(row["id"]) for row in rows]


def _live_runtime_rows(
    db,
    *,
    relation_name: str,
    scan_pattern_ids: list[int],
    user_id: int,
    since: datetime,
) -> list[dict[str, Any]]:
    if not scan_pattern_ids:
        return []
    relation = _relation_sql(relation_name)
    binds, params = _bind_int_list("sp", scan_pattern_ids)
    params.update({"user_id": int(user_id), "since": since})
    return _rows(
        db,
        f"""
        SELECT id,
               user_id,
               scan_pattern_id,
               ticker,
               direction,
               entry_price,
               exit_price,
               quantity,
               exit_date,
               pnl,
               tca_entry_slippage_bps,
               tca_exit_slippage_bps,
               trade_type,
               asset_kind,
               indicator_snapshot IS NOT NULL AS has_indicator_snapshot,
               CASE
                   WHEN entry_price > 0
                    AND quantity > 0
                    AND pnl IS NOT NULL
                   THEN ROUND(((pnl / (entry_price * quantity)) * 100.0)::numeric, 6)
                   ELSE NULL
               END AS notional_return_pct
          FROM {relation}
         WHERE user_id = :user_id
           AND status = 'closed'
           AND scan_pattern_id IN ({binds})
           AND exit_date IS NOT NULL
           AND exit_date >= :since
         ORDER BY scan_pattern_id, exit_date, id
        """,
        params,
    )


def _live_win_counts(
    db,
    *,
    relation_name: str,
    scan_pattern_ids: list[int],
    user_id: int,
    since: datetime,
) -> list[dict[str, Any]]:
    if not scan_pattern_ids:
        return []
    relation = _relation_sql(relation_name)
    binds, params = _bind_int_list("sp", scan_pattern_ids)
    params.update({"user_id": int(user_id), "since": since})
    return _rows(
        db,
        f"""
        SELECT scan_pattern_id,
               COUNT(*) AS n_live,
               COUNT(*) FILTER (WHERE COALESCE(pnl, 0) > 0) AS wins_live,
               SUM(COALESCE(pnl, 0)) AS pnl_sum,
               MIN(exit_date) AS first_exit,
               MAX(exit_date) AS latest_exit
          FROM {relation}
         WHERE user_id = :user_id
           AND status = 'closed'
           AND scan_pattern_id IN ({binds})
           AND exit_date IS NOT NULL
           AND exit_date >= :since
         GROUP BY scan_pattern_id
         ORDER BY scan_pattern_id
        """,
        params,
    )


def _slippage_inputs(
    db,
    *,
    relation_name: str,
    scan_pattern_ids: list[int],
    user_id: int,
    since: datetime,
) -> list[dict[str, Any]]:
    if not scan_pattern_ids:
        return []
    relation = _relation_sql(relation_name)
    binds, params = _bind_int_list("sp", scan_pattern_ids)
    params.update({"user_id": int(user_id), "since": since})
    return _rows(
        db,
        f"""
        SELECT id,
               scan_pattern_id,
               tca_entry_slippage_bps,
               tca_exit_slippage_bps
          FROM {relation}
         WHERE user_id = :user_id
           AND status = 'closed'
           AND scan_pattern_id IN ({binds})
           AND exit_date IS NOT NULL
           AND exit_date >= :since
           AND (
               tca_entry_slippage_bps IS NOT NULL
               OR tca_exit_slippage_bps IS NOT NULL
           )
         ORDER BY scan_pattern_id, id
        """,
        params,
    )


def _scope_values(
    db,
    *,
    relation_name: str,
    scan_pattern_ids: list[int],
    user_id: int,
    since: datetime,
) -> dict[str, Any]:
    return {
        "live_runtime_rows": _live_runtime_rows(
            db,
            relation_name=relation_name,
            scan_pattern_ids=scan_pattern_ids,
            user_id=user_id,
            since=since,
        ),
        "live_slippage_inputs": _slippage_inputs(
            db,
            relation_name=relation_name,
            scan_pattern_ids=scan_pattern_ids,
            user_id=user_id,
            since=since,
        ),
        "live_win_counts": _live_win_counts(
            db,
            relation_name=relation_name,
            scan_pattern_ids=scan_pattern_ids,
            user_id=user_id,
            since=since,
        ),
    }


def run_probe(db, *, now: datetime | None = None) -> dict[str, Any]:
    effective_now = now or datetime.now(timezone.utc)
    window_days = int(getattr(settings, "brain_live_drift_window_days", 120) or 120)
    since = effective_now - timedelta(days=max(1, window_days))
    user_id = _probe_user_id()
    if user_id is None:
        return {
            "status": "ALERT",
            "reason": "brain_default_user_id missing",
            "relation_kinds": {},
            "checks": 0,
            "mismatches": 0,
            "pattern_count": 0,
            "window_days": window_days,
            "since": since.isoformat(),
            "comparisons": [],
        }

    pattern_ids = _active_repeatable_pattern_ids(db)
    old_values = _scope_values(
        db,
        relation_name=LEGACY_TRADES_COMPAT_RELATION,
        scan_pattern_ids=pattern_ids,
        user_id=int(user_id),
        since=since,
    )
    new_values = _scope_values(
        db,
        relation_name=MANAGEMENT_ENVELOPES_RELATION,
        scan_pattern_ids=pattern_ids,
        user_id=int(user_id),
        since=since,
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
        f"{len(comparisons)} live_drift live-runtime checks matched"
        if status == "COMPLETE_POSITIVE"
        else "live_drift live-runtime parity drift or relation-kind drift"
    )
    return {
        "status": status,
        "reason": reason,
        "relation_kinds": relation_kinds,
        "checks": len(comparisons),
        "mismatches": mismatches,
        "pattern_count": len(pattern_ids),
        "window_days": window_days,
        "user_id": int(user_id),
        "since": since.isoformat(),
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
    print(f"RELATION_KINDS={result.get('relation_kinds')}")
    print(f"LIVE_DRIFT_USER_ID={result.get('user_id')}")
    print(f"LIVE_DRIFT_WINDOW_DAYS={result['window_days']}")
    print(f"LIVE_DRIFT_SINCE={result['since']}")
    print(f"LIVE_DRIFT_PATTERN_COUNT={result['pattern_count']}")
    print(f"LIVE_DRIFT_CHECKS={result['checks']}")
    print(f"LIVE_DRIFT_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "LIVE_DRIFT_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
