#!/usr/bin/env python
"""Read-only Phase 5AB scheduler-scope parity probe.

``trading_scheduler.py`` still uses the legacy ``Trade`` ORM in monitor
selection paths. Those reads decide which users/tickers/trades get evaluated by
price, stop, crypto, broker-backed, and pattern-position monitor jobs, so they
are live runtime behavior rather than passive reporting. This probe compares
the current compatibility-view scopes with the physical management-envelope
source before any scheduler conversion.
"""
from __future__ import annotations

import os
import sys
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
from app.models.trade_relation_symbols import (  # noqa: E402
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
)
from app.services.trading.broker_position_truth import (  # noqa: E402
    BROKER_POSITION_TRUTH_SOURCES,
)


LIVE_PROBE_OPT_IN = "PHASE5AB_ALLOW_LIVE_PROBE"
DAYTRADE_TYPES = ("scalp", "daytrade", "breakout", "momentum")
SCHEDULER_SCOPE_RELATIONS = {
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
        "Phase 5AB trading-scheduler scope probe defaults to test-only "
        f"validation. Set {LIVE_PROBE_OPT_IN}=true to run manually authorized "
        "read-only live/non-test DB evidence."
    )


def _relation_kind(db, relation_name: str) -> str | None:
    return db.execute(
        text("SELECT relkind FROM pg_class WHERE relname = :name"),
        {"name": relation_name},
    ).scalar()


def _relation_sql(relation_name: str) -> str:
    if relation_name not in SCHEDULER_SCOPE_RELATIONS:
        raise ValueError(f"unsupported relation: {relation_name!r}")
    return relation_name


def _bind_list(prefix: str, values: list[str]) -> tuple[str, dict[str, str]]:
    binds: list[str] = []
    params: dict[str, str] = {}
    for idx, value in enumerate(values):
        key = f"{prefix}_{idx}"
        binds.append(f":{key}")
        params[key] = value
    if not binds:
        return "NULL", {}
    return ", ".join(binds), params


def _scalar_rows(db, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    rows = db.execute(text(sql), params or {}).fetchall() or []
    return [row[0] for row in rows]


def _mapping_rows(db, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = db.execute(text(sql), params or {})
    return [dict(row) for row in result.mappings().all()]


def _user_ids(db, *, relation_name: str, where_sql: str, params: dict[str, Any] | None = None) -> list[int]:
    relation = _relation_sql(relation_name)
    rows = _scalar_rows(
        db,
        f"""
        SELECT DISTINCT user_id
          FROM {relation}
         WHERE status = 'open'
           AND user_id IS NOT NULL
           AND ({where_sql})
         ORDER BY user_id
        """,
        params,
    )
    return [int(row) for row in rows if row is not None]


def _tickers(db, *, relation_name: str, where_sql: str, params: dict[str, Any] | None = None) -> list[str]:
    relation = _relation_sql(relation_name)
    rows = _scalar_rows(
        db,
        f"""
        SELECT DISTINCT UPPER(ticker) AS ticker
          FROM {relation}
         WHERE status = 'open'
           AND ticker IS NOT NULL
           AND ticker <> ''
           AND ({where_sql})
         ORDER BY UPPER(ticker)
        """,
        params,
    )
    return [str(row).upper() for row in rows if row]


def _crypto_counts_by_user(db, *, relation_name: str) -> list[dict[str, int]]:
    relation = _relation_sql(relation_name)
    rows = _mapping_rows(
        db,
        f"""
        SELECT user_id, COUNT(*)::bigint AS n
          FROM {relation}
         WHERE status = 'open'
           AND user_id IS NOT NULL
           AND ticker LIKE '%-USD'
         GROUP BY user_id
         ORDER BY user_id
        """,
    )
    return [
        {"user_id": int(row["user_id"]), "n": int(row["n"])}
        for row in rows
    ]


def _pattern_trigger_trade_ids(db, *, relation_name: str) -> list[int]:
    relation = _relation_sql(relation_name)
    rows = _scalar_rows(
        db,
        f"""
        SELECT id
          FROM {relation}
         WHERE status = 'open'
           AND (
                related_alert_id IS NOT NULL
             OR (
                related_alert_id IS NULL
                AND (stop_loss IS NOT NULL OR take_profit IS NOT NULL)
             )
           )
         ORDER BY id
        """,
    )
    return [int(row) for row in rows]


def _source_filter() -> tuple[str, dict[str, Any]]:
    sources = sorted(str(src).lower() for src in BROKER_POSITION_TRUTH_SOURCES)
    binds, params = _bind_list("src", sources)
    return f"LOWER(COALESCE(broker_source, '')) IN ({binds})", params


def _trade_type_filter() -> tuple[str, dict[str, Any]]:
    binds, params = _bind_list("trade_type", list(DAYTRADE_TYPES))
    return f"trade_type IN ({binds})", params


def _pattern_position_filter() -> str:
    return """
        related_alert_id IS NOT NULL
        OR (
            related_alert_id IS NULL
            AND (stop_loss IS NOT NULL OR take_profit IS NOT NULL)
        )
    """


def _scope_values(db, *, relation_name: str) -> dict[str, Any]:
    source_filter, source_params = _source_filter()
    trade_type_filter, trade_type_params = _trade_type_filter()
    pattern_filter = _pattern_position_filter()
    return {
        "price_monitor_user_ids": _user_ids(
            db,
            relation_name=relation_name,
            where_sql="TRUE",
        ),
        "price_monitor_pattern_tickers": _tickers(
            db,
            relation_name=relation_name,
            where_sql="related_alert_id IS NOT NULL",
        ),
        "broker_position_user_ids": _user_ids(
            db,
            relation_name=relation_name,
            where_sql=source_filter,
            params=source_params,
        ),
        "broker_position_pattern_tickers": _tickers(
            db,
            relation_name=relation_name,
            where_sql=f"related_alert_id IS NOT NULL AND {source_filter}",
            params=source_params,
        ),
        "daytrade_fast_user_ids": _user_ids(
            db,
            relation_name=relation_name,
            where_sql=trade_type_filter,
            params=trade_type_params,
        ),
        "crypto_stop_user_ids": _user_ids(
            db,
            relation_name=relation_name,
            where_sql="ticker LIKE '%-USD'",
        ),
        "crypto_stop_counts_by_user": _crypto_counts_by_user(
            db,
            relation_name=relation_name,
        ),
        "pattern_position_user_ids": _user_ids(
            db,
            relation_name=relation_name,
            where_sql=pattern_filter,
        ),
        "pattern_trigger_trade_ids": _pattern_trigger_trade_ids(
            db,
            relation_name=relation_name,
        ),
    }


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
        f"{len(comparisons)} scheduler scope checks matched"
        if status == "COMPLETE_POSITIVE"
        else "scheduler scope parity drift or relation-kind drift"
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
    print(f"SCHEDULER_SCOPE_CHECKS={result['checks']}")
    print(f"SCHEDULER_SCOPE_MISMATCHES={result['mismatches']}")
    for row in result["comparisons"]:
        print(
            "SCOPE_CHECK "
            f"scope={row['scope']} match={row['match']} "
            f"old_count={row['old_count']} new_count={row['new_count']}"
        )
    return 0 if result["status"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())

