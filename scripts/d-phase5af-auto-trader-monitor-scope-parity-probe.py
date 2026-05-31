#!/usr/bin/env python
"""Read-only Phase 5AF AutoTrader monitor scope parity probe.

``auto_trader_monitor.py`` is live exit behavior: it selects open management
envelopes, partitions option/crypto rows away from the equity monitor, seeds
missing levels, and can submit Robinhood market exits. This probe does not
touch quotes or broker actions. It only compares the monitor's current
compatibility-view selection scope with the physical management-envelope source.
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

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.trade_relation_symbols import (  # noqa: E402
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
)
from app.services.trading.autopilot_scope import (  # noqa: E402
    classify_live_autopilot_trade_scope,
    is_option_trade,
)


LIVE_PROBE_OPT_IN = "PHASE5AF_ALLOW_LIVE_PROBE"
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
        "Phase 5AF AutoTrader monitor probe defaults to test-only validation. "
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


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    return value


def _rows(db, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = db.execute(text(sql), params or {})
    out: list[dict[str, Any]] = []
    for row in result.mappings().all():
        out.append({str(key): _normalize_scalar(value) for key, value in dict(row).items()})
    return out


def _monitor_user_id() -> int | None:
    uid = getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings, "brain_default_user_id", None
    )
    if uid is None:
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None


def _monitor_scope_rows(db, *, relation_name: str, user_id: int) -> list[dict[str, Any]]:
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
         WHERE user_id = :uid
           AND status = 'open'
           AND (
                auto_trader_version = 'v1'
             OR scan_pattern_id IS NOT NULL
             OR related_alert_id IS NOT NULL
             OR stop_loss IS NOT NULL
             OR take_profit IS NOT NULL
           )
         ORDER BY id
        """,
        {"uid": int(user_id)},
    )


def _as_runtime(row: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**row)


def _partition_ids(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    option_ids: list[int] = []
    crypto_ids: list[int] = []
    equity_ids: list[int] = []
    scope_counts: dict[str, int] = {}
    for row in rows:
        obj = _as_runtime(row)
        trade_id = int(row["id"])
        scope = classify_live_autopilot_trade_scope(obj)
        scope_counts[scope] = scope_counts.get(scope, 0) + 1
        is_option = bool(is_option_trade(obj))
        broker_source = str(row.get("broker_source") or "").strip().lower()
        ticker = str(row.get("ticker") or "").upper()
        is_crypto = ticker.endswith("-USD") or broker_source == "coinbase"
        if is_option:
            option_ids.append(trade_id)
        elif is_crypto:
            crypto_ids.append(trade_id)
        else:
            equity_ids.append(trade_id)
    return {
        "selected_ids": [int(row["id"]) for row in rows],
        "option_ids": sorted(option_ids),
        "crypto_ids": sorted(crypto_ids),
        "equity_monitor_ids": sorted(equity_ids),
        "scope_counts": [
            {"scope": scope, "n": n}
            for scope, n in sorted(scope_counts.items())
        ],
    }


def _scope_values(db, *, relation_name: str, user_id: int | None) -> dict[str, Any]:
    if user_id is None:
        return {
            "monitor_user_id": [],
            "selected_ids": [],
            "option_ids": [],
            "crypto_ids": [],
            "equity_monitor_ids": [],
            "scope_counts": [],
        }
    rows = _monitor_scope_rows(db, relation_name=relation_name, user_id=user_id)
    partitions = _partition_ids(rows)
    return {
        "monitor_user_id": [user_id],
        **partitions,
    }


def run_probe(db, *, user_id: int | None = None) -> dict[str, Any]:
    resolved_user_id = _monitor_user_id() if user_id is None else user_id
    old_values = _scope_values(
        db,
        relation_name=LEGACY_TRADES_COMPAT_RELATION,
        user_id=resolved_user_id,
    )
    new_values = _scope_values(
        db,
        relation_name=MANAGEMENT_ENVELOPES_RELATION,
        user_id=resolved_user_id,
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
        f"{len(comparisons)} AutoTrader monitor scope checks matched"
        if status == "COMPLETE_POSITIVE"
        else "AutoTrader monitor scope parity drift or relation-kind drift"
    )
    return {
        "status": status,
        "reason": reason,
        "relation_kinds": relation_kinds,
        "checks": len(comparisons),
        "mismatches": mismatches,
        "comparisons": comparisons,
        "user_id": resolved_user_id,
    }


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    _assert_probe_database_allowed(database_url)
    user_raw = os.getenv("PHASE5AF_USER_ID", "").strip()
    user_id = int(user_raw) if user_raw else None
    db = SessionLocal()
    try:
        result = run_probe(db, user_id=user_id)
    finally:
        db.rollback()
        db.close()

    print(f"VERDICT_STATUS={result['status']}")
    print(f"VERDICT_REASON={result['reason']}")
    print(f"RELATION_KINDS={result['relation_kinds']}")
    print(f"USER_ID={result['user_id']}")
    print(f"AUTOTRADER_MONITOR_CHECKS={result['checks']}")
    print(f"AUTOTRADER_MONITOR_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "AUTOTRADER_MONITOR_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
