#!/usr/bin/env python
"""Read-only Phase 5O AutoTrader synergy envelope parity probe.

``auto_trader_synergy.py`` is a live capital-control surface. It decides
whether a new confirming alert can scale into an already-open AutoTrader v1
management envelope. This probe does not call the planner. It compares the
current legacy compatibility-view scope with the physical management-envelope
scope for the rows and fields that influence scale-in eligibility.
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
from app.services.trading.auto_trader_synergy import (  # noqa: E402
    SCALE_IN_ALERT_IDS_SNAPSHOT_KEY,
    SCALE_IN_PATTERN_IDS_SNAPSHOT_KEY,
)
from app.services.trading.autopilot_scope import is_option_trade  # noqa: E402


LIVE_PROBE_OPT_IN = "PHASE5O_SYNERGY_ALLOW_LIVE_PROBE"
SYNERGY_RELATIONS = {
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
        "Phase 5O synergy probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in SYNERGY_RELATIONS:
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


def _v1_rows(db, *, relation_name: str) -> list[dict[str, Any]]:
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
               scale_in_count,
               stop_loss,
               take_profit,
               entry_price,
               quantity,
               asset_kind,
               tags,
               indicator_snapshot
          FROM {relation}
         WHERE status IN ('open', 'working')
           AND auto_trader_version = 'v1'
         ORDER BY COALESCE(user_id, -1), UPPER(ticker), id DESC
        """,
    )


def _coerce_int_set(raw_values: Any) -> set[int]:
    if raw_values is None:
        return set()
    if isinstance(raw_values, (str, bytes)):
        values = [raw_values]
    elif isinstance(raw_values, (list, tuple, set)):
        values = list(raw_values)
    else:
        values = [raw_values]

    out: set[int] = set()
    for raw in values:
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _legacy_scan_pattern_ids_by_alert_id(db, alert_ids: set[int]) -> dict[int, int]:
    if not alert_ids:
        return {}
    ordered = sorted(int(alert_id) for alert_id in alert_ids)
    params = {f"id{i}": value for i, value in enumerate(ordered)}
    placeholders = ", ".join(f":id{i}" for i in range(len(ordered)))
    rows = _rows(
        db,
        f"""
        SELECT id, scan_pattern_id
          FROM trading_breakout_alerts
         WHERE id IN ({placeholders})
           AND scan_pattern_id IS NOT NULL
         ORDER BY id
        """,
        params,
    )
    return {int(row["id"]): int(row["scan_pattern_id"]) for row in rows}


def _all_scale_in_alert_ids(rows: list[dict[str, Any]]) -> set[int]:
    out: set[int] = set()
    for row in rows:
        snap = _normalize_json(row.get("indicator_snapshot"))
        out |= _coerce_int_set(snap.get(SCALE_IN_ALERT_IDS_SNAPSHOT_KEY))
    return out


def _used_pattern_ids_for_row(
    row: dict[str, Any],
    *,
    legacy_scan_pattern_ids_by_alert_id: dict[int, int],
) -> list[int]:
    snap = _normalize_json(row.get("indicator_snapshot"))
    pattern_ids = _coerce_int_set(snap.get(SCALE_IN_PATTERN_IDS_SNAPSHOT_KEY))
    alert_ids = _coerce_int_set(snap.get(SCALE_IN_ALERT_IDS_SNAPSHOT_KEY))
    for alert_id in alert_ids:
        scan_pattern_id = legacy_scan_pattern_ids_by_alert_id.get(int(alert_id))
        if scan_pattern_id is not None:
            pattern_ids.add(int(scan_pattern_id))
    return sorted(pattern_ids)


def _pair_key(row: dict[str, Any]) -> str:
    user_id = row.get("user_id")
    user_part = "NULL" if user_id is None else str(user_id)
    return f"{user_part}:{str(row.get('ticker') or '').upper()}"


