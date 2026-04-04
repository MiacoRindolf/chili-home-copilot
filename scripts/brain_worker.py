"""
CHILI Brain Worker - Continuous Learning Loop

Runs as a separate process, continuously executing the FULL learning cycle:
- Pre-filter market (Massive.com)
- Deep score candidates
- (Snapshots via scheduler job ``brain_market_snapshots`` unless BRAIN_SNAPSHOTS_ON_LEARNING_CYCLE=1)
- Backfill future returns
- Decay stale insights
- Mine patterns
- Backtest patterns
- Validate hypotheses
- Learn from breakout outcomes
- Specialized mining (intraday, fakeout, synergy) — optional via settings
- Pattern evolution
- Journal, signals, ML training

Status is written to data/brain_worker_status.json for monitoring.
Control via signal files in data/ directory (stop, pause, wake to skip idle sleep).

Default --interval is 5 minutes when the backtest queue is empty (1 minute breather when queue has work).

Usage:
    python scripts/brain_worker.py [--interval MINUTES] [--once]
"""
import sys
import os
import json
import time
import signal
import argparse
import logging
import atexit
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal

# Global status reference for cleanup
_global_status: "BrainWorkerStatus | None" = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("brain_worker.log"),
    ],
)
logger = logging.getLogger(__name__)

# Repo-root data/ — must match app.db.DATA_DIR (not process cwd)
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
STATUS_FILE = DATA_DIR / "brain_worker_status.json"
STOP_SIGNAL = DATA_DIR / "brain_worker_stop"
PAUSE_SIGNAL = DATA_DIR / "brain_worker_pause"
WAKE_SIGNAL = DATA_DIR / "brain_worker_wake"
LOCK_FILE = DATA_DIR / "brain_worker.lock"

DEFAULT_CYCLE_INTERVAL = 5  # minutes between cycles when queue empty (override with --interval)

# Global lock file handle (kept open while running)
_lock_handle = None


def acquire_lock() -> bool:
    """Acquire an exclusive lock to prevent multiple brain workers.
    
    Returns True if lock acquired, False if another worker is running.
    """
    global _lock_handle
    DATA_DIR.mkdir(exist_ok=True)
    
    try:
        _lock_handle = open(LOCK_FILE, "w")
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        
        # Write PID to lock file
        _lock_handle.write(str(os.getpid()))
        _lock_handle.flush()
        return True
    except (IOError, OSError) as e:
        logger.error(f"[brain] Failed to acquire lock — another worker may be running: {e}")
        if _lock_handle:
            _lock_handle.close()
            _lock_handle = None
        return False


def release_lock():
    """Release the exclusive lock."""
    global _lock_handle
    if _lock_handle:
        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            _lock_handle.close()
        except Exception:
            pass
        _lock_handle = None
    
    # Remove lock file
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


class BrainWorkerStatus:
    """Manages worker status persistence."""
    
    def __init__(self):
        self.pid = os.getpid()
        self.status = "starting"
        self.started_at = datetime.utcnow().isoformat()
        self.current_step = ""
        self.current_progress = ""
        self.wake_skip_idle = threading.Event()
        self.last_cycle = {}
        self.totals = {
            "cycles_completed": 0,
            "tickers_scanned": 0,
            "snapshots_taken": 0,
            "patterns_mined": 0,
            "patterns_tested": 0,
            "queue_patterns_processed": 0,
            "hypotheses_validated": 0,
            "hypotheses_confirmed": 0,
            "patterns_spawned": 0,
            "patterns_evolved": 0,
            "patterns_variant_promoted": 0,
            "patterns_pruned": 0,
            "insights_decayed": 0,
        }
        self._load_existing()
    
    def _load_existing(self):
        """Load existing totals if status file exists."""
        if STATUS_FILE.exists():
            try:
                with open(STATUS_FILE, "r") as f:
                    data = json.load(f)
                    if "totals" in data:
                        for k, v in data["totals"].items():
                            if k in self.totals:
                                self.totals[k] = v
            except Exception:
                pass
    
    def save(self):
        """Write status to JSON file (atomic so readers never see partial content)."""
        DATA_DIR.mkdir(exist_ok=True)
        data = {
            "pid": self.pid,
            "status": self.status,
            "started_at": self.started_at,
            "current_step": self.current_step,
            "current_progress": self.current_progress,
            "last_cycle": self.last_cycle,
            "totals": self.totals,
            "updated_at": datetime.utcnow().isoformat(),
        }
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(STATUS_FILE)
    
    def set_step(self, step: str, progress: str = ""):
        self.current_step = step
        self.current_progress = progress
        self.save()
    
    def clear(self):
        """Clear status file on shutdown."""
        self.status = "stopped"
        self.current_step = ""
        self.current_progress = ""
        self.save()


