#!/usr/bin/env python
"""Read-only Phase 5AG /trades open-row runtime-adapter parity probe.

The public /api/trading/trades route can now render closed rows from
``trading_management_envelopes`` behind a default-off flag, but open rows still
need broker-truth overlays and stale-open suppression. This probe compares the
current ``Trade`` ORM object path with candidate management-envelope runtime
objects before any open/all route cutover is allowed.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
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
from app.models.trading import Trade  # noqa: E402


PUBLIC_OPEN_TRADE_FIELDS = (
    "id",
    "ticker",
    "direction",
    "entry_price",
    "exit_price",
    "quantity",
    "local_entry_price",
    "local_quantity",
    "entry_date",
    "exit_date",
    "status",
    "pnl",
    "tags",
    "notes",
    "broker_source",
    "broker_status",
    "broker_order_id",
    "filled_at",
    "avg_fill_price",
    "tca_reference_entry_price",
    "tca_entry_slippage_bps",
    "tca_reference_exit_price",
    "tca_exit_slippage_bps",
    "strategy_proposal_id",
    "scan_pattern_id",
    "position_id",
    "broker_truth_entry_price",
    "broker_truth_quantity",
    "broker_truth_position_id",
    "broker_truth_current_envelope_id",
    "broker_truth_metrics_source",
)

LIVE_PROBE_OPT_IN = "PHASE5AG_ALLOW_LIVE_PROBE"


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
        "Phase 5AG /trades open-row probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, tuple):
        return [_normalize(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _relation_kind(db, relation_name: str) -> str | None:
    return db.execute(
        text("SELECT relkind FROM pg_class WHERE relname = :name"),
        {"name": relation_name},
    ).scalar()


def load_trade_objects(db, user_id: int | None) -> list[Any]:
    q = db.query(Trade).filter(Trade.status == "open")
    if user_id is None:
        q = q.filter(Trade.user_id.is_(None))
    else:
        q = q.filter(Trade.user_id == user_id)
    return q.order_by(Trade.entry_date.desc(), Trade.id.desc()).limit(50).all()


def load_envelope_objects(db, user_id: int | None) -> list[Any]:
    from app.services.trading.management_envelopes import _envelope_runtime_object

    rows = db.execute(
        text(
            """
            SELECT *
              FROM trading_management_envelopes
             WHERE user_id IS NOT DISTINCT FROM :uid
               AND status = 'open'
             ORDER BY entry_date DESC, id DESC
             LIMIT 50
            """
        ),
        {"uid": user_id},
    ).mappings().all()
    return [_envelope_runtime_object(dict(row)) for row in rows]


def serialize_open_trades_api_rows(db, trades: list[Any]) -> dict[str, Any]:
    from app.services.trading.broker_position_truth import (
        broker_position_display_metrics,
        filter_broker_stale_open_trades,
    )

    trades, suppressed_stale_trades = filter_broker_stale_open_trades(db, trades)
    rows: list[dict[str, Any]] = []
    for t in trades:
        broker_metrics = (
            broker_position_display_metrics(db, t)
            if getattr(t, "status", None) == "open"
            else None
        ) or {}
        display_entry = broker_metrics.get("entry_price") or getattr(t, "entry_price", None)
        display_quantity = broker_metrics.get("quantity") or getattr(t, "quantity", None)
        entry_date = getattr(t, "entry_date", None)
        exit_date = getattr(t, "exit_date", None)
        filled_at = getattr(t, "filled_at", None)
        row = {
            "id": getattr(t, "id", None),
            "ticker": getattr(t, "ticker", None),
            "direction": getattr(t, "direction", None),
            "entry_price": display_entry,
            "exit_price": getattr(t, "exit_price", None),
            "quantity": display_quantity,
            "local_entry_price": getattr(t, "entry_price", None),
            "local_quantity": getattr(t, "quantity", None),
            "entry_date": entry_date.isoformat() if entry_date else None,
            "exit_date": exit_date.isoformat() if exit_date else None,
            "status": getattr(t, "status", None),
            "pnl": getattr(t, "pnl", None),
            "tags": getattr(t, "tags", None),
            "notes": getattr(t, "notes", None),
            "broker_source": getattr(t, "broker_source", None),
            "broker_status": getattr(t, "broker_status", None),
            "broker_order_id": getattr(t, "broker_order_id", None),
            "filled_at": filled_at.isoformat() if filled_at else None,
            "avg_fill_price": getattr(t, "avg_fill_price", None),
            "tca_reference_entry_price": getattr(t, "tca_reference_entry_price", None),
            "tca_entry_slippage_bps": getattr(t, "tca_entry_slippage_bps", None),
            "tca_reference_exit_price": getattr(t, "tca_reference_exit_price", None),
            "tca_exit_slippage_bps": getattr(t, "tca_exit_slippage_bps", None),
            "strategy_proposal_id": getattr(t, "strategy_proposal_id", None),
            "scan_pattern_id": getattr(t, "scan_pattern_id", None),
            "position_id": getattr(t, "position_id", None),
            "broker_truth_entry_price": broker_metrics.get("entry_price"),
            "broker_truth_quantity": broker_metrics.get("quantity"),
            "broker_truth_position_id": broker_metrics.get("position_id"),
            "broker_truth_current_envelope_id": broker_metrics.get("current_envelope_id"),
            "broker_truth_metrics_source": broker_metrics.get("source"),
        }
        rows.append({field: row.get(field) for field in PUBLIC_OPEN_TRADE_FIELDS})

    return {
        "trades": sorted((_normalize(row) for row in rows), key=lambda row: int(row["id"] or 0)),
        "suppressed_stale_trades": sorted(
            (_normalize(row) for row in suppressed_stale_trades),
            key=lambda row: int(row.get("id") or 0),
        ),
        "suppressed_stale_count": len(suppressed_stale_trades),
    }


def run_probe(user_id: int | None = 1) -> dict[str, Any]:
    database_url = os.getenv("DATABASE_URL")
    _assert_probe_database_allowed(database_url)
    db = SessionLocal()
    try:
        relation_kinds = {
            "trading_management_envelopes": _relation_kind(db, "trading_management_envelopes"),
            "trading_trades": _relation_kind(db, "trading_trades"),
        }
        old_payload = serialize_open_trades_api_rows(db, load_trade_objects(db, user_id))
        new_payload = serialize_open_trades_api_rows(db, load_envelope_objects(db, user_id))
        matched = old_payload == new_payload
        return {
            "status": "COMPLETE_POSITIVE" if matched else "MISMATCH",
            "matched": matched,
            "database_scope": "test" if _is_test_database_url(database_url) else "live_or_non_test",
            "user_id": user_id,
            "old_trades": len(old_payload["trades"]),
            "new_trades": len(new_payload["trades"]),
            "old_suppressed": old_payload["suppressed_stale_count"],
            "new_suppressed": new_payload["suppressed_stale_count"],
            "relation_kinds": relation_kinds,
            "first_mismatch": None if matched else {
                "old": old_payload,
                "new": new_payload,
            },
        }
    finally:
        db.close()


def main() -> int:
    user_id_env = os.getenv("PHASE5AG_USER_ID", "1").strip()
    user_id = None if user_id_env.lower() in {"", "none", "null"} else int(user_id_env)
    payload = run_probe(user_id=user_id)
    print(f"VERDICT_STATUS={payload['status']}")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
