#!/usr/bin/env python
"""Read-only Phase 5AB-C pattern-monitor runtime-object parity probe.

``trading_scheduler.trigger_pattern_monitor_for_tickers(...)`` still loads
SQLAlchemy ``Trade`` objects and passes them to
``run_pattern_position_monitor_for_trades(...)``. Phase 5AB proved the
selected ids match between the compatibility view and the management-envelope
table, but that does not prove the downstream runtime object contract. This
probe compares the current ORM objects with candidate envelope-shaped runtime
objects for the same open pattern/plan-monitor scope without executing monitor
side effects.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
import os
import sys
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
from app.models.trading import Trade  # noqa: E402
from app.services.trading.broker_position_truth import (  # noqa: E402
    filter_broker_stale_open_trades,
)


LIVE_PROBE_OPT_IN = "PHASE5AB_C_ALLOW_LIVE_PROBE"

PATTERN_MONITOR_FIELDS = (
    "id",
    "user_id",
    "ticker",
    "status",
    "related_alert_id",
    "scan_pattern_id",
    "entry_price",
    "direction",
    "broker_source",
    "stop_loss",
    "take_profit",
    "asset_kind",
    "tags",
    "indicator_snapshot",
    "auto_trader_version",
    "trade_type",
    "position_id",
    "filled_at",
    "submitted_at",
    "entry_date",
    "last_broker_sync",
    "broker_sync_missing_streak",
    "quantity",
    "filled_quantity",
    "avg_fill_price",
)


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
        "Phase 5AB-C pattern-monitor runtime-object probe defaults to "
        f"test-only validation. Set {LIVE_PROBE_OPT_IN}=true to run manually "
        "authorized read-only live/non-test DB evidence."
    )


def _relation_kind(db, relation_name: str) -> str | None:
    return db.execute(
        text("SELECT relkind FROM pg_class WHERE relname = :name"),
        {"name": relation_name},
    ).scalar()


def _pattern_filter_sql() -> str:
    return """
        related_alert_id IS NOT NULL
        OR (
            related_alert_id IS NULL
            AND (stop_loss IS NOT NULL OR take_profit IS NOT NULL)
        )
    """


def _scope_tickers(db, *, relation_name: str) -> list[str]:
    rows = db.execute(
        text(
            f"""
            SELECT DISTINCT UPPER(ticker) AS ticker
              FROM {relation_name}
             WHERE status = 'open'
               AND ticker IS NOT NULL
               AND ticker <> ''
               AND ({_pattern_filter_sql()})
             ORDER BY UPPER(ticker)
            """
        )
    ).fetchall() or []
    return [str(row[0]).upper() for row in rows if row[0]]


def _load_old_trade_objects(db, *, tickers: list[str]) -> list[Trade]:
    from sqlalchemy import and_, or_

    q = db.query(Trade).filter(
        Trade.status == "open",
        or_(
            Trade.related_alert_id.isnot(None),
            and_(
                Trade.related_alert_id.is_(None),
                or_(Trade.stop_loss.isnot(None), Trade.take_profit.isnot(None)),
            ),
        ),
    )
    if tickers:
        q = q.filter(Trade.ticker.in_(tickers))
    return q.order_by(Trade.id).all()


def _load_new_envelope_objects(db, *, tickers: list[str]) -> list[SimpleNamespace]:
    params: dict[str, Any] = {}
    ticker_sql = ""
    if tickers:
        params = {f"ticker_{idx}": ticker for idx, ticker in enumerate(tickers)}
        binds = ", ".join(f":ticker_{idx}" for idx in range(len(tickers)))
        ticker_sql = f"AND UPPER(ticker) IN ({binds})"
    result = db.execute(
        text(
            f"""
            SELECT *
              FROM {MANAGEMENT_ENVELOPES_RELATION}
             WHERE status = 'open'
               {ticker_sql}
               AND ({_pattern_filter_sql()})
             ORDER BY id
            """
        ),
        params,
    )
    return [SimpleNamespace(**dict(row)) for row in result.mappings().all()]


def _normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def _object_snapshot(obj: Any) -> dict[str, Any]:
    return {
        field: _normalize(getattr(obj, field, None))
        for field in PATTERN_MONITOR_FIELDS
    }


def _objects_by_id(objects: list[Any]) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for obj in objects:
        raw_id = getattr(obj, "id", None)
        if raw_id is None:
            continue
        out[int(raw_id)] = obj
    return out


def _field_mismatches(old_objects: list[Any], new_objects: list[Any]) -> list[dict[str, Any]]:
    old_by_id = _objects_by_id(old_objects)
    new_by_id = _objects_by_id(new_objects)
    mismatches: list[dict[str, Any]] = []
    for trade_id in sorted(set(old_by_id) | set(new_by_id)):
        old_obj = old_by_id.get(trade_id)
        new_obj = new_by_id.get(trade_id)
        if old_obj is None or new_obj is None:
            mismatches.append(
                {
                    "id": trade_id,
                    "field": "presence",
                    "old": old_obj is not None,
                    "new": new_obj is not None,
                }
            )
            continue
        old_snap = _object_snapshot(old_obj)
        new_snap = _object_snapshot(new_obj)
        for field in PATTERN_MONITOR_FIELDS:
            if old_snap[field] != new_snap[field]:
                mismatches.append(
                    {
                        "id": trade_id,
                        "field": field,
                        "old": old_snap[field],
                        "new": new_snap[field],
                    }
                )
    return mismatches


def _stale_snapshot_for_compare(snapshot: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "id",
        "ticker",
        "broker_source",
        "position_id",
        "position_state",
        "position_quantity",
        "position_envelope_id",
        "reason",
        "broker_truth_status",
        "broker_truth_reason",
        "stale_broker_position",
        "broker_sync_missing_streak",
        "entry_date",
        "last_broker_sync",
    }
    return {
        key: _normalize(snapshot.get(key))
        for key in sorted(keep)
    }


def _broker_truth_projection(db, objects: list[Any]) -> dict[str, Any]:
    live, stale = filter_broker_stale_open_trades(db, objects)
    return {
        "live_ids": sorted(
            int(getattr(obj, "id"))
            for obj in live
            if getattr(obj, "id", None) is not None
        ),
        "stale": sorted(
            (_stale_snapshot_for_compare(row) for row in stale),
            key=lambda row: (int(row.get("id") or 0), str(row.get("reason") or "")),
        ),
    }


def run_probe(db) -> dict[str, Any]:
    old_tickers = _scope_tickers(db, relation_name=LEGACY_TRADES_COMPAT_RELATION)
    new_tickers = _scope_tickers(db, relation_name=MANAGEMENT_ENVELOPES_RELATION)
    tickers = sorted(set(old_tickers) | set(new_tickers))
    old_objects = _load_old_trade_objects(db, tickers=tickers)
    new_objects = _load_new_envelope_objects(db, tickers=tickers)

    old_ids = sorted(_objects_by_id(old_objects))
    new_ids = sorted(_objects_by_id(new_objects))
    field_mismatches = _field_mismatches(old_objects, new_objects)

    old_broker_truth = _broker_truth_projection(db, old_objects)
    new_broker_truth = _broker_truth_projection(db, new_objects)
    broker_truth_match = old_broker_truth == new_broker_truth

    relation_kinds = {
        MANAGEMENT_ENVELOPES_RELATION: _relation_kind(db, MANAGEMENT_ENVELOPES_RELATION),
        LEGACY_TRADES_COMPAT_RELATION: _relation_kind(db, LEGACY_TRADES_COMPAT_RELATION),
    }
    relation_ok = (
        relation_kinds.get(MANAGEMENT_ENVELOPES_RELATION) == "r"
        and relation_kinds.get(LEGACY_TRADES_COMPAT_RELATION) == "v"
    )
    selected_ids_match = old_ids == new_ids
    tickers_match = old_tickers == new_tickers
    ok = (
        relation_ok
        and tickers_match
        and selected_ids_match
        and not field_mismatches
        and broker_truth_match
    )
    if ok:
        status = "COMPLETE_POSITIVE"
        reason = f"{len(old_ids)} pattern-monitor runtime objects matched"
    else:
        status = "ALERT"
        reason = "pattern-monitor runtime object parity drift"
    return {
        "status": status,
        "reason": reason,
        "relation_kinds": relation_kinds,
        "tickers": tickers,
        "old_tickers": old_tickers,
        "new_tickers": new_tickers,
        "old_ids": old_ids,
        "new_ids": new_ids,
        "field_mismatches": field_mismatches,
        "old_broker_truth": old_broker_truth,
        "new_broker_truth": new_broker_truth,
        "broker_truth_match": broker_truth_match,
    }


def main() -> int:
    _assert_probe_database_allowed(os.getenv("DATABASE_URL"))
    db = SessionLocal()
    try:
        result = run_probe(db)
        print(f"VERDICT_STATUS={result['status']}")
        print(f"VERDICT_REASON={result['reason']}")
        print(f"RELATION_KINDS={result['relation_kinds']}")
        print(f"OLD_TICKERS={len(result['old_tickers'])}")
        print(f"NEW_TICKERS={len(result['new_tickers'])}")
        print(f"RUNTIME_OBJECTS_OLD={len(result['old_ids'])}")
        print(f"RUNTIME_OBJECTS_NEW={len(result['new_ids'])}")
        print(f"FIELD_MISMATCHES={len(result['field_mismatches'])}")
        print(f"BROKER_TRUTH_MATCH={result['broker_truth_match']}")
        if result["old_ids"] != result["new_ids"]:
            print(f"OLD_IDS={result['old_ids']}")
            print(f"NEW_IDS={result['new_ids']}")
        for mismatch in result["field_mismatches"][:20]:
            print("FIELD_MISMATCH " + json.dumps(mismatch, sort_keys=True, default=str))
        if not result["broker_truth_match"]:
            print("OLD_BROKER_TRUTH=" + json.dumps(result["old_broker_truth"], sort_keys=True, default=str))
            print("NEW_BROKER_TRUTH=" + json.dumps(result["new_broker_truth"], sort_keys=True, default=str))
        return 0 if result["status"] == "COMPLETE_POSITIVE" else 2
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
