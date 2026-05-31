#!/usr/bin/env python
"""Read-only Phase 5O position-plan generator envelope parity probe.

``position_plan_generator.py`` is a live/risk-adjacent planning surface. It
loads open live management envelopes, enriches them with monitor decisions,
patterns, alert plans, quote inputs, and market regime, then calls an LLM and
caches advisory plans keyed by trade ids. This probe does not call the LLM and
does not mutate the plan cache. It compares the open-position inputs through
the legacy ``trading_trades`` compatibility view and the physical
``trading_management_envelopes`` source.
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
from app.services.trading.autopilot_scope import is_option_trade  # noqa: E402
from app.services.trading.position_plan_generator import (  # noqa: E402
    _build_position_context,
)


LIVE_PROBE_OPT_IN = "PHASE5O_POSITION_PLAN_ALLOW_LIVE_PROBE"
PROBE_USER_ID_ENV = "PHASE5O_POSITION_PLAN_USER_ID"
PLAN_RELATIONS = {
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
        "Phase 5O position-plan probe defaults to test-only validation. "
        f"Set {LIVE_PROBE_OPT_IN}=true to run manually authorized read-only "
        "live/non-test DB evidence."
    )


def _relation_sql(relation_name: str) -> str:
    if relation_name not in PLAN_RELATIONS:
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
           AND entry_price > 0
           AND user_id IS NOT NULL
         GROUP BY user_id
         ORDER BY COUNT(*) DESC, user_id
         LIMIT 1
        """,
    )
    if not rows:
        return None
    return int(rows[0]["user_id"])


def _open_plan_rows(
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
               sector,
               trade_type,
               notes,
               asset_kind,
               tags,
               indicator_snapshot
          FROM {relation}
         WHERE user_id IS NOT DISTINCT FROM :uid
           AND status = 'open'
           AND entry_price > 0
         ORDER BY entry_date DESC NULLS LAST, id DESC
        """,
        {"uid": user_id},
    )


def _as_runtime(row: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**row)


def _row_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
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
        "sector": row.get("sector"),
        "trade_type": row.get("trade_type"),
        "asset_kind": row.get("asset_kind"),
        "is_option": bool(is_option_trade(runtime)),
    }


def _static_quote_inputs(
    runtime_rows: list[SimpleNamespace],
) -> tuple[dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    quotes: dict[str, dict[str, Any]] = {}
    trade_quotes: dict[int, dict[str, Any]] = {}
    for row in runtime_rows:
        price = getattr(row, "entry_price", None)
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None or price <= 0:
            continue
        payload = {"price": price, "source": "phase5o_probe_static_entry"}
        if is_option_trade(row):
            trade_quotes[int(row.id)] = payload
        else:
            quotes[str(row.ticker).upper()] = payload
    return quotes, trade_quotes


def _context_fingerprint(pos: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": pos.get("trade_id"),
        "ticker": pos.get("ticker"),
        "asset_type": pos.get("asset_type"),
        "direction": pos.get("direction"),
        "entry_price": pos.get("entry_price"),
        "current_price": pos.get("current_price"),
        "quantity": pos.get("quantity"),
        "entry_value_usd": pos.get("entry_value_usd"),
        "current_value_usd": pos.get("current_value_usd"),
        "unrealized_pnl_usd": pos.get("unrealized_pnl_usd"),
        "stop_loss": pos.get("stop_loss"),
        "take_profit": pos.get("take_profit"),
        "sector": pos.get("sector"),
        "trade_type": pos.get("trade_type"),
        "pattern_name": pos.get("pattern_name"),
        "pattern_timeframe": pos.get("pattern_timeframe"),
        "latest_monitor": pos.get("latest_monitor"),
        "price_domain": pos.get("price_domain"),
        "price_domains": pos.get("price_domains"),
        "option_meta": pos.get("option_meta"),
        "has_bars_held": pos.get("bars_held") is not None,
    }


def _plan_values_for_rows(db, rows: list[dict[str, Any]]) -> dict[str, Any]:
    runtime_rows = [_as_runtime(row) for row in rows]
    quotes, trade_quotes = _static_quote_inputs(runtime_rows)
    contexts = _build_position_context(db, runtime_rows, quotes, trade_quotes)
    row_fingerprints = [_row_fingerprint(row) for row in rows]
    return {
        "open_plan_rows": row_fingerprints,
        "plan_cache_trade_ids": [fp["id"] for fp in row_fingerprints],
        "plan_quote_inputs": {
            "market_quote_tickers": sorted(quotes),
            "option_quote_trade_ids": sorted(trade_quotes),
        },
        "plan_context_rows": [_context_fingerprint(pos) for pos in contexts],
    }


def _scope_values(db, *, relation_name: str, user_id: int | None) -> dict[str, Any]:
    return _plan_values_for_rows(
        db,
        _open_plan_rows(db, relation_name=relation_name, user_id=user_id),
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
        f"{len(comparisons)} position-plan checks matched"
        if status == "COMPLETE_POSITIVE"
        else "position-plan parity drift or relation-kind drift"
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
    print(f"POSITION_PLAN_CHECKS={result['checks']}")
    print(f"POSITION_PLAN_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "POSITION_PLAN_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
        if not row["match"]:
            print("OLD=" + json.dumps(row["old"], sort_keys=True, default=str))
            print("NEW=" + json.dumps(row["new"], sort_keys=True, default=str))
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
