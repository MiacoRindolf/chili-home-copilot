import os
import sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DATA_DIR.mkdir(exist_ok=True)

# PostgreSQL only — DATABASE_URL validated in config (see .env.example).
DATABASE_URL = settings.database_url

# FIX 32 (deep audit 2026-04-28): set ``application_name`` on every PG
# connection so db_watchdog can identify which process owns the session
# and apply per-app kill thresholds. Without this, all sessions show
# blank application_name in pg_stat_activity and the watchdog can't
# distinguish a leaking chili API request from a legitimately long
# brain-worker reconcile pass — leading to FIX 5 killing the wrong
# sessions and triggering 'server closed the connection unexpectedly'.
#
# The brain-worker is launched via scripts/brain_worker.py (sys.argv[0]
# contains 'brain_worker'). Other invocations (FastAPI, scheduler,
# pytest, ad-hoc scripts) get a generic 'chili'. Override via
# CHILI_APP_NAME env var when needed.
_app_name = os.environ.get("CHILI_APP_NAME", "").strip()
if not _app_name:
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    if "brain_worker" in argv0:
        _app_name = "chili-brain-worker"
    elif "scheduler" in argv0 or os.environ.get("CHILI_SCHEDULER_ROLE") not in (None, "", "none"):
        # FIX 45a follow-up (2026-04-29): derive app_name from scheduler role
        # so per-container DB activity is distinguishable in pg_stat_activity.
        # Without this, autotrader-worker + broker-sync-worker + scheduler-
        # worker all show as "chili-scheduler" — defeats the whole point of
        # the container split for diagnostic purposes.
        _role = (os.environ.get("CHILI_SCHEDULER_ROLE") or "").strip().lower()
        if _role == "autotrader_only":
            _app_name = "chili-autotrader-worker"
        elif _role == "broker_sync_only":
            _app_name = "chili-broker-sync-worker"
        elif _role == "cron_only":
            _app_name = "chili-scheduler-cron"
        else:
            _app_name = "chili-scheduler"
    elif "pytest" in argv0:
        _app_name = "chili-pytest"
    else:
        _app_name = "chili-app"

# Process-pool queue workers set CHILI_MP_BACKTEST_CHILD before first db import (see backtest_queue_worker).
_mp_child = os.environ.get("CHILI_MP_BACKTEST_CHILD", "").strip().lower() in ("1", "true", "yes")
if _mp_child:
    _app_name = "chili-backtest-child"
_pool_size = (
    settings.brain_mp_child_database_pool_size
    if _mp_child
    else settings.database_pool_size
)
_max_overflow = (
    settings.brain_mp_child_database_max_overflow
    if _mp_child
    else settings.database_max_overflow
)

engine = create_engine(
    DATABASE_URL,
    pool_size=_pool_size,
    max_overflow=_max_overflow,
    pool_pre_ping=True,  # detect stale connections at checkout
    pool_recycle=3600,  # recycle at checkout if older than 1h
    # FIX 13+14 (deep audit 2026-04-28): keep-alives at the TCP level so a
    # long-running brain-worker learning cycle (~34min hold) can't have its
    # connection silently closed by the server. The brain-worker grabs a
    # session, runs through 20+ steps, and the snapshot SELECT (LIMIT 5000)
    # is the long pole — without keepalives, postgres closes the idle TCP
    # socket and the next query fails with 'server closed the connection
    # unexpectedly'. The cycle then rolls back, the predictions cache fails
    # to emit ('Promoted prediction cache at cycle end failed'), and the
    # next consumer reads cached_result_count=0.
    #
    # 30s keepalive_idle is well below typical net.ipv4.tcp_keepalive_time
    # (default 7200s on Linux) so we don't depend on OS defaults; 5s
    # keepalives_interval gives 6 keepalives before tcp_keepalives_count
    # gives up — the connection stays alive even when fully idle.
    connect_args={
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 5,
        "keepalives_count": 5,
        # FIX 32: tag every connection with our application_name so
        # db_watchdog can apply per-app kill thresholds.
        "application_name": _app_name,
    },
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

if _mp_child:
    import atexit

    def _dispose_mp_child_engine() -> None:
        # Return pooled connections to Postgres when a queue worker process exits (spawn child).
        try:
            engine.dispose()
        except Exception:
            pass

    atexit.register(_dispose_mp_child_engine)
