"""
Dedicated APScheduler process. Docker Compose sets CHILI_SCHEDULER_ROLE=all on this service
so broker sync, scans, code/reasoning cycles, etc. stay out of Uvicorn; the web app uses
CHILI_SCHEDULER_ROLE=none.

Local default below is ``worker`` (heavy scans + heartbeat only) if you run the script
without env vars.

Usage:
  python scripts/scheduler_worker.py

Docker: see docker-compose ``scheduler-worker`` service.
"""
import os
import sys
import time

# Must run before importing app settings (Compose overrides to ``all``).
os.environ.setdefault("CHILI_SCHEDULER_ROLE", "worker")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def main() -> None:
    from app.services.trading_scheduler import start_scheduler, stop_scheduler

    try:
        from app.services.trading.brain_io_concurrency import log_brain_io_profile

        log_brain_io_profile(logger)
    except Exception as _e:
        logger.debug("[scheduler_worker] brain I/O profile log skipped: %s", _e)

    start_scheduler()
    logger.info("[scheduler_worker] Started (CHILI_SCHEDULER_ROLE=%s)", os.environ.get("CHILI_SCHEDULER_ROLE"))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("[scheduler_worker] Shutting down")
    finally:
        stop_scheduler()


if __name__ == "__main__":
    main()
