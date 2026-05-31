#!/usr/bin/env python
"""Read-only Phase 5O pattern-position-monitor envelope parity probe.

``pattern_position_monitor.py`` is a live monitor/advisory surface: it selects
open positions, may reconcile stale broker rows, persists monitor decisions,
can tighten stored stop/target levels, and emits exit/tighten alerts. This
probe does not call the monitor. It compares only the current selection scope
and monitor-visible row fields between the legacy compatibility view and the
physical management-envelope table.
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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
from app.services.trading.autopilot_scope import is_option_trade  # noqa: E402


LIVE_PROBE_OPT_IN = "PHASE5O_PATTERN_MONITOR_ALLOW_LIVE_PROBE"
MONITOR_RELATIONS = {
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
        "Phase 5O pattern-monitor probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in MONITOR_RELATIONS:
        raise ValueError(f"unsupported relation: {relation_name!r}")
    return relation_name


def _relation_kind(db, relation_name: str) -> str | None:
    return db.execute(
        text("SELECT relkind FROM pg_class WHERE relname = :name"),
        {"name": relation_name},
    ).scalar()


def _normalize_json(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(value, dict):
        return value
    return {}


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    return value


def _rows(db, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = db.execute(text(sql), params or {})
    out: list[dict[str, Any]] = []
    for row in result.mappings().all():
        normalized = {str(key): _normalize_scalar(value) for key, value in dict(row).items()}
        if "indicator_snapshot" in normalized:
            normalized["indicator_snapshot"] = _normalize_json(normalized["indicator_snapshot"])
        out.append(normalized)
    return out


def _monitor_rows(db, *, relation_name: str) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    return _rows(
        db,
        f"""
        WITH pattern_rows AS (
            SELECT *, 'pattern_linked'::text AS monitor_lane
              FROM {relation}
             WHERE status = 'open'
               AND related_alert_id IS NOT NULL
        ),
        plan_rows AS (
            SELECT *, 'plan_levels'::text AS monitor_lane
              FROM {relation}
             WHERE status = 'open'
               AND related_alert_id IS NULL
               AND (stop_loss IS NOT NULL OR take_profit IS NOT NULL)
               AND id NOT IN (SELECT id FROM pattern_rows)
        )
        SELECT id,
               user_id,
               UPPER(ticker) AS ticker,
               status,
               direction,
               broker_source,
               auto_trader_version,
               related_alert_id,
               scan_pattern_id,
               stop_loss,
               take_profit,
               entry_price,
               quantity,
               asset_kind,
               tags,
               indicator_snapshot,
               monitor_lane
          FROM (
                SELECT * FROM pattern_rows
                UNION ALL
                SELECT * FROM plan_rows
               ) rows
         ORDER BY id
        """,
    )


def _as_runtime(row: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**row)


def _row_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": row.get("user_id"),
        "ticker": row.get("ticker"),
        "status": row.get("status"),
        "direction": row.get("direction"),
        "broker_source": row.get("broker_source"),
        "auto_trader_version": row.get("auto_trader_version"),
        "related_alert_id": row.get("related_alert_id"),
        "scan_pattern_id": row.get("scan_pattern_id"),
        "stop_loss": row.get("stop_loss"),
        "take_profit": row.get("take_profit"),
        "entry_price": row.get("entry_price"),
        "quantity": row.get("quantity"),
        "asset_kind": row.get("asset_kind"),
        "monitor_lane": row.get("monitor_lane"),
        "is_option": bool(is_option_trade(_as_runtime(row))),
    }


def _control_values_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fingerprints = [_row_fingerprint(row) for row in rows]
    return {
        "monitor_row_fingerprints": fingerprints,
        "selected_trade_ids": [int(row["id"]) for row in rows],
        "pattern_linked_trade_ids": [
            int(row["id"]) for row in rows if row.get("monitor_lane") == "pattern_linked"
        ],
        "plan_level_trade_ids": [
            int(row["id"]) for row in rows if row.get("monitor_lane") == "plan_levels"
        ],
        "option_trade_ids": sorted(
            fp["id"] for fp in fingerprints if fp["is_option"]
        ),
        "lane_by_trade_id": {
            str(fp["id"]): fp["monitor_lane"]
            for fp in sorted(fingerprints, key=lambda item: item["id"])
        },
    }


def _scope_values(db, *, relation_name: str) -> dict[str, Any]:
    return _control_values_for_rows(_monitor_rows(db, relation_name=relation_name))


def run_probe(db) -> dict[str, Any]:
    old_values = _scope_values(db, relation_name=LEGACY_TRADES_COMPAT_RELATION)
    new_values = _scope_values(db, relation_name=MANAGEMENT_ENVELOPES_RELATION)
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
        f"{len(comparisons)} pattern-monitor selection checks matched"
        if status == "COMPLETE_POSITIVE"
        else "pattern-monitor selection parity drift or relation-kind drift"
    )
    return {
        "status": status,
        "reason": reason,
        "relation_kinds": relation_kinds,
        "checks": len(comparisons),
        "mismatches": mismatches,
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
    print(f"PATTERN_MONITOR_CHECKS={result['checks']}")
    print(f"PATTERN_MONITOR_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "PATTERN_MONITOR_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
