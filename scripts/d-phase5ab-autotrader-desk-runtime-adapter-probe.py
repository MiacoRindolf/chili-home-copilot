#!/usr/bin/env python
"""Read-only Phase 5AB AutoTrader desk runtime-adapter parity probe."""
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
from app.models.trading import ScanPattern, Trade  # noqa: E402
from app.services.trading import autotrader_desk as desk  # noqa: E402
from app.services.trading.auto_trader_position_overrides import (  # noqa: E402
    _opened_today_et,
    list_position_overrides,
)
from app.services.trading.autopilot_scope import (  # noqa: E402
    classify_live_autopilot_trade_scope,
    is_option_trade,
    live_autopilot_trade_filter,
)
from app.services.trading.broker_position_truth import (  # noqa: E402
    broker_position_display_metrics,
    broker_stale_open_trade_snapshot,
    filter_broker_stale_open_trades,
)
from app.services.trading.management_envelopes import _envelope_runtime_object  # noqa: E402


LIVE_PROBE_OPT_IN = "PHASE5AB_ALLOW_LIVE_PROBE"

DESK_TRADE_FIELDS = (
    "kind",
    "id",
    "ticker",
    "direction",
    "entry_price",
    "entry_date",
    "quantity",
    "stop_loss",
    "take_profit",
    "scan_pattern_id",
    "pattern_name",
    "monitor_scope",
    "related_alert_id",
    "broker_source",
    "asset_type",
    "auto_trader_v1",
    "scale_in_count",
    "tags",
    "overrides",
    "opened_today_et",
    "controls_supported",
    "close_supported",
    "current_price",
    "unrealized_pnl_usd",
    "unrealized_pnl_pct",
    "quote_source",
    "broker_truth_entry_price",
    "broker_truth_quantity",
    "broker_truth_position_id",
    "broker_truth_current_envelope_id",
    "broker_truth_metrics_source",
)


def _database_name(url: str) -> str:
    clean = url.split("?", 1)[0].rstrip("/")
    return clean.rsplit("/", 1)[-1]


