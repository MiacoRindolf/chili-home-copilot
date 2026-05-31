#!/usr/bin/env python
"""Read-only Phase 5O brain plasticity envelope parity probe.

``brain_neural_mesh/plasticity.py`` is learning-mutation code: on close, it can
use a management envelope's PnL, stop, quantity, and mesh correlation to write
edge-mutation audit rows and, when not dry-run, mutate graph edge weights. This
probe does not call the plasticity engine and does not mutate weights. It only
compares the closed-outcome evidence that would feed plasticity through the
legacy ``Trade`` compatibility view and the physical
``trading_management_envelopes`` source.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
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


LIVE_PROBE_OPT_IN = "PHASE5O_BRAIN_PLASTICITY_ALLOW_LIVE_PROBE"
PLASTICITY_RELATIONS = {
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
        "Phase 5O brain plasticity probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in PLASTICITY_RELATIONS:
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


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return out


def _risked_capital(row: dict[str, Any]) -> float:
    entry = _float_or_none(row.get("entry_price"))
    qty = _float_or_none(row.get("quantity"))
    stop = _float_or_none(row.get("stop_loss"))
    if entry is None or qty is None or stop is None:
        return 0.0
    if entry <= 0.0 or qty <= 0.0:
        return 0.0
    return round(abs(entry - stop) * qty, 10)


def _closed_correlation_rows(db, *, relation_name: str) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    return _rows(
        db,
        f"""
        SELECT id,
               user_id,
               ticker,
               status,
               pnl,
               entry_price,
               stop_loss,
               quantity,
               mesh_entry_correlation_id,
               exit_date
          FROM {relation}
         WHERE status = 'closed'
           AND mesh_entry_correlation_id IS NOT NULL
         ORDER BY exit_date, id
        """,
    )


def _row_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    risked = _risked_capital(row)
    return {
        "id": int(row["id"]),
        "user_id": row.get("user_id"),
        "ticker": row.get("ticker"),
        "status": row.get("status"),
        "pnl": row.get("pnl"),
        "entry_price": row.get("entry_price"),
        "stop_loss": row.get("stop_loss"),
        "quantity": row.get("quantity"),
        "mesh_entry_correlation_id": row.get("mesh_entry_correlation_id"),
        "exit_date": row.get("exit_date"),
        "risked_capital": risked,
        "plasticity_eligible": bool(row.get("mesh_entry_correlation_id")) and risked > 0.0,
    }


def _edge_counts_by_correlation(db, correlation_ids: list[str]) -> list[dict[str, Any]]:
    ids = sorted({str(value) for value in correlation_ids if str(value or "").strip()})
    if not ids:
        return []
    binds: list[str] = []
    params: dict[str, str] = {}
    for idx, value in enumerate(ids):
        key = f"corr_{idx}"
        binds.append(f":{key}")
        params[key] = value
    return _rows(
        db,
        f"""
        SELECT correlation_id,
               COUNT(DISTINCT edge_id) AS edge_count
          FROM brain_activation_path_log
         WHERE correlation_id IN ({", ".join(binds)})
           AND edge_id IS NOT NULL
         GROUP BY correlation_id
         ORDER BY correlation_id
        """,
        params,
    )


def _scope_values(db, *, relation_name: str) -> dict[str, Any]:
    rows = _closed_correlation_rows(db, relation_name=relation_name)
    fingerprints = [_row_fingerprint(row) for row in rows]
    eligible = [row for row in fingerprints if row["plasticity_eligible"]]
    edge_counts = _edge_counts_by_correlation(
        db,
        [str(row["mesh_entry_correlation_id"]) for row in eligible],
    )
    edge_count_map = {str(row["correlation_id"]): int(row["edge_count"]) for row in edge_counts}
    eligible_with_path = [
        row["id"]
        for row in eligible
        if edge_count_map.get(str(row["mesh_entry_correlation_id"]), 0) > 0
    ]
    return {
        "closed_correlation_rows": fingerprints,
        "eligible_trade_ids": sorted(row["id"] for row in eligible),
        "eligible_with_path_trade_ids": sorted(eligible_with_path),
        "path_edge_counts": edge_counts,
    }


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
        f"{len(comparisons)} brain plasticity outcome checks matched"
        if status == "COMPLETE_POSITIVE"
        else "brain plasticity outcome parity drift or relation-kind drift"
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
    print(f"BRAIN_PLASTICITY_CHECKS={result['checks']}")
    print(f"BRAIN_PLASTICITY_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "BRAIN_PLASTICITY_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
