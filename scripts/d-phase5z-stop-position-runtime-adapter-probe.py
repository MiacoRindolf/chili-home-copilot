#!/usr/bin/env python
"""Read-only Phase 5Z stop-position runtime-adapter parity probe.

The live endpoint still reads SQLAlchemy ``Trade`` objects because its helper
chain is risk-display sensitive. This probe tests whether a runtime object
built from ``trading_management_envelopes`` rows can flow through the same
helper chain and serialize the same payload.
"""
from __future__ import annotations

import json
import math
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

os.environ.setdefault("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")

from app.db import SessionLocal  # noqa: E402
from app.models.trading import Trade  # noqa: E402


PUBLIC_POSITION_FIELDS = (
    "id",
    "ticker",
    "asset_type",
    "direction",
    "entry_price",
    "current_price",
    "stop_loss",
    "take_profit",
    "trail_stop",
    "high_watermark",
    "stop_model",
    "quantity",
    "broker_source",
    "broker_truth_entry_price",
    "broker_truth_quantity",
    "broker_truth_position_id",
    "broker_truth_current_envelope_id",
    "broker_truth_metrics_source",
    "R",
    "current_r",
    "stop_distance_pct",
    "pnl_pct",
    "state",
    "entry_date",
    "brain",
)


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > 0 else None


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
    return q.order_by(Trade.entry_date.desc(), Trade.id.desc()).all()


def load_envelope_objects(db, user_id: int | None) -> list[Any]:
    from app.services.trading.management_envelopes import (
        load_open_stop_position_envelope_objects,
    )

    return load_open_stop_position_envelope_objects(db, user_id=user_id)


