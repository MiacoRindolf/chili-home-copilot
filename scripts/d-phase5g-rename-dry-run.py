"""Phase 5G dry-run for the trading_trades physical rename.

This script proves the compatibility-first rename shape without touching the
live database:

1. Drop the existing Phase 5B compatibility view
   ``trading_management_envelopes``.
2. Rename the base table ``trading_trades`` to
   ``trading_management_envelopes``.
3. Recreate ``trading_trades`` as a simple updatable view over the renamed
   base table.
4. Exercise old SQL, new SQL, and a SQLAlchemy ``Trade`` flush.
5. Roll the whole transaction back.

By default it refuses to run unless the target database name ends in
``_test``. Pass ``--allow-staging`` only for an explicitly isolated staging
database.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, configure_mappers


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_env_url() -> str:
    keys = ("PHASE5G_DRY_RUN_DATABASE_URL", "TEST_DATABASE_URL", "DATABASE_URL")
    for key in keys:
        val = os.environ.get(key, "").strip()
        if val:
            return val

    env_path = ROOT / ".env"
    if env_path.is_file():
        try:
            from dotenv import dotenv_values
        except Exception:
            dotenv_values = None
        if dotenv_values is not None:
            vals = dotenv_values(env_path)
            for key in keys:
                val = (vals.get(key) or "").strip()
                if val:
                    return val
    return ""


def _database_name(url: str) -> str:
    try:
        return (make_url(url).database or "").strip()
    except Exception:
        return ""


def _assert_safe_target(url: str, *, allow_staging: bool) -> None:
    if not url:
        raise SystemExit(
            "No database URL found. Set PHASE5G_DRY_RUN_DATABASE_URL or TEST_DATABASE_URL."
        )
    lowered = url.lower()
    if not (
        lowered.startswith("postgresql://")
        or lowered.startswith("postgresql+psycopg2://")
        or lowered.startswith("postgresql+psycopg://")
    ):
        raise SystemExit("Phase 5G dry-run requires a PostgreSQL URL.")
    db_name = _database_name(url).lower()
    if db_name.endswith("_test"):
        return
    if allow_staging and "staging" in db_name:
        return
    raise SystemExit(
        f"Refusing to run Phase 5G dry-run against database {db_name!r}. "
        "Use a *_test DB, or pass --allow-staging for an isolated staging DB."
    )


def _relkind(conn, relname: str) -> str | None:
    return conn.execute(
        text(
            """
            SELECT c.relkind
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = ANY(current_schemas(false))
               AND c.relname = :relname
             LIMIT 1
            """
        ),
        {"relname": relname},
    ).scalar()


def _count(conn, sql: str, params: dict[str, Any] | None = None) -> int:
    return int(conn.execute(text(sql), params or {}).scalar() or 0)


def _insert_envelope_sql(conn, table_name: str, ticker: str) -> int:
    return int(
        conn.execute(
            text(
                f"""
                INSERT INTO {table_name}
                    (ticker, direction, entry_price, quantity, entry_date,
                     status, broker_source)
                VALUES
                    (:ticker, 'long', 12.34, 1.0, NOW(), 'open', 'manual')
                RETURNING id
                """
            ),
            {"ticker": ticker},
        ).scalar_one()
    )


def _phase5b_hard_issues(conn) -> int | None:
    if _relkind(conn, "trading_phase5b_decision_envelope_position") not in {"v", "m"}:
        return None
    return _count(
        conn,
        """
        SELECT COUNT(*)
          FROM trading_phase5b_decision_envelope_position
         WHERE linkage_status NOT IN (
             'linked',
             'historical_broker_envelope_missing_position'
         )
        """,
    )


def _run(url: str) -> dict[str, Any]:
    # Make app imports bind to the dry-run URL if app.db is imported by models.
    os.environ["DATABASE_URL"] = url
    os.environ.setdefault("CHILI_PYTEST", "1")
    os.environ.setdefault("CHILI_SCHEDULER_ROLE", "none")

    engine = create_engine(url, pool_pre_ping=True)
    payload: dict[str, Any] = {
        "ok": False,
        "database": _database_name(url),
        "started_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "checks": {},
    }

    with engine.connect() as conn:
        before = {
            "trading_trades": _relkind(conn, "trading_trades"),
            "trading_management_envelopes": _relkind(conn, "trading_management_envelopes"),
            "phase5b_view": _relkind(conn, "trading_phase5b_decision_envelope_position"),
            "trading_trades_rows": _count(conn, "SELECT COUNT(*) FROM trading_trades"),
            "phase5b_hard_issues": _phase5b_hard_issues(conn),
        }
        payload["before"] = before
        if before["trading_trades"] != "r":
            raise RuntimeError(
                f"Expected trading_trades to be a table before dry-run, got {before['trading_trades']!r}"
            )
        if before["trading_management_envelopes"] not in {None, "v"}:
            raise RuntimeError(
                "Expected trading_management_envelopes to be absent or a view before dry-run, "
                f"got {before['trading_management_envelopes']!r}"
            )

        conn.rollback()
        tx = conn.begin()
        orm_session: Session | None = None
        try:
            conn.execute(text("DROP VIEW IF EXISTS trading_management_envelopes"))
            conn.execute(
                text("ALTER TABLE trading_trades RENAME TO trading_management_envelopes")
            )
            conn.execute(
                text(
                    """
                    CREATE VIEW trading_trades AS
                    SELECT * FROM trading_management_envelopes
                    """
                )
            )

            after_rename = {
                "trading_trades": _relkind(conn, "trading_trades"),
                "trading_management_envelopes": _relkind(conn, "trading_management_envelopes"),
                "phase5b_view": _relkind(conn, "trading_phase5b_decision_envelope_position"),
                "old_view_count": _count(conn, "SELECT COUNT(*) FROM trading_trades"),
                "new_table_count": _count(conn, "SELECT COUNT(*) FROM trading_management_envelopes"),
                "phase5b_hard_issues": _phase5b_hard_issues(conn),
            }
            payload["after_rename"] = after_rename

            old_sql_id = _insert_envelope_sql(conn, "trading_trades", "PHASE5G-OLD")
            old_visible_new = _count(
                conn,
                "SELECT COUNT(*) FROM trading_management_envelopes WHERE id = :id",
                {"id": old_sql_id},
            )
            new_sql_id = _insert_envelope_sql(
                conn, "trading_management_envelopes", "PHASE5G-NEW"
            )
            new_visible_old = _count(
                conn,
                "SELECT COUNT(*) FROM trading_trades WHERE id = :id",
                {"id": new_sql_id},
            )

            from app.models.trading import Trade

            configure_mappers()
            try:
                orm_session = Session(bind=conn, join_transaction_mode="create_savepoint")
            except TypeError:
                orm_session = Session(bind=conn)
            orm_trade = Trade(
                ticker="PHASE5G-ORM",
                direction="long",
                entry_price=23.45,
                quantity=1.0,
                status="open",
                broker_source="manual",
            )
            orm_session.add(orm_trade)
            orm_session.flush()
            orm_id = int(orm_trade.id)
            orm_visible_new = _count(
                conn,
                "SELECT COUNT(*) FROM trading_management_envelopes WHERE id = :id",
                {"id": orm_id},
            )
            orm_session.rollback()

            payload["checks"] = {
                "old_sql_inserted_through_trading_trades_view": old_visible_new == 1,
                "new_sql_inserted_through_management_envelopes_table": new_visible_old == 1,
                "orm_trade_flush_through_trading_trades_view": orm_visible_new == 1,
                "phase5b_view_survived": after_rename["phase5b_view"] in {"v", "m"},
                "phase5b_hard_issues_unchanged": (
                    before["phase5b_hard_issues"] == after_rename["phase5b_hard_issues"]
                ),
            }
            payload["insert_ids"] = {
                "old_sql_id": old_sql_id,
                "new_sql_id": new_sql_id,
                "orm_id": orm_id,
            }
        finally:
            if tx.is_active:
                tx.rollback()
            else:
                conn.rollback()
            if orm_session is not None:
                orm_session.close()

        rollback_state = {
            "trading_trades": _relkind(conn, "trading_trades"),
            "trading_management_envelopes": _relkind(conn, "trading_management_envelopes"),
            "phase5b_view": _relkind(conn, "trading_phase5b_decision_envelope_position"),
            "trading_trades_rows": _count(conn, "SELECT COUNT(*) FROM trading_trades"),
            "phase5b_hard_issues": _phase5b_hard_issues(conn),
        }
        payload["rollback_state"] = rollback_state

    payload["ok"] = all(payload["checks"].values()) and (
        payload["rollback_state"] == payload["before"]
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="", help="Override the dry-run DB URL.")
    parser.add_argument(
        "--allow-staging",
        action="store_true",
        help="Allow a database whose name contains 'staging' instead of requiring *_test.",
    )
    args = parser.parse_args(argv)

    url = (args.database_url or _load_env_url()).strip()
    _assert_safe_target(url, allow_staging=bool(args.allow_staging))

    try:
        payload = _run(url)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "database": _database_name(url),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
