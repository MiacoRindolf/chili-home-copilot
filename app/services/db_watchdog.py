"""Database idle-transaction watchdog.

Polls ``pg_stat_activity`` periodically and:

* logs WARN when a connection is held ``idle in transaction`` longer than
  :data:`IDLE_TX_WARN_SEC` (default 5 min);
* calls ``pg_terminate_backend()`` for any connection held longer than
  :data:`IDLE_TX_KILL_SEC` (default 30 min).

This guards against the 2026-04-28 incident where a brain-worker
FractionalBacktest opened a transaction, hung for 17 hours, accumulated
``AccessShareLock`` waiters on ``scan_patterns``, and blocked migration
193's ``ALTER TABLE``. The watchdog is a defense-in-depth backstop for
the same class of bug.

The watchdog runs as a daemon thread started from :mod:`app.main` on
process startup. It uses a short-lived ``SessionLocal()`` per poll and
never holds its own transaction open; logs are best-effort.

Tuning::

    CHILI_DB_WATCHDOG_ENABLED       = '1'   (default '1')
    CHILI_DB_WATCHDOG_POLL_SEC      = 60     (default 60)
    CHILI_DB_WATCHDOG_WARN_SEC      = 300    (default 300 = 5min)
    CHILI_DB_WATCHDOG_KILL_SEC      = 1800   (default 1800 = 30min)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool = True) -> bool:
    val = (os.environ.get(name) or "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _poll_once() -> tuple[int, int]:
    """One pass: warn-then-kill stale ``idle in transaction`` connections.

    Returns ``(warned_count, killed_count)`` for testability.
    """
    from ..db import SessionLocal

    warn_sec = _env_int("CHILI_DB_WATCHDOG_WARN_SEC", 300)
    kill_sec = _env_int("CHILI_DB_WATCHDOG_KILL_SEC", 1800)

    warned = 0
    killed = 0
    sess = SessionLocal()
    try:
        # Look at our own role's connections only — never touch other apps that
        # may share the cluster (postgres/superuser-managed jobs).
        rows = sess.execute(text(
            """
            SELECT pid,
                   EXTRACT(EPOCH FROM (now() - query_start))::bigint AS dur_s,
                   left(query, 120) AS q
            FROM pg_stat_activity
            WHERE state = 'idle in transaction'
              AND usename = current_user
              AND pid <> pg_backend_pid()
              AND query_start IS NOT NULL
              AND age(now(), query_start) > make_interval(secs => :warn_sec)
            """
        ), {"warn_sec": warn_sec}).fetchall()

        for r in rows:
            dur_s = int(r.dur_s or 0)
            if dur_s >= kill_sec:
                # Auto-terminate: log first so we have an audit trail even if
                # something downstream eats the kill result.
                logger.warning(
                    "[db_watchdog] terminating idle-in-tx pid=%s held for %ds (>= %d kill threshold) query=%s",
                    r.pid, dur_s, kill_sec, r.q,
                )
                try:
                    sess.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": r.pid})
                    sess.commit()
                    killed += 1
                except Exception as e:
                    logger.warning("[db_watchdog] pg_terminate_backend(%s) failed: %s", r.pid, e)
                    sess.rollback()
            else:
                logger.warning(
                    "[db_watchdog] idle-in-tx pid=%s held for %ds (warn threshold %d) query=%s",
                    r.pid, dur_s, warn_sec, r.q,
                )
                warned += 1
    finally:
        sess.close()

    return warned, killed


def _loop() -> None:
    poll_sec = max(15, _env_int("CHILI_DB_WATCHDOG_POLL_SEC", 60))
    logger.info("[db_watchdog] started: poll=%ds  warn>=%ds  kill>=%ds",
                poll_sec,
                _env_int("CHILI_DB_WATCHDOG_WARN_SEC", 300),
                _env_int("CHILI_DB_WATCHDOG_KILL_SEC", 1800))
    while not _stop_event.is_set():
        try:
            _poll_once()
        except Exception as e:
            # Never let the watchdog crash. Log + continue.
            logger.warning("[db_watchdog] poll raised (continuing): %s", e)
        # Sleep cooperatively so shutdown is fast.
        _stop_event.wait(timeout=poll_sec)


def start_watchdog() -> None:
    """Start the watchdog daemon thread (idempotent).

    Safe to call multiple times — second call is a no-op. Daemon=True so
    it never blocks process shutdown.
    """
    global _thread
    if not _env_flag("CHILI_DB_WATCHDOG_ENABLED", True):
        logger.info("[db_watchdog] disabled via CHILI_DB_WATCHDOG_ENABLED=0")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="db_watchdog", daemon=True)
    _thread.start()


def stop_watchdog() -> None:
    """Signal the watchdog to stop. Best-effort, daemon thread will exit on process exit anyway."""
    _stop_event.set()
