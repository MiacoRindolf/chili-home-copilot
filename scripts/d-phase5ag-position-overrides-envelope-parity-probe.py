#!/usr/bin/env python
"""Read-only Phase 5AG position-override envelope parity probe.

``auto_trader_position_overrides.py`` is a live control surface: it can close
live positions, adopt/unadopt rows into AutoTrader v1, seed stops/targets, and
clear per-position overrides. This probe does not call those control helpers.
It only compares the helper's current compatibility-view scope with the
physical management-envelope source.
"""
from __future__ import annotations

import json
import os
import re
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
    is_live_autopilot_trade,
    is_option_trade,
)


LIVE_PROBE_OPT_IN = "PHASE5AG_POSITION_OVERRIDES_ALLOW_LIVE_PROBE"
OVERRIDE_RELATIONS = {
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
}
TRADE_OVERRIDE_SLICE_RE = re.compile(r"^autotrader_v1_position:trade:(\d+)$")


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
        "Phase 5AG position-overrides probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in OVERRIDE_RELATIONS:
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


def _control_rows(db, *, relation_name: str) -> list[dict[str, Any]]:
    relation = _relation_sql(relation_name)
    return _rows(
        db,
        f"""
        SELECT id,
               user_id,
               UPPER(ticker) AS ticker,
               status,
               direction,
               broker_source,
               auto_trader_version,
               management_scope,
               scan_pattern_id,
               related_alert_id,
               stop_loss,
               take_profit,
               quantity,
               entry_price,
               entry_date,
               asset_kind,
               tags,
               indicator_snapshot
          FROM {relation}
         WHERE status = 'open'
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


def _as_runtime(row: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**row)


def _positive_quantity(row: dict[str, Any]) -> bool:
    try:
        return float(row.get("quantity") or 0.0) > 0.0
    except (TypeError, ValueError):
        return False


def _row_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": row.get("user_id"),
        "ticker": row.get("ticker"),
        "status": row.get("status"),
        "direction": row.get("direction"),
        "broker_source": row.get("broker_source"),
        "auto_trader_version": row.get("auto_trader_version"),
        "management_scope": row.get("management_scope"),
        "scan_pattern_id": row.get("scan_pattern_id"),
        "related_alert_id": row.get("related_alert_id"),
        "stop_loss": row.get("stop_loss"),
        "take_profit": row.get("take_profit"),
        "quantity": row.get("quantity"),
        "entry_price": row.get("entry_price"),
        "asset_kind": row.get("asset_kind"),
    }


def _parse_trade_override_slice(row: dict[str, Any]) -> dict[str, Any] | None:
    match = TRADE_OVERRIDE_SLICE_RE.match(str(row.get("slice_name") or ""))
    if not match:
        return None
    payload = row.get("payload_json") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "trade_id": int(match.group(1)),
        "monitor_paused": bool(payload.get("monitor_paused", False)),
        "synergy_excluded": bool(payload.get("synergy_excluded", False)),
    }


def _trade_override_rows(db) -> list[dict[str, Any]]:
    rows = _rows(
        db,
        """
        SELECT slice_name, payload_json
          FROM trading_brain_runtime_modes
         WHERE slice_name LIKE 'autotrader_v1_position:trade:%'
         ORDER BY slice_name
        """,
    )
    parsed = [_parse_trade_override_slice(row) for row in rows]
    return [row for row in parsed if row is not None]


def _ids_in_relation(db, *, relation_name: str, ids: list[int]) -> set[int]:
    relation = _relation_sql(relation_name)
    unique_ids = sorted(set(int(i) for i in ids))
    if not unique_ids:
        return set()
    params = {f"id{i}": value for i, value in enumerate(unique_ids)}
    placeholders = ", ".join(f":id{i}" for i in range(len(unique_ids)))
    rows = _rows(
        db,
        f"SELECT id FROM {relation} WHERE id IN ({placeholders}) ORDER BY id",
        params,
    )
    return {int(row["id"]) for row in rows}


def _override_values_for_relation(
    db, *, relation_name: str, override_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    present_ids = _ids_in_relation(
        db,
        relation_name=relation_name,
        ids=[int(row["trade_id"]) for row in override_rows],
    )
    active = [row for row in override_rows if int(row["trade_id"]) in present_ids]
    return {
        "override_linked_trade_ids": sorted(int(row["trade_id"]) for row in active),
        "monitor_paused_trade_ids": sorted(
            int(row["trade_id"]) for row in active if row["monitor_paused"]
        ),
        "synergy_excluded_trade_ids": sorted(
            int(row["trade_id"]) for row in active if row["synergy_excluded"]
        ),
    }


def _control_values_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    close_candidate_ids: list[int] = []
    close_option_candidate_ids: list[int] = []
    close_spot_candidate_ids: list[int] = []
    close_good_qty_candidate_ids: list[int] = []
    adopt_candidate_ids: list[int] = []
    unadopt_candidate_ids: list[int] = []
    for row in rows:
        obj = _as_runtime(row)
        trade_id = int(row["id"])
        if is_live_autopilot_trade(obj):
            close_candidate_ids.append(trade_id)
            if is_option_trade(obj):
                close_option_candidate_ids.append(trade_id)
            else:
                close_spot_candidate_ids.append(trade_id)
            if _positive_quantity(row):
                close_good_qty_candidate_ids.append(trade_id)
        if row.get("scan_pattern_id") is not None or row.get("related_alert_id") is not None:
            if str(row.get("auto_trader_version") or "") != "v1":
                adopt_candidate_ids.append(trade_id)
        if str(row.get("auto_trader_version") or "") == "v1":
            unadopt_candidate_ids.append(trade_id)
    return {
        "control_row_fingerprints": [_row_fingerprint(row) for row in rows],
        "close_candidate_ids": sorted(close_candidate_ids),
        "close_option_candidate_ids": sorted(close_option_candidate_ids),
        "close_spot_candidate_ids": sorted(close_spot_candidate_ids),
        "close_good_qty_candidate_ids": sorted(close_good_qty_candidate_ids),
        "adopt_candidate_ids": sorted(adopt_candidate_ids),
        "unadopt_candidate_ids": sorted(unadopt_candidate_ids),
    }


def _scope_values(
    db, *, relation_name: str, override_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    rows = _control_rows(db, relation_name=relation_name)
    return {
        **_control_values_for_rows(rows),
        **_override_values_for_relation(
            db,
            relation_name=relation_name,
            override_rows=override_rows,
        ),
    }


def run_probe(db) -> dict[str, Any]:
    override_rows = _trade_override_rows(db)
    old_values = _scope_values(
        db,
        relation_name=LEGACY_TRADES_COMPAT_RELATION,
        override_rows=override_rows,
    )
    new_values = _scope_values(
        db,
        relation_name=MANAGEMENT_ENVELOPES_RELATION,
        override_rows=override_rows,
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
        f"{len(comparisons)} position-override control-scope checks matched"
        if status == "COMPLETE_POSITIVE"
        else "position-override control-scope parity drift or relation-kind drift"
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
    print(f"POSITION_OVERRIDE_CHECKS={result['checks']}")
    print(f"POSITION_OVERRIDE_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "POSITION_OVERRIDE_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
