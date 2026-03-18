"""
CHILI Brain Worker - Continuous Learning Loop

Runs as a separate process, continuously executing the learning cycle:
1. Mine patterns from market data
2. Backtest active patterns
3. Validate hypotheses
4. Evolve/spawn patterns
5. Prune underperformers

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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal

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

DEFAULT_CYCLE_INTERVAL = 30  # minutes


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
            "patterns_mined": 0,
            "patterns_tested": 0,
            "hypotheses_validated": 0,
            "hypotheses_confirmed": 0,
            "patterns_spawned": 0,
            "patterns_evolved": 0,
            "patterns_pruned": 0,
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
        """Write status to JSON file."""
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
        with open(STATUS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    
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
    """Execute one full learning cycle."""
    from app.services.trading.learning import (
        mine_patterns,
        _auto_backtest_patterns,
        validate_and_evolve,
        evolve_pattern_strategies,
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
    }
    
    db = SessionLocal()
    
    try:
        # Step 1: Mine patterns
        status.set_step("Mining patterns", "Scanning market data...")
        logger.info("[brain] Step 1: Mining patterns from market data")
        try:
            mined = mine_patterns(db, user_id=None)
            cycle_stats["patterns_mined"] = len(mined) if mined else 0
            logger.info(f"[brain] Mined {cycle_stats['patterns_mined']} patterns")
        except Exception as e:
            logger.warning(f"[brain] Pattern mining failed: {e}")
        
        if check_stop_signal():
            return cycle_stats
        
        # Step 2: Backtest active patterns
        status.set_step("Backtesting patterns", "Running backtests...")
        logger.info("[brain] Step 2: Backtesting active patterns")
        try:
            bt_count = _auto_backtest_patterns(db, user_id=None)
            cycle_stats["patterns_tested"] = bt_count or 0
            logger.info(f"[brain] Ran {cycle_stats['patterns_tested']} backtests")
        except Exception as e:
            logger.warning(f"[brain] Backtesting failed: {e}")
        
        if check_stop_signal():
            return cycle_stats
        
        # Step 3: Validate hypotheses
        status.set_step("Validating hypotheses", "Testing A/B conditions...")
        logger.info("[brain] Step 3: Validating hypotheses")
        try:
            evolve_result = validate_and_evolve(db, user_id=None)
            cycle_stats["hypotheses_validated"] = evolve_result.get("hypotheses_tested", 0)
            cycle_stats["hypotheses_confirmed"] = evolve_result.get("confirmed", 0)
            logger.info(
                f"[brain] Validated {cycle_stats['hypotheses_validated']} hypotheses, "
                f"{cycle_stats['hypotheses_confirmed']} confirmed"
            )
        except Exception as e:
            logger.warning(f"[brain] Hypothesis validation failed: {e}")
        
        if check_stop_signal():
            return cycle_stats
        
        # Step 4: Evolve patterns
        status.set_step("Evolving patterns", "Spawning variants...")
        logger.info("[brain] Step 4: Evolving pattern strategies")
        try:
            evo_result = evolve_pattern_strategies(db)
            cycle_stats["patterns_evolved"] = (
                evo_result.get("forked_exit", 0) +
                evo_result.get("forked_entry", 0) +
                evo_result.get("forked_combo", 0)
            )
            cycle_stats["patterns_spawned"] = evo_result.get("promoted", 0)
            cycle_stats["patterns_pruned"] = evo_result.get("deactivated", 0)
            logger.info(
                f"[brain] Evolved {cycle_stats['patterns_evolved']} variants, "
                f"promoted {cycle_stats['patterns_spawned']}, pruned {cycle_stats['patterns_pruned']}"
            )
        except Exception as e:
            logger.warning(f"[brain] Pattern evolution failed: {e}")
        
        cycle_stats["completed"] = datetime.utcnow().isoformat()
        
    finally:
        db.close()
    
    return cycle_stats


def main():
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
    
    status = BrainWorkerStatus()
    
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
            
            try:
                cycle_stats = run_learning_cycle(status)
                
                # Update totals
                status.totals["cycles_completed"] += 1
                for key in ["patterns_mined", "patterns_tested", "hypotheses_validated",
                           "hypotheses_confirmed", "patterns_spawned", "patterns_evolved",
                           "patterns_pruned"]:
                    status.totals[key] += cycle_stats.get(key, 0)
                
                cycle_duration = time.time() - cycle_start
                cycle_stats["duration_seconds"] = round(cycle_duration, 1)
                status.last_cycle = cycle_stats
                
                logger.info(
                    f"[brain] Cycle completed in {cycle_duration:.1f}s | "
                    f"Mined: {cycle_stats['patterns_mined']}, Tested: {cycle_stats['patterns_tested']}, "
                    f"Validated: {cycle_stats['hypotheses_validated']}, Confirmed: {cycle_stats['hypotheses_confirmed']}"
                )
                
            except Exception as e:
                logger.error(f"[brain] Cycle failed: {e}")
                import traceback
                traceback.print_exc()
            
            status.set_step("Idle", f"Next cycle in {args.interval} minutes")
            status.save()
            
            if args.once:
                logger.info("[brain] Single cycle mode, exiting")
                break
            
            # Check for stop signal
            if check_stop_signal():
                logger.info("[brain] Stop signal received, shutting down")
                break
            
            # Sleep until next cycle
            sleep_seconds = args.interval * 60
            logger.info(f"[brain] Sleeping for {args.interval} minutes until next cycle")
            
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