def serialize_stop_positions(
    db,
    trades: list[Any],
    *,
    quote_cache: dict[tuple[str, str, str, str], dict[str, Any] | None],
) -> dict[str, Any]:
    from app.services.trading.autopilot_scope import is_option_trade
    from app.services.trading.broker_position_truth import (
        broker_position_display_metrics,
        filter_broker_stale_open_trades,
    )
    from app.services.trading.stop_engine import _build_brain_context

    trades, suppressed_stale_trades = filter_broker_stale_open_trades(db, trades)

    result = []
    for t in trades:
        trade_is_option = is_option_trade(t)
        broker_metrics = (
            broker_position_display_metrics(db, t)
            if not trade_is_option
            else None
        ) or {}
        entry = (
            _positive_float(broker_metrics.get("entry_price"))
            or _positive_float(getattr(t, "entry_price", None))
            or 0
        )
        quantity = (
            _positive_float(broker_metrics.get("quantity"))
            or _positive_float(getattr(t, "quantity", None))
            or 0
        )
        stop = getattr(t, "stop_loss", None)
        target = getattr(t, "take_profit", None)
        R = abs(entry - stop) if stop and entry else 0
        hwm = getattr(t, "high_watermark", None) or entry
        ticker = getattr(t, "ticker", None)
        broker_source = getattr(t, "broker_source", None)

        try:
            q = None
            if (broker_source or "").strip() or trade_is_option:
                from app.services.trading.broker_quotes import broker_quote_for_trade

                key = (
                    "broker_quote",
                    str(broker_source or ""),
                    str(ticker or "").upper(),
                    "option" if trade_is_option else "standard",
                )
                if key not in quote_cache:
                    quote_cache[key] = broker_quote_for_trade(t, purpose="display")
                q = quote_cache[key]
            if (not q or q.get("price") is None) and not trade_is_option:
                from app.services.trading.market_data import fetch_quote

                key = ("market_quote", "", str(ticker or "").upper(), "standard")
                if key not in quote_cache:
                    quote_cache[key] = fetch_quote(ticker)
                q = quote_cache[key]
            price = q.get("price", 0) if q else 0
        except Exception:
            price = 0

        direction = getattr(t, "direction", None) or "long"
        current_r = 0
        if R > 0 and price and entry:
            if direction == "long":
                current_r = round((price - entry) / R, 2)
            else:
                current_r = round((entry - price) / R, 2)

        stop_distance_pct = 0
        if stop and price and price > 0:
            stop_distance_pct = round(abs(price - stop) / price * 100, 2)

        pnl_pct = 0
        if entry and price and entry > 0:
            if direction == "long":
                pnl_pct = round((price - entry) / entry * 100, 2)
            else:
                pnl_pct = round((entry - price) / entry * 100, 2)

        state = "initial"
        if current_r >= 2.0:
            state = "trailing"
        elif current_r >= 1.0:
            state = "breakeven"
        if stop and price:
            if (direction == "long" and price <= stop) or (
                direction != "long" and price >= stop
            ):
                state = "triggered"
            elif R > 0 and abs(price - stop) / R <= 0.25:
                state = "warn"

        brain_ctx = {}
        try:
            brain = _build_brain_context(t, db)
            brain_ctx = brain.summary_dict()
        except Exception:
            pass

        entry_date = getattr(t, "entry_date", None)
        row = {
            "id": getattr(t, "id", None),
            "ticker": ticker,
            "asset_type": "options" if trade_is_option else (
                "crypto" if (ticker or "").upper().endswith("-USD") else "stock"
            ),
            "direction": direction,
            "entry_price": entry,
            "current_price": price,
            "stop_loss": stop,
            "take_profit": target,
            "trail_stop": getattr(t, "trail_stop", None),
            "high_watermark": hwm,
            "stop_model": getattr(t, "stop_model", None),
            "quantity": quantity,
            "broker_source": broker_source,
            "broker_truth_entry_price": broker_metrics.get("entry_price"),
            "broker_truth_quantity": broker_metrics.get("quantity"),
            "broker_truth_position_id": broker_metrics.get("position_id"),
            "broker_truth_current_envelope_id": broker_metrics.get("current_envelope_id"),
            "broker_truth_metrics_source": broker_metrics.get("source"),
            "R": round(R, 4),
            "current_r": current_r,
            "stop_distance_pct": stop_distance_pct,
            "pnl_pct": pnl_pct,
            "state": state,
            "entry_date": entry_date.isoformat() if entry_date else None,
            "brain": brain_ctx,
        }
        result.append({field: row.get(field) for field in PUBLIC_POSITION_FIELDS})

    return {
        "positions": sorted((_normalize(r) for r in result), key=lambda r: int(r["id"] or 0)),
        "suppressed_stale_trades": sorted(
            (_normalize(r) for r in suppressed_stale_trades),
            key=lambda r: int(r.get("id") or 0),
        ),
        "suppressed_stale_count": len(suppressed_stale_trades),
    }


def run_probe(user_id: int | None = 1) -> dict[str, Any]:
    db = SessionLocal()
    try:
        relation_kinds = {
            "trading_management_envelopes": _relation_kind(db, "trading_management_envelopes"),
            "trading_trades": _relation_kind(db, "trading_trades"),
        }
        quote_cache: dict[tuple[str, str, str, str], dict[str, Any] | None] = {}
        old_payload = serialize_stop_positions(
            db,
            load_trade_objects(db, user_id),
            quote_cache=quote_cache,
        )
        new_payload = serialize_stop_positions(
            db,
            load_envelope_objects(db, user_id),
            quote_cache=quote_cache,
        )
        matched = old_payload == new_payload
        return {
            "status": "COMPLETE_POSITIVE" if matched else "MISMATCH",
            "matched": matched,
            "user_id": user_id,
            "old_positions": len(old_payload["positions"]),
            "new_positions": len(new_payload["positions"]),
            "old_suppressed": old_payload["suppressed_stale_count"],
            "new_suppressed": new_payload["suppressed_stale_count"],
            "quote_cache_entries": len(quote_cache),
            "relation_kinds": relation_kinds,
            "first_mismatch": None if matched else {
                "old": old_payload,
                "new": new_payload,
            },
        }
    finally:
        db.close()


def main() -> int:
    user_id_env = os.getenv("PHASE5Z_USER_ID", "1").strip()
    user_id = None if user_id_env.lower() in {"", "none", "null"} else int(user_id_env)
    payload = run_probe(user_id=user_id)
    print(f"VERDICT_STATUS={payload['status']}")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
