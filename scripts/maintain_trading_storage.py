"""Maintain high-volume trading storage without blocking app startup.

Dry-run is the default. Pass --execute to delete rows, and pass
--create-indexes during a quiet maintenance window to create timestamp-leading
retention indexes with CREATE INDEX CONCURRENTLY.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CHILI_APP_NAME", "chili-trading-storage-maintenance")

from app.db import SessionLocal, engine  # noqa: E402
from app.services.trading.data_retention import ensure_fast_path_partitions  # noqa: E402

logger = logging.getLogger("trading_storage_maintenance")


@dataclass(frozen=True)
class IndexSpec:
    table: str
    name: str
    columns: tuple[str, ...]
    where: str | None = None


@dataclass(frozen=True)
class TimePurgeSpec:
    key: str
    table: str
    ts_col: str
    retain_days: int


INDEX_SPECS = [
    IndexSpec(
        "fast_orderbook_default",
        "ix_fast_orderbook_default_snapshot_id_retention",
        ("snapshot_at", "id"),
    ),
    IndexSpec(
        "trading_exit_parity_log",
        "ix_exit_parity_created_retention",
        ("created_at", "id"),
    ),
    IndexSpec(
        "trading_exit_parity_log",
        "ix_exit_parity_created_pattern",
        ("created_at", "scan_pattern_id", "id"),
        "scan_pattern_id IS NOT NULL",
    ),
    IndexSpec(
        "trading_exit_parity_log",
        "ix_exit_parity_pattern_created",
        ("scan_pattern_id", "created_at", "id"),
        "scan_pattern_id IS NOT NULL",
    ),
    IndexSpec(
        "trading_ledger_parity_log",
        "ix_ledger_parity_created_pattern",
        ("created_at", "scan_pattern_id", "id"),
        "scan_pattern_id IS NOT NULL",
    ),
    IndexSpec(
        "trading_venue_truth_log",
        "ix_venue_truth_log_created_trade",
        ("created_at", "trade_id", "id"),
        "trade_id IS NOT NULL",
    ),
    IndexSpec(
        "trading_bracket_reconciliation_log",
        "ix_bracket_reconciliation_observed_retention",
        ("observed_at", "id"),
    ),
    IndexSpec(
        "trading_bracket_reconciliation_log",
        "ix_bracket_reconciliation_observed_trade",
        ("observed_at", "trade_id", "id"),
        "trade_id IS NOT NULL",
    ),
    IndexSpec(
        "trading_position_sizer_log",
        "ix_position_sizer_log_observed_pattern",
        ("observed_at", "pattern_id", "id"),
        "pattern_id IS NOT NULL",
    ),
    IndexSpec(
        "trading_pattern_trades",
        "ix_pattern_trades_created_retention",
        ("created_at", "id"),
    ),
    IndexSpec(
        "trading_execution_events",
        "ix_execution_events_recorded_retention",
        ("recorded_at", "id"),
    ),
]

INDEX_TARGET_TABLES = {
    "fast-orderbook": {"fast_orderbook_default"},
    "exit-parity": {"trading_exit_parity_log"},
    "divergence-discovery": {
        "trading_exit_parity_log",
        "trading_ledger_parity_log",
        "trading_venue_truth_log",
        "trading_bracket_reconciliation_log",
        "trading_position_sizer_log",
    },
    "bracket-reconciliation": {"trading_bracket_reconciliation_log"},
    "pattern-trades": {"trading_pattern_trades"},
    "execution-events": {"trading_execution_events"},
}


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def _bounded_int(value: str, *, min_value: int, name: str) -> int:
    parsed = int(value)
    if parsed < min_value:
        raise argparse.ArgumentTypeError(f"{name} must be >= {min_value}")
    return parsed


def _quote_ident(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum() or identifier[0].isdigit():
        raise ValueError(f"unsafe SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _format_bytes(value: int) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.2f} GB"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f} MB"
    return f"{value} B"


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute(text("SELECT to_regclass(:name)"), {"name": table}).scalar())


def _relation_size(conn, table: str) -> int:
    return int(
        conn.execute(
            text("SELECT COALESCE(pg_total_relation_size(to_regclass(:name)), 0)"),
            {"name": table},
        ).scalar()
        or 0
    )


def _index_exists(conn, index_name: str) -> bool:
    return bool(
        conn.execute(text("SELECT to_regclass(:name)"), {"name": index_name}).scalar()
    )


def _has_leading_time_index(
    conn,
    *,
    table: str,
    ts_col: str,
    include_default_partition: bool = False,
    require_id_second: bool = False,
) -> bool:
    default_table = f"{table}_default" if include_default_partition else table
    rows = conn.execute(text("""
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = ANY(current_schemas(false))
          AND tablename IN (:table, :default_table)
    """), {"table": table, "default_table": default_table}).fetchall()
    needle = (
        f"using btree ({ts_col.lower()}, id"
        if require_id_second
        else f"using btree ({ts_col.lower()}"
    )
    return any(needle in str(row[0] or "").lower() for row in rows)


def _create_retention_indexes(*, dry_run: bool, targets: list[str]) -> None:
    allowed_tables: set[str] = set()
    for target in targets:
        allowed_tables.update(INDEX_TARGET_TABLES.get(target, set()))
    if not allowed_tables:
        logger.info("index maintenance skipped: no index-backed targets selected")
        return

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for spec in INDEX_SPECS:
            if spec.table not in allowed_tables:
                continue
            if not _table_exists(conn, spec.table):
                logger.info("index skip table=%s reason=missing", spec.table)
                continue
            table_size = _relation_size(conn, spec.table)
            if _index_exists(conn, spec.name):
                logger.info(
                    "index exists table=%s index=%s size=%s",
                    spec.table,
                    spec.name,
                    _format_bytes(table_size),
                )
                continue
            sql = (
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_quote_ident(spec.name)} "
                f"ON {_quote_ident(spec.table)} "
                f"({', '.join(_quote_ident(col) for col in spec.columns)})"
            )
            if spec.where:
                sql = f"{sql} WHERE {spec.where}"
            if dry_run:
                logger.info(
                    "index planned table=%s index=%s size=%s",
                    spec.table,
                    spec.name,
                    _format_bytes(table_size),
                )
                continue
            logger.info(
                "creating index concurrently table=%s index=%s size=%s",
                spec.table,
                spec.name,
                _format_bytes(table_size),
            )
            conn.execute(text(sql))


def _limited_count_by_time(
    *,
    table: str,
    ts_col: str,
    cutoff: datetime,
    batch_size: int,
    statement_timeout_ms: int,
) -> int:
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))
        if not _table_exists(conn, table):
            return 0
        return int(conn.execute(text(f"""
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM {_quote_ident(table)}
                WHERE {_quote_ident(ts_col)} < :cutoff
                ORDER BY {_quote_ident(ts_col)} ASC, id ASC
                LIMIT :limit
            ) limited
        """), {"cutoff": cutoff, "limit": batch_size}).scalar() or 0)


def _delete_batch_by_time(
    *,
    table: str,
    ts_col: str,
    cutoff: datetime,
    batch_size: int,
    statement_timeout_ms: int,
) -> int:
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))
        conn.execute(text("SET LOCAL lock_timeout = 5000"))
        result = conn.execute(text(f"""
            WITH doomed AS (
                SELECT ctid
                FROM {_quote_ident(table)}
                WHERE {_quote_ident(ts_col)} < :cutoff
                ORDER BY {_quote_ident(ts_col)} ASC, id ASC
                LIMIT :limit
            )
            DELETE FROM {_quote_ident(table)} t
            USING doomed
            WHERE t.ctid = doomed.ctid
        """), {"cutoff": cutoff, "limit": batch_size})
        return int(result.rowcount or 0)


def _limited_exit_parity_count(
    *,
    backtest_cutoff: datetime,
    live_cutoff: datetime,
    batch_size: int,
    statement_timeout_ms: int,
) -> int:
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))
        if not _table_exists(conn, "trading_exit_parity_log"):
            return 0
        return int(conn.execute(text("""
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM trading_exit_parity_log
                WHERE (
                    source = 'backtest'
                    AND created_at < :backtest_cutoff
                )
                OR (
                    COALESCE(source, '') <> 'backtest'
                    AND created_at < :live_cutoff
                )
                ORDER BY created_at ASC, id ASC
                LIMIT :limit
            ) limited
        """), {
            "backtest_cutoff": backtest_cutoff,
            "live_cutoff": live_cutoff,
            "limit": batch_size,
        }).scalar() or 0)


def _delete_exit_parity_batch(
    *,
    backtest_cutoff: datetime,
    live_cutoff: datetime,
    batch_size: int,
    statement_timeout_ms: int,
) -> int:
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))
        conn.execute(text("SET LOCAL lock_timeout = 5000"))
        result = conn.execute(text("""
            WITH doomed AS (
                SELECT ctid
                FROM trading_exit_parity_log
                WHERE (
                    source = 'backtest'
                    AND created_at < :backtest_cutoff
                )
                OR (
                    COALESCE(source, '') <> 'backtest'
                    AND created_at < :live_cutoff
                )
                ORDER BY created_at ASC, id ASC
                LIMIT :limit
            )
            DELETE FROM trading_exit_parity_log t
            USING doomed
            WHERE t.ctid = doomed.ctid
        """), {
            "backtest_cutoff": backtest_cutoff,
            "live_cutoff": live_cutoff,
            "limit": batch_size,
        })
        return int(result.rowcount or 0)


def _run_batched_purge(
    *,
    label: str,
    first_batch_count: int,
    delete_batch,
    execute: bool,
    max_batches: int,
    max_runtime_minutes: int,
    sleep_seconds: float,
    vacuum_table: str | None,
    vacuum_analyze: bool,
) -> int:
    logger.info("%s old-row limited count: %s", label, first_batch_count)
    if not execute:
        logger.info("%s dry-run complete; pass --execute to purge", label)
        return 0

    deadline = time.monotonic() + (max_runtime_minutes * 60)
    deleted_total = 0
    batches = 0
    while batches < max_batches and time.monotonic() < deadline:
        deleted = int(delete_batch())
        batches += 1
        deleted_total += deleted
        logger.info(
            "%s batch=%s deleted=%s total_deleted=%s",
            label,
            batches,
            deleted,
            deleted_total,
        )
        if deleted <= 0:
            break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    if vacuum_analyze and vacuum_table and deleted_total > 0:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            logger.info("running VACUUM ANALYZE %s", vacuum_table)
            conn.execute(text(f"VACUUM (ANALYZE) {_quote_ident(vacuum_table)}"))

    return deleted_total


def _run_fast_orderbook(args) -> int:
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=args.retain_fast_orderbook_days)
    with engine.connect() as conn:
        if not _has_leading_time_index(
            conn,
            table="fast_orderbook_default",
            ts_col="snapshot_at",
            require_id_second=True,
        ):
            logger.warning(
                "fast_orderbook_default skipped: missing snapshot_at-leading "
                "btree index with id as the second column; run with "
                "--create-indexes --execute first"
            )
            return 0
    if args.ensure_partitions:
        db = SessionLocal()
        try:
            partitions = ensure_fast_path_partitions(
                db,
                days_ahead=args.partition_days,
                include_today=False,
                force_large_default=args.force_partitions,
                dry_run=not args.execute,
            )
            if args.execute:
                db.commit()
            logger.info("fast partition maintenance: %s", partitions)
        except Exception:
            db.rollback()
            logger.exception("fast partition maintenance failed")
            return -1
        finally:
            db.close()

    try:
        first = _limited_count_by_time(
            table="fast_orderbook_default",
            ts_col="snapshot_at",
            cutoff=cutoff,
            batch_size=args.batch_size,
            statement_timeout_ms=args.statement_timeout_ms,
        )
    except Exception as exc:
        logger.warning("fast_orderbook_default limited count failed: %s", exc)
        return 0
    return _run_batched_purge(
        label="fast_orderbook_default",
        first_batch_count=first,
        delete_batch=lambda: _delete_batch_by_time(
            table="fast_orderbook_default",
            ts_col="snapshot_at",
            cutoff=cutoff,
            batch_size=args.batch_size,
            statement_timeout_ms=args.statement_timeout_ms,
        ),
        execute=args.execute,
        max_batches=args.max_batches,
        max_runtime_minutes=args.max_runtime_minutes,
        sleep_seconds=args.sleep_seconds,
        vacuum_table="fast_orderbook_default",
        vacuum_analyze=args.vacuum_analyze,
    )


def _run_exit_parity(args) -> int:
    with engine.connect() as conn:
        if not _has_leading_time_index(
            conn, table="trading_exit_parity_log", ts_col="created_at",
            require_id_second=True,
        ):
            logger.warning(
                "trading_exit_parity_log skipped: missing created_at-leading "
                "btree index; run with --create-indexes --execute first"
            )
            return 0
    backtest_cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=args.retain_exit_parity_backtest_days,
    )
    live_cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=args.retain_exit_parity_live_days,
    )
    try:
        first = _limited_exit_parity_count(
            backtest_cutoff=backtest_cutoff,
            live_cutoff=live_cutoff,
            batch_size=args.batch_size,
            statement_timeout_ms=args.statement_timeout_ms,
        )
    except Exception as exc:
        logger.warning("trading_exit_parity_log limited count failed: %s", exc)
        return 0
    return _run_batched_purge(
        label="trading_exit_parity_log",
        first_batch_count=first,
        delete_batch=lambda: _delete_exit_parity_batch(
            backtest_cutoff=backtest_cutoff,
            live_cutoff=live_cutoff,
            batch_size=args.batch_size,
            statement_timeout_ms=args.statement_timeout_ms,
        ),
        execute=args.execute,
        max_batches=args.max_batches,
        max_runtime_minutes=args.max_runtime_minutes,
        sleep_seconds=args.sleep_seconds,
        vacuum_table="trading_exit_parity_log",
        vacuum_analyze=args.vacuum_analyze,
    )


def _run_time_spec(args, spec: TimePurgeSpec) -> int:
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=spec.retain_days)
    with engine.connect() as conn:
        if not _has_leading_time_index(
            conn, table=spec.table, ts_col=spec.ts_col, require_id_second=True,
        ):
            logger.warning(
                "%s skipped: missing %s-leading btree index",
                spec.table,
                spec.ts_col,
            )
            return 0
    try:
        first = _limited_count_by_time(
            table=spec.table,
            ts_col=spec.ts_col,
            cutoff=cutoff,
            batch_size=args.batch_size,
            statement_timeout_ms=args.statement_timeout_ms,
        )
    except Exception as exc:
        logger.warning("%s limited count failed: %s", spec.table, exc)
        return 0
    return _run_batched_purge(
        label=spec.table,
        first_batch_count=first,
        delete_batch=lambda: _delete_batch_by_time(
            table=spec.table,
            ts_col=spec.ts_col,
            cutoff=cutoff,
            batch_size=args.batch_size,
            statement_timeout_ms=args.statement_timeout_ms,
        ),
        execute=args.execute,
        max_batches=args.max_batches,
        max_runtime_minutes=args.max_runtime_minutes,
        sleep_seconds=args.sleep_seconds,
        vacuum_table=spec.table,
        vacuum_analyze=args.vacuum_analyze,
    )


def _resolve_targets(targets: list[str]) -> list[str]:
    if "all" in targets:
        return [
            "fast-orderbook",
            "exit-parity",
            "divergence-discovery",
            "bracket-reconciliation",
            "execution-events",
        ]
    return targets


def main() -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="mutate the database")
    parser.add_argument(
        "--target",
        action="append",
        choices=[
            "all",
            "fast-orderbook",
            "exit-parity",
            "divergence-discovery",
            "bracket-reconciliation",
            "execution-events",
            "pattern-trades",
        ],
        default=None,
        help="maintenance target; repeat to run multiple targets",
    )
    parser.add_argument("--create-indexes", action="store_true", help="create retention indexes concurrently")
    parser.add_argument("--ensure-partitions", action="store_true", help="create future fast-path partitions")
    parser.add_argument("--force-partitions", action="store_true", help="allow partition creation with large DEFAULT partitions")
    parser.add_argument("--partition-days", type=lambda v: _bounded_int(v, min_value=1, name="partition-days"), default=7)
    parser.add_argument("--retain-fast-orderbook-days", type=lambda v: _bounded_int(v, min_value=1, name="retain-fast-orderbook-days"), default=3)
    parser.add_argument("--retain-exit-parity-backtest-days", type=lambda v: _bounded_int(v, min_value=1, name="retain-exit-parity-backtest-days"), default=7)
    parser.add_argument("--retain-exit-parity-live-days", type=lambda v: _bounded_int(v, min_value=1, name="retain-exit-parity-live-days"), default=30)
    parser.add_argument("--retain-bracket-reconciliation-days", type=lambda v: _bounded_int(v, min_value=1, name="retain-bracket-reconciliation-days"), default=30)
    parser.add_argument("--retain-execution-events-days", type=lambda v: _bounded_int(v, min_value=1, name="retain-execution-events-days"), default=180)
    parser.add_argument("--retain-pattern-trades-days", type=lambda v: _bounded_int(v, min_value=1, name="retain-pattern-trades-days"), default=365)
    parser.add_argument("--batch-size", type=lambda v: _bounded_int(v, min_value=1, name="batch-size"), default=50_000)
    parser.add_argument("--max-batches", type=lambda v: _bounded_int(v, min_value=1, name="max-batches"), default=10)
    parser.add_argument("--max-runtime-minutes", type=lambda v: _bounded_int(v, min_value=1, name="max-runtime-minutes"), default=10)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--statement-timeout-ms", type=lambda v: _bounded_int(v, min_value=1000, name="statement-timeout-ms"), default=60_000)
    parser.add_argument("--vacuum-analyze", action="store_true", help="run VACUUM ANALYZE after deletes")
    args = parser.parse_args()

    targets = _resolve_targets(args.target or ["all"])
    if args.create_indexes:
        _create_retention_indexes(dry_run=not args.execute, targets=targets)

    logger.info(
        "starting storage maintenance execute=%s targets=%s batch=%s",
        args.execute,
        targets,
        args.batch_size,
    )

    deleted_total = 0
    for target in targets:
        if target == "fast-orderbook":
            deleted = _run_fast_orderbook(args)
        elif target == "exit-parity":
            deleted = _run_exit_parity(args)
        elif target == "bracket-reconciliation":
            deleted = _run_time_spec(args, TimePurgeSpec(
                target,
                "trading_bracket_reconciliation_log",
                "observed_at",
                args.retain_bracket_reconciliation_days,
            ))
        elif target == "execution-events":
            deleted = _run_time_spec(args, TimePurgeSpec(
                target,
                "trading_execution_events",
                "recorded_at",
                args.retain_execution_events_days,
            ))
        elif target == "pattern-trades":
            deleted = _run_time_spec(args, TimePurgeSpec(
                target,
                "trading_pattern_trades",
                "created_at",
                args.retain_pattern_trades_days,
            ))
        else:
            deleted = 0
        if deleted < 0:
            return 2
        deleted_total += deleted

    logger.info("complete deleted_total=%s", deleted_total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
