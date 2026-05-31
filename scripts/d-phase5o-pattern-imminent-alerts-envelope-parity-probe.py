#!/usr/bin/env python
"""Read-only Phase 5O pattern-imminent-alerts envelope parity probe.

``pattern_imminent_alerts.py`` uses open AutoTrader v1 positions to deflect
same pattern/ticker imminent candidates before alert generation. That is a
selection gate, not passive reporting. This probe does not call the scanner or
fetch market data; it compares only the open-position deflection scope between
the legacy compatibility view and the physical management-envelope table.
"""
from __future__ import annotations

import json
import os
import sys
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


LIVE_PROBE_OPT_IN = "PHASE5O_PATTERN_IMMINENT_ALLOW_LIVE_PROBE"
DEFLECTION_RELATIONS = {
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
}
AUTOTRADER_POSITION_DEFLECTION_VERSION = "v1"
AUTOTRADER_POSITION_DEFLECTION_STATUSES = ("open", "working")


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
        "Phase 5O pattern-imminent probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in DEFLECTION_RELATIONS:
        raise ValueError(f"unsupported relation: {relation_name!r}")
    return relation_name


def _relation_kind(db, relation_name: str) -> str | None:
    return db.execute(
        text("SELECT relkind FROM pg_class WHERE relname = :name"),
        {"name": relation_name},
    ).scalar()


def _rows(db, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(text(sql), params or {}).mappings().all()]


def _deflection_rows(db, *, relation_name: str) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    params = {
        "version": AUTOTRADER_POSITION_DEFLECTION_VERSION,
        "s0": AUTOTRADER_POSITION_DEFLECTION_STATUSES[0],
        "s1": AUTOTRADER_POSITION_DEFLECTION_STATUSES[1],
    }
    return _rows(
        db,
        f"""
        SELECT id,
               user_id,
               scan_pattern_id,
               UPPER(ticker) AS ticker,
               status,
               auto_trader_version,
               broker_source
          FROM {relation}
         WHERE auto_trader_version = :version
           AND status IN (:s0, :s1)
           AND scan_pattern_id IS NOT NULL
         ORDER BY COALESCE(user_id, -1), scan_pattern_id, UPPER(ticker), id
        """,
        params,
    )


def _pattern_ticker_key(row: dict[str, Any]) -> str | None:
    try:
        pattern_id = int(row.get("scan_pattern_id") or 0)
    except (TypeError, ValueError):
        pattern_id = 0
    ticker = str(row.get("ticker") or "").strip().upper()
    if pattern_id <= 0 or not ticker:
        return None
    return f"{pattern_id}:{ticker}"


def _user_pattern_ticker_key(row: dict[str, Any]) -> str | None:
    key = _pattern_ticker_key(row)
    if key is None:
        return None
    user_id = row.get("user_id")
    return f"{-1 if user_id is None else int(user_id)}:{key}"


def _row_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": row.get("user_id"),
        "scan_pattern_id": row.get("scan_pattern_id"),
        "ticker": row.get("ticker"),
        "status": row.get("status"),
        "auto_trader_version": row.get("auto_trader_version"),
        "broker_source": row.get("broker_source"),
    }


def _control_values_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fingerprints = [_row_fingerprint(row) for row in rows]
    pattern_ticker_keys = sorted(
        key for row in rows if (key := _pattern_ticker_key(row)) is not None
    )
    user_pattern_ticker_keys = sorted(
        key for row in rows if (key := _user_pattern_ticker_key(row)) is not None
    )
    return {
        "deflection_row_fingerprints": fingerprints,
        "deflection_trade_ids": [fp["id"] for fp in fingerprints],
        "pattern_ticker_keys": pattern_ticker_keys,
        "user_pattern_ticker_keys": user_pattern_ticker_keys,
        "keys_by_user": {
            user_key: sorted(
                key
                for row in rows
                if str(-1 if row.get("user_id") is None else int(row["user_id"])) == user_key
                if (key := _pattern_ticker_key(row)) is not None
            )
            for user_key in sorted(
                {
                    str(-1 if row.get("user_id") is None else int(row["user_id"]))
                    for row in rows
                }
            )
        },
    }


def _scope_values(db, *, relation_name: str) -> dict[str, Any]:
    return _control_values_for_rows(_deflection_rows(db, relation_name=relation_name))


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
        f"{len(comparisons)} pattern-imminent deflection checks matched"
        if status == "COMPLETE_POSITIVE"
        else "pattern-imminent deflection parity drift or relation-kind drift"
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
    print(f"PATTERN_IMMINENT_CHECKS={result['checks']}")
    print(f"PATTERN_IMMINENT_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "PATTERN_IMMINENT_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
