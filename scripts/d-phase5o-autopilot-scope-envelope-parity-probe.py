#!/usr/bin/env python
"""Read-only Phase 5O autopilot-scope envelope parity probe.

``autopilot_scope.py`` owns live scope classification and the entry
mutual-exclusion gate between AutoTrader v1 and momentum_neural. This probe
does not call order paths or acquire any lease. It compares the legacy
compatibility-view scope with the physical management-envelope scope for the
fields that influence live scope classification and AutoTrader v1 ownership.
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
from app.services.trading.autopilot_scope import (  # noqa: E402
    ACTIVE_ENTRY_STATUSES,
    classify_live_autopilot_trade_scope,
    is_live_autopilot_trade,
    is_option_trade,
)


LIVE_PROBE_OPT_IN = "PHASE5O_AUTOPILOT_SCOPE_ALLOW_LIVE_PROBE"
SCOPE_RELATIONS = {
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
        "Phase 5O autopilot-scope probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in SCOPE_RELATIONS:
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


def _scope_rows(db, *, relation_name: str) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    return _rows(
        db,
        f"""
        SELECT id,
               user_id,
               UPPER(ticker) AS ticker,
               status,
               broker_source,
               auto_trader_version,
               scan_pattern_id,
               related_alert_id,
               stop_loss,
               take_profit,
               asset_kind,
               tags,
               indicator_snapshot
          FROM {relation}
         WHERE status IN ('open', 'working')
           AND (
                auto_trader_version = 'v1'
             OR scan_pattern_id IS NOT NULL
             OR related_alert_id IS NOT NULL
             OR stop_loss IS NOT NULL
             OR take_profit IS NOT NULL
           )
         ORDER BY id
        """,
    )


def _active_v1_counts(db, *, relation_name: str) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    params = {f"s{i}": status for i, status in enumerate(ACTIVE_ENTRY_STATUSES)}
    placeholders = ", ".join(f":s{i}" for i in range(len(params)))
    return _rows(
        db,
        f"""
        SELECT COALESCE(user_id, -1) AS user_id_key,
               UPPER(ticker) AS ticker,
               COUNT(*)::int AS v1_open_trades
          FROM {relation}
         WHERE auto_trader_version = 'v1'
           AND status IN ({placeholders})
         GROUP BY COALESCE(user_id, -1), UPPER(ticker)
         ORDER BY COALESCE(user_id, -1), UPPER(ticker)
        """,
        params,
    )


def _as_runtime(row: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**row)


def _row_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    obj = _as_runtime(row)
    return {
        "id": int(row["id"]),
        "user_id": row.get("user_id"),
        "ticker": row.get("ticker"),
        "status": row.get("status"),
        "broker_source": row.get("broker_source"),
        "auto_trader_version": row.get("auto_trader_version"),
        "scan_pattern_id": row.get("scan_pattern_id"),
        "related_alert_id": row.get("related_alert_id"),
        "stop_loss": row.get("stop_loss"),
        "take_profit": row.get("take_profit"),
        "asset_kind": row.get("asset_kind"),
        "scope": classify_live_autopilot_trade_scope(obj),
        "is_live_autopilot": bool(is_live_autopilot_trade(obj)),
        "is_option": bool(is_option_trade(obj)),
    }


def _scope_values_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fingerprints = [_row_fingerprint(row) for row in rows]
    return {
        "scope_row_fingerprints": fingerprints,
        "live_autopilot_trade_ids": sorted(
            fp["id"] for fp in fingerprints if fp["is_live_autopilot"]
        ),
        "option_trade_ids": sorted(fp["id"] for fp in fingerprints if fp["is_option"]),
        "scope_by_trade_id": {
            str(fp["id"]): fp["scope"] for fp in sorted(fingerprints, key=lambda item: item["id"])
        },
    }


def _count_values_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "v1_open_counts_by_user_symbol": {
            f"{row['user_id_key']}:{row['ticker']}": int(row["v1_open_trades"])
            for row in rows
        },
        "v1_owned_symbols": sorted(
            f"{row['user_id_key']}:{row['ticker']}" for row in rows
        ),
    }


def _scope_values(db, *, relation_name: str) -> dict[str, Any]:
    return {
        **_scope_values_for_rows(_scope_rows(db, relation_name=relation_name)),
        **_count_values_for_rows(_active_v1_counts(db, relation_name=relation_name)),
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
        f"{len(comparisons)} autopilot-scope checks matched"
        if status == "COMPLETE_POSITIVE"
        else "autopilot-scope parity drift or relation-kind drift"
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
    print(f"AUTOPILOT_SCOPE_CHECKS={result['checks']}")
    print(f"AUTOPILOT_SCOPE_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "AUTOPILOT_SCOPE_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
