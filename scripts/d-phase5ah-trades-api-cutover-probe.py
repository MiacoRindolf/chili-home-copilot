#!/usr/bin/env python
"""Read-only Phase 5AH /trades envelope cutover probe.

This compares the current Trade ORM /trades response construction with the
default-off management-envelope runtime-object path introduced for open/all
rows. Live/non-test use requires explicit opt-in because it touches production
broker-truth readers, even though it does not mutate data.
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
from app.routers.trading_sub.trades import _trade_like_public_response  # noqa: E402
from app.services import trading_service as ts  # noqa: E402
from app.services.trading.management_envelopes import (  # noqa: E402
    load_trades_api_envelope_objects,
)


LIVE_PROBE_OPT_IN = "PHASE5AH_ALLOW_LIVE_PROBE"


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
        "Phase 5AH /trades cutover probe defaults to test-only validation. "
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


def _payload(db, rows: list[Any], status: str | None) -> dict[str, Any]:
    rendered, suppressed = _trade_like_public_response(
        db,
        rows,
        apply_open_stale_filter=(status is None or str(status).strip().lower() == "open"),
    )
    return {
        "trades": _normalize(rendered),
        "suppressed_stale_trades": _normalize(suppressed),
        "suppressed_stale_count": len(suppressed),
    }


def _compare_status(db, user_id: int | None, status: str | None) -> dict[str, Any]:
    old_payload = _payload(db, ts.get_trades(db, user_id, status=status), status)
    new_payload = _payload(
        db,
        load_trades_api_envelope_objects(db, user_id=user_id, status=status, limit=50),
        status,
    )
    exact = old_payload == new_payload
    return {
        "status_filter": status,
        "exact_match": exact,
        "accepted": exact,
        "old_rows": len(old_payload["trades"]),
        "new_rows": len(new_payload["trades"]),
        "old_suppressed": old_payload["suppressed_stale_count"],
        "new_suppressed": new_payload["suppressed_stale_count"],
        "old_ids": [row.get("id") for row in old_payload["trades"]],
        "new_ids": [row.get("id") for row in new_payload["trades"]],
    }


def run_probe(user_id: int | None = 1) -> dict[str, Any]:
    database_url = os.getenv("DATABASE_URL")
    _assert_probe_database_allowed(database_url)
    db = SessionLocal()
    try:
        checks = [
            _compare_status(db, user_id, None),
            _compare_status(db, user_id, "open"),
            _compare_status(db, user_id, "closed"),
        ]
        ok = all(check["accepted"] for check in checks)
        return {
            "status": "COMPLETE_POSITIVE" if ok else "MISMATCH",
            "database_scope": "test" if _is_test_database_url(database_url) else "live_or_non_test",
            "user_id": user_id,
            "relation_kinds": {
                "trading_management_envelopes": _relation_kind(db, "trading_management_envelopes"),
                "trading_trades": _relation_kind(db, "trading_trades"),
            },
            "checks": checks,
        }
    finally:
        db.close()


def main() -> int:
    user_id_env = os.getenv("PHASE5AH_USER_ID", "1").strip()
    user_id = None if user_id_env.lower() in {"", "none", "null"} else int(user_id_env)
    payload = run_probe(user_id=user_id)
    print(f"VERDICT_STATUS={payload['status']}")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
