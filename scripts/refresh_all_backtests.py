"""
Bounded refresh of smart backtests for active trading insights.

Run from project root:

  python scripts/refresh_all_backtests.py
  python scripts/refresh_all_backtests.py --force
  python scripts/refresh_all_backtests.py --limit 5 --target-tickers 12
  python scripts/refresh_all_backtests.py --all --target-tickers 20

The scheduled task intentionally uses a small limit/ticker count. This runs on
the operator PC, so it must not monopolize sockets, CPU, DB connections, or log
I/O. Use ``--all`` only for a supervised maintenance window.
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import json
import logging
import os
import sys
import time
from datetime import datetime as dt, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


os.environ.setdefault("CHILI_APP_NAME", "chili-backtest-refresh")
os.environ.setdefault("TQDM_DISABLE", "1")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

LOG_PATH = REPO_ROOT / "backtest_refresh.log"
LOCK_PATH = REPO_ROOT / "data" / "backtest_refresh.lock"
DEFAULT_LIMIT = int(os.environ.get("CHILI_BACKTEST_REFRESH_LIMIT", "2"))
DEFAULT_TARGET_TICKERS = int(os.environ.get("CHILI_BACKTEST_REFRESH_TARGET_TICKERS", "12"))
DEFAULT_WORKERS = int(os.environ.get("CHILI_BACKTEST_REFRESH_WORKERS", "4"))
DEFAULT_MAX_RUNTIME_MINUTES = int(
    os.environ.get("CHILI_BACKTEST_REFRESH_MAX_RUNTIME_MINUTES", "90")
)


def _configure_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                LOG_PATH,
                maxBytes=int(os.environ.get("CHILI_BACKTEST_REFRESH_LOG_MAX_BYTES", "20000000")),
                backupCount=int(os.environ.get("CHILI_BACKTEST_REFRESH_LOG_BACKUPS", "3")),
                encoding="utf-8",
            ),
        ],
    )
    # Backtest internals emit one line per bar/exit parity row. For scheduled
    # refreshes that is noise and was the source of multi-GB logs.
    logging.getLogger("app.services.backtest_service").setLevel(logging.WARNING)
    logging.getLogger("backtesting").setLevel(logging.WARNING)
    return logging.getLogger(__name__)


logger = _configure_logging()

from app.db import SessionLocal, engine  # noqa: E402
from app.models.trading import BacktestResult, TradingInsight  # noqa: E402
from app.services.trading.backtest_engine import smart_backtest_insight  # noqa: E402


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class SingleInstance:
    def __init__(self, path: Path, *, stale_hours: float) -> None:
        self.path = path
        self.stale_hours = stale_hours
        self._owns_lock = False

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            age_hours = (time.time() - self.path.stat().st_mtime) / 3600.0
            lock_info: dict[str, Any] = {}
            try:
                lock_info = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                lock_info = {}
            pid = int(lock_info.get("pid") or 0)
            if pid and _pid_running(pid):
                raise SystemExit(
                    f"backtest refresh already running as pid={pid}; "
                    f"lock_age_h={age_hours:.2f}"
                )
            if age_hours > self.stale_hours or not pid:
                self.path.unlink(missing_ok=True)
            else:
                raise SystemExit(
                    f"recent backtest refresh lock exists without live pid; "
                    f"lock_age_h={age_hours:.2f}"
                )

        fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "pid": os.getpid(),
                    "started_at": dt.now(timezone.utc).isoformat(),
                    "script": "scripts/refresh_all_backtests.py",
                },
                fh,
            )
        self._owns_lock = True
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._owns_lock:
            self.path.unlink(missing_ok=True)


def _dispose_db() -> None:
    try:
        engine.dispose()
    except Exception:
        pass


def _recent_insight_ids(db, *, force: bool) -> set[int]:
    if force:
        return set()
    cutoff = dt.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    return {
        int(r[0]) for r in db.query(BacktestResult.related_insight_id).filter(
            BacktestResult.ran_at >= cutoff,
            BacktestResult.related_insight_id.isnot(None),
        ).distinct().all()
        if r[0] is not None
    }


def _load_insight_ids(*, force: bool, limit: int | None) -> tuple[list[int], int]:
    db = SessionLocal()
    try:
        recent_ids = _recent_insight_ids(db, force=force)
        query = db.query(TradingInsight.id).filter(TradingInsight.active.is_(True))
        if recent_ids:
            query = query.filter(~TradingInsight.id.in_(recent_ids))
        query = query.order_by(TradingInsight.id.asc())
        if limit is not None:
            query = query.limit(max(0, int(limit)))
        ids = [int(r[0]) for r in query.all()]
        return ids, len(recent_ids)
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()
        _dispose_db()


def main(
    *,
    force: bool = False,
    limit: int | None = None,
    run_all: bool = False,
    target_tickers: int = DEFAULT_TARGET_TICKERS,
    workers: int = DEFAULT_WORKERS,
    max_runtime_minutes: int = DEFAULT_MAX_RUNTIME_MINUTES,
    sleep_seconds: float = 5.0,
) -> int:
    os.environ["CHILI_BACKTEST_REFRESH_WORKERS"] = str(max(1, int(workers)))
    effective_limit = None if run_all else max(0, int(limit if limit is not None else DEFAULT_LIMIT))
    target_tickers = max(1, int(target_tickers))
    deadline = time.monotonic() + max(1, int(max_runtime_minutes)) * 60

    try:
        from app.config import settings
        settings.brain_smart_bt_max_workers = max(1, int(workers))
        settings.brain_exit_engine_ops_log_enabled = False
    except Exception:
        pass

    insight_ids, recent_count = _load_insight_ids(force=force, limit=effective_limit)
    logger.info(
        "Found %s active insights to process (force=%s, limit=%s, target_tickers=%s, "
        "workers=%s, skipped_recent=%s)",
        len(insight_ids), force, effective_limit, target_tickers, workers, recent_count,
    )

    total_wins = 0
    total_losses = 0
    total_backtests = 0
    processed = 0

    for i, insight_id in enumerate(insight_ids, 1):
        if time.monotonic() >= deadline:
            logger.warning("Stopping on max-runtime budget before insight_id=%s", insight_id)
            break

        db = SessionLocal()
        try:
            insight = db.get(TradingInsight, insight_id)
            if insight is None or not insight.active:
                continue
            logger.info(
                "[%s/%s] Processing insight %s: %s...",
                i, len(insight_ids), insight.id, (insight.pattern_description or "")[:80],
            )
            result = smart_backtest_insight(
                db,
                insight,
                target_tickers=target_tickers,
                update_confidence=True,
            )
            wins = int(result.get("wins", 0))
            losses = int(result.get("losses", 0))
            bt_count = int(result.get("backtests_run", 0))
            total_wins += wins
            total_losses += losses
            total_backtests += bt_count
            processed += 1
            logger.info("  -> Completed: %s backtests, %s wins, %s losses", bt_count, wins, losses)
        except KeyboardInterrupt:
            logger.warning("Interrupted by operator; stopping after %s processed insights", processed)
            try:
                db.rollback()
            except Exception:
                pass
            break
        except Exception as exc:
            logger.exception("  -> Error on insight_id=%s: %s", insight_id, exc)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
            _dispose_db()
            gc.collect()

        if sleep_seconds > 0 and i < len(insight_ids):
            time.sleep(float(sleep_seconds))

    logger.info("=" * 60)
    logger.info("COMPLETED: processed=%s total_backtests=%s", processed, total_backtests)
    logger.info("  Wins: %s, Losses: %s", total_wins, total_losses)
    logger.info(
        "  Win rate: %.1f%%",
        total_wins / max(1, total_wins + total_losses) * 100,
    )
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Refresh smart backtests for active insights")
    ap.add_argument("--force", action="store_true", help="Do not skip recently refreshed insights")
    ap.add_argument("--all", action="store_true", help="Process all eligible insights; supervised only")
    ap.add_argument("--limit", type=int, default=None, metavar="N", help="Maximum insights to process")
    ap.add_argument("--target-tickers", type=int, default=DEFAULT_TARGET_TICKERS)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--max-runtime-minutes", type=int, default=DEFAULT_MAX_RUNTIME_MINUTES)
    ap.add_argument("--sleep-seconds", type=float, default=5.0)
    ap.add_argument("--stale-lock-hours", type=float, default=12.0)
    args = ap.parse_args()

    start = dt.now()
    logger.info("Starting backtest refresh at %s", start)
    with SingleInstance(LOCK_PATH, stale_hours=args.stale_lock_hours):
        code = main(
            force=args.force,
            limit=args.limit,
            run_all=args.all,
            target_tickers=args.target_tickers,
            workers=args.workers,
            max_runtime_minutes=args.max_runtime_minutes,
            sleep_seconds=args.sleep_seconds,
        )
    end = dt.now()
    logger.info("Finished at %s (duration: %s)", end, end - start)
    raise SystemExit(code)
