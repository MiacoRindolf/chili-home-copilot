"""Daemon: bridge Ross stream transcript ticker mentions into momentum viability."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("CHILI_APP_NAME", "chili-ross-transcript-bridge")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.services.trading.momentum_neural.ross_transcript_bridge import (  # noqa: E402
    DEFAULT_TRANSCRIPT_PATH,
    run_ross_transcript_bridge_once,
)


def _interval_seconds(cli_value: float | None) -> float:
    if cli_value is not None:
        return max(0.5, float(cli_value))
    raw = getattr(settings, "chili_momentum_ross_transcript_bridge_interval_seconds", 2.0)
    try:
        return max(0.5, float(raw or 2.0))
    except (TypeError, ValueError):
        return 2.0


def _signature(summary: dict[str, Any]) -> tuple[Any, ...]:
    return (
        summary.get("skipped"),
        tuple(summary.get("symbols") or []),
        summary.get("scored"),
        summary.get("field_symbols"),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--path", default=None)
    parser.add_argument("--lookback-seconds", type=float, default=None)
    parser.add_argument("--interval-seconds", type=float, default=None)
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger("ross_transcript_viability_bridge")
    path = args.path or getattr(
        settings,
        "chili_momentum_ross_transcript_path",
        DEFAULT_TRANSCRIPT_PATH,
    )
    interval = _interval_seconds(args.interval_seconds)
    log.warning(
        "Ross transcript viability bridge started path=%s interval=%.1fs once=%s",
        path,
        interval,
        args.once,
    )

    processed_keys: set[str] = set()
    last_sig: tuple[Any, ...] | None = None
    while True:
        db = SessionLocal()
        try:
            summary = run_ross_transcript_bridge_once(
                db,
                transcript_path=path,
                lookback_seconds=args.lookback_seconds,
                max_symbols=args.max_symbols,
                processed_keys=processed_keys,
            )
            db.commit()
            sig = _signature(summary)
            if summary.get("scored") or sig != last_sig:
                log.warning("bridge summary=%s", summary)
            last_sig = sig
        except Exception:
            db.rollback()
            log.exception("bridge pass failed")
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
