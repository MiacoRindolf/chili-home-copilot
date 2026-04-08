"""Logging helpers for neural momentum."""

from __future__ import annotations

import logging

LOG_PREFIX = "[momentum_neural]"

logger = logging.getLogger(__name__)


def log_tick(summary: str, *args: object) -> None:
    logger.info("%s %s", LOG_PREFIX, summary % args if args else summary)
