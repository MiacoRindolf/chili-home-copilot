"""Container entrypoint for the fast-data-worker service (F1).

Streams Coinbase Advanced Trade WebSocket data into the ``fast_*``
tables. Standalone from the main chili process; restarts independently.

Usage (local):
    CHILI_FAST_PATH_ENABLED=1 \\
    CHILI_FAST_PATH_PAIRS=BTC-USD,ETH-USD \\
    DATABASE_URL=postgresql://... \\
    python scripts/fast_data_worker.py

In docker compose, the service ``fast-data-worker`` runs this with
the env vars already configured.

See ``docs/ARCHITECTURE-fast-path.md`` for the contract.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

# Mirror brain_worker.py / scheduler_worker.py: insert repo root onto sys.path
# so `from app.<x> import ...` works regardless of cwd. Python prepends the
# script's directory (``scripts/``) to sys.path, NOT the cwd, so without this
# the ``app`` package — sibling of ``scripts/`` — isn't importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _configure_logging() -> None:
    level = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> int:
    _configure_logging()
    logger = logging.getLogger("fast_data_worker")
    logger.info("[fast_data_worker] starting")

    # Tag DB sessions for pg_stat_activity visibility (mirrors the
    # convention in app/db.py for other workers).
    os.environ.setdefault("CHILI_APP_NAME", "chili-fast-data-worker")

    # Lazy imports — keep startup logging visible even if any import explodes.
    try:
        from app.db import engine
        from app.services.trading.fast_path.settings import load as load_settings
        from app.services.trading.fast_path.supervisor import FastPathSupervisor
        from app.migrations import run_migrations
    except Exception as exc:
        logger.critical(
            "[fast_data_worker] import failed; container will exit: %s",
            exc, exc_info=True,
        )
        return 1

    # Schema check — fast-path tables must exist before we accept any data.
    # Migrations are idempotent; re-running them here is safe.
    if (os.environ.get("CHILI_FAST_PATH_RUN_MIGRATIONS") or "1") not in ("0", "false", "False"):
        try:
            logger.info("[fast_data_worker] running migrations (idempotent)")
            run_migrations(engine)
        except Exception as exc:
            logger.critical(
                "[fast_data_worker] migrations failed: %s", exc, exc_info=True,
            )
            return 1

    settings = load_settings()
    logger.info(
        "[fast_data_worker] loaded settings enabled=%s mode=%s pairs=%s "
        "queue_max=%s batch_size=%s batch_interval_ms=%s",
        settings.enabled, settings.mode, settings.pairs,
        settings.queue_max, settings.batch_size, settings.batch_interval_ms,
    )

    supervisor = FastPathSupervisor(settings, engine)

    try:
        asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        logger.info("[fast_data_worker] interrupted")
    except Exception as exc:
        logger.critical(
            "[fast_data_worker] supervisor crashed: %s", exc, exc_info=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
