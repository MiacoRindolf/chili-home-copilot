import os
import sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

_TRUE_ENV_VALUES = frozenset({"1", "true", "yes"})
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off"})
_PG_KEEPALIVES_ENABLED = 1
_PG_KEEPALIVE_IDLE_SECONDS = 30
_PG_KEEPALIVE_INTERVAL_SECONDS = 5
_PG_KEEPALIVE_COUNT = 5
_SERVICE_POOL_CAPS_ENABLED_ENV = "CHILI_DATABASE_SERVICE_POOL_CAPS_ENABLED"
_SERVICE_RETAINED_POOL_CAPS: dict[str, int] = {
    # Keep resident pools small for long-lived, low-concurrency services. These
    # caps only shrink the steady-state pool; burst capacity is moved into
    # max_overflow so the service keeps the same peak checkout budget.
    "chili-app": 8,
    "chili-scheduler-cron": 8,
    "chili-scheduler": 6,
    "chili-autotrader-worker": 4,
    "chili-broker-sync-worker": 4,
    "chili-autotrader-runtime-gate": 2,
    "chili-kill-switch-reader": 2,
}

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
_DISCONNECT_ERROR_TOKENS = (
    "server closed the connection unexpectedly",
    "connection already closed",
    "connection not open",
    "could not receive data from server",
    "terminating connection due to administrator command",
    "ssl syscall error",
)


def _resolve_app_name(
    *,
    argv0: str | None = None,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> str:
    """Return the Postgres application_name for this process."""
    env = environ if environ is not None else os.environ
    explicit = (env.get("CHILI_APP_NAME", "") or "").strip()
    if explicit:
        return explicit

    argv0_s = (argv0 if argv0 is not None else ((sys.argv[0] if sys.argv else "") or ""))
    argv0_s = argv0_s.lower()
    scheduler_role_raw = env.get("CHILI_SCHEDULER_ROLE")
    scheduler_role = (scheduler_role_raw or "").strip().lower()
    if "brain_worker" in argv0_s:
        return "chili-brain-worker"
    if "scheduler" in argv0_s or scheduler_role not in ("", "none"):
        if scheduler_role == "autotrader_only":
            return "chili-autotrader-worker"
        if scheduler_role == "broker_sync_only":
            return "chili-broker-sync-worker"
        if scheduler_role == "cron_only":
            return "chili-scheduler-cron"
        return "chili-scheduler"
    if "pytest" in argv0_s:
        return "chili-pytest"
    return "chili-app"


def is_disconnect_error(exc: BaseException | str | None) -> bool:
    """True for DB connection-closure errors that need pool invalidation."""
    if exc is None:
        return False
    if getattr(exc, "connection_invalidated", False):
        return True
    parts = [type(exc).__name__, str(exc)]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts.append(str(orig))
    text = " ".join(parts).lower()
    return any(token in text for token in _DISCONNECT_ERROR_TOKENS)


def recover_session_after_db_error(
    session,
    exc: BaseException | str | None,
    *,
    logger=None,
    context: str = "database session",
) -> str:
    """Rollback and invalidate a SQLAlchemy session after connection loss."""
    disconnected = is_disconnect_error(exc)
    rollback_ok = True
    try:
        session.rollback()
    except Exception:
        rollback_ok = False
        if logger is not None:
            logger.debug("%s rollback during DB recovery failed", context, exc_info=True)

    if disconnected:
        try:
            session.invalidate()
            if logger is not None:
                logger.warning("%s invalidated SQLAlchemy session after DB disconnect", context)
            return "invalidated"
        except Exception:
            if logger is not None:
                logger.debug("%s session invalidation failed", context, exc_info=True)
            return "invalidate_failed"

    return "rolled_back" if rollback_ok else "rollback_failed"


_app_name = _resolve_app_name()


def _is_true_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_ENV_VALUES


def _is_pytest_process() -> bool:
    return _is_true_env("CHILI_PYTEST") or "pytest" in ((sys.argv[0] if sys.argv else "") or "").lower()


def _service_pool_caps_enabled(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> bool:
    env = environ if environ is not None else os.environ
    raw = (env.get(_SERVICE_POOL_CAPS_ENABLED_ENV, "") or "").strip().lower()
    return raw not in _FALSE_ENV_VALUES


def _apply_service_pool_cap(
    pool_size: int,
    max_overflow: int,
    *,
    app_name: str | None,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[int, int]:
    if not app_name or not _service_pool_caps_enabled(environ):
        return pool_size, max_overflow
    cap_pool = _SERVICE_RETAINED_POOL_CAPS.get(app_name)
    if cap_pool is None or pool_size <= cap_pool:
        return pool_size, max_overflow
    released_slots = pool_size - cap_pool
    return max(1, cap_pool), max(0, max_overflow + released_slots)


def _resolve_pool_config(
    settings_obj,
    *,
    mp_child: bool,
    pytest_process: bool,
    app_name: str | None = None,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[int, int, float]:
    if mp_child:
        return (
            int(settings_obj.brain_mp_child_database_pool_size),
            int(settings_obj.brain_mp_child_database_max_overflow),
            float(settings_obj.database_pool_timeout_seconds),
        )
    if pytest_process:
        return (
            min(int(settings_obj.database_pool_size), int(settings_obj.database_pytest_pool_size)),
            min(int(settings_obj.database_max_overflow), int(settings_obj.database_pytest_max_overflow)),
            float(settings_obj.database_pytest_pool_timeout_seconds),
        )
    pool_size, max_overflow = _apply_service_pool_cap(
        int(settings_obj.database_pool_size),
        int(settings_obj.database_max_overflow),
        app_name=app_name,
        environ=environ,
    )
    return (
        pool_size,
        max_overflow,
        float(settings_obj.database_pool_timeout_seconds),
    )


# Process-pool queue workers set CHILI_MP_BACKTEST_CHILD before first db import (see backtest_queue_worker).
_mp_child = _is_true_env("CHILI_MP_BACKTEST_CHILD")
if _mp_child:
    _app_name = "chili-backtest-child"
_pytest_process = _is_pytest_process()
_pool_size, _max_overflow, _pool_timeout = _resolve_pool_config(
    settings,
    mp_child=_mp_child,
    pytest_process=_pytest_process,
    app_name=_app_name,
)
_connect_options: list[str] = []
_idle_xact_timeout_ms = int(settings.database_idle_in_transaction_timeout_ms)
if _idle_xact_timeout_ms > 0:
    _connect_options.extend(
        [
            "-c",
            f"idle_in_transaction_session_timeout={_idle_xact_timeout_ms}",
        ]
    )
_connect_args = {
    "keepalives": _PG_KEEPALIVES_ENABLED,
    "keepalives_idle": _PG_KEEPALIVE_IDLE_SECONDS,
    "keepalives_interval": _PG_KEEPALIVE_INTERVAL_SECONDS,
    "keepalives_count": _PG_KEEPALIVE_COUNT,
    # FIX 32: tag every connection with our application_name so
    # db_watchdog can apply per-app kill thresholds.
    "application_name": _app_name,
}
if _connect_options:
    _connect_args["options"] = " ".join(_connect_options)

engine = create_engine(
    DATABASE_URL,
    pool_size=_pool_size,
    max_overflow=_max_overflow,
    pool_timeout=float(_pool_timeout),
    pool_use_lifo=True,
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
    # The keepalive constants above are well below typical OS defaults, so
    # long-running background work does not depend on host TCP settings.
    connect_args=_connect_args,
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
