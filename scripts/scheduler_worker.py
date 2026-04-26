"""
Dedicated APScheduler process. Docker Compose sets CHILI_SCHEDULER_ROLE=all on this service
so broker sync, scans, code/reasoning cycles, etc. stay out of Uvicorn; the web app uses
CHILI_SCHEDULER_ROLE=none.

Local default below is ``worker`` (heavy scans + heartbeat only) if you run the script
without env vars.

Usage:
  python scripts/scheduler_worker.py

Docker: see docker-compose ``scheduler-worker`` service.
"""
import os
import sys
import time

# Must run before importing app settings (Compose overrides to ``all``).
os.environ.setdefault("CHILI_SCHEDULER_ROLE", "worker")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def main() -> None:
    from app.services.trading_scheduler import start_scheduler, stop_scheduler

    try:
        from app.services.trading.brain_io_concurrency import log_brain_io_profile

        log_brain_io_profile(logger)
    except Exception as _e:
        logger.debug("[scheduler_worker] brain I/O profile log skipped: %s", _e)

    # Restore Robinhood session so broker sync can run.
    try:
        from app.services import broker_service

        ok = broker_service.try_restore_session()
        logger.info("[scheduler_worker] Broker session restore: %s", "ok" if ok else "no session")
    except Exception as _e:
        logger.warning("[scheduler_worker] Broker session restore failed: %s", _e)

    # Restore kill-switch state before scheduler starts (Hard Rule 1/2:
    # a tripped breaker must survive process restarts — otherwise the safety
    # guarantee silently evaporates on every redeploy).
    try:
        from app.services.trading.governance import (
            get_kill_switch_status,
            restore_kill_switch_from_db,
        )

        restore_kill_switch_from_db()
        status = get_kill_switch_status()
        if status.get("active"):
            logger.warning(
                "[scheduler_worker] Kill switch restored ACTIVE: %s — autotrader blocked until manual reset",
                status.get("reason"),
            )
        else:
            logger.info("[scheduler_worker] Kill switch restored: inactive")
    except Exception as _e:
        logger.warning("[scheduler_worker] Kill switch restore failed: %s", _e)

    start_scheduler()
    logger.info("[scheduler_worker] Started (CHILI_SCHEDULER_ROLE=%s)", os.environ.get("CHILI_SCHEDULER_ROLE"))

    # ── DO NOT REMOVE — CHILI Code Brain wiring (Phase E reactive) ─────
    # Reactive neural architecture that REPLACES the dumb 60s
    # ``run_code_learning_cycle`` timer. The brain now operates on events,
    # not a clock:
    #   * trigger_watcher (every 30s, cheap DB reads, NO LLM) detects new
    #     ready tasks / validation failures and enqueues code_brain_events.
    #   * event_processor (every 30s, claims one event) routes through
    #     decision_router which prefers TEMPLATE > LOCAL_MODEL > PREMIUM.
    #     LLM calls only happen for PREMIUM, and only when the daily
    #     budget cap allows.
    #   * pattern_miner (every 6 HOURS — never more often) extracts
    #     deterministic templates from llm_call_log + coding_agent_suggestion
    #     so the brain learns and gradually stops needing the LLM.
    #
    # Mode switching via ``code_brain_runtime_state.mode``:
    #   * 'reactive'   (default) — new behavior described above
    #   * 'paused'              — watchers + processor return immediately
    #   * 'legacy_60s'          — falls back to the old timer-driven cycle
    _dispatch_sched = None
    if os.environ.get("CHILI_DISPATCH_ENABLED", "0") == "1":
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
            from app.services.code_dispatch.governance import restore_from_db as _dispatch_restore
            _dispatch_restore()
            _dispatch_sched = BackgroundScheduler()

            # --- Pull current mode from DB (defaults to reactive on first boot). ---
            try:
                from app.db import SessionLocal as _SL
                from app.services.code_brain import runtime_state as _rs
                with _SL() as _s:
                    _state = _rs.get_state(_s)
                    _mode = _state.mode
            except Exception as _e:
                logger.warning(
                    "[code_brain] runtime_state read failed: %s — defaulting to reactive",
                    _e,
                )
                _mode = "reactive"

            if _mode == "legacy_60s":
                # Backwards-compat path. Costly. Only use for direct A/B
                # with the new architecture.
                from app.services.code_dispatch.cycle import run_code_learning_cycle
                _interval = int(os.environ.get("CHILI_DISPATCH_INTERVAL_SEC", "60"))
                _dispatch_sched.add_job(
                    run_code_learning_cycle,
                    IntervalTrigger(seconds=_interval),
                    id="code_dispatch_cycle_legacy",
                    max_instances=1,
                    coalesce=True,
                    replace_existing=True,
                )
                logger.warning(
                    "[code_brain] LEGACY 60s loop active (interval=%ss). "
                    "Switch to reactive via "
                    "UPDATE code_brain_runtime_state SET mode='reactive'.",
                    _interval,
                )
            else:
                # Reactive default.
                from app.services.code_brain.trigger_watcher import run_all_watchers
                from app.services.code_brain.event_processor import process_one_event
                from app.services.code_brain.pattern_miner import mine_recent

                def _watch_job():
                    try:
                        from app.db import SessionLocal as _SL2
                        with _SL2() as _s2:
                            run_all_watchers(_s2)
                    except Exception as _ex:
                        logger.exception("[code_brain.watch] failed: %s", _ex)

                def _process_job():
                    try:
                        from app.db import SessionLocal as _SL3
                        with _SL3() as _s3:
                            # Process up to 3 events per tick so a backlog
                            # drains without spinning a tight loop.
                            for _ in range(3):
                                report = process_one_event(_s3)
                                if report is None:
                                    break
                    except Exception as _ex:
                        logger.exception("[code_brain.process] failed: %s", _ex)

                def _mine_job():
                    try:
                        from app.db import SessionLocal as _SL4
                        with _SL4() as _s4:
                            mine_recent(_s4)
                    except Exception as _ex:
                        logger.exception("[code_brain.mine] failed: %s", _ex)

                _watch_secs = int(os.environ.get("CHILI_BRAIN_WATCH_SEC", "30"))
                _process_secs = int(os.environ.get("CHILI_BRAIN_PROCESS_SEC", "30"))
                _mine_hours = int(os.environ.get("CHILI_BRAIN_MINE_HOURS", "6"))

                _dispatch_sched.add_job(
                    _watch_job,
                    IntervalTrigger(seconds=_watch_secs),
                    id="code_brain_watch",
                    max_instances=1,
                    coalesce=True,
                    replace_existing=True,
                )
                _dispatch_sched.add_job(
                    _process_job,
                    IntervalTrigger(seconds=_process_secs),
                    id="code_brain_process",
                    max_instances=1,
                    coalesce=True,
                    replace_existing=True,
                )
                _dispatch_sched.add_job(
                    _mine_job,
                    IntervalTrigger(hours=_mine_hours),
                    id="code_brain_mine",
                    max_instances=1,
                    coalesce=True,
                    replace_existing=True,
                )
                logger.info(
                    "[code_brain] REACTIVE mode ENABLED "
                    "(watch=%ss, process=%ss, mine=%sh, mode=%s)",
                    _watch_secs, _process_secs, _mine_hours, _mode,
                )

            _dispatch_sched.start()
        except Exception as _e:
            logger.exception("[code_brain] wiring failed: %s", _e)
    else:
        logger.info("[code_brain] DISABLED (set CHILI_DISPATCH_ENABLED=1 to turn on)")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("[scheduler_worker] Shutting down")
    finally:
        stop_scheduler()
        if _dispatch_sched is not None:
            try:
                _dispatch_sched.shutdown(wait=False)
            except Exception:
                pass


if __name__ == "__main__":
    main()
