#!/usr/bin/env python
"""Read-only Phase 5O AutoTrader desk envelope parity probe.

``autotrader_desk.py`` is operator-visible live position state. It loads live
management envelopes, suppresses broker-stale rows, overlays broker truth,
looks up per-position overrides, and exposes close/control affordances. This
probe does not call the desk function or mutate overrides. It compares the
desk's live envelope inputs through the legacy ``trading_trades`` compatibility
view and the physical ``trading_management_envelopes`` source.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
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


LIVE_PROBE_OPT_IN = "PHASE5O_AUTOTRADER_DESK_ALLOW_LIVE_PROBE"
PROBE_USER_ID_ENV = "PHASE5O_AUTOTRADER_DESK_USER_ID"
DESK_RELATIONS = {
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
        "Phase 5O AutoTrader desk probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in DESK_RELATIONS:
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
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _rows(db, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = db.execute(text(sql), params or {})
    out: list[dict[str, Any]] = []
    for row in result.mappings().all():
        normalized = {
            str(key): _normalize_scalar(value) for key, value in dict(row).items()
        }
        if "indicator_snapshot" in normalized:
            normalized["indicator_snapshot"] = _normalize_json(
                normalized["indicator_snapshot"]
            )
        out.append(normalized)
    return out


def _probe_user_id(db) -> int | None:
    override = os.getenv(PROBE_USER_ID_ENV)
    if override:
        try:
            return int(override)
        except (TypeError, ValueError):
            return None
    value = getattr(settings, "brain_default_user_id", None)
    try:
        if value is not None:
            return int(value)
    except (TypeError, ValueError):
        pass
    rows = _rows(
        db,
        f"""
        SELECT user_id, COUNT(*) AS n
          FROM {MANAGEMENT_ENVELOPES_RELATION}
         WHERE status = 'open'
           AND user_id IS NOT NULL
           AND (
                auto_trader_version = 'v1'
             OR scan_pattern_id IS NOT NULL
             OR related_alert_id IS NOT NULL
             OR stop_loss IS NOT NULL
             OR take_profit IS NOT NULL
           )
         GROUP BY user_id
         ORDER BY COUNT(*) DESC, user_id
         LIMIT 1
        """,
    )
    if not rows:
        return None
    return int(rows[0]["user_id"])


def _desk_live_rows(
    db,
    *,
    relation_name: str,
    user_id: int | None,
) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    return _rows(
        db,
        f"""
        SELECT id,
               user_id,
               UPPER(ticker) AS ticker,
               direction,
               entry_price,
               entry_date,
               quantity,
               stop_loss,
               take_profit,
               scan_pattern_id,
               related_alert_id,
               broker_source,
               asset_kind,
               trade_type,
               auto_trader_version,
               scale_in_count,
               tags,
               position_id,
               indicator_snapshot
          FROM {relation}
         WHERE user_id IS NOT DISTINCT FROM :uid
           AND status = 'open'
           AND (
                auto_trader_version = 'v1'
             OR scan_pattern_id IS NOT NULL
             OR related_alert_id IS NOT NULL
             OR stop_loss IS NOT NULL
             OR take_profit IS NOT NULL
           )
         ORDER BY id DESC
        """,
        {"uid": user_id},
    )


def _as_runtime(row: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**row)


def _desk_row_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    runtime = _as_runtime(row)
    return {
        "id": int(row["id"]),
        "user_id": row.get("user_id"),
        "ticker": row.get("ticker"),
        "direction": row.get("direction"),
        "entry_price": row.get("entry_price"),
        "entry_date": row.get("entry_date"),
        "quantity": row.get("quantity"),
        "stop_loss": row.get("stop_loss"),
        "take_profit": row.get("take_profit"),
        "scan_pattern_id": row.get("scan_pattern_id"),
        "related_alert_id": row.get("related_alert_id"),
        "broker_source": row.get("broker_source"),
        "asset_kind": row.get("asset_kind"),
        "trade_type": row.get("trade_type"),
        "auto_trader_version": row.get("auto_trader_version"),
        "scale_in_count": row.get("scale_in_count"),
        "tags": row.get("tags"),
        "position_id": row.get("position_id"),
        "monitor_scope": classify_live_autopilot_trade_scope(runtime),
        "asset_type": "options" if is_option_trade(runtime) else (
            "crypto" if str(row.get("ticker") or "").upper().endswith("-USD") else "stock"
        ),
    }


def _desk_values_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fingerprints = [_desk_row_fingerprint(row) for row in rows]
    return {
        "desk_live_rows": fingerprints,
        "desk_override_keys": [f"trade:{fp['id']}" for fp in fingerprints],
        "desk_broker_truth_inputs": [
            {
                "id": fp["id"],
                "position_id": fp["position_id"],
                "ticker": fp["ticker"],
                "broker_source": fp["broker_source"],
                "entry_price": fp["entry_price"],
                "quantity": fp["quantity"],
            }
            for fp in fingerprints
            if fp["position_id"] is not None
        ],
        "desk_quote_inputs": [
            {
                "id": fp["id"],
                "ticker": fp["ticker"],
                "broker_source": fp["broker_source"],
                "asset_type": fp["asset_type"],
            }
            for fp in fingerprints
        ],
        "desk_scope_by_trade_id": {
            str(fp["id"]): fp["monitor_scope"]
            for fp in sorted(fingerprints, key=lambda item: item["id"])
        },
    }


def _scope_values(db, *, relation_name: str, user_id: int | None) -> dict[str, Any]:
    return _desk_values_for_rows(
        _desk_live_rows(db, relation_name=relation_name, user_id=user_id)
    )


def run_probe(db) -> dict[str, Any]:
    user_id = _probe_user_id(db)
    old_values = _scope_values(
        db,
        relation_name=LEGACY_TRADES_COMPAT_RELATION,
        user_id=user_id,
    )
    new_values = _scope_values(
        db,
        relation_name=MANAGEMENT_ENVELOPES_RELATION,
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
        f"{len(comparisons)} AutoTrader desk checks matched"
        if status == "COMPLETE_POSITIVE"
        else "AutoTrader desk parity drift or relation-kind drift"
    )
    return {
        "status": status,
        "reason": reason,
        "user_id": user_id,
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
    print(f"PROBE_USER_ID={result['user_id']}")
    print(f"RELATION_KINDS={result['relation_kinds']}")
    print(f"AUTOTRADER_DESK_CHECKS={result['checks']}")
    print(f"AUTOTRADER_DESK_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "AUTOTRADER_DESK_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
