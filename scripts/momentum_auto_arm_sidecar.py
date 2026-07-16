"""Dedicated live momentum auto-arm loop.

This is intentionally narrower than ``scheduler_worker.py``: it only runs the
Ross-lane auto-arm pass and leaves live session ticking to the existing
``momentum_exec_only`` worker. It is useful for hot enabling the arm lane when a
long-lived scheduler process was started with stale disabled env flags.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from app.db import SessionLocal
from app.services.trading.momentum_neural.auto_arm import run_auto_arm_pass
from app.services.trading.momentum_neural.lane_health import record_auto_arm_run


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("momentum_auto_arm_sidecar")


def _interval_seconds() -> float:
    raw = os.environ.get("CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_INTERVAL_SECONDS", "10")
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 10.0


def _signature(summary: dict[str, Any]) -> tuple[Any, ...]:
    return (
        summary.get("armed"),
        summary.get("symbol"),
        summary.get("skipped"),
        summary.get("scanned"),
        summary.get("checked"),
        summary.get("busy_skipped"),
        summary.get("ross_evidence_skipped"),
        summary.get("ross_evidence_skip_reasons"),
        summary.get("trigger"),
        summary.get("begin_error"),
        summary.get("confirm_error"),
    )


def main() -> None:
    interval = _interval_seconds()
    logger.warning("Ross momentum auto-arm sidecar started interval=%.1fs", interval)
    last_sig: tuple[Any, ...] | None = None
    while True:
        db = SessionLocal()
        try:
            record_auto_arm_run()
            summary = run_auto_arm_pass(db)
            db.commit()
            sig = _signature(summary)
            if summary.get("armed") or summary.get("begin_error") or summary.get("confirm_error") or sig != last_sig:
                logger.warning("auto_arm summary=%s", summary)
            last_sig = sig
        except Exception:
            db.rollback()
            logger.exception("auto_arm pass failed")
        finally:
            db.close()
        time.sleep(interval)


if __name__ == "__main__":
    main()
