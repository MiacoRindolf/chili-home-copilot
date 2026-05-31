#!/usr/bin/env python
"""Read-only Phase 5AD alerts.py envelope parity probe.

``alerts.py`` still owns a mixed legacy ``Trade`` ORM surface:

* legacy price-monitor fallback reads open management envelopes
* proposal execution creates management envelopes after broker/manual intent
* sector concentration gating counts open long envelopes by coarse sector

The writer/order path is intentionally not converted here. This probe only
compares old compatibility-view reads with the physical management-envelope
source for the read surfaces that would need evidence before any future
``alerts.py`` conversion.
"""
from __future__ import annotations

import json
import os
import sys
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
from app.services.trading.backtest_engine import TICKER_TO_SECTOR  # noqa: E402


LIVE_PROBE_OPT_IN = "PHASE5AD_ALLOW_LIVE_PROBE"
ALERTS_PARITY_RELATIONS = {
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
        "Phase 5AD alerts parity probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in ALERTS_PARITY_RELATIONS:
        raise ValueError(f"unsupported relation: {relation_name!r}")
    return relation_name


def _relation_kind(db, relation_name: str) -> str | None:
    return db.execute(
        text("SELECT relkind FROM pg_class WHERE relname = :name"),
        {"name": relation_name},
    ).scalar()


def _rows(db, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = db.execute(text(sql), params or {})
    return [_normalize_row(dict(row)) for row in result.mappings().all()]


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    return value


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _normalize_scalar(value) for key, value in row.items()}


def _open_position_fallback_rows(db, *, relation_name: str) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    return _rows(
        db,
        f"""
        SELECT id,
               user_id,
               UPPER(ticker) AS ticker,
               direction,
               status,
               broker_source,
               entry_price,
               quantity,
               stop_loss,
               take_profit,
               related_alert_id,
               scan_pattern_id
          FROM {relation}
         WHERE status = 'open'
         ORDER BY id
        """,
    )


def _sector_cap_counts(db, *, relation_name: str) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    rows = _rows(
        db,
        f"""
        SELECT user_id, UPPER(ticker) AS ticker
          FROM {relation}
         WHERE status = 'open'
           AND direction = 'long'
           AND user_id IS NOT NULL
         ORDER BY user_id, UPPER(ticker), id
        """,
    )
    counts: dict[tuple[int, str], int] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        sector = TICKER_TO_SECTOR.get(ticker, "unknown")
        key = (int(row["user_id"]), sector)
        counts[key] = counts.get(key, 0) + 1
    return [
        {"user_id": user_id, "sector": sector, "n": n}
        for (user_id, sector), n in sorted(counts.items())
    ]


def _scope_values(db, *, relation_name: str) -> dict[str, Any]:
    open_rows = _open_position_fallback_rows(db, relation_name=relation_name)
    return {
        "legacy_fallback_open_position_rows": open_rows,
        "legacy_fallback_open_position_ids": [int(row["id"]) for row in open_rows],
        "sector_cap_counts": _sector_cap_counts(db, relation_name=relation_name),
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
        f"{len(comparisons)} alerts.py read-scope checks matched"
        if status == "COMPLETE_POSITIVE"
        else "alerts.py read-scope parity drift or relation-kind drift"
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
    print(f"ALERTS_SCOPE_CHECKS={result['checks']}")
    print(f"ALERTS_SCOPE_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "ALERTS_SCOPE_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
