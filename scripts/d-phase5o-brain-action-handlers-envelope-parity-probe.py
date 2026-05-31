#!/usr/bin/env python
"""Read-only Phase 5O brain action-handlers envelope parity probe.

``brain_neural_mesh/action_handlers.py`` is live-action-adjacent: critical
mesh signals are dispatched to Telegram only after the handler revalidates the
referenced local management envelope and broker-position truth. This probe does
not dispatch alerts and does not call broker APIs. It compares the local
management-envelope fields that the critical revalidation path reads through
the legacy ``Trade`` compatibility view with the physical
``trading_management_envelopes`` source.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
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


LIVE_PROBE_OPT_IN = "PHASE5O_BRAIN_ACTION_HANDLERS_ALLOW_LIVE_PROBE"
ACTION_HANDLER_RELATIONS = {
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
}
CRITICAL_ACTIONS = {"exit_now", "STOP_HIT", "stop_hit", "TIME_EXIT"}


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
        "Phase 5O brain action-handlers probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in ACTION_HANDLER_RELATIONS:
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


def _state_trade_id(row: dict[str, Any]) -> int | None:
    raw = row.get("trade_id")
    try:
        trade_id = int(raw)
    except (TypeError, ValueError):
        return None
    return trade_id if trade_id > 0 else None


def _mesh_child_state_rows(db) -> list[dict[str, Any]]:
    return _rows(
        db,
        """
        SELECT node_id,
               local_state->>'trade_id' AS trade_id,
               local_state->>'ticker' AS ticker,
               COALESCE(local_state->>'action', local_state->>'alert_event', '') AS action,
               COALESCE(local_state->>'urgency', '') AS urgency,
               updated_at
          FROM brain_node_states
         WHERE local_state IS NOT NULL
           AND local_state ? 'trade_id'
         ORDER BY node_id
        """,
    )


def _trade_ids_from_states(rows: list[dict[str, Any]]) -> list[int]:
    return sorted({trade_id for row in rows if (trade_id := _state_trade_id(row)) is not None})


def _critical_trade_ids_from_states(rows: list[dict[str, Any]]) -> list[int]:
    critical: set[int] = set()
    for row in rows:
        trade_id = _state_trade_id(row)
        if trade_id is None:
            continue
        action = str(row.get("action") or "").strip()
        urgency = str(row.get("urgency") or "").strip().lower()
        if action in CRITICAL_ACTIONS or urgency == "critical":
            critical.add(trade_id)
    return sorted(critical)


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


def _local_trade_validation_rows(
    db,
    *,
    relation_name: str,
    trade_ids: list[int],
) -> list[dict[str, Any]]:
    if not trade_ids:
        return []
    relation = _relation_sql(relation_name)
    trade_binds, params = _bind_int_list("trade_id", trade_ids)
    return _rows(
        db,
        f"""
        SELECT id,
               user_id,
               ticker,
               status,
               broker_source,
               position_id,
               direction,
               entry_date,
               exit_date
          FROM {relation}
         WHERE id IN ({trade_binds})
         ORDER BY id
        """,
        params,
    )


def _row_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": row.get("user_id"),
        "ticker": row.get("ticker"),
        "status": row.get("status"),
        "broker_source": row.get("broker_source"),
        "position_id": row.get("position_id"),
        "direction": row.get("direction"),
        "entry_date": row.get("entry_date"),
        "exit_date": row.get("exit_date"),
    }


def _scope_values(db, *, relation_name: str, state_rows: list[dict[str, Any]]) -> dict[str, Any]:
    all_trade_ids = _trade_ids_from_states(state_rows)
    critical_trade_ids = _critical_trade_ids_from_states(state_rows)
    rows = _local_trade_validation_rows(
        db,
        relation_name=relation_name,
        trade_ids=all_trade_ids,
    )
    fingerprints = [_row_fingerprint(row) for row in rows]
    found_ids = sorted(fp["id"] for fp in fingerprints)
    missing_ids = sorted(set(all_trade_ids) - set(found_ids))
    open_ids = sorted(fp["id"] for fp in fingerprints if str(fp.get("status") or "") == "open")
    non_open_ids = sorted(fp["id"] for fp in fingerprints if str(fp.get("status") or "") != "open")
    return {
        "all_child_trade_ids": all_trade_ids,
        "critical_child_trade_ids": critical_trade_ids,
        "local_trade_validation_rows": fingerprints,
        "missing_child_trade_ids": missing_ids,
        "open_child_trade_ids": open_ids,
        "non_open_child_trade_ids": non_open_ids,
    }


def run_probe(db) -> dict[str, Any]:
    state_rows = _mesh_child_state_rows(db)
    old_values = _scope_values(
        db,
        relation_name=LEGACY_TRADES_COMPAT_RELATION,
        state_rows=state_rows,
    )
    new_values = _scope_values(
        db,
        relation_name=MANAGEMENT_ENVELOPES_RELATION,
        state_rows=state_rows,
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
        f"{len(comparisons)} brain action-handler local validation checks matched"
        if status == "COMPLETE_POSITIVE"
        else "brain action-handler parity drift or relation-kind drift"
    )
    return {
        "status": status,
        "reason": reason,
        "relation_kinds": relation_kinds,
        "state_rows": len(state_rows),
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
    print(f"MESH_CHILD_STATE_ROWS={result['state_rows']}")
    print(f"BRAIN_ACTION_HANDLER_CHECKS={result['checks']}")
    print(f"BRAIN_ACTION_HANDLER_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "BRAIN_ACTION_HANDLER_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