def _selected_by_user_ticker(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _pair_key(row)
        current = selected.get(key)
        if current is None or int(row["id"]) > int(current["id"]):
            selected[key] = row
    return selected


def _as_runtime(row: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**row)


def _row_fingerprint(
    row: dict[str, Any],
    *,
    legacy_scan_pattern_ids_by_alert_id: dict[int, int],
) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": row.get("user_id"),
        "ticker": row.get("ticker"),
        "status": row.get("status"),
        "broker_source": row.get("broker_source"),
        "auto_trader_version": row.get("auto_trader_version"),
        "scan_pattern_id": row.get("scan_pattern_id"),
        "scale_in_count": row.get("scale_in_count"),
        "stop_loss": row.get("stop_loss"),
        "take_profit": row.get("take_profit"),
        "entry_price": row.get("entry_price"),
        "quantity": row.get("quantity"),
        "asset_kind": row.get("asset_kind"),
        "is_option": bool(is_option_trade(_as_runtime(row))),
        "used_scale_in_pattern_ids": _used_pattern_ids_for_row(
            row,
            legacy_scan_pattern_ids_by_alert_id=legacy_scan_pattern_ids_by_alert_id,
        ),
    }


def _synergy_excluded_trade_ids(db, candidate_ids: list[int]) -> list[int]:
    ids = sorted(set(int(trade_id) for trade_id in candidate_ids))
    if not ids:
        return []
    params = {f"id{i}": f"autotrader_v1_position:trade:{trade_id}" for i, trade_id in enumerate(ids)}
    placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
    rows = _rows(
        db,
        f"""
        SELECT slice_name, payload_json
          FROM trading_brain_runtime_modes
         WHERE slice_name IN ({placeholders})
         ORDER BY slice_name
        """,
        params,
    )
    out: list[int] = []
    for row in rows:
        payload = _normalize_json(row.get("payload_json"))
        if not payload.get("synergy_excluded"):
            continue
        raw_id = str(row.get("slice_name") or "").rsplit(":", 1)[-1]
        try:
            out.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def _control_values_for_rows(
    rows: list[dict[str, Any]],
    *,
    legacy_scan_pattern_ids_by_alert_id: dict[int, int],
    synergy_excluded_trade_ids: list[int] | None = None,
) -> dict[str, Any]:
    selected = _selected_by_user_ticker(rows)
    selected_rows = [selected[key] for key in sorted(selected)]
    selected_ids = [int(row["id"]) for row in selected_rows]
    option_ids = [
        int(row["id"])
        for row in selected_rows
        if is_option_trade(_as_runtime(row))
    ]
    spot_ids = [int(row["id"]) for row in selected_rows if int(row["id"]) not in option_ids]
    raw_scale_counts = {
        str(int(row["id"])): int(row.get("scale_in_count") or 0)
        for row in selected_rows
    }
    used_pattern_ids = {
        str(int(row["id"])): _used_pattern_ids_for_row(
            row,
            legacy_scan_pattern_ids_by_alert_id=legacy_scan_pattern_ids_by_alert_id,
        )
        for row in selected_rows
    }
    return {
        "v1_pair_keys": sorted(selected.keys()),
        "selected_trade_ids_by_pair": {
            key: int(row["id"]) for key, row in sorted(selected.items())
        },
        "selected_trade_fingerprints": [
            _row_fingerprint(
                row,
                legacy_scan_pattern_ids_by_alert_id=legacy_scan_pattern_ids_by_alert_id,
            )
            for row in selected_rows
        ],
        "selected_option_trade_ids": sorted(option_ids),
        "selected_spot_trade_ids": sorted(spot_ids),
        "scale_in_count_by_selected_id": raw_scale_counts,
        "used_scale_in_pattern_ids_by_selected_id": used_pattern_ids,
        "synergy_excluded_selected_trade_ids": sorted(
            int(trade_id)
            for trade_id in (synergy_excluded_trade_ids or [])
            if int(trade_id) in selected_ids
        ),
    }


def _scope_values(db, *, relation_name: str) -> dict[str, Any]:
    rows = _v1_rows(db, relation_name=relation_name)
    alert_ids = _all_scale_in_alert_ids(rows)
    legacy_scan_pattern_ids = _legacy_scan_pattern_ids_by_alert_id(db, alert_ids)
    selected = _selected_by_user_ticker(rows)
    excluded = _synergy_excluded_trade_ids(
        db,
        [int(row["id"]) for row in selected.values()],
    )
    return _control_values_for_rows(
        rows,
        legacy_scan_pattern_ids_by_alert_id=legacy_scan_pattern_ids,
        synergy_excluded_trade_ids=excluded,
    )


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
        f"{len(comparisons)} AutoTrader synergy scale-in checks matched"
        if status == "COMPLETE_POSITIVE"
        else "AutoTrader synergy scale-in parity drift or relation-kind drift"
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
    print(f"SYNERGY_CHECKS={result['checks']}")
    print(f"SYNERGY_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "SYNERGY_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
