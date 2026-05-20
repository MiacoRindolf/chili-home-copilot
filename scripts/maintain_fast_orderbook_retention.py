"""Maintain fast_orderbook retention without blocking app startup.

This is the operator path for the large historical default partition:

* optionally create near-future daily fast-path partitions during a quiet
  maintenance window;
* optionally purge old rows from fast_orderbook_default in small committed
  batches using the existing timestamp/pkey indexes.

Dry-run is the default. Pass --execute to mutate the database.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CHILI_APP_NAME", "chili-fast-orderbook-maintenance")

from app.db import SessionLocal, engine  # noqa: E402
from app.services.trading.data_retention import ensure_fast_path_partitions  # noqa: E402

logger = logging.getLogger("fast_orderbook_maintenance")


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


def _limited_old_count(cutoff: datetime, batch_size: int) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("""
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM fast_orderbook_default
                WHERE snapshot_at < :cutoff
                ORDER BY id ASC
                LIMIT :limit
            ) limited
        """), {"cutoff": cutoff, "limit": batch_size}).scalar() or 0)


def _delete_old_batch(cutoff: datetime, batch_size: int, statement_timeout_ms: int) -> int:
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))
        conn.execute(text("SET LOCAL lock_timeout = 5000"))
        result = conn.execute(text("""
            WITH doomed AS (
                SELECT ctid
                FROM fast_orderbook_default
                WHERE snapshot_at < :cutoff
                ORDER BY id ASC
                LIMIT :limit
            )
            DELETE FROM fast_orderbook_default t
            USING doomed
            WHERE t.ctid = doomed.ctid
        """), {"cutoff": cutoff, "limit": batch_size})
        return int(result.rowcount or 0)


def _vacuum_analyze() -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM (ANALYZE) fast_orderbook_default"))


def main() -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="mutate the database")
    parser.add_argument("--ensure-partitions", action="store_true", help="create future partitions; use only in a quiet maintenance window")
    parser.add_argument("--force-partitions", action="store_true", help="allow partition creation even when DEFAULT partitions are large")
    parser.add_argument("--partition-days", type=lambda v: _bounded_int(v, min_value=1, name="partition-days"), default=7)
    parser.add_argument("--skip-purge", action="store_true", help="do not purge old default rows")
    parser.add_argument("--retain-days", type=lambda v: _bounded_int(v, min_value=1, name="retain-days"), default=3)
    parser.add_argument("--batch-size", type=lambda v: _bounded_int(v, min_value=1, name="batch-size"), default=50_000)
    parser.add_argument("--max-batches", type=lambda v: _bounded_int(v, min_value=1, name="max-batches"), default=10)
    parser.add_argument("--max-runtime-minutes", type=lambda v: _bounded_int(v, min_value=1, name="max-runtime-minutes"), default=10)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--statement-timeout-ms", type=lambda v: _bounded_int(v, min_value=1000, name="statement-timeout-ms"), default=60_000)
    parser.add_argument("--vacuum-analyze", action="store_true", help="run VACUUM ANALYZE on fast_orderbook_default after deletes")
    args = parser.parse_args()

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=args.retain_days)
    logger.info(
        "starting fast_orderbook maintenance execute=%s cutoff=%s batch=%s",
        args.execute,
        cutoff.isoformat(timespec="seconds"),
        args.batch_size,
    )

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
            logger.info("partition maintenance: %s", partitions)
        except Exception:
            db.rollback()
            logger.exception("partition maintenance failed")
            return 2
        finally:
            db.close()

    if args.skip_purge:
        return 0

    first_batch = _limited_old_count(cutoff, args.batch_size)
    logger.info("old-row limited count: %s rows in first batch window", first_batch)
    if not args.execute:
        logger.info("dry-run complete; pass --execute to purge batches")
        return 0

    deadline = time.monotonic() + (args.max_runtime_minutes * 60)
    deleted_total = 0
    batches = 0
    while batches < args.max_batches and time.monotonic() < deadline:
        deleted = _delete_old_batch(cutoff, args.batch_size, args.statement_timeout_ms)
        batches += 1
        deleted_total += deleted
        logger.info("batch=%s deleted=%s total_deleted=%s", batches, deleted, deleted_total)
        if deleted <= 0:
            break
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    if args.vacuum_analyze and deleted_total > 0:
        logger.info("running VACUUM ANALYZE fast_orderbook_default")
        _vacuum_analyze()

    logger.info("complete batches=%s deleted=%s", batches, deleted_total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