def check_stop_signal() -> bool:
    """Check if stop signal file exists."""
    if STOP_SIGNAL.exists():
        STOP_SIGNAL.unlink()
        return True
    return False


def check_pause_signal() -> bool:
    """Check if pause signal file exists."""
    return PAUSE_SIGNAL.exists()


def check_wake_signal() -> bool:
    """If wake file exists (from UI 'Run next cycle'), remove it and skip idle sleep."""
    if WAKE_SIGNAL.exists():
        try:
            WAKE_SIGNAL.unlink()
        except OSError:
            pass
        return True
    return False


def check_any_wake() -> bool:
    """File wake and/or PostgreSQL wake (reliable when API and worker disagree on data/)."""
    got = False
    if check_wake_signal():
        got = True
    db = SessionLocal()
    try:
        from app.services.brain_worker_signals import consume_db_wake

        if consume_db_wake(db):
            got = True
    except Exception as e:
        logger.warning("[brain] DB wake consume failed: %s", e)
    finally:
        db.close()
    return got


def _check_db_stop_idle() -> bool:
    """True if PostgreSQL control row requested stop (used between cycles / during idle sleep)."""
    from app.services.brain_worker_signals import clear_stop_requested, is_stop_requested
    from app.services.trading.learning import signal_shutdown

    db = SessionLocal()
    try:
        if is_stop_requested(db):
            logger.info("[brain] DB stop requested (idle check), shutting down")
            signal_shutdown()
            clear_stop_requested(db)
            db.commit()
            return True
        return False
    except Exception as e:
        logger.warning("[brain] DB stop check (idle) failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return False
    finally:
        db.close()


def _consume_wake_queued_during_cycle(status: BrainWorkerStatus) -> bool:
    """If DB wake was peeked during a long cycle, consume it now and skip idle."""
    if not status.wake_skip_idle.is_set():
        return False
    db = SessionLocal()
    try:
        from app.services.brain_worker_signals import consume_db_wake

        consume_db_wake(db)
    except Exception as e:
        logger.warning("[brain] DB wake consume after cycle failed: %s", e)
    finally:
        db.close()
    status.wake_skip_idle.clear()
    logger.info("[brain] Wake was queued during cycle — skipping idle sleep")
    return True


def _brain_db_poll_loop(
    stop_polling: threading.Event,
    status: BrainWorkerStatus,
    wake_skip_idle: threading.Event,
    track_learning_progress: bool,
) -> None:
    """PostgreSQL stop/wake/heartbeat + optional learning progress; file stop; ~4s cadence."""
    from app.services.brain_worker_signals import (
        clear_stop_requested,
        is_stop_requested,
        peek_wake_requested,
        update_worker_heartbeat,
    )
    from app.services.trading.learning import get_learning_status, signal_shutdown

    while not stop_polling.is_set():
        if check_stop_signal():
            logger.info("[brain] Stop file seen during cycle, requesting shutdown")
            signal_shutdown()

        db = SessionLocal()
        try:
            if is_stop_requested(db):
                logger.info("[brain] DB stop requested during cycle, cooperative shutdown")
                signal_shutdown()
                clear_stop_requested(db)
            if peek_wake_requested(db):
                wake_skip_idle.set()
            update_worker_heartbeat(db)
            db.commit()
        except Exception as e:
            logger.warning("[brain] DB control poll tick failed: %s", e)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()

        if track_learning_progress:
            try:
                ls = get_learning_status()
                if ls.get("running"):
                    phase = ls.get("phase", "")
                    step = ls.get("current_step", "")
                    steps_done = ls.get("steps_completed", 0)
                    total_steps = ls.get("total_steps", 24)
                    progress = f"Step {steps_done}/{total_steps}"
                    status.set_step(step or phase, progress)
            except Exception:
                pass

        stop_polling.wait(4.0)


def _get_live_queue_status():
    """Return current backtest queue (pending count, queue_empty) from DB."""
    from app.services.trading.backtest_queue import get_queue_status
    db = SessionLocal()
    try:
        status = get_queue_status(db, use_cache=False)
        return status.get("pending", 0), status.get("queue_empty", True)
    finally:
        db.close()


def _apply_learning_result_to_stats(result: dict, cycle_stats: dict, db) -> None:
    """Map `run_learning_cycle` return dict into worker cycle_stats (in-process or remote)."""
    if result.get("ok", True):
        cycle_stats["tickers_scanned"] = result.get("tickers_scanned", 0)
        cycle_stats["snapshots_taken"] = result.get("snapshots_taken", 0)
        cycle_stats["patterns_mined"] = result.get("patterns_discovered", 0)
        cycle_stats["patterns_tested"] = result.get("backtests_run", 0)
        cycle_stats["hypotheses_validated"] = result.get("hypotheses_tested", 0)
        cycle_stats["hypotheses_confirmed"] = result.get("hypotheses_challenged", 0)
        cycle_stats["insights_decayed"] = result.get("insights_decayed", 0)
        cycle_stats["patterns_pruned"] = result.get("insights_pruned", 0)
        cycle_stats["patterns_spawned"] = result.get("hypothesis_patterns_spawned", 0)
        evo = result.get("evolution", {})
        if isinstance(evo, dict):
            cycle_stats["patterns_evolved"] = (
                int(evo.get("forked_exit", 0) or 0)
                + int(evo.get("forked_entry", 0) or 0)
                + int(evo.get("forked_combo", 0) or 0)
                + int(evo.get("forked_tf", 0) or 0)
                + int(evo.get("forked_scope", 0) or 0)
                + int(evo.get("mutated_exit", 0) or 0)
                + int(evo.get("mutated_entry", 0) or 0)
            )
            cycle_stats["patterns_variant_promoted"] = int(evo.get("promoted", 0) or 0)
            cycle_stats["patterns_pruned"] += int(evo.get("deactivated", 0) or 0)
        cycle_stats["queue_empty"] = result.get("queue_empty", True)
        cycle_stats["queue_pending"] = result.get("queue_pending", 0)
        cycle_stats["queue_exploration_added"] = int(
            result.get("queue_exploration_added", 0) or 0
        )
        cycle_stats["step_timings"] = result.get("step_timings", {})
        cycle_stats["elapsed_s"] = result.get("elapsed_s")
        logger.info(
            f"[brain] Full cycle completed | "
            f"Scanned: {cycle_stats['tickers_scanned']}, "
            f"Mined: {cycle_stats['patterns_mined']}, "
            f"Tested: {cycle_stats['patterns_tested']}, "
            f"Hypotheses: {cycle_stats['hypotheses_validated']}"
        )
    else:
        logger.warning(f"[brain] Learning cycle returned: {result.get('reason', 'unknown')}")
        if db is not None:
            try:
                from app.services.trading.backtest_queue import get_queue_status

                qstatus = get_queue_status(db, use_cache=False)
                cycle_stats["queue_empty"] = qstatus.get("queue_empty", True)
                cycle_stats["queue_pending"] = qstatus.get("pending", 0)
            except Exception as qe:
                logger.warning(f"[brain] Could not get live queue status: {qe}")


def run_learning_cycle(status: BrainWorkerStatus) -> dict:
    """Execute the FULL learning cycle (23 in-cycle steps; prescreen/scan are cron jobs).
    
    This replaces the old 4-step minimal cycle with the complete
    run_learning_cycle from app.services.trading.learning.
    """
    from app.services.trading.learning import (
        run_learning_cycle as full_learning_cycle,
    )

    cycle_stats = {
        "started": datetime.utcnow().isoformat(),
        "patterns_mined": 0,
        "patterns_tested": 0,
        "hypotheses_validated": 0,
        "hypotheses_confirmed": 0,
        "patterns_spawned": 0,
        "patterns_evolved": 0,
        "patterns_variant_promoted": 0,
        "patterns_pruned": 0,
        "tickers_scanned": 0,
        "snapshots_taken": 0,
        "insights_decayed": 0,
    }

    use_remote = os.environ.get("CHILI_USE_BRAIN_SERVICE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    status.wake_skip_idle.clear()
    stop_polling = threading.Event()
    wake_skip = status.wake_skip_idle

    def poll_loop():
        _brain_db_poll_loop(
            stop_polling,
            status,
            wake_skip,
            track_learning_progress=not use_remote,
        )

    poll_thread = threading.Thread(target=poll_loop, daemon=True, name="brain-db-poll")
    poll_thread.start()

    try:
        if use_remote:
            status.set_step("Running full learning cycle", "Brain HTTP service...")
            logger.info("[brain] CHILI_USE_BRAIN_SERVICE enabled — delegating to Brain service")
            try:
                from app.services.brain_client import run_learning_cycle_via_brain_service

                result = run_learning_cycle_via_brain_service()
                db_remote = SessionLocal()
                try:
                    _apply_learning_result_to_stats(result, cycle_stats, db_remote)
                finally:
                    db_remote.close()
            except Exception as e:
                logger.error(f"[brain] Remote learning cycle failed: {e}")
                import traceback

                traceback.print_exc()
                try:
                    live_pending, live_empty = _get_live_queue_status()
                    cycle_stats["queue_empty"] = live_empty
                    cycle_stats["queue_pending"] = live_pending
                except Exception as qe:
                    logger.warning(f"[brain] Could not get live queue status: {qe}")
            cycle_stats["completed"] = datetime.utcnow().isoformat()
            return cycle_stats

        db = SessionLocal()
        try:
            status.set_step("Running full learning cycle", "Starting...")
            logger.info("[brain] Starting FULL learning cycle (24 steps)")
            # Do not skip here: run_learning_cycle() clears stale locks and returns
            # {"ok": False} if a non-stale cycle is already in progress.

            from app.config import settings as _settings

            _uid = getattr(_settings, "brain_default_user_id", None)
            result = full_learning_cycle(db, user_id=_uid, full_universe=True)
            if not result.get("ok", True):
                logger.warning(
                    "[brain] Learning cycle did not run: %s",
                    result.get("reason", result),
                )
            _apply_learning_result_to_stats(result, cycle_stats, db)

            cycle_stats["completed"] = datetime.utcnow().isoformat()

        except Exception as e:
            logger.error(f"[brain] Full learning cycle failed: {e}")
            import traceback

            traceback.print_exc()
            try:
                live_pending, live_empty = _get_live_queue_status()
                cycle_stats["queue_empty"] = live_empty
                cycle_stats["queue_pending"] = live_pending
            except Exception as qe:
                logger.warning(f"[brain] Could not get live queue status: {qe}")

        finally:
            db.close()

        return cycle_stats
    finally:
        stop_polling.set()
        poll_thread.join(timeout=8)


def _cleanup_on_exit():
    """Cleanup handler for atexit - ensures status file is cleared and lock released."""
    global _global_status
    if _global_status is not None:
        try:
            _global_status.clear()
            logger.info("[brain] Cleanup: status file cleared")
        except Exception as e:
            logger.warning(f"[brain] Cleanup failed: {e}")
    
    release_lock()
    logger.info("[brain] Cleanup: lock released")


def main():
    global _global_status
    
    parser = argparse.ArgumentParser(description="CHILI Brain Worker")
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_CYCLE_INTERVAL,
        help=f"Minutes between cycles (default: {DEFAULT_CYCLE_INTERVAL})"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one cycle and exit"
    )
    args = parser.parse_args()
    
    # Acquire exclusive lock to prevent multiple workers
    if not acquire_lock():
        logger.error("[brain] Another brain worker is already running. Exiting.")
        sys.exit(1)
    
    status = BrainWorkerStatus()
    _global_status = status  # Set global reference for atexit cleanup
    
    # Register cleanup handler
    atexit.register(_cleanup_on_exit)
    
    # Clean up any stale signal files
    if STOP_SIGNAL.exists():
        STOP_SIGNAL.unlink()
    if WAKE_SIGNAL.exists():
        WAKE_SIGNAL.unlink()
    
    def handle_shutdown(signum, frame):
        logger.info("[brain] Received shutdown signal")
        status.clear()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    logger.info(f"[brain] Brain Worker starting (PID: {status.pid})")
    logger.info(f"[brain] DATA_DIR (must match app DB / API): {DATA_DIR.resolve()}")
    logger.info(f"[brain] Cycle interval: {args.interval} minutes")
    
    status.status = "running"
    status.save()

    # Fresh process: clear stale DB stop from a prior run so we don't exit immediately.
    db_boot = SessionLocal()
    try:
        from app.services.brain_worker_signals import clear_stop_requested

        clear_stop_requested(db_boot)
        db_boot.commit()
    except Exception as e:
        logger.warning("[brain] Could not clear DB stop flag on startup: %s", e)
        try:
            db_boot.rollback()
        except Exception:
            pass
    finally:
        db_boot.close()
    
    try:
        while True:
            # Check for pause
            while check_pause_signal():
                status.status = "paused"
                status.set_step("Paused", "Waiting for resume...")
                logger.info("[brain] Paused, waiting for resume signal...")
                _db_heartbeat_tick()
                time.sleep(10)
            
            status.status = "running"
            
            # Run learning cycle
            logger.info("[brain] Starting learning cycle")
            cycle_start = time.time()
            
            cycle_stats = {}
            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    cycle_stats = run_learning_cycle(status)
                    
                    # Update totals
                    status.totals["cycles_completed"] += 1
                    for key in ["tickers_scanned", "snapshots_taken", "patterns_mined", 
                               "patterns_tested", "queue_patterns_processed",
                               "hypotheses_validated", "hypotheses_confirmed", 
                               "patterns_spawned", "patterns_evolved", "patterns_variant_promoted",
                               "patterns_pruned", "insights_decayed"]:
                        status.totals[key] += cycle_stats.get(key, 0)
                    
                    cycle_duration = time.time() - cycle_start
                    cycle_stats["duration_seconds"] = round(cycle_duration, 1)
                    status.last_cycle = cycle_stats
                    
                    logger.info(
                        f"[brain] Cycle completed in {cycle_duration:.1f}s | "
                        f"Scanned: {cycle_stats.get('tickers_scanned', 0)}, "
                        f"Mined: {cycle_stats['patterns_mined']}, Tested: {cycle_stats['patterns_tested']}, "
                        f"Evolved: {cycle_stats.get('patterns_evolved', 0)}, Pruned: {cycle_stats.get('patterns_pruned', 0)}"
                    )
                    break  # Success, exit retry loop
                    
                except Exception as e:
                    error_str = str(e).lower()
                    is_db_locked = "database is locked" in error_str or "locked" in error_str
                    
                    if is_db_locked and attempt < max_retries:
                        wait_time = 30 * (attempt + 1)  # 30s, 60s
                        logger.warning(f"[brain] Database locked, waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                        status.set_step("Waiting", f"Database locked, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    
                    logger.error(f"[brain] Cycle failed (attempt {attempt + 1}): {e}")
                    import traceback
                    traceback.print_exc()
                    
                    # On final failure, set error status but don't crash
                    status.set_step("Error", f"Cycle failed: {str(e)[:50]}...")
                    cycle_stats = {}
                    break
            
            if args.once:
                logger.info("[brain] Single cycle mode, exiting")
                break
            
            # Check for stop signal (file or DB)
            if check_stop_signal() or _check_db_stop_idle():
                logger.info("[brain] Stop signal received, shutting down")
                break

            # Wake queued during the long cycle (peeked in DB poll) or file/DB after cycle
            if _consume_wake_queued_during_cycle(status) or check_any_wake():
                logger.info("[brain] Wake after cycle — skipping idle sleep")
                continue
            
            # Sleep policy: DB "pending" ignores exploration-eligible patterns, so also treat
            # productive backtests this cycle (and exploration fill) as "keep cycling soon".
            queue_pending = cycle_stats.get("queue_pending", 0)
            patterns_tested = int(cycle_stats.get("patterns_tested", 0) or 0)
            exploration_added = int(cycle_stats.get("queue_exploration_added", 0) or 0)
            try:
                live_pending, _ = _get_live_queue_status()
                if live_pending > 0:
                    queue_pending = live_pending
            except Exception as e:
                logger.warning(f"[brain] Could not re-check queue before sleep: {e}")

            should_short_sleep = (
                queue_pending > 0
                or patterns_tested > 0
                or exploration_added > 0
            )

            if should_short_sleep:
                sleep_seconds = 60  # 1 minute breather
                if queue_pending > 0:
                    idle_msg = (
                        f"{queue_pending} pattern(s) due for retest. Continuing in 1 minute."
                    )
                    log_msg = (
                        f"[brain] Retest queue: {queue_pending} pending. Continuing in 1 minute"
                    )
                elif patterns_tested > 0:
                    idle_msg = (
                        f"Ran {patterns_tested} backtest(s) this cycle. Continuing in 1 minute."
                    )
                    log_msg = (
                        f"[brain] Backtests ran ({patterns_tested}); short sleep before next cycle"
                    )
                else:
                    idle_msg = (
                        f"Exploration queued {exploration_added} pattern(s). Continuing in 1 minute."
                    )
                    log_msg = (
                        f"[brain] Exploration fill ({exploration_added}); short sleep before next cycle"
                    )
                status.set_step("Idle", idle_msg)
                logger.info(log_msg)
            else:
                sleep_seconds = args.interval * 60
                status.set_step(
                    "Idle",
                    f"No retest due and no backtests this cycle. Next in {args.interval} min.",
                )
                logger.info(
                    f"[brain] Retest queue clear and idle cycle. Sleeping {args.interval} minutes"
                )
            
            status.save()
            
            # Sleep in short chunks with stop / wake checks (wake skips remaining idle sleep)
            remaining = sleep_seconds
            stop_during_idle_sleep = False
            while remaining > 0:
                if check_stop_signal() or _check_db_stop_idle():
                    logger.info("[brain] Stop signal received during sleep")
                    stop_during_idle_sleep = True
                    break
                if check_any_wake():
                    logger.info("[brain] Wake during idle sleep — starting next cycle now")
                    break
                _db_heartbeat_tick()
                # Shorter chunks = faster response to brain_worker_wake (UI "Run next cycle")
                chunk = min(5, remaining)
                time.sleep(chunk)
                remaining -= chunk

            if stop_during_idle_sleep:
                logger.info("[brain] Shutting down after stop during idle")
                break
    
    finally:
        status.clear()
        logger.info("[brain] Brain Worker stopped")


if __name__ == "__main__":
    main()
