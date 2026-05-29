"""
CHILI Brain Worker — multi-mode trading brain process.

Modes (``--mode``) are dispatched explicitly in ``main()``:

- ``lean-cycle`` (default): each iteration runs a **work-ledger dispatch round**, then a
  **full reconcile pass** (``run_learning_cycle`` — legacy in-process step orchestrator),
  then **maintenance subtasks** between iterations. When ``TRADING_BRAIN_NEURAL_MESH_ENABLED=1``,
  also runs a short Postgres activation batch after a successful reconcile pass.
- ``activation-loop``: neural mesh event queue only (dev/soak); no full reconcile pass.
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

os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("BRAIN_SMART_BT_MAX_WORKERS", "1")
os.environ.setdefault("CHILI_BRAIN_DISPATCH_MARKET_SNAPSHOTS_ENABLED", "0")
os.environ.setdefault("BRAIN_IO_WORKERS_HIGH", "2")
os.environ.setdefault("BRAIN_IO_WORKERS_MED", "2")
os.environ.setdefault("BRAIN_IO_WORKERS_LOW", "1")
os.environ.setdefault("BRAIN_SNAPSHOT_IO_WORKERS", "2")
os.environ.setdefault("BRAIN_PREDICTION_IO_WORKERS", "2")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal, is_disconnect_error, recover_session_after_db_error

try:
    from app.services.trading.brain_io_concurrency import log_brain_io_profile
except Exception:
    log_brain_io_profile = None  # type: ignore[misc, assignment]

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
STATUS_TMP_SUFFIX = ".tmp"
STOP_SIGNAL = DATA_DIR / "brain_worker_stop"
PAUSE_SIGNAL = DATA_DIR / "brain_worker_pause"
WAKE_SIGNAL = DATA_DIR / "brain_worker_wake"
LOCK_FILE = DATA_DIR / "brain_worker.lock"
LOCK_FILE_PREFIX = "brain_worker"
LOCK_FILE_SUFFIX = ".lock"

DEFAULT_CYCLE_INTERVAL = 5  # minutes between cycles when queue empty (override with --interval)
FAST_BACKTEST_BATCH_ENV = "CHILI_BRAIN_FAST_BACKTEST_BATCH"
FAST_BACKTEST_BATCH_SOURCE_ENV = "env"
FAST_BACKTEST_BATCH_SOURCE_MODE_DEFAULT = "mode_default"
FAST_BACKTEST_MODE_LEAN_CYCLE = "lean-cycle"
FAST_BACKTEST_MODE_BACKTEST = "backtest"
FAST_BACKTEST_DEFAULT_BATCH_BY_MODE = {
    FAST_BACKTEST_MODE_LEAN_CYCLE: 0,
    FAST_BACKTEST_MODE_BACKTEST: 30,
}
FAST_BACKTEST_DEFAULT_BATCH = 0
FAST_BACKTEST_RATE_SECONDS_PER_MINUTE = 60.0
FAST_BACKTEST_RATE_MIN_ELAPSED_SECONDS = 0.001

# Global lock file handle (kept open while running)
_lock_handle = None
_lock_path: Path | None = None


def _status_tmp_file() -> Path:
    """Per-process/thread temp path for atomic status writes.

    Multiple worker modes can run at once. A shared ``brain_worker_status.json.tmp``
    lets one process replace another process's temp file before it calls
    ``replace()``, producing noisy FileNotFoundError tracebacks during startup
    and shutdown.
    """
    return STATUS_FILE.with_name(
        f"{STATUS_FILE.name}.{os.getpid()}.{threading.get_ident()}{STATUS_TMP_SUFFIX}"
    )


def _parse_non_negative_int(value: object, default: int) -> int:
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return max(0, int(default))


def _fast_backtest_default_batch_for_mode(mode: str) -> int:
    normalized = (mode or "").strip().lower()
    try:
        from app.config import settings as _settings

        if normalized == FAST_BACKTEST_MODE_LEAN_CYCLE:
            return _parse_non_negative_int(
                getattr(
                    _settings,
                    "brain_fast_backtest_batch_lean_cycle",
                    FAST_BACKTEST_DEFAULT_BATCH_BY_MODE[FAST_BACKTEST_MODE_LEAN_CYCLE],
                ),
                FAST_BACKTEST_DEFAULT_BATCH_BY_MODE[FAST_BACKTEST_MODE_LEAN_CYCLE],
            )
        if normalized == FAST_BACKTEST_MODE_BACKTEST:
            return _parse_non_negative_int(
                getattr(
                    _settings,
                    "brain_fast_backtest_batch_backtest",
                    FAST_BACKTEST_DEFAULT_BATCH_BY_MODE[FAST_BACKTEST_MODE_BACKTEST],
                ),
                FAST_BACKTEST_DEFAULT_BATCH_BY_MODE[FAST_BACKTEST_MODE_BACKTEST],
            )
    except Exception:
        pass
    return FAST_BACKTEST_DEFAULT_BATCH_BY_MODE.get(
        normalized,
        FAST_BACKTEST_DEFAULT_BATCH,
    )


def _configure_fast_backtest_batch_for_mode(mode: str) -> dict[str, object]:
    raw = os.environ.get(FAST_BACKTEST_BATCH_ENV)
    if raw is None or not str(raw).strip():
        batch_size = _fast_backtest_default_batch_for_mode(mode)
        os.environ[FAST_BACKTEST_BATCH_ENV] = str(batch_size)
        source = FAST_BACKTEST_BATCH_SOURCE_MODE_DEFAULT
    else:
        batch_size = _parse_non_negative_int(raw, FAST_BACKTEST_DEFAULT_BATCH)
        os.environ[FAST_BACKTEST_BATCH_ENV] = str(batch_size)
        source = FAST_BACKTEST_BATCH_SOURCE_ENV
    return {"mode": mode, "batch_size": batch_size, "source": source}


def _fast_backtest_batch_size() -> int:
    return _parse_non_negative_int(
        os.environ.get(FAST_BACKTEST_BATCH_ENV),
        FAST_BACKTEST_DEFAULT_BATCH,
    )


def _should_start_independent_fast_backtest_loop(mode: str, independent_loop: bool) -> bool:
    return (
        bool(independent_loop)
        and (mode or "").strip().lower() == FAST_BACKTEST_MODE_LEAN_CYCLE
        and _fast_backtest_batch_size() > 0
    )


def _fast_backtest_executor_label() -> str:
    try:
        from app.config import settings as _settings

        return str(getattr(_settings, "brain_queue_backtest_executor", "threads") or "threads")
    except Exception:
        return "unknown"


def _fast_backtest_queue_status_snapshot() -> dict[str, object]:
    db = SessionLocal()
    try:
        from app.services.trading.backtest_queue import get_queue_status

        return dict(get_queue_status(db, use_cache=False))
    except Exception as exc:
        logger.debug("[brain:subtask] fast_backtest queue status unavailable: %s", exc)
        return {}
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def _lock_file_for_mode(mode: str | None) -> Path:
    normalized = (mode or FAST_BACKTEST_MODE_LEAN_CYCLE).strip().lower()
    if normalized == FAST_BACKTEST_MODE_LEAN_CYCLE:
        return LOCK_FILE
    token = "".join(
        ch if ch.isalnum() or ch in ("-", "_") else "_"
        for ch in normalized
    ) or "worker"
    return DATA_DIR / f"{LOCK_FILE_PREFIX}.{token}{LOCK_FILE_SUFFIX}"


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
            # FIX 46 pattern: rollback before close (read txn cleanup).
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
    except Exception as exc:
        logger.debug("[brain] heartbeat tick failed: %s", exc)


def acquire_lock(mode: str | None = None) -> bool:
    """Acquire an exclusive lock to prevent duplicate workers for one mode.
    
    Returns True if lock acquired, False if this mode is already running.
    """
    global _lock_handle, _lock_path
    DATA_DIR.mkdir(exist_ok=True)
    lock_path = _lock_file_for_mode(mode)
    
    try:
        _lock_handle = open(lock_path, "w")
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        
        # Write PID to lock file
        _lock_handle.write(str(os.getpid()))
        _lock_handle.flush()
        _lock_path = lock_path
        return True
    except (IOError, OSError) as e:
        logger.error(f"[brain] Failed to acquire lock — another worker may be running: {e}")
        if _lock_handle:
            _lock_handle.close()
            _lock_handle = None
        return False


def release_lock():
    """Release the exclusive lock."""
    global _lock_handle, _lock_path
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
        lock_path = _lock_path or LOCK_FILE
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        pass
    _lock_path = None


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
        tmp = _status_tmp_file()
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
    """If wake file exists (from UI wake control), remove it and skip idle sleep."""
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
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
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
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
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
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
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
            # FIX 46 pattern: rollback before close (read txn cleanup).
            try:
                db.rollback()
            except Exception:
                pass
            db.close()

        if track_learning_progress:
            try:
                ls = get_learning_status()
                if ls.get("running"):
                    phase = ls.get("phase", "")
                    step = ls.get("current_step", "")
                    nodes_done = ls.get("nodes_completed", 0)
                    total_nodes = ls.get("total_nodes", 0)
                    clusters_done = ls.get("clusters_completed", 0)
                    total_clusters = ls.get("total_clusters", 0)
                    progress = f"Nodes {nodes_done}/{total_nodes}  Clusters {clusters_done}/{total_clusters}"
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
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
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
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
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
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def _run_subtask_signal_refresh(status: "BrainWorkerStatus") -> dict:
    """Refresh promoted pattern prediction cache."""
    status.set_step("SignalRefresh", "Refreshing promoted predictions...")
    db = SessionLocal()
    try:
        from app.services.trading.learning import (
            provider_egress_available_for_brain_work,
            refresh_promoted_prediction_cache,
        )
        if not provider_egress_available_for_brain_work():
            logger.info("[brain:subtask] signal_refresh skipped - provider egress unavailable")
            return {"skipped": True, "skip_reason": "provider_egress_unavailable"}
        result = refresh_promoted_prediction_cache(db)
        return result
    except Exception as e:
        logger.warning("[brain:subtask] signal_refresh failed: %s", e)
        return {"error": str(e)}
    finally:
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def _run_subtask_fast_backtest(status: "BrainWorkerStatus") -> dict:
    """Process backtest queue items.

    FIX B (2026-04-28): batch size raised 5 → 30 per cycle to drain the
    141-deep backlog of untested candidates.

    FIX 43 (2026-04-29): EARLY-EXIT when data providers are dead. The
    Massive circuit breaker opens after 5 consecutive TCP failures and
    cools down for 900s. Without this guard the FIX 34 timer would still
    fire every 60s, and each tick spawned a fresh ``execute_queue_backtest_for_pattern``
    worker that wedged on the data-fetch retry loop (observed: brain-worker
    at 115% CPU, ~10 simultaneous FractionalBacktest tqdm bars at 0%/0 bar/s,
    each holding memory + threads).

    Now: when the Massive breaker is OPEN, skip the entire tick so brain-worker
    stays idle until the cooldown expires and the breaker probes again. The
    Coinbase fallback (FIX 42) is also network-blocked from the host right
    now, so there's no useful work to do during an outage.

    Override via ``CHILI_BRAIN_FAST_BACKTEST_BATCH`` env var.

    Delegates to the shared queue executor so ``brain_queue_backtest_executor``
    and process caps are honored; each worker item still owns its DB session.
    """
    status.set_step("FastBacktest", "Processing backtest queue...")
    from app.config import settings as _s

    try:
        from app.services.trading.learning import provider_egress_available_for_brain_work

        if not provider_egress_available_for_brain_work():
            logger.info(
                "[brain:subtask] fast_backtest skipped - provider egress unavailable"
            )
            return {
                "completed": 0,
                "errors": 0,
                "skipped": True,
                "skip_reason": "provider_egress_unavailable",
            }
    except Exception as e:
        logger.debug("[brain:subtask] provider preflight failed: %s", e)

    # FIX 43: skip the whole tick if the primary data provider is down.
    # When Coinbase fallback (FIX 42) is also unreachable (current host
    # network state), there's nothing to backtest with — running the path
    # just spawns wedged FractionalBacktest workers.
    if getattr(_s, "brain_fast_backtest_skip_when_provider_down", True):
        try:
            from app.services.massive_client import get_breaker_status as _massive_breaker

            br = _massive_breaker()
            if isinstance(br, dict) and br.get("state") == "open":
                cooldown_remaining = int(br.get("cooldown_remaining_sec") or 0)
                logger.info(
                    "[brain:subtask] fast_backtest skipped — Massive breaker OPEN "
                    "(cooldown %ds). Re-probes when network egress recovers.",
                    cooldown_remaining,
                )
                return {
                    "completed": 0,
                    "errors": 0,
                    "skipped": True,
                    "skip_reason": f"massive_breaker_open_cooldown={cooldown_remaining}s",
                }
        except Exception as e:
            logger.debug("[brain:subtask] fast_backtest breaker probe failed: %s", e)

    uid = getattr(_s, "brain_default_user_id", None)
    queue_before = _fast_backtest_queue_status_snapshot()
    pending_before = int(queue_before.get("pending") or 0)
    batch_size = _fast_backtest_batch_size()
    if batch_size <= 0:
        log_fn = logger.warning if pending_before > 0 else logger.info
        log_fn(
            "[brain:subtask] fast_backtest skipped - batch disabled "
            "pending=%d boosted=%s needs_retest=%s promotion_path_debt=%s executor=%s",
            pending_before,
            queue_before.get("boosted", "-"),
            queue_before.get("needs_retest", "-"),
            queue_before.get("promotion_path_debt_pending", "-"),
            _fast_backtest_executor_label(),
        )
        return {
            "completed": 0,
            "errors": 0,
            "skipped": True,
            "skip_reason": "batch_disabled",
            "batch_size": batch_size,
            "pending_before": pending_before,
            "pending_after": pending_before,
            "promotion_path_debt_pending_before": queue_before.get(
                "promotion_path_debt_pending"
            ),
            "promotion_path_debt_pending_after": queue_before.get(
                "promotion_path_debt_pending"
            ),
            "drain_rate_per_min": 0.0,
        }
    logger.info(
        "[brain:subtask] fast_backtest start batch_size=%d executor=%s "
        "pending=%d boosted=%s needs_retest=%s never_tested=%s "
        "promotion_path_debt=%s",
        batch_size,
        _fast_backtest_executor_label(),
        pending_before,
        queue_before.get("boosted", "-"),
        queue_before.get("needs_retest", "-"),
        queue_before.get("never_tested", "-"),
        queue_before.get("promotion_path_debt_pending", "-"),
    )
    started_at = time.monotonic()
    completed = 0
    errors = 0
    processed_patterns = 0
    queue_executor = _fast_backtest_executor_label()
    db = SessionLocal()
    try:
        from app.services.trading.learning import _auto_backtest_from_queue

        result = _auto_backtest_from_queue(db, uid, batch_size=batch_size)
        completed = int(result.get("backtests_run") or 0)
        processed_patterns = int(result.get("patterns_processed") or 0)
        queue_executor = str(result.get("queue_executor") or queue_executor)
        logger.info(
            "[brain:subtask] fast_backtest queue executor finished "
            "executor=%s processed_patterns=%d completed=%d",
            queue_executor,
            processed_patterns,
            completed,
        )
    except Exception as e:
        logger.warning("[brain:subtask] fast_backtest queue executor failed: %s", e)
        errors += 1
    finally:
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()
    queue_after = _fast_backtest_queue_status_snapshot()
    pending_after_raw = queue_after.get("pending")
    pending_after = int(pending_after_raw) if pending_after_raw is not None else pending_before
    drained = max(0, pending_before - pending_after)
    elapsed_s = max(
        FAST_BACKTEST_RATE_MIN_ELAPSED_SECONDS,
        time.monotonic() - started_at,
    )
    drain_rate = drained / (elapsed_s / FAST_BACKTEST_RATE_SECONDS_PER_MINUTE)
    logger.info(
        "[brain:subtask] fast_backtest done processed_patterns=%d completed=%d "
        "errors=%d pending_before=%d pending_after=%d "
        "promotion_path_debt_before=%s promotion_path_debt_after=%s "
        "drain_rate_per_min=%.2f",
        processed_patterns,
        completed,
        errors,
        pending_before,
        pending_after,
        queue_before.get("promotion_path_debt_pending", "-"),
        queue_after.get("promotion_path_debt_pending", "-"),
        drain_rate,
    )
    return {
        "completed": completed,
        "errors": errors,
        "batch_size": batch_size,
        "processed_patterns": processed_patterns,
        "queue_executor": queue_executor,
        "pending_before": pending_before,
        "pending_after": pending_after,
        "promotion_path_debt_pending_before": queue_before.get(
            "promotion_path_debt_pending"
        ),
        "promotion_path_debt_pending_after": queue_after.get(
            "promotion_path_debt_pending"
        ),
        "drain_rate_per_min": drain_rate,
    }


def _run_subtask_pattern_regime_ledger(status: "BrainWorkerStatus") -> dict:
    """Build trading_pattern_regime_performance_daily — the per-pattern x
    regime evidence ledger. Joins closed trades with ticker_regime snapshots
    and aggregates per (pattern_id, regime_label).
    """
    status.set_step("PatternRegimeLedger", "Joining closed trades with regime snapshots...")
    from app.services.trading.pattern_regime_ledger import build_ledger
    db = SessionLocal()
    try:
        return build_ledger(db)
    except Exception as e:
        logger.warning("[brain:subtask] pattern_regime_ledger failed: %s", e)
        return {"error": str(e)}
    finally:
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def _run_subtask_realized_sync(status: "BrainWorkerStatus") -> dict:
    """Sync ScanPattern realized stats (trade_count, win_rate, avg_return_pct)
    from trading_trades. Source-of-truth maintenance for the EV gate +
    autotune — closes the sync gap audit-found 2026-04-28.
    """
    status.set_step("RealizedSync", "Syncing realized stats from trading_trades...")
    from app.services.trading.realized_stats_sync import sync_realized_stats
    db = SessionLocal()
    try:
        return sync_realized_stats(db)
    except Exception as e:
        logger.warning("[brain:subtask] realized_sync failed: %s", e)
        return {"error": str(e)}
    finally:
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def _run_subtask_crypto_pattern_miner(status: "BrainWorkerStatus") -> dict:
    """Run crypto-native pattern miner — extracts indicator signatures
    from recent profitable crypto trades and spawns variant candidates.

    Added 2026-04-29 to address the operator goal of 10 profitable crypto
    trades/day. The general web_pattern_researcher is biased toward
    1d swing setups for stocks; this complements it with crypto-specific
    candidates mined from real winners. Runs every 6 cycles since
    realized winners don't accumulate quickly.
    """
    status.set_step("CryptoMiner", "Mining crypto patterns from realized winners...")
    from app.services.trading.crypto.pattern_miner import run_crypto_pattern_miner
    db = SessionLocal()
    try:
        return run_crypto_pattern_miner(db)
    except Exception as e:
        logger.warning("[brain:subtask] crypto_pattern_miner failed: %s", e)
        return {"error": str(e)}
    finally:
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def _run_subtask_ticker_autotune(status: "BrainWorkerStatus") -> dict:
    """Auto-narrow ScanPattern.scope_tickers from realized per-ticker PnL.

    Added 2026-04-28 — the brain learns ticker dependency rather than us
    banning tickers manually. Run every 6 cycles since per-ticker stats
    don't change quickly and this touches the primary operator truth column
    ``ticker_scope`` which auto-trader reads on every alert.
    """
    status.set_step("TickerAutotune", "Auto-tuning scope_tickers from realized PnL...")
    from app.services.trading.ticker_scope_autotune import run_autotune
    db = SessionLocal()
    try:
        actions = run_autotune(db)
        narrowed = sum(1 for a in actions if a.decision == "narrow_to_explicit")
        return {"actions": len(actions), "narrowed": narrowed}
    except Exception as e:
        logger.warning("[brain:subtask] ticker_autotune failed: %s", e)
        return {"error": str(e)}
    finally:
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


# Subtask registry: (name, function, run_every_n_cycles) — maintenance / edge refresh, not primary operator truth.
# FIX 34 (2026-04-29): fast_backtest is intentionally NOT in this list — it now
# runs on its own independent timer (see _run_fast_backtest_independent_loop)
# so the queue drains regardless of whether run_learning_cycle is stuck on a
# stalled provider chain. If the independent loop is disabled via settings,
# fast_backtest is re-injected at startup (see main()).
_SUBTASKS = [
    ("alpha_decay", _run_subtask_alpha_decay, 3),
    ("signal_refresh", _run_subtask_signal_refresh, 1),
    ("retention", _run_subtask_retention, 12),
    ("realized_sync", _run_subtask_realized_sync, 1),
    ("ticker_autotune", _run_subtask_ticker_autotune, 6),
    ("crypto_pattern_miner", _run_subtask_crypto_pattern_miner, 6),
    ("pattern_regime_ledger", _run_subtask_pattern_regime_ledger, 12),
]
_subtask_counters: dict[str, int] = {name: 0 for name, _, _ in _SUBTASKS}


# FIX 34 (2026-04-29): Independent fast_backtest loop.
# Module-scope lock prevents concurrent invocation if cycle's after-cycle
# sweep (in degenerate config where independent loop is disabled but the
# subtask is also re-injected) tries to fire fast_backtest at the same time
# as the timer thread.
_FAST_BACKTEST_LOCK = threading.Lock()


def _run_fast_backtest_independent_loop(stop_event: "threading.Event", status: "BrainWorkerStatus") -> None:
    """Independent fast_backtest timer thread.

    FIX 34 (2026-04-29): Previously fast_backtest ran only after
    ``run_learning_cycle`` completed. With Massive/yfinance frequently down,
    Step 1 (mine) stalls for tens of minutes on data-fetch retries, blocking
    the queue drain. The 354-pattern queue (incl. priority=100 crypto seeds)
    rotted at ``candidate``.

    This loop ticks every ``brain_fast_backtest_interval_s`` (default 60s)
    independent of the cycle. The cycle still runs (FIX 31 gates it), but
    backtest queue drain no longer waits on it. Bridge to FIX 31 event-driven
    endgame.
    """
    from app.config import settings as _settings

    interval_s = int(os.environ.get(
        "CHILI_BRAIN_FAST_BACKTEST_INTERVAL_S",
        str(getattr(_settings, "brain_fast_backtest_interval_s", 60)),
    ))
    queue_status = _fast_backtest_queue_status_snapshot()
    logger.info(
        "[brain:fast_backtest_loop] starting interval=%ds batch_size=%d "
        "executor=%s pending=%s boosted=%s promotion_path_debt=%s",
        interval_s,
        _fast_backtest_batch_size(),
        _fast_backtest_executor_label(),
        queue_status.get("pending", "-"),
        queue_status.get("boosted", "-"),
        queue_status.get("promotion_path_debt_pending", "-"),
    )
    while not stop_event.is_set():
        try:
            # Lock prevents concurrent fast_backtest with the after-cycle sweep
            # if the operator re-injects the subtask via degenerate config.
            acquired = _FAST_BACKTEST_LOCK.acquire(blocking=False)
            if not acquired:
                logger.debug("[brain:fast_backtest_loop] tick skipped (lock held)")
            else:
                try:
                    t0 = time.time()
                    r = _run_subtask_fast_backtest(status)
                    elapsed = round(time.time() - t0, 1)
                    completed = int((r or {}).get("completed", 0) or 0)
                    errors = int((r or {}).get("errors", 0) or 0)
                    if completed > 0 or errors > 0:
                        logger.info(
                            "[brain:fast_backtest_loop] tick completed=%d "
                            "processed_patterns=%s errors=%d pending_before=%s "
                            "pending_after=%s drain_rate_per_min=%.2f elapsed=%.1fs",
                            completed,
                            (r or {}).get("processed_patterns", "-"),
                            errors,
                            (r or {}).get("pending_before", "-"),
                            (r or {}).get("pending_after", "-"),
                            float((r or {}).get("drain_rate_per_min") or 0.0),
                            elapsed,
                        )
                finally:
                    _FAST_BACKTEST_LOCK.release()
        except Exception as e:
            logger.warning("[brain:fast_backtest_loop] tick failed: %s", e)
            try:
                _FAST_BACKTEST_LOCK.release()
            except (RuntimeError, threading.ThreadError):
                pass

        # Sleep in 5s chunks for fast stop response
        remaining = interval_s
        while remaining > 0 and not stop_event.is_set():
            chunk = min(5, remaining)
            time.sleep(chunk)
            remaining -= chunk
    logger.info("[brain:fast_backtest_loop] stopped")


def _start_fast_backtest_thread(status: "BrainWorkerStatus") -> "threading.Event":
    """Start the independent fast_backtest timer thread. Returns its stop event."""
    stop_event = threading.Event()
    t = threading.Thread(
        target=_run_fast_backtest_independent_loop,
        args=(stop_event, status),
        daemon=True,
        name="brain-fast-backtest",
    )
    t.start()
    return stop_event


def run_subtasks(status: "BrainWorkerStatus") -> dict:
    """Run due maintenance / edge-refresh subtasks after a full reconcile pass."""
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
            f"[brain] Reconcile pass completed | "
            f"Scanned: {cycle_stats['tickers_scanned']}, "
            f"Mined: {cycle_stats['patterns_mined']}, "
            f"Tested: {cycle_stats['patterns_tested']}, "
            f"Hypotheses: {cycle_stats['hypotheses_validated']}"
        )
    else:
        logger.warning(f"[brain] Reconcile pass returned: {result.get('reason', 'unknown')}")
        if db is not None:
            try:
                from app.services.trading.backtest_queue import get_queue_status

                qstatus = get_queue_status(db, use_cache=False)
                cycle_stats["queue_empty"] = qstatus.get("queue_empty", True)
                cycle_stats["queue_pending"] = qstatus.get("pending", 0)
            except Exception as qe:
                logger.warning(f"[brain] Could not get live queue status: {qe}")


def _learning_cycle_audit_begin() -> str | None:
    """Insert a brain_batch_jobs 'running' row in its OWN short-lived session.

    Round-18 FIX (2026-04-30): visibility for the legacy reconcile pass.
    The cycle holds a single ~13-34min session that periodically dies
    mid-flight (`server closed the connection unexpectedly` per FIX 31
    notes). Without an audit row, those crashes are invisible.

    Use a separate session for begin/finish so the audit row commits
    independently of the cycle's session lifecycle. Returns the
    job_id, or None if begin failed (in which case finish is skipped).
    """
    db = SessionLocal()
    try:
        from app.services.trading.brain_batch_job_log import brain_batch_job_begin
        jid = brain_batch_job_begin(db, "learning_cycle")
        db.commit()
        return jid
    except Exception as e:
        logger.warning("[brain] learning_cycle audit begin failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def _learning_cycle_audit_finish(jid: str | None, *, ok: bool, error: str | None,
                                   meta: dict | None) -> None:
    """Mark the audit row finished in its OWN short-lived session.

    Round-18 FIX (2026-04-30). Caller is the cycle's wrapper; jid is
    None when begin failed. Any exception here is swallowed -- the cycle
    has already returned its real result; we don't want the audit
    bookkeeping to mask it.
    """
    if not jid:
        return
    db = SessionLocal()
    try:
        from app.services.trading.brain_batch_job_log import brain_batch_job_finish
        brain_batch_job_finish(db, jid, ok=ok, error=error, meta=meta)
        db.commit()
    except Exception as e:
        logger.warning("[brain] learning_cycle audit finish failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def run_learning_cycle(status: BrainWorkerStatus) -> dict:
    """Execute the full in-process **reconcile pass** (``run_learning_cycle`` in learning.py).

    This is the legacy step-orchestrated pass (20+ steps); prescreen/scan are often cron-driven.
    Primary live operator truth is the durable work ledger + scheduler, not this step bar.

    Round-18 FIX (2026-04-30): wrapped with brain_batch_jobs begin/finish
    in separate sessions so a cycle that dies mid-flight (FIX 31's
    'server closed the connection unexpectedly' scenario) still leaves
    a 'running' or 'error' audit row. Previously these crashes were
    silent.
    """
    from app.services.trading.learning import (
        run_learning_cycle as full_learning_cycle,
    )

    audit_jid = _learning_cycle_audit_begin()

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
    audit_ok = False
    audit_error: str | None = None

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
            status.set_step("Running full reconcile pass", "Brain HTTP service...")
            logger.info("[brain] CHILI_USE_BRAIN_SERVICE enabled — delegating reconcile pass to Brain service")
            try:
                from app.services.brain_client import run_learning_cycle_via_brain_service

                result = run_learning_cycle_via_brain_service()
                db_remote = SessionLocal()
                try:
                    _apply_learning_result_to_stats(result, cycle_stats, db_remote)
                finally:
                    # FIX 46 pattern: rollback before close (read txn cleanup).
                    try:
                        db_remote.rollback()
                    except Exception:
                        pass
                    db_remote.close()
                audit_ok = True
            except Exception as e:
                audit_error = f"remote: {e}"
                logger.error(f"[brain] Remote reconcile pass failed: {e}")
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
            status.set_step("Running full reconcile pass", "Starting...")
            logger.info("[brain] Starting full reconcile pass (run_learning_cycle)")
            # Do not skip here: run_learning_cycle() clears stale locks and returns
            # {"ok": False} if a non-stale cycle is already in progress.

            from app.config import settings as _settings

            _uid = getattr(_settings, "brain_default_user_id", None)
            result = full_learning_cycle(db, user_id=_uid, full_universe=True)
            if not result.get("ok", True):
                logger.warning(
                    "[brain] Reconcile pass did not run: %s",
                    result.get("reason", result),
                )
                # Don't mark error -- "already in progress" is benign.
                audit_ok = True
                audit_error = (
                    f"skipped: {result.get('reason')}" if result.get("reason") else None
                )
            else:
                audit_ok = True
            _apply_learning_result_to_stats(result, cycle_stats, db)

            cycle_stats["completed"] = datetime.utcnow().isoformat()

        except Exception as e:
            audit_ok = False
            audit_error = str(e)[:2000]
            logger.error(f"[brain] Full reconcile pass failed: {e}")
            import traceback

            traceback.print_exc()
            try:
                live_pending, live_empty = _get_live_queue_status()
                cycle_stats["queue_empty"] = live_empty
                cycle_stats["queue_pending"] = live_pending
            except Exception as qe:
                logger.warning(f"[brain] Could not get live queue status: {qe}")

        finally:
            # FIX 46 pattern: rollback before close (read txn cleanup).
            try:
                db.rollback()
            except Exception:
                pass
            db.close()

        return cycle_stats
    finally:
        stop_polling.set()
        poll_thread.join(timeout=8)
        # Round-18: write the audit-finish row in its own session so it
        # lands even if the cycle's session was torn down by a TCP reset.
        _learning_cycle_audit_finish(
            audit_jid,
            ok=audit_ok,
            error=audit_error,
            meta={
                "patterns_mined": cycle_stats.get("patterns_mined", 0),
                "patterns_tested": cycle_stats.get("patterns_tested", 0),
                "tickers_scanned": cycle_stats.get("tickers_scanned", 0),
                "elapsed_s": cycle_stats.get("elapsed_s"),
                "queue_pending": cycle_stats.get("queue_pending"),
                "use_remote": use_remote,
            },
        )


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
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def _maybe_run_brain_work_batch() -> None:
    """Durable work ledger: dispatch round (execution_feedback_digest + backtest_requested)."""
    def _run_once(db) -> dict:
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
        return summary

    db = SessionLocal()
    try:
        _run_once(db)
    except Exception as e:
        logger.warning("[brain] work ledger batch failed: %s", e)
        recover_session_after_db_error(
            db,
            e,
            logger=logger,
            context="[brain] work ledger dispatch",
        )
        if is_disconnect_error(e):
            retry_db = SessionLocal()
            try:
                _run_once(retry_db)
                logger.info("[brain] work ledger dispatch recovered on fresh DB session")
            except Exception as retry_e:
                logger.warning("[brain] work ledger retry failed: %s", retry_e)
                recover_session_after_db_error(
                    retry_db,
                    retry_e,
                    logger=logger,
                    context="[brain] work ledger retry",
                )
            finally:
                try:
                    retry_db.rollback()
                except Exception:
                    pass
                retry_db.close()
    finally:
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


# f-handler-load-verification (2026-05-05): startup self-check that every
# brain_work handler module imports cleanly + exposes its handle_* callable.
# Replaces the silent regression class that bit pattern_stats earlier today
# (5 of 6 handlers had broken `from ....db import SessionLocal` for 6 days
# undetected because the dispatcher's try/except swallowed the failure).
# Crashes brain-worker on startup with a clear multi-line error if any
# handler fails to load -- better than dispatching to a no-op handler.
_HANDLER_MODULES_EXPECTED: dict[str, list[str]] = {
    "app.services.trading.brain_work.handlers.mine":
        ["handle_market_snapshots_batch"],
    "app.services.trading.brain_work.handlers.cpcv_gate":
        ["handle_backtest_completed"],
    "app.services.trading.brain_work.handlers.promote":
        ["handle_pattern_eligible_promotion"],
    "app.services.trading.brain_work.handlers.demote":
        ["handle_trade_closed"],
    "app.services.trading.brain_work.handlers.regime_ledger":
        ["handle_trade_closed_for_ledger"],
    "app.services.trading.brain_work.handlers.pattern_stats":
        ["handle_paper_trade_closed",
         "handle_live_trade_closed",
         "handle_broker_fill_closed"],
}


def _verify_handler_modules(
    expected: dict[str, list[str]] | None = None,
) -> None:
    """Startup verification: every handler module must import cleanly and
    expose its expected ``handle_*`` callables.

    Failed handlers crash brain-worker on startup with a clear
    multi-line error so the operator sees the regression immediately.

    The ``expected`` parameter is for tests; production callers pass
    nothing and the module-level ``_HANDLER_MODULES_EXPECTED`` map is
    used.
    """
    import importlib

    expected = expected if expected is not None else _HANDLER_MODULES_EXPECTED
    failures: list[str] = []
    for mod_name, callable_names in expected.items():
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            failures.append(
                f"  IMPORT-FAIL {mod_name}: {type(e).__name__}: {e}"
            )
            continue
        for callable_name in callable_names:
            if not callable(getattr(mod, callable_name, None)):
                failures.append(
                    f"  MISSING-CALLABLE {mod_name}.{callable_name}"
                )
    if failures:
        msg = (
            "[handler_verify] STARTUP CHECK FAILED -- brain-worker would "
            "dispatch to broken handlers. Fix before re-running.\n"
            + "\n".join(failures)
        )
        logger.error(msg)
        raise SystemExit(msg)
    logger.info(
        "[handler_verify] OK %d/%d handlers loaded cleanly: %s",
        len(expected),
        len(expected),
        ", ".join(sorted(m.rsplit(".", 1)[1] for m in expected)),
    )


# FIX 31 (deep audit 2026-04-28): conditional gate for the legacy reconcile
# pass. The cycle was running every iteration regardless of work, holding a
# single PG session for ~13–34 minutes and getting killed by `server closed
# the connection unexpectedly` mid-flight. The endgame is to migrate every
# step into brain_work_event handlers; this gate is the bridge.

# Floor: never let more than this many seconds pass between cycles, even
# without explicit signals — guards against slow drift in tables that
# haven't yet emitted events on every change.
#
# f-kill-legacy-learning-cycle (2026-05-05): default raised from 4h to 1y.
# The legacy cycle is gated off via CHILI_BRAIN_LEGACY_CYCLE_ENABLED, so the
# safety floor is moot when the cycle is disabled. If the operator re-enables
# the cycle for emergency rollback, set
# ``CHILI_BRAIN_RECONCILE_MAX_INTERVAL_S=14400`` (4h) explicitly to restore
# the floor. A 1y default means "effectively never" while keeping the env
# var as the override knob.
_RECONCILE_PASS_MAX_INTERVAL_S = int(os.environ.get(
    "CHILI_BRAIN_RECONCILE_MAX_INTERVAL_S", str(365 * 24 * 3600)
))

# In-memory watermark of the last completed reconcile pass. Survives only
# within the brain-worker process lifetime; on restart we run once and
# then start gating again — that's deliberate, restarts are rare.
_LAST_RECONCILE_PASS_AT: float | None = None


def _record_reconcile_pass_completed() -> None:
    global _LAST_RECONCILE_PASS_AT
    _LAST_RECONCILE_PASS_AT = time.time()


def _should_skip_reconcile_pass(status: "BrainWorkerStatus") -> tuple[bool, str]:
    """Return (skip, reason). Skip when nothing has happened that the
    cycle could possibly need to reconcile.

    Triggers that REQUIRE a cycle (skip=False):
      * Never run before in this process lifetime (cold start)
      * MAX_INTERVAL_S elapsed since last cycle (safety floor)
      * Any brain_work_events with status='done' since last cycle
        (a backtest completed, a promotion fired, etc.)
      * Recent scan_pattern lifecycle/promotion changes since last cycle

    Otherwise: skip and let the event-driven path do the work.
    """
    global _LAST_RECONCILE_PASS_AT
    now = time.time()

    if _LAST_RECONCILE_PASS_AT is None:
        # f-kill-legacy-learning-cycle (2026-05-05): cold start no longer
        # auto-triggers a cycle. Pre-fix this returned ``False`` ("don't
        # skip"), forcing every brain-worker restart to attempt a
        # 60-140 minute reconcile pass that crashed 100% of the time over
        # the prior 24h (61% silent TCP drops, 28% transaction-rolled-back).
        # Initialise the watermark to "now" so the safety-floor branch
        # below can take over naturally if the cycle is re-enabled via
        # CHILI_BRAIN_LEGACY_CYCLE_ENABLED=1.
        _LAST_RECONCILE_PASS_AT = now
        return True, "cold_start_no_auto_trigger"

    elapsed = now - _LAST_RECONCILE_PASS_AT
    if elapsed >= _RECONCILE_PASS_MAX_INTERVAL_S:
        return False, f"safety_floor_elapsed_s={int(elapsed)}"

    db = SessionLocal()
    try:
        from sqlalchemy import text

        # Convert wall-time watermark to a SQL-friendly NOW() - INTERVAL
        # since we don't store the watermark in DB (in-memory only).
        # Look at events / pattern changes since last cycle.
        secs = int(elapsed)
        result = db.execute(text(
            """
            SELECT
                (SELECT COUNT(*) FROM brain_work_events
                 WHERE status = 'done'
                   AND updated_at > NOW() - make_interval(secs => :s)) AS work_done,
                (SELECT COUNT(*) FROM scan_patterns
                 WHERE lifecycle_changed_at IS NOT NULL
                   AND lifecycle_changed_at > NOW() - make_interval(secs => :s)) AS lc_changed,
                (SELECT COUNT(*) FROM scan_patterns
                 WHERE updated_at > NOW() - make_interval(secs => :s)) AS pat_updated
            """
        ), {"s": secs}).fetchone()

        work_done = int(result.work_done or 0) if result else 0
        lc_changed = int(result.lc_changed or 0) if result else 0
        pat_updated = int(result.pat_updated or 0) if result else 0

        if work_done > 0:
            return False, f"work_events_done={work_done}"
        if lc_changed > 0:
            return False, f"lifecycle_changes={lc_changed}"
        if pat_updated > 50:  # threshold to avoid trivial bumps
            return False, f"pattern_updates={pat_updated}"

        return True, f"no_signal_elapsed_s={secs}_floor={_RECONCILE_PASS_MAX_INTERVAL_S}"
    except Exception as e:
        # Defensive: any DB error -> run the cycle (fail-open). Better to
        # spend compute than silently drop work.
        logger.warning("[brain] reconcile-skip probe failed; running cycle: %s", e)
        return False, f"probe_error:{type(e).__name__}"
    finally:
        try:
            db.close()
        except Exception:
            pass


def _run_lean_cycle_loop(args: argparse.Namespace, status: BrainWorkerStatus) -> None:
    """Default: dispatch round → full reconcile pass → maintenance subtasks + idle sleep."""
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

        # FIX 31 (architecture, deep audit 2026-04-28): the legacy reconcile
        # pass was running every iteration regardless of whether anything
        # changed — typically holding a single PG session for ~13–34 minutes
        # and getting killed by `server closed the connection unexpectedly`
        # mid-flight. Recent cycles produced "Mined: 0, Tested: 0, Evolved: 0,
        # Pruned: 0" — pure waste of compute and connection budget.
        #
        # Gate the cycle on actual signal:
        #   * If brain_work_events processed any *_completed events this
        #     iteration, OR
        #   * If patterns were promoted/demoted/discovered since last cycle, OR
        #   * If we exceed the safety floor (max time between cycles)
        # then run. Otherwise skip and sleep.
        #
        # The endgame (issue #31) is to migrate every step of the reconcile
        # pass into a brain_work_event handler under app/services/trading/
        # brain_work/ — at which point this gate becomes "always skip" and
        # this whole block goes away. This is the bridge.
        #
        # f-kill-legacy-learning-cycle (2026-05-05): added the kill switch
        # below. The legacy cycle has been crashing 100% of the time over
        # the last 24h (61% silent TCP drops, 28% transaction-rolled-back)
        # and silently swallowing every downstream learning step. Default
        # disabled. Set CHILI_BRAIN_LEGACY_CYCLE_ENABLED=1 to re-engage
        # the cycle path for emergency rollback. Phase 2 event handlers
        # (mine, cpcv_gate, promote, demote, regime_ledger -- all five
        # already shipped per dispatcher.py:272-321) handle the bulk of
        # the work the cycle used to do; the remaining cycle-only steps
        # are inventoried in docs/STRATEGY/PHASE2_HANDLER_BACKLOG.md.
        legacy_cycle_enabled = (
            os.environ.get("CHILI_BRAIN_LEGACY_CYCLE_ENABLED", "0").lower()
            in ("1", "true", "yes")
        )
        if not legacy_cycle_enabled:
            logger.info(
                "[brain] legacy run_learning_cycle DISABLED via "
                "CHILI_BRAIN_LEGACY_CYCLE_ENABLED=0. Phase 2 handlers run "
                "instead. Set =1 to re-enable for emergency rollback."
            )
            skip_cycle, skip_reason = True, "legacy_cycle_disabled"
        else:
            skip_cycle, skip_reason = _should_skip_reconcile_pass(status)
        cycle_stats: dict = {}

        if skip_cycle:
            logger.info("[brain] Reconcile pass SKIPPED (%s); event-driven only this iteration",
                        skip_reason)
            cycle_stats = {"skipped": True, "skip_reason": skip_reason, "duration_seconds": 0.0}
            status.last_cycle = cycle_stats
            # Still run the post-cycle subtasks + neural batch — those are
            # the event-driven parts that should always run, regardless of
            # whether the legacy reconcile pass fired.
            try:
                sub_results = run_subtasks(status)
                if sub_results:
                    cycle_stats["subtasks"] = sub_results
            except Exception as sub_e:
                logger.warning("[brain] Subtask sweep failed (post-skip): %s", sub_e)
            try:
                _maybe_run_neural_activation_batch()
            except Exception as _ne:
                logger.warning("[brain] neural batch (post-skip) skipped: %s", _ne)
        else:
            logger.info("[brain] Starting reconcile pass (after work-ledger dispatch)")
            cycle_start = time.time()
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
                        f"[brain] Reconcile pass finished in {cycle_duration:.1f}s | "
                        f"Scanned: {cycle_stats.get('tickers_scanned', 0)}, "
                        f"Mined: {cycle_stats['patterns_mined']}, Tested: {cycle_stats['patterns_tested']}, "
                        f"Evolved: {cycle_stats.get('patterns_evolved', 0)}, Pruned: {cycle_stats.get('patterns_pruned', 0)}"
                    )

                    # Record skip-floor watermark so future iterations can
                    # decide whether enough has happened to warrant another.
                    _record_reconcile_pass_completed()

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

        import gc as _gc
        _gc.collect()

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
                idle_msg = f"Ran {patterns_tested} backtest(s) this reconcile pass. Continuing in 1 minute."
                log_msg = f"[brain] Backtests ran ({patterns_tested}); short sleep before next iteration"
            else:
                idle_msg = f"Exploration queued {exploration_added} pattern(s). Continuing in 1 minute."
                log_msg = f"[brain] Exploration fill ({exploration_added}); short sleep before next iteration"
            status.set_step("Idle", idle_msg)
            logger.info(log_msg)
        else:
            sleep_seconds = args.interval * 60
            status.set_step(
                "Idle",
                f"No retest due and no backtests this pass. Next worker idle window: {args.interval} min.",
            )
            logger.info(f"[brain] Retest queue clear and idle reconcile pass. Sleeping {args.interval} minutes")

        status.save()

        remaining = sleep_seconds
        stop_during_idle_sleep = False
        while remaining > 0:
            if check_stop_signal() or _check_db_stop_idle():
                logger.info("[brain] Stop signal received during sleep")
                stop_during_idle_sleep = True
                break
            if check_any_wake():
                logger.info("[brain] Wake during idle sleep — starting next worker iteration now")
                break
            _db_heartbeat_tick()
            chunk = min(5, remaining)
            time.sleep(chunk)
            remaining -= chunk

        if stop_during_idle_sleep:
            logger.info("[brain] Shutting down after stop during idle")
            break


def _pg_listen_thread(wake_event: threading.Event, stop_flag: list[bool]) -> None:
    """Background thread: LISTEN on Postgres 'mesh_activation' channel.

    When a NOTIFY arrives (from the trigger on brain_activation_events INSERT),
    set the wake_event so the main loop processes immediately instead of sleeping.
    Falls back to periodic wake if the LISTEN connection drops.
    """
    import select
    try:
        from app.config import settings
        dsn = settings.database_url
        if not dsn:
            logger.warning("[brain] No DATABASE_URL for LISTEN/NOTIFY; falling back to polling")
            return
        # psycopg2 LISTEN requires autocommit mode on a raw connection
        import psycopg2
        conn = psycopg2.connect(dsn)
        conn.set_isolation_level(0)  # autocommit
        cur = conn.cursor()
        cur.execute("LISTEN mesh_activation")
        logger.info("[brain] LISTEN mesh_activation — reactive mode active")

        while not stop_flag[0]:
            if select.select([conn], [], [], 5.0) != ([], [], []):
                conn.poll()
                while conn.notifies:
                    conn.notifies.pop(0)
                    wake_event.set()
    except ImportError:
        logger.info("[brain] psycopg2 not available for LISTEN/NOTIFY; polling fallback")
    except Exception as e:
        logger.warning("[brain] LISTEN thread exiting: %s", e)
    finally:
        try:
            conn.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass


def _run_activation_loop(args: argparse.Namespace, status: BrainWorkerStatus) -> None:
    """Neural mesh queue consumer with Postgres LISTEN/NOTIFY reactivity.

    Uses a background thread that LISTENs on 'mesh_activation'. When a sensor
    publishes an event (INSERT into brain_activation_events), the Postgres trigger
    fires NOTIFY and the main loop wakes instantly to process. Falls back to
    polling if LISTEN is unavailable.
    """
    logger.info("[brain] activation-loop mode — reactive neural mesh (LISTEN/NOTIFY)")
    nap = max(1, min(30, int(args.interval) if args.interval else 2))

    wake_event = threading.Event()
    stop_flag = [False]
    listener = threading.Thread(
        target=_pg_listen_thread,
        args=(wake_event, stop_flag),
        daemon=True,
        name="mesh-listen",
    )
    listener.start()

    try:
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
                # FIX 46 pattern: rollback before close (read txn cleanup).
                try:
                    db.rollback()
                except Exception:
                    pass
                db.close()
            status.save()
            if args.once:
                break
            _db_heartbeat_tick()
            # Wait for NOTIFY wake or fall back to polling interval
            wake_event.wait(timeout=float(nap))
            wake_event.clear()
    finally:
        stop_flag[0] = True


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
            # FIX 46 pattern: rollback before close (read txn cleanup).
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
        status.save()
        if args.once:
            break
        _sleep_chunked(sleep_s, status)


def _run_backtest_loop(args: argparse.Namespace, status: BrainWorkerStatus) -> None:
    sleep_s = max(30, min(300, int(args.interval) * 60 if args.interval else 60))
    queue_status = _fast_backtest_queue_status_snapshot()
    logger.info(
        "[brain] backtest queue owner starting batch_size=%d executor=%s "
        "pending=%s boosted=%s needs_retest=%s promotion_path_debt=%s",
        _fast_backtest_batch_size(),
        _fast_backtest_executor_label(),
        queue_status.get("pending", "-"),
        queue_status.get("boosted", "-"),
        queue_status.get("needs_retest", "-"),
        queue_status.get("promotion_path_debt_pending", "-"),
    )
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
            # FIX 46 pattern: rollback before close (read txn cleanup).
            try:
                db.rollback()
            except Exception:
                pass
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
    
    # Acquire exclusive lock to prevent duplicate workers for this mode.
    if not acquire_lock(args.mode):
        logger.error("[brain] Another %s worker is already running. Exiting.", args.mode)
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
    
    fast_backtest_config = _configure_fast_backtest_batch_for_mode(args.mode)
    logger.info(f"[brain] Brain Worker starting (PID: {status.pid}, mode: {args.mode})")
    logger.info(f"[brain] DATA_DIR (must match app DB / API): {DATA_DIR.resolve()}")
    logger.info(f"[brain] Cycle interval: {args.interval} minutes")
    logger.info(
        "[brain] fast_backtest config mode=%s batch_size=%s source=%s executor=%s",
        fast_backtest_config["mode"],
        fast_backtest_config["batch_size"],
        fast_backtest_config["source"],
        _fast_backtest_executor_label(),
    )
    if log_brain_io_profile:
        log_brain_io_profile(logger)

    # f-handler-load-verification (2026-05-05): import every brain_work
    # handler module + assert each callable is present BEFORE entering the
    # work loop. This prevents the regression class that bit pattern_stats
    # earlier today (6-day silent ModuleNotFoundError on a relative-import
    # depth bug; 5 of 6 handlers had been silently broken since FIX 36-39).
    # Failure here is SystemExit, not warning -- a brain-worker that
    # dispatches to broken handlers is worse than one that doesn't start.
    if os.environ.get("CHILI_PYTEST", "").strip() not in ("1", "true", "yes"):
        _verify_handler_modules()

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
        # FIX 46 pattern: rollback before close (read txn cleanup).
        try:
            db_boot.rollback()
        except Exception:
            pass
        db_boot.close()

    # FIX 34 (2026-04-29): Start the independent fast_backtest timer thread
    # if enabled (default). Decoupled from the cycle so the queue drains
    # regardless of whether run_learning_cycle is stuck on a stalled provider.
    # If the operator disables it, re-inject the subtask into _SUBTASKS so
    # the queue still drains via the after-cycle sweep.
    fast_backtest_stop_event = None
    try:
        from app.config import settings as _cfg
        independent_loop = bool(getattr(_cfg, "brain_fast_backtest_independent_loop", True))
    except Exception:
        independent_loop = True
    if _should_start_independent_fast_backtest_loop(args.mode, independent_loop):
        fast_backtest_stop_event = _start_fast_backtest_thread(status)
        logger.info("[brain] FIX 34: independent fast_backtest timer thread started")
    elif args.mode == "lean-cycle" and _fast_backtest_batch_size() <= 0:
        logger.info(
            "[brain] fast_backtest loop disabled for lean-cycle mode; "
            "dedicated backtest-worker owns the queue"
        )
    elif args.mode == "lean-cycle":
        # Degenerate config: operator disabled the timer; re-inject subtask
        # so the queue still drains after each cycle.
        if not any(name == "fast_backtest" for name, _, _ in _SUBTASKS):
            _SUBTASKS.insert(2, ("fast_backtest", _run_subtask_fast_backtest, 1))
            _subtask_counters["fast_backtest"] = 0
            logger.info("[brain] FIX 34 disabled — fast_backtest re-injected into after-cycle sweep")

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
        if fast_backtest_stop_event is not None:
            fast_backtest_stop_event.set()
        status.clear()
        logger.info("[brain] Brain Worker stopped")


if __name__ == "__main__":
    main()
