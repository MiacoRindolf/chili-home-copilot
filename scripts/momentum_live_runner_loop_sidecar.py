"""Dedicated event-driven live momentum runner loop sidecar."""

from __future__ import annotations

import logging
import threading

from app.services.trading.momentum_neural.live_runner_loop import start_live_runner_loop


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("momentum_live_runner_loop_sidecar")


def main() -> None:
    start_live_runner_loop()
    logger.warning("momentum live runner loop sidecar started")
    threading.Event().wait()


if __name__ == "__main__":
    main()