def _live_probe_allowed() -> bool:
    return str(os.getenv(LIVE_PROBE_OPT_IN, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _assert_probe_database_allowed(database_url: str) -> str:
    name = _database_name(database_url)
    if name.endswith("_test"):
        return "test"
    if not _live_probe_allowed():
        raise RuntimeError(
            "Phase 5AB AutoTrader desk probe defaults to test-only validation. "
            f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
            "live/non-test DB evidence."
        )
    return "live_or_non_test"


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


def _broker_stale_filtered(db, rows: list[Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    rows, suppressed = filter_broker_stale_open_trades(db, rows)
    live_rows: list[Any] = []
    for row in rows:
        snap = None
        if getattr(row, "position_id", None) is not None:
            snap = broker_stale_open_trade_snapshot(db, row, grace_seconds=0)
        if snap and snap.get("reason") in {
            "position_identity_closed",
            "position_identity_zero_qty",
        }:
            suppressed.append(snap)
            continue
        live_rows.append(row)
    return live_rows, suppressed


def load_trade_objects(db, user_id: int) -> tuple[list[Any], list[dict[str, Any]]]:
    rows = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "open",
            live_autopilot_trade_filter(),
        )
        .order_by(Trade.id.desc())
        .all()
    )
    return _broker_stale_filtered(db, rows)


def load_envelope_objects(db, user_id: int) -> tuple[list[Any], list[dict[str, Any]]]:
    rows = db.execute(
        text(
            """
            SELECT *
              FROM trading_management_envelopes
             WHERE user_id = :uid
               AND status = 'open'
               AND (
                    auto_trader_version = 'v1'
                 OR scan_pattern_id IS NOT NULL
                 OR related_alert_id IS NOT NULL
                 OR stop_loss IS NOT NULL
                 OR take_profit IS NOT NULL
               )
             ORDER BY id DESC
            """
        ),
        {"uid": user_id},
    ).mappings().all()
    return _broker_stale_filtered(
        db,
        [_envelope_runtime_object(dict(row)) for row in rows],
    )


def _quote_cache_key(trade: Any, trade_is_option: bool) -> tuple[str, str, str, str]:
    try:
        snap = json.dumps(
            getattr(trade, "indicator_snapshot", None),
            sort_keys=True,
            default=str,
        )
    except Exception:
        snap = ""
    return (
        str(getattr(trade, "broker_source", "") or "").strip().lower(),
        str(getattr(trade, "ticker", "") or "").strip().upper(),
        "option" if trade_is_option else "standard",
        snap,
    )


def _broker_quote_price_for_trade_cached(
    trade: Any,
    *,
    trade_is_option: bool,
    quote_cache: dict[tuple[str, str, str, str], tuple[float | None, str]],
    allow_external_reads: bool = False,
) -> tuple[float | None, str]:
    if not allow_external_reads:
        return None, "unavailable"
    broker_source = str(getattr(trade, "broker_source", "") or "").strip().lower()
    if not broker_source and not trade_is_option:
        return None, "unavailable"
    key = _quote_cache_key(trade, trade_is_option)
    if key not in quote_cache:
        try:
            from app.services.trading.broker_quotes import broker_quote_for_trade

            quote = broker_quote_for_trade(trade, purpose="display")
            px = desk._safe_quote_float(quote.get("price"))
            source = str(quote.get("source") or f"{broker_source}_unavailable")
            quote_cache[key] = (px, source) if px is not None else (None, source)
        except Exception:
            quote_cache[key] = (None, f"{broker_source or 'broker'}_unavailable")
    return quote_cache[key]


def _fallback_quote_cached(
    ticker: str,
    *,
    fallback_cache: dict[str, float | None],
    allow_external_reads: bool = False,
) -> float | None:
    if not allow_external_reads:
        return None
    key = str(ticker or "").strip().upper()
    if key not in fallback_cache:
        fallback_cache[key] = desk._fallback_quote(key)
    return fallback_cache[key]


def serialize_desk_trades(
    db,
    trades: list[Any],
    suppressed_stale_trades: list[dict[str, Any]],
    *,
    quote_cache: dict[tuple[str, str, str, str], tuple[float | None, str]],
    fallback_cache: dict[str, float | None],
    allow_external_reads: bool = False,
) -> dict[str, Any]:
    override_pairs = [("trade", int(t.id)) for t in trades]
    overrides_map = list_position_overrides(db, override_pairs)

    out: list[dict[str, Any]] = []
    for t in trades:
        monitor_scope = classify_live_autopilot_trade_scope(t)
        pat_name = None
        if getattr(t, "scan_pattern_id", None):
            p = db.get(ScanPattern, int(t.scan_pattern_id))
            if p:
                pat_name = p.name
        is_atv1 = (getattr(t, "auto_trader_version", None) or "") == "v1"
        ov = overrides_map.get(("trade", int(t.id)))
        opened_today = bool(getattr(t, "entry_date", None) and _opened_today_et(t.entry_date))
        trade_is_option = is_option_trade(t)
        current_price, quote_source = _broker_quote_price_for_trade_cached(
            t,
            trade_is_option=trade_is_option,
            quote_cache=quote_cache,
            allow_external_reads=allow_external_reads,
        )
        if (
            current_price is None
            and not trade_is_option
            and not (getattr(t, "broker_source", None) or "").strip()
        ):
            current_price = _fallback_quote_cached(
                t.ticker,
                fallback_cache=fallback_cache,
                allow_external_reads=allow_external_reads,
            )
            quote_source = "market_data" if current_price is not None else "unavailable"
        broker_metrics = (
            broker_position_display_metrics(db, t)
            if allow_external_reads and not trade_is_option
            else None
        ) or {}
        display_entry = broker_metrics.get("entry_price") or t.entry_price
        display_quantity = broker_metrics.get("quantity") or t.quantity
        pnl_usd, pnl_pct = desk._compute_unrealized(
            entry_price=float(display_entry),
            current_price=current_price,
            quantity=float(display_quantity or 0),
            direction=t.direction,
            multiplier=100.0 if trade_is_option else 1.0,
        )
        row = {
            "kind": "trade",
            "id": t.id,
            "ticker": t.ticker,
            "direction": t.direction,
            "entry_price": float(display_entry),
            "entry_date": t.entry_date.isoformat() if t.entry_date else None,
            "quantity": float(display_quantity or 0),
            "stop_loss": float(t.stop_loss) if t.stop_loss is not None else None,
            "take_profit": float(t.take_profit) if t.take_profit is not None else None,
            "scan_pattern_id": t.scan_pattern_id,
            "pattern_name": pat_name,
            "monitor_scope": monitor_scope,
            "related_alert_id": t.related_alert_id,
            "broker_source": t.broker_source,
            "asset_type": desk._trade_asset_type(t),
            "auto_trader_v1": is_atv1,
            "scale_in_count": int(t.scale_in_count or 0),
            "tags": t.tags,
            "overrides": ov or {"monitor_paused": False, "synergy_excluded": False},
            "opened_today_et": opened_today,
            "controls_supported": True,
            "close_supported": True,
            "current_price": float(current_price) if current_price is not None else None,
            "unrealized_pnl_usd": pnl_usd,
            "unrealized_pnl_pct": pnl_pct,
            "quote_source": quote_source,
            "broker_truth_entry_price": broker_metrics.get("entry_price"),
            "broker_truth_quantity": broker_metrics.get("quantity"),
            "broker_truth_position_id": broker_metrics.get("position_id"),
            "broker_truth_current_envelope_id": broker_metrics.get("current_envelope_id"),
            "broker_truth_metrics_source": broker_metrics.get("source"),
        }
        out.append({field: row.get(field) for field in DESK_TRADE_FIELDS})

    out.sort(key=lambda row: int(row.get("id") or 0), reverse=True)
    return {
        "trades": _normalize(out),
        "suppressed_stale_trades": sorted(
            (_normalize(row) for row in suppressed_stale_trades),
            key=lambda row: int(row.get("id") or 0),
        ),
    }


def run_probe(user_id: int = 1) -> dict[str, Any]:
    database_url = os.getenv("DATABASE_URL", "")
    database_scope = _assert_probe_database_allowed(database_url)
    allow_external_reads = _live_probe_allowed()
    db = SessionLocal()
    try:
        relation_kinds = {
            "trading_management_envelopes": _relation_kind(db, "trading_management_envelopes"),
            "trading_trades": _relation_kind(db, "trading_trades"),
        }
        quote_cache: dict[tuple[str, str, str, str], tuple[float | None, str]] = {}
        fallback_cache: dict[str, float | None] = {}
        old_rows, old_suppressed = load_trade_objects(db, user_id)
        new_rows, new_suppressed = load_envelope_objects(db, user_id)
        old_payload = serialize_desk_trades(
            db,
            old_rows,
            old_suppressed,
            quote_cache=quote_cache,
            fallback_cache=fallback_cache,
            allow_external_reads=allow_external_reads,
        )
        new_payload = serialize_desk_trades(
            db,
            new_rows,
            new_suppressed,
            quote_cache=quote_cache,
            fallback_cache=fallback_cache,
            allow_external_reads=allow_external_reads,
        )
        matched = old_payload == new_payload
        return {
            "status": "COMPLETE_POSITIVE" if matched else "MISMATCH",
            "matched": matched,
            "database_scope": database_scope,
            "external_reads_enabled": allow_external_reads,
            "user_id": user_id,
            "old_trades": len(old_payload["trades"]),
            "new_trades": len(new_payload["trades"]),
            "old_suppressed": len(old_payload["suppressed_stale_trades"]),
            "new_suppressed": len(new_payload["suppressed_stale_trades"]),
            "quote_cache_entries": len(quote_cache),
            "fallback_cache_entries": len(fallback_cache),
            "relation_kinds": relation_kinds,
            "first_mismatch": None if matched else {
                "old": old_payload,
                "new": new_payload,
            },
        }
    finally:
        db.close()


def main() -> int:
    user_id = int(os.getenv("PHASE5AB_USER_ID", "1"))
    payload = run_probe(user_id=user_id)
    print(f"VERDICT_STATUS={payload['status']}")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
