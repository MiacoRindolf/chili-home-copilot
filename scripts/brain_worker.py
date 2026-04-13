"""
CHILI Brain Worker — multi-mode trading brain process.

Modes (``--mode``) are dispatched explicitly in ``main()``:

- ``lean-cycle`` (default): full ``run_learning_cycle`` loop + subtasks between cycles.
  When ``TRADING_BRAIN_NEURAL_MESH_ENABLED=1``, also runs a short Postgres activation
  batch after each successful cycle.
- ``activation-loop``: neural mesh event queue only (dev/soak); no full learning cycle.
- ``mining``: repeated ``mine_patterns`` passes (Compose ``mining-worker``).
- ``backtest``: drains the ScanPattern backtest queue via the fast-backtest subtask.
- ``fast-scan``: repeated ``run_pattern_imminent_scan`` (Compose ``fast-scan-worker``).

Previously, non-default modes were accepted by argparse but still ran ``lean-cycle``;
that is fixed.

Status: data/brain_worker_status.json. Signals: data/brain_worker_stop|pause|wake.

Usage:
    python scripts/brain_worker.py [--mode MODE] [--interval MINUTES] [--once]
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


def _db_heartbeat_tick() -> None:
    """Update the worker heartbeat in PostgreSQL during pause/idle so the UI knows we're alive."""
    try:
        from app.services.brain_worker_signals import update_worker_heartbeat

        db = SessionLocal()
        try:
            update_worker_heartbeat(db)
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()
    except Exception as exc:
        logger.debug("[brain] heartbeat tick failed: %s", exc)


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
            "hypotheses_challenged": 0,
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
        try:
            from app.services.trading.learning import snapshot_learning_for_brain_worker_status_file

            learning_snap = snapshot_learning_for_brain_worker_status_file()
        except Exception:
            learning_snap = {}
        data = {
            "pid": self.pid,
            "status": self.status,
            "started_at": self.started_at,
            "current_step": self.current_step,
            "current_progress": self.current_progress,
            "last_cycle": self.last_cycle,
            "totals": self.totals,
            "learning": learning_snap,
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


# ── Multi-mode sub-tasks (run between full cycles) ─────────────────────

def _run_subtask_alpha_decay(status: "BrainWorkerStatus") -> dict:
    """Check live patterns for alpha decay and auto-demote."""
    status.set_step("AlphaDecay", "Checking live patterns...")
    db = SessionLocal()
    try:
        from app.services.trading.alpha_decay import check_alpha_decay
        from app.config import settings as _s
        result = check_alpha_decay(db, user_id=getattr(_s, "brain_default_user_id", None))
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        logger.warning("[brain:subtask] alpha_decay failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


def _run_subtask_retention(status: "BrainWorkerStatus") -> dict:
    """Run data retention sweep."""
    status.set_step("Retention", "Archiving old data...")
    db = SessionLocal()
    try:
        from app.services.trading.data_retention import run_retention_policy
        result = run_retention_policy(db)
        return result
    except Exception as e:
        logger.warning("[brain:subtask] retention failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


def _run_subtask_signal_refresh(status: "BrainWorkerStatus") -> dict:
    """Refresh promoted pattern prediction cache."""
    status.set_step("SignalRefresh", "Refreshing promoted predictions...")
    db = SessionLocal()
    try:
        from app.services.trading.learning import refresh_promoted_prediction_cache
        result = refresh_promoted_prediction_cache(db)
        return result
    except Exception as e:
        logger.warning("[brain:subtask] signal_refresh failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


def _run_subtask_fast_backtest(status: "BrainWorkerStatus") -> dict:
    """Process backtest queue items without running a full cycle."""
    status.set_step("FastBacktest", "Processing backtest queue...")
    db = SessionLocal()
    try:
        from app.services.trading.backtest_engine import smart_backtest_insight
        from app.config import settings as _s
        result = smart_backtest_insight(
            db, user_id=getattr(_s, "brain_default_user_id", None), max_patterns=5,
        )
        return result or {}
    except Exception as e:
        logger.warning("[brain:subtask] fast_backtest failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


# Subtask registry: (name, function, run_every_n_cycles)
_SUBTASKS = [
    ("alpha_decay", _run_subtask_alpha_decay, 3),
    ("signal_refresh", _run_subtask_signal_refresh, 1),
    ("fast_backtest", _run_subtask_fast_backtest, 1),
    ("retention", _run_subtask_retention, 12),
]
_subtask_counters: dict[str, int] = {name: 0 for name, _, _ in _SUBTASKS}


def run_subtasks(status: "BrainWorkerStatus") -> dict:
    """Run due subtasks between full learning cycles."""
    results = {}
    for name, fn, every_n in _SUBTASKS:
        _subtask_counters[name] = _subtask_counters.get(name, 0) + 1
        if _subtask_counters[name] >= every_n:
            _subtask_counters[name] = 0
            try:
                t0 = time.time()
                r = fn(status)
                elapsed = round(time.time() - t0, 1)
                results[name] = {"result": r, "elapsed_s": elapsed}
                logger.info("[brain:subtask] %s completed in %.1fs", name, elapsed)
            except Exception as e:
                results[name] = {"error": str(e)}
                logger.warning("[brain:subtask] %s failed: %s", name, e)
    return results


def _apply_learning_result_to_stats(result: dict, cycle_stats: dict, db) -> None:
    """Map `run_learning_cycle` return dict into worker cycle_stats (in-process or remote)."""
    if result.get("ok", True):
        cycle_stats["tickers_scanned"] = result.get("tickers_scanned", 0)
        cycle_stats["snapshots_taken"] = result.get("snapshots_taken", 0)
        cycle_stats["patterns_mined"] = result.get("patterns_discovered", 0)
        cycle_stats["patterns_tested"] = result.get("backtests_run", 0)
        cycle_stats["hypotheses_validated"] = result.get("hypotheses_tested", 0)
        cycle_stats["hypotheses_challenged"] = result.get("hypotheses_challenged", 0)
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
        "hypotheses_challenged": 0,
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


def _maybe_run_neural_activation_batch() -> None:
    """Postgres neural mesh: bounded activation batch (no new infra)."""
    db = SessionLocal()
    try:
        from app.services.trading.brain_neural_mesh import run_activation_batch

        summary = run_activation_batch(db, time_budget_sec=2.5, max_events=24)
        db.commit()
        if summary.get("processed"):
            logger.info("[brain] neural mesh batch %s", summary)
    except Exception as e:
        logger.warning("[brain] neural activation batch failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _maybe_run_brain_work_batch() -> None:
    """Durable work ledger: dispatch round (execution_feedback_digest + backtest_requested)."""
    db = SessionLocal()
    try:
        from app.config import settings as _settings
        from app.services.trading.brain_work.dispatcher import run_brain_work_batch

        _uid = getattr(_settings, "brain_default_user_id", None)
        summary = run_brain_work_batch(db, user_id=_uid)
        # Always log once per call so production logs prove dispatch ran before cycle (even when idle).
        logger.info(
            "[brain] work ledger dispatch round processed=%s claimed=%s per_type=%s errors=%s",
            summary.get("processed"),
            summary.get("claimed"),
            summary.get("per_type"),
            summary.get("errors"),
        )
    except Exception as e:
        logger.warning("[brain] work ledger batch failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _run_lean_cycle_loop(args: argparse.Namespace, status: BrainWorkerStatus) -> None:
    """Default: full learning cycle + idle sleep policy."""
    while True:
        while check_pause_signal():
            status.status = "paused"
            status.set_step("Paused", "Waiting for resume...")
            logger.info("[brain] Paused, waiting for resume signal...")
            _db_heartbeat_tick()
            time.sleep(10)

        status.status = "running"

        try:
            _maybe_run_brain_work_batch()
        except Exception as _we:
            logger.warning("[brain] work ledger batch before cycle skipped: %s", _we)

        logger.info("[brain] Starting learning cycle")
        cycle_start = time.time()

        cycle_stats: dict = {}
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                cycle_stats = run_learning_cycle(status)

                status.totals["cycles_completed"] += 1
                for key in [
                    "tickers_scanned",
                    "snapshots_taken",
                    "patterns_mined",
                    "patterns_tested",
                    "queue_patterns_processed",
                    "hypotheses_validated",
                    "hypotheses_challenged",
                    "patterns_spawned",
                    "patterns_evolved",
                    "patterns_variant_promoted",
                    "patterns_pruned",
                    "insights_decayed",
                ]:
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

                try:
                    sub_results = run_subtasks(status)
                    if sub_results:
                        cycle_stats["subtasks"] = sub_results
                except Exception as sub_e:
                    logger.warning("[brain] Subtask sweep failed: %s", sub_e)

                try:
                    _maybe_run_neural_activation_batch()
                except Exception as _ne:
                    logger.warning("[brain] neural batch after cycle skipped: %s", _ne)

                break

            except Exception as e:
                error_str = str(e).lower()
                is_db_locked = "database is locked" in error_str or "locked" in error_str

                if is_db_locked and attempt < max_retries:
                    wait_time = 30 * (attempt + 1)
                    logger.warning(
                        f"[brain] Database locked, waiting {wait_time}s before retry {attempt + 1}/{max_retries}"
                    )
                    status.set_step("Waiting", f"Database locked, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue

                logger.error(f"[brain] Cycle failed (attempt {attempt + 1}): {e}")
                import traceback

                traceback.print_exc()
                status.set_step("Error", f"Cycle failed: {str(e)[:50]}...")
                cycle_stats = {}
                break

        if args.once:
            logger.info("[brain] Single cycle mode, exiting")
            break

        if check_stop_signal() or _check_db_stop_idle():
            logger.info("[brain] Stop signal received, shutting down")
            break

        if _consume_wake_queued_during_cycle(status) or check_any_wake():
            logger.info("[brain] Wake after cycle — skipping idle sleep")
            continue

        queue_pending = cycle_stats.get("queue_pending", 0)
        patterns_tested = int(cycle_stats.get("patterns_tested", 0) or 0)
        exploration_added = int(cycle_stats.get("queue_exploration_added", 0) or 0)
        try:
            live_pending, _ = _get_live_queue_status()
            if live_pending > 0:
                queue_pending = live_pending
        except Exception as e:
            logger.warning(f"[brain] Could not re-check queue before sleep: {e}")

        should_short_sleep = queue_pending > 0 or patterns_tested > 0 or exploration_added > 0

        if should_short_sleep:
            sleep_seconds = 60
            if queue_pending > 0:
                idle_msg = f"{queue_pending} pattern(s) due for retest. Continuing in 1 minute."
                log_msg = f"[brain] Retest queue: {queue_pending} pending. Continuing in 1 minute"
            elif patterns_tested > 0:
                idle_msg = f"Ran {patterns_tested} backtest(s) this cycle. Continuing in 1 minute."
                log_msg = f"[brain] Backtests ran ({patterns_tested}); short sleep before next cycle"
            else:
                idle_msg = f"Exploration queued {exploration_added} pattern(s). Continuing in 1 minute."
                log_msg = f"[brain] Exploration fill ({exploration_added}); short sleep before next cycle"
            status.set_step("Idle", idle_msg)
            logger.info(log_msg)
        else:
            sleep_seconds = args.interval * 60
            status.set_step(
                "Idle",
                f"No retest due and no backtests this cycle. Next in {args.interval} min.",
            )
            logger.info(f"[brain] Retest queue clear and idle cycle. Sleeping {args.interval} minutes")

        status.save()

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
            chunk = min(5, remaining)
            time.sleep(chunk)
            remaining -= chunk

        if stop_during_idle_sleep:
            logger.info("[brain] Shutting down after stop during idle")
            break


def _run_activation_loop(args: argparse.Namespace, status: BrainWorkerStatus) -> None:
    """Neural mesh queue consumer only (requires TRADING_BRAIN_NEURAL_MESH_ENABLED)."""
    logger.info("[brain] activation-loop mode — processing neural mesh events")
    nap = max(1, min(30, int(args.interval) if args.interval else 2))
    while True:
        while check_pause_signal():
            status.status = "paused"
            status.set_step("Paused", "Neural mesh paused...")
            _db_heartbeat_tick()
            time.sleep(10)
        if check_stop_signal() or _check_db_stop_idle():
            break
        status.status = "running"
        status.set_step("NeuralActivation", "Draining activation queue...")
        try:
            _maybe_run_brain_work_batch()
        except Exception as _we:
            logger.warning("[brain] work ledger before activation batch skipped: %s", _we)
        db = SessionLocal()
        try:
            from app.services.trading.brain_neural_mesh import run_activation_batch

            summary = run_activation_batch(db, time_budget_sec=float(min(8, nap * 2)), max_events=48)
            db.commit()
            if summary.get("processed"):
                logger.info("[brain] activation-loop %s", summary)
        except Exception as e:
            logger.warning("[brain] activation-loop batch failed: %s", e)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()
        status.save()
        if args.once:
            break
        _db_heartbeat_tick()
        time.sleep(float(nap))


def _run_mining_loop(args: argparse.Namespace, status: BrainWorkerStatus) -> None:
    from app.config import settings as _settings

    uid = getattr(_settings, "brain_default_user_id", None)
    sleep_s = max(60, int(args.interval) * 60)
    while True:
        if check_pause_signal():
            status.status = "paused"
            status.set_step("Paused", "Mining paused...")
            _db_heartbeat_tick()
            time.sleep(10)
            continue
        if check_stop_signal() or _check_db_stop_idle():
            break
        status.status = "running"
        status.set_step("Mining", "mine_patterns batch...")
        db = SessionLocal()
        try:
            from app.services.trading.learning import mine_patterns

            mine_patterns(db, uid)
            db.commit()
            logger.info("[brain] mining mode: mine_patterns pass committed")
        except Exception as e:
            logger.warning("[brain] mining mode failed: %s", e)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()
        status.save()
        if args.once:
            break
        _sleep_chunked(sleep_s, status)


def _run_backtest_loop(args: argparse.Namespace, status: BrainWorkerStatus) -> None:
    sleep_s = max(30, min(300, int(args.interval) * 60 if args.interval else 60))
    while True:
        if check_pause_signal():
            status.status = "paused"
            status.set_step("Paused", "Backtest worker paused...")
            _db_heartbeat_tick()
            time.sleep(10)
            continue
        if check_stop_signal() or _check_db_stop_idle():
            break
        status.status = "running"
        try:
            _maybe_run_brain_work_batch()
        except Exception as _we:
            logger.warning("[brain] work ledger in backtest mode skipped: %s", _we)
        try:
            _run_subtask_fast_backtest(status)
        except Exception as e:
            logger.warning("[brain] backtest mode subtask failed: %s", e)
        status.save()
        if args.once:
            break
        _sleep_chunked(sleep_s, status)


def _run_fast_scan_loop(args: argparse.Namespace, status: BrainWorkerStatus) -> None:
    from app.config import settings as _settings

    uid = getattr(_settings, "brain_default_user_id", None)
    sleep_s = max(60, min(600, int(args.interval) * 60 if args.interval else 120))
    while True:
        if check_pause_signal():
            status.status = "paused"
            status.set_step("Paused", "Fast-scan paused...")
            _db_heartbeat_tick()
            time.sleep(10)
            continue
        if check_stop_signal() or _check_db_stop_idle():
            break
        status.status = "running"
        status.set_step("FastScan", "pattern imminent scan...")
        db = SessionLocal()
        try:
            from app.services.trading.pattern_imminent_alerts import run_pattern_imminent_scan

            run_pattern_imminent_scan(db, user_id=uid)
            db.commit()
        except Exception as e:
            logger.warning("[brain] fast-scan mode failed: %s", e)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()
        status.save()
        if args.once:
            break
        _sleep_chunked(sleep_s, status)


def _sleep_chunked(total_seconds: int, status: BrainWorkerStatus) -> None:
    remaining = int(total_seconds)
    while remaining > 0:
        if check_stop_signal() or _check_db_stop_idle():
            return
        if check_any_wake():
            return
        _db_heartbeat_tick()
        chunk = min(10, remaining)
        time.sleep(chunk)
        remaining -= chunk


def main():
    global _global_status
    
    parser = argparse.ArgumentParser(description="CHILI Brain Worker")
    parser.add_argument(
        "--mode",
        type=str,
        default="lean-cycle",
        choices=["lean-cycle", "activation-loop", "mining", "backtest", "fast-scan"],
        help="Worker mode (default: lean-cycle). activation-loop = neural mesh queue only.",
    )
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
    
    logger.info(f"[brain] Brain Worker starting (PID: {status.pid}, mode: {args.mode})")
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
        if args.mode == "lean-cycle":
            _run_lean_cycle_loop(args, status)
        elif args.mode == "activation-loop":
            _run_activation_loop(args, status)
        elif args.mode == "mining":
            _run_mining_loop(args, status)
        elif args.mode == "backtest":
            _run_backtest_loop(args, status)
        elif args.mode == "fast-scan":
            _run_fast_scan_loop(args, status)
        else:
            logger.error("[brain] Unknown mode %r", args.mode)
    finally:
        status.clear()
        logger.info("[brain] Brain Worker stopped")


if __name__ == "__main__":
    main()
