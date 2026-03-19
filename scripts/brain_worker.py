"""
CHILI Brain Worker - Continuous Learning Loop

Runs as a separate process, continuously executing the FULL learning cycle:
- Pre-filter market (Massive.com)
- Deep score candidates
- Take snapshots
- Backfill future returns
- Decay stale insights
- Mine patterns
- Backtest patterns
- Validate hypotheses
- Learn from breakout outcomes
- Specialized mining (intraday, fakeout, synergy)
- Pattern evolution
- Journal, signals, ML training

Status is written to data/brain_worker_status.json for monitoring.
Control via signal files in data/ directory.

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

DATA_DIR = Path("data")
STATUS_FILE = DATA_DIR / "brain_worker_status.json"
STOP_SIGNAL = DATA_DIR / "brain_worker_stop"
PAUSE_SIGNAL = DATA_DIR / "brain_worker_pause"
LOCK_FILE = DATA_DIR / "brain_worker.lock"

DEFAULT_CYCLE_INTERVAL = 30  # minutes

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


def run_learning_cycle(status: BrainWorkerStatus) -> dict:
    """Execute the FULL learning cycle (all 23 steps).
    
    This replaces the old 4-step minimal cycle with the complete
    run_learning_cycle from app.services.trading.learning.
    """
    from app.services.trading.learning import (
        run_learning_cycle as full_learning_cycle,
        get_learning_status,
        signal_shutdown,
    )

    cycle_stats = {
        "started": datetime.utcnow().isoformat(),
        "patterns_mined": 0,
        "patterns_tested": 0,
        "hypotheses_validated": 0,
        "hypotheses_confirmed": 0,
        "patterns_spawned": 0,
        "patterns_evolved": 0,
        "patterns_pruned": 0,
        "tickers_scanned": 0,
        "snapshots_taken": 0,
        "insights_decayed": 0,
    }
    
    db = SessionLocal()
    
    try:
        status.set_step("Running full learning cycle", "Starting...")
        logger.info("[brain] Starting FULL learning cycle (23 steps)")
        
        # Check if another learning cycle is already running
        learning_status = get_learning_status()
        if learning_status.get("running"):
            logger.warning("[brain] Another learning cycle is already running, skipping")
            return cycle_stats
        
        # Start a thread to poll learning status and update worker status
        import threading
        stop_polling = threading.Event()
        
        def poll_learning_status():
            while not stop_polling.is_set():
                try:
                    if check_stop_signal():
                        logger.info("[brain] Stop signal seen during cycle, requesting shutdown")
                        signal_shutdown()
                        break
                    ls = get_learning_status()
                    if ls.get("running"):
                        phase = ls.get("phase", "")
                        step = ls.get("current_step", "")
                        steps_done = ls.get("steps_completed", 0)
                        total_steps = ls.get("total_steps", 23)
                        progress = f"Step {steps_done}/{total_steps}"
                        status.set_step(step or phase, progress)
                except Exception:
                    pass
                stop_polling.wait(2)
        
        poll_thread = threading.Thread(target=poll_learning_status, daemon=True)
        poll_thread.start()
        
        try:
            # Run the full learning cycle
            result = full_learning_cycle(db, user_id=None, full_universe=True)
            
            # Map results to our stats format
            if result.get("ok", True):
                cycle_stats["tickers_scanned"] = result.get("tickers_scanned", 0)
                cycle_stats["snapshots_taken"] = result.get("snapshots_taken", 0)
                cycle_stats["patterns_mined"] = result.get("patterns_discovered", 0)
                cycle_stats["patterns_tested"] = result.get("backtests_run", 0)
                cycle_stats["hypotheses_validated"] = result.get("hypotheses_tested", 0)
                cycle_stats["hypotheses_confirmed"] = result.get("hypotheses_challenged", 0)
                cycle_stats["insights_decayed"] = result.get("insights_decayed", 0)
                cycle_stats["patterns_pruned"] = result.get("insights_pruned", 0)
                
                # Evolution stats
                evo = result.get("evolution", {})
                if isinstance(evo, dict):
                    cycle_stats["patterns_evolved"] = (
                        evo.get("forked_exit", 0) +
                        evo.get("forked_entry", 0) +
                        evo.get("forked_combo", 0)
                    )
                    cycle_stats["patterns_spawned"] = evo.get("promoted", 0)
                    cycle_stats["patterns_pruned"] += evo.get("deactivated", 0)
                
                # Backtest queue status so worker knows whether to sleep or continue
                cycle_stats["queue_empty"] = result.get("queue_empty", True)
                cycle_stats["queue_pending"] = result.get("queue_pending", 0)
                # Step timings for speed / bottleneck visibility
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
                
        finally:
            stop_polling.set()
            poll_thread.join(timeout=3)
        
        cycle_stats["completed"] = datetime.utcnow().isoformat()
        
    except Exception as e:
        logger.error(f"[brain] Full learning cycle failed: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        db.close()
    
    return cycle_stats


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
    
    def handle_shutdown(signum, frame):
        logger.info("[brain] Received shutdown signal")
        status.clear()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    logger.info(f"[brain] Brain Worker starting (PID: {status.pid})")
    logger.info(f"[brain] Cycle interval: {args.interval} minutes")
    
    status.status = "running"
    status.save()
    
    try:
        while True:
            # Check for pause
            while check_pause_signal():
                status.status = "paused"
                status.set_step("Paused", "Waiting for resume...")
                logger.info("[brain] Paused, waiting for resume signal...")
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
                               "patterns_spawned", "patterns_evolved", "patterns_pruned",
                               "insights_decayed"]:
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
            
            # Check for stop signal
            if check_stop_signal():
                logger.info("[brain] Stop signal received, shutting down")
                break
            
            # Determine sleep time based on queue status
            queue_empty = cycle_stats.get("queue_empty", True)
            queue_pending = cycle_stats.get("queue_pending", 0)
            
            if queue_empty:
                # Queue is empty - sleep for full interval
                sleep_seconds = args.interval * 60
                status.set_step("Idle", f"Queue empty. Next cycle in {args.interval} minutes")
                logger.info(f"[brain] Queue empty. Sleeping for {args.interval} minutes")
            else:
                # Queue has work - short pause then continue
                sleep_seconds = 60  # 1 minute breather
                status.set_step("Idle", f"{queue_pending} patterns pending. Continuing in 1 minute...")
                logger.info(f"[brain] Queue has {queue_pending} pending patterns. Continuing in 1 minute")
            
            status.save()
            
            # Sleep with stop signal checks
            for _ in range(sleep_seconds // 10):
                if check_stop_signal():
                    logger.info("[brain] Stop signal received during sleep")
                    break
                time.sleep(10)
            else:
                time.sleep(sleep_seconds % 10)
    
    finally:
        status.clear()
        logger.info("[brain] Brain Worker stopped")


if __name__ == "__main__":
    main()
