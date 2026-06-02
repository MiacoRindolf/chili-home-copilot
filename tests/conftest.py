"""Shared fixtures for CHILI tests.

Requires PostgreSQL: set ``TEST_DATABASE_URL`` or ``DATABASE_URL`` to a
*dedicated* database (e.g. ``chili_test``) before running pytest. See
``docs/DATABASE_POSTGRES.md``.

``DATABASE_URL`` is set from ``TEST_DATABASE_URL`` when present. Schema is
applied the first time a test uses the ``db`` fixture, or when ``app.main``
loads for ``client``. Pure unit tests with no DB fixture skip DB setup.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient


_PYTEST_DB_LOCK_CLASSID = 0x4348494C
_PYTEST_DB_LOCK_OBJID = 0x54455354
_pytest_db_lock_engine = None
_pytest_db_slot_lock_file = None
_pytest_db_slot_id = None
_schema_initialized = False
_db_stack_loaded = False
_test_database_ready = False
_isolated_test_database_created = False
_isolated_test_database_cloned_from_base = False

Base = None
engine = None
get_db = None
User = None
Device = None
DEVICE_COOKIE_NAME = "chili_device_token"


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _hydrate_test_database_url_from_dotenv() -> None:
    """If ``TEST_DATABASE_URL`` is not in the process environment, read it from repo ``.env``.

    We intentionally do **not** load ``DATABASE_URL`` from ``.env`` here: that often points at
    the dev ``chili`` database, and pytest truncates tables between tests.
    """
    if os.environ.get("TEST_DATABASE_URL", "").strip():
        return
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    vals = dotenv_values(env_path)
    tdu = (vals.get("TEST_DATABASE_URL") or "").strip()
    if tdu:
        os.environ["TEST_DATABASE_URL"] = tdu


def _ensure_postgres_test_url() -> str:
    # Safety: NEVER fall back to DATABASE_URL. A missing TEST_DATABASE_URL must be a hard error
    # so pytest can never truncate the live `chili` database (this wiped app data on 2026-04-18).
    _hydrate_test_database_url_from_dotenv()
    raw = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if not raw:
        raise RuntimeError(
            "Tests require TEST_DATABASE_URL pointing at a dedicated test database (e.g. "
            "postgresql://chili:chili@localhost:5433/chili_test). Set it in your shell or .env. "
            "DATABASE_URL is intentionally not a fallback — see docs/DATABASE_POSTGRES.md."
        )
    lowered = raw.lower()
    if not (
        lowered.startswith("postgresql://")
        or lowered.startswith("postgresql+psycopg2://")
        or lowered.startswith("postgresql+psycopg://")
    ):
        raise RuntimeError("TEST_DATABASE_URL must be a PostgreSQL URL for pytest.")
    # Extract database name (last path segment, before any ?query).
    try:
        db_name = raw.rsplit("/", 1)[-1].split("?", 1)[0].strip().lower()
    except Exception:
        db_name = ""
    if not db_name.endswith("_test"):
        raise RuntimeError(
            f"Refusing to run pytest against database {db_name!r}: the TEST_DATABASE_URL database "
            "name must end with '_test' (e.g. chili_test). This guard prevents accidental TRUNCATE "
            "of the live chili database."
        )
    return raw


def _pytest_uses_shared_database() -> bool:
    isolation = os.environ.get("CHILI_PYTEST_DB_ISOLATION", "").strip().lower()
    return _truthy_env("CHILI_PYTEST_SHARED_DB") or isolation == "shared"


def _database_url_with_name(raw_url: str, database_name: str) -> str:
    url = make_url(raw_url)
    return url.set(database=database_name).render_as_string(hide_password=False)


def _admin_database_url(base_url: str | None = None) -> str:
    admin_database = os.environ.get("CHILI_PYTEST_ADMIN_DATABASE", "postgres").strip()
    return _database_url_with_name(base_url or _BASE_TEST_DATABASE_URL, admin_database or "postgres")


def _safe_database_name_parts(base_url: str) -> str:
    base_name = make_url(base_url).database or "chili_test"
    safe_base = re.sub(r"[^A-Za-z0-9_]", "_", base_name)
    if not re.match(r"^[A-Za-z_]", safe_base):
        safe_base = f"t_{safe_base}"
    return safe_base


def _database_name_with_suffix(base_url: str, suffix: str) -> str:
    safe_base = _safe_database_name_parts(base_url)
    max_base_len = max(1, 63 - len(suffix) - 1)
    return f"{safe_base[:max_base_len]}_{suffix}"


def _ephemeral_database_name(base_url: str) -> str:
    worker = re.sub(
        r"[^A-Za-z0-9_]",
        "_",
        os.environ.get("PYTEST_XDIST_WORKER", "solo"),
    )
    suffix = f"pytest_{worker}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    return _database_name_with_suffix(base_url, suffix)


def _pooled_database_name(base_url: str, slot_id: int) -> str:
    return _database_name_with_suffix(base_url, f"pytest_slot_{slot_id}")


def _try_lock_slot_file(slot_lock_file) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(slot_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(slot_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_slot_file(slot_lock_file) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            slot_lock_file.seek(0)
            msvcrt.locking(slot_lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return

    import fcntl

    try:
        fcntl.flock(slot_lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _acquire_pooled_database_name(base_url: str) -> str | None:
    global _pytest_db_slot_id, _pytest_db_slot_lock_file
    if _truthy_env("CHILI_PYTEST_EPHEMERAL_DB"):
        return None
    slot_count = max(0, int(os.environ.get("CHILI_PYTEST_DB_POOL_SIZE", "8")))
    if slot_count <= 0:
        return None
    lock_dir = Path(__file__).resolve().parents[1] / ".pytest_cache" / "db-slots"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    for slot_id in range(slot_count):
        slot_lock_file = open(lock_dir / f"slot-{slot_id}.lock", "a+b")
        if _try_lock_slot_file(slot_lock_file):
            _pytest_db_slot_lock_file = slot_lock_file
            _pytest_db_slot_id = slot_id
            return _pooled_database_name(base_url, slot_id)
        slot_lock_file.close()
    return None


def _isolated_database_name(base_url: str) -> str:
    return _acquire_pooled_database_name(base_url) or _ephemeral_database_name(base_url)


def _configure_pytest_database_url(base_url: str) -> str:
    os.environ.setdefault("CHILI_PYTEST_BASE_DATABASE_URL", base_url)
    if _pytest_uses_shared_database():
        os.environ["DATABASE_URL"] = base_url
        os.environ["TEST_DATABASE_URL"] = base_url
        return base_url

    db_name = _isolated_database_name(base_url)
    isolated_url = _database_url_with_name(base_url, db_name)
    os.environ["CHILI_PYTEST_ISOLATED_DB_NAME"] = db_name
    os.environ["DATABASE_URL"] = isolated_url
    os.environ["TEST_DATABASE_URL"] = isolated_url
    return isolated_url

# Skip heavy / lock-prone module-level pattern seeding in app.main when the test client
# imports the app (see app.main). Pytest truncates tables per test; momentum tests seed
# variants via ensure_momentum_strategy_variants, not ScanPattern builtins.
os.environ["CHILI_PYTEST"] = "1"
# Avoid APScheduler + most deferred-startup DB maintenance during TestClient runs (reduces
# concurrent DB sessions when pytest shares a dev database with Docker Compose).
os.environ.setdefault("CHILI_SCHEDULER_ROLE", "none")
_BASE_TEST_DATABASE_URL = _ensure_postgres_test_url()
_CONFIGURED_TEST_DATABASE_URL = _configure_pytest_database_url(_BASE_TEST_DATABASE_URL)

_AGENT_DEBUG_LOG = Path(__file__).resolve().parents[1] / "debug-42a690.log"


def _agent_ndjson(*, hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    # #region agent log
    payload = {
        "sessionId": "42a690",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "timestamp": int(time.time() * 1000),
        "data": data or {},
    }
    try:
        with open(_AGENT_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        pass
    # #endregion


def _validate_database_identifier(db_name: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", db_name):
        raise RuntimeError(f"Refusing unsafe pytest database name: {db_name!r}")


def _quoted_database_identifier(db_name: str) -> str:
    _validate_database_identifier(db_name)
    return f'"{db_name}"'


def _ensure_test_database_ready() -> None:
    """Create the per-process pytest database lazily, only for DB-backed tests."""
    global _isolated_test_database_cloned_from_base, _isolated_test_database_created
    global _test_database_ready
    if _test_database_ready:
        return
    if _pytest_uses_shared_database():
        _test_database_ready = True
        return

    db_name = make_url(_CONFIGURED_TEST_DATABASE_URL).database or ""
    base_db_name = make_url(_BASE_TEST_DATABASE_URL).database or ""
    if db_name == base_db_name:
        raise RuntimeError("Isolated pytest database resolved to the shared base database.")
    quoted_db_name = _quoted_database_identifier(db_name)
    quoted_base_db_name = _quoted_database_identifier(base_db_name)
    admin_engine = create_engine(
        _admin_database_url(),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
        future=True,
    )
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db_name"),
                {"db_name": db_name},
            ).scalar()
            if not exists:
                try:
                    conn.execute(
                        text(
                            f"CREATE DATABASE {quoted_db_name} "
                            f"TEMPLATE {quoted_base_db_name}"
                        )
                    )
                    _isolated_test_database_cloned_from_base = True
                except Exception:
                    conn.execute(text(f"CREATE DATABASE {quoted_db_name} TEMPLATE template0"))
                    _isolated_test_database_cloned_from_base = False
                _isolated_test_database_created = True
    finally:
        admin_engine.dispose()
    _test_database_ready = True


def _ensure_app_db_loaded() -> None:
    """Load app DB globals after pytest has selected the correct database URL."""
    global Base, Device, DEVICE_COOKIE_NAME, User, _db_stack_loaded, engine, get_db
    if _db_stack_loaded:
        return
    _ensure_test_database_ready()
    from app.db import Base as app_base
    from app.db import engine as app_engine
    from app.deps import get_db as app_get_db
    from app.models import Device as app_device
    from app.models import User as app_user
    from app.pairing import DEVICE_COOKIE_NAME as app_device_cookie_name

    Base = app_base
    engine = app_engine
    get_db = app_get_db
    User = app_user
    Device = app_device
    DEVICE_COOKIE_NAME = app_device_cookie_name
    _db_stack_loaded = True


def _dispose_app_engine_if_loaded() -> None:
    if _pytest_db_lock_engine is not None:
        try:
            _pytest_db_lock_engine.dispose()
        except Exception:
            pass
    if engine is not None:
        try:
            engine.dispose()
        except Exception:
            pass


def _release_pooled_database_slot() -> None:
    global _pytest_db_slot_id, _pytest_db_slot_lock_file
    if _pytest_db_slot_lock_file is not None:
        try:
            _unlock_slot_file(_pytest_db_slot_lock_file)
        except Exception:
            pass
        try:
            _pytest_db_slot_lock_file.close()
        except Exception:
            pass
    _pytest_db_slot_lock_file = None
    _pytest_db_slot_id = None


def pytest_sessionfinish(session, exitstatus) -> None:
    """Drop the temporary pytest database after the process is done."""
    if _pytest_uses_shared_database():
        _dispose_app_engine_if_loaded()
        _release_pooled_database_slot()
        return
    should_drop_database = (
        _pytest_db_slot_id is None
        or _truthy_env("CHILI_PYTEST_DROP_ISOLATED_DB")
    )
    if not _isolated_test_database_created or not should_drop_database:
        _dispose_app_engine_if_loaded()
        _release_pooled_database_slot()
        return

    db_name = make_url(_CONFIGURED_TEST_DATABASE_URL).database or ""
    quoted_db_name = _quoted_database_identifier(db_name)
    _dispose_app_engine_if_loaded()
    admin_engine = create_engine(
        _admin_database_url(),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
        future=True,
    )
    try:
        with admin_engine.connect() as conn:
            drop_timeout_ms = max(
                1000,
                int(os.environ.get("CHILI_PYTEST_DROP_TIMEOUT_MS", "5000")),
            )
            conn.execute(text(f"SET statement_timeout = {drop_timeout_ms}"))
            conn.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = :db_name
                      AND pid <> pg_backend_pid()
                    """
                ),
                {"db_name": db_name},
            )
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted_db_name}"))
    except Exception:
        pass
    finally:
        admin_engine.dispose()
        _release_pooled_database_slot()


_PROJECT_DOMAIN_TARGETED_TABLES = frozenset(
    {
        "users",
        "devices",
        "projects",
        "project_files",
        "conversations",
        "messages",
        "plan_projects",
        "project_members",
        "plan_tasks",
        "task_comments",
        "task_activities",
        "plan_labels",
        "task_labels",
        "task_watchers",
        "plan_task_coding_profile",
        "task_clarification",
        "coding_task_brief",
        "coding_task_validation_run",
        "coding_validation_artifact",
        "coding_agent_suggestion",
        "coding_agent_suggestion_apply",
        "coding_blocker_report",
        "code_repos",
        "code_insights",
        "code_snapshots",
        "code_hotspots",
        "code_learning_events",
        "code_dependencies",
        "code_quality_snapshots",
        "code_reviews",
        "code_dep_alerts",
        "code_search_index",
        "project_agent_states",
        "agent_findings",
        "agent_research",
        "agent_goals",
        "agent_evolution",
        "agent_messages",
        "po_questions",
        "po_requirements",
        "qa_test_cases",
        "qa_test_runs",
        "qa_bug_reports",
        "project_domain_runs",
        "project_analysis_snapshots",
        "project_autonomy_agent_profiles",
        "project_autonomy_agent_schedules",
        "project_autonomy_delegations",
        "project_autonomy_operator_questions",
        "project_autonomy_runs",
        "project_autonomy_messages",
        "project_autonomy_steps",
        "project_autonomy_artifacts",
        "project_autonomy_architect_reviews",
        "project_autonomy_leases",
        "project_autonomy_learning_samples",
    }
)
_PROJECT_DOMAIN_TARGETED_TESTS = (
    "test_planner_coding",
    "test_brain_page_domain.py",
    "test_brain_http_domain.py",
    "test_brain_project_",
    "test_projects.py",
    "test_code_agent.py",
)
_TRADING_DOMAIN_BASE_TARGETED_TABLES = frozenset(
    {
        "users",
        "devices",
        "brain_work_events",
        "broker_credentials",
        "broker_sessions",
        "pattern_evidence_corrections",
        "scan_patterns",
    }
)
_TRADING_DOMAIN_TARGETED_TABLES_CACHE = None
_POSITION_SIZER_EMITTER_TARGETED_TABLES = frozenset(
    {
        "trading_position_sizer_log",
        "trading_trades",
    }
)
_TRADING_DOMAIN_TARGETED_TESTS = (
    "test_alerts_options_skip.py",
    "test_alpha_portfolio_gate.py",
    "test_auto_trader_synergy.py",
    "test_auto_trader_safety.py",
    "test_auto_trader_monitor.py",
    "test_autotrader_desk_api.py",
    "test_autotrader_deployment_report.py",
    "test_autotrader_deployment_unblock.py",
    "test_autotrader_payoff_sizing.py",
    "test_autotrader_pdt_soft_warn.py",
    "test_autotrader_position_overrides.py",
    "test_broker_sync.py",
    "test_broker_truth_safety.py",
    "test_bracket_reconciliation_service.py",
    "test_bracket_reconciler_hardening.py",
    "test_brain_work_ledger.py",
    "test_canonical_outcome_layer.py",
    "test_cash_deployment.py",
    "test_coinbase_zero_fill_guard.py",
    "test_composite_reweight.py",
    "test_cpcv_promotion_gate.py",
    "test_crypto_exit_monitor_pattern_exit_now.py",
    "test_governance_daily_loss.py",
    "test_edge_aware_evolution.py",
    "test_edge_reliability.py",
    "test_edge_reliability_exit_noop_policy.py",
    "test_edge_reliability_exit_variant_noop.py",
    "test_edge_reliability_recert_policy.py",
    "test_emergency_liquidation_no_quote.py",
    "test_market_data_dead_cache_fallback.py",
    "test_monitor_api_execution_state.py",
    "test_paper_shadow_mode.py",
    "test_phase3_stop_bleed.py",
    "test_portfolio_options_close.py",
    "test_portfolio_risk_options_mtm.py",
    "test_pattern_directional_outcome.py",
    "test_pattern_cohort_promote.py",
    "test_pattern_imminent_alerts.py",
    "test_research_shadow_fastlane.py",
    "test_recert_rescue_signal_tickers.py",
    "test_stop_engine_options_auto_exec.py",
    "test_stuck_order_watchdog.py",
    "test_trade_assign_pattern.py",
    "test_trades_api_broker_truth.py",
    "test_trades_sync.py",
    "test_trading_decision_stack.py",
    "test_venue_robinhood_adapter.py",
)
_TRADING_DEFAULT_USER_TESTS = (
    "test_pattern_imminent_alerts.py",
    "test_signal_to_reconcile_e2e.py",
    "test_trade_assign_pattern.py",
    "test_trades_sync.py",
)


def _database_has_current_app_schema() -> bool:
    from app.migrations import MIGRATIONS

    latest_migration = MIGRATIONS[-1][0] if MIGRATIONS else None
    if latest_migration is None:
        return False
    try:
        with engine.connect() as conn:
            return bool(
                conn.execute(text("SELECT to_regclass('public.users')")).scalar()
                and conn.execute(text("SELECT to_regclass('public.schema_version')")).scalar()
                and conn.execute(
                    text(
                        "SELECT 1 FROM schema_version "
                        "WHERE version_id = :latest_migration"
                    ),
                    {"latest_migration": latest_migration},
                ).scalar()
            )
    except Exception:
        return False


def _bootstrap_test_schema() -> None:
    """Create tables and run versioned migrations (idempotent)."""
    global _schema_initialized
    if _schema_initialized:
        return
    _ensure_app_db_loaded()
    if (
        not _pytest_uses_shared_database()
        and not _truthy_env("CHILI_PYTEST_FORCE_SCHEMA_BOOTSTRAP")
        and _database_has_current_app_schema()
    ):
        _schema_initialized = True
        return
    Base.metadata.create_all(bind=engine)
    from app.migrations import run_migrations

    run_migrations(engine)
    _schema_initialized = True


@pytest.fixture(scope="session")
def fastapi_app():
    """Load FastAPI app once per pytest session (heavy: migrations + seeds)."""
    global _schema_initialized
    import sys

    _ensure_test_database_ready()
    sys.stderr.write(
        "pytest: loading app.main (routers; schema via db fixture when CHILI_PYTEST=1)...\n"
    )
    sys.stderr.flush()
    from app.main import app as _app

    return _app


@pytest.fixture(scope="session")
def _asgi_test_client(fastapi_app):
    """Single Starlette TestClient for the whole session (Windows: avoids WinError 10055).

    Opening ``with TestClient(app)`` per test spawns a new asyncio loop + socketpair each
    time; under load Windows can exhaust ephemeral buffers (10055).
    """
    # #region agent log
    _agent_ndjson(
        hypothesis_id="H1",
        location="conftest.py:_asgi_test_client",
        message="session TestClient __enter__ (expect once per pytest session)",
        data={},
    )
    # #endregion
    with TestClient(fastapi_app) as c:
        # #region agent log
        _agent_ndjson(
            hypothesis_id="H1",
            location="conftest.py:_asgi_test_client",
            message="session TestClient entered OK",
            data={},
        )
        # #endregion
        yield c


def _evict_idle_in_transaction_peers() -> None:
    """Pytest only: end *idle-in-transaction* client backends (not all peers).

    Broad ``pg_terminate_backend`` on every client wipes pooled connections and can
    destabilize Postgres when many short-lived poolers reconnect. Only IIT sessions
    typically block ``TRUNCATE ... CASCADE`` indefinitely.
    """
    if not (os.environ.get("CHILI_PYTEST") or "").strip():
        return
    _ensure_app_db_loaded()
    import sys

    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT pid, pg_terminate_backend(pid) AS killed "
                    "FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() "
                    "AND backend_type = 'client backend' "
                    "AND state = 'idle in transaction'"
                )
            ).fetchall()
        nk = sum(1 for _pid, killed in rows if killed)
        if nk:
            sys.stderr.write(
                "pytest: terminated %d idle-in-transaction peer session(s)\n" % nk
            )
            sys.stderr.flush()
    except Exception:
        pass


def _truncate_lock_recoverable(exc: BaseException) -> bool:
    """True when TRUNCATE failed due to PostgreSQL lock timeout / contention."""
    orig = getattr(exc, "orig", None)
    if orig is not None and orig.__class__.__name__ == "LockNotAvailable":
        return True
    low = str(exc).lower()
    return "locknotavailable" in low.replace(" ", "") or "lock timeout" in low


def _terminate_stale_truncate_peers(max_age_s: int = 90) -> None:
    """Kill stale active TRUNCATE sessions left behind by timed-out pytest runs."""
    if not (os.environ.get("CHILI_PYTEST") or "").strip():
        return
    _ensure_app_db_loaded()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND backend_type = 'client backend'
                      AND state = 'active'
                      AND query ILIKE 'TRUNCATE %'
                      AND now() - query_start > make_interval(secs => :max_age_s)
                    """
                ),
                {"max_age_s": int(max_age_s)},
            )
    except Exception:
        pass


def _terminate_stale_pytest_lock_holders(max_age_s: int = 90) -> None:
    """Kill stale pytest advisory-lock holders from timed-out local runs.

    The DB fixture serializes destructive cleanup with a session-level advisory
    lock. A killed pytest process can leave its backend alive briefly; plain
    ``pg_advisory_lock`` then waits forever and makes the next focused test look
    hung. This only targets our dedicated pytest lock in the current test DB.
    """
    if not (os.environ.get("CHILI_PYTEST") or "").strip():
        return
    _ensure_app_db_loaded()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    SELECT pg_terminate_backend(a.pid)
                    FROM pg_locks AS l
                    JOIN pg_stat_activity AS a ON a.pid = l.pid
                    WHERE l.locktype = 'advisory'
                      AND l.classid = :classid
                      AND l.objid = :objid
                      AND l.granted
                      AND a.datname = current_database()
                      AND a.pid <> pg_backend_pid()
                      AND now() - COALESCE(a.xact_start, a.query_start, a.backend_start)
                          > make_interval(secs => :max_age_s)
                    """
                ),
                {
                    "classid": _PYTEST_DB_LOCK_CLASSID,
                    "objid": _PYTEST_DB_LOCK_OBJID,
                    "max_age_s": int(max_age_s),
                },
            )
    except Exception:
        pass


def _truncate_relation_names(conn, logical_names: list[str]) -> list[str]:
    """Map ORM logical names to physical relations for TRUNCATE.

    Position-identity Phase 5H turns ``trading_trades`` into a simple
    compatibility view over the physical ``trading_management_envelopes`` table.
    PostgreSQL can DELETE through that view, but cannot TRUNCATE it. Full pytest
    cleanup therefore truncates the physical table when the rename is present.
    """
    if "trading_trades" not in logical_names:
        return logical_names
    try:
        rows = conn.execute(text("""
            SELECT c.relname, c.relkind
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = ANY(current_schemas(false))
               AND c.relname IN ('trading_trades', 'trading_management_envelopes')
        """)).fetchall()
        kinds = {str(row[0]): str(row[1]) for row in rows}
    except Exception:
        return logical_names
    if kinds.get("trading_trades") == "v" and kinds.get("trading_management_envelopes") == "r":
        return [
            "trading_management_envelopes" if name == "trading_trades" else name
            for name in logical_names
        ]
    return logical_names


def _metadata_tables() -> list:
    _ensure_app_db_loaded()
    return list(Base.metadata.tables.values())


def _metadata_sorted_tables() -> list:
    _ensure_app_db_loaded()
    import warnings

    from sqlalchemy.exc import SAWarning

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Cannot correctly sort tables;.*",
            category=SAWarning,
        )
        return list(Base.metadata.sorted_tables)


def _targeted_users_delete_sql(table_names: frozenset[str]):
    """Delete only users that are not protected by out-of-scope NO ACTION FKs."""
    _ensure_app_db_loaded()
    blockers: list[str] = []
    for table in _metadata_tables():
        if table.name in table_names:
            continue
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name != "users":
                    continue
                if (fk.ondelete or "").upper() in {"CASCADE", "SET NULL"}:
                    continue
                blockers.append(
                    f'NOT EXISTS (SELECT 1 FROM "{table.name}" AS child '
                    f'WHERE child."{column.name}" = u."id")'
                )
    predicate = " AND ".join(blockers) if blockers else "TRUE"
    return text(f'DELETE FROM "users" AS u WHERE {predicate}')


def _truncate_app_tables(table_names: frozenset[str] | None = None) -> None:
    """Remove row data between tests; keep schema_version so migrations are not re-run."""
    _ensure_app_db_loaded()
    # Static neural mesh topology is seeded by migration 086; keep nodes/edges so tests
    # do not need to re-seed the graph definition every time.
    _skip_truncate = frozenset({"schema_version", "brain_graph_nodes", "brain_graph_edges"})
    if table_names is not None:
        _evict_idle_in_transaction_peers()
        _terminate_stale_truncate_peers()
        with engine.begin() as conn:
            targeted_tables = [
                table
                for table in reversed(_metadata_sorted_tables())
                if table.name not in _skip_truncate and table.name in table_names
            ]
            for table in targeted_tables:
                if table.name == "users":
                    continue
                conn.execute(text(f'DELETE FROM "{table.name}"'))
            for table in targeted_tables:
                if table.name in _skip_truncate or table.name not in table_names:
                    continue
                if table.name == "users":
                    conn.execute(_targeted_users_delete_sql(table_names))
        return
    logical_names = [
        t.name
        for t in _metadata_tables()
        if t.name not in _skip_truncate and (table_names is None or t.name in table_names)
    ]
    if not logical_names:
        return
    attempts = max(1, int(os.environ.get("CHILI_PYTEST_TRUNCATE_ATTEMPTS", "6")))
    lock_s = max(30, int(os.environ.get("CHILI_PYTEST_LOCK_TIMEOUT_S", "120")))
    for attempt in range(attempts):
        _evict_idle_in_transaction_peers()
        _terminate_stale_truncate_peers()
        try:
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL lock_timeout = '{lock_s}s'"))
                names = [f'"{name}"' for name in _truncate_relation_names(conn, logical_names)]
                stmt = text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE")
                conn.execute(stmt)
            # #region agent log
            if attempt:
                _agent_ndjson(
                    hypothesis_id="H5",
                    location="conftest.py:_truncate_app_tables",
                    message="TRUNCATE succeeded after retry",
                    data={"attempt": attempt + 1},
                )
            # #endregion
            return
        except OperationalError as e:
            if not _truncate_lock_recoverable(e) or attempt + 1 >= attempts:
                raise
            # #region agent log
            _agent_ndjson(
                hypothesis_id="H5",
                location="conftest.py:_truncate_app_tables",
                message="TRUNCATE lock contention, will retry",
                data={"attempt": attempt + 1, "max": attempts},
            )
            # #endregion
            time.sleep(0.4 * (attempt + 1))


def _test_prefers_targeted_cleanup(request) -> bool:
    try:
        name = Path(str(request.node.fspath)).name.lower()
    except Exception:
        return False
    return any(token in name for token in _PROJECT_DOMAIN_TARGETED_TESTS)


def _trading_domain_targeted_tables() -> frozenset[str]:
    global _TRADING_DOMAIN_TARGETED_TABLES_CACHE
    if _TRADING_DOMAIN_TARGETED_TABLES_CACHE is None:
        _ensure_app_db_loaded()
        _TRADING_DOMAIN_TARGETED_TABLES_CACHE = frozenset(
            {
                *_TRADING_DOMAIN_BASE_TARGETED_TABLES,
                *(
                    table.name
                    for table in _metadata_tables()
                    if table.name.startswith(("trading_", "fast_path_", "momentum_"))
                ),
            }
        )
    return _TRADING_DOMAIN_TARGETED_TABLES_CACHE


def _test_targeted_cleanup_tables(request) -> frozenset[str] | None:
    try:
        name = Path(str(request.node.fspath)).name.lower()
    except Exception:
        return None
    if any(token in name for token in _PROJECT_DOMAIN_TARGETED_TESTS):
        return _PROJECT_DOMAIN_TARGETED_TABLES
    if name == "test_position_sizer_emitter.py":
        return _POSITION_SIZER_EMITTER_TARGETED_TABLES
    if name in _TRADING_DOMAIN_TARGETED_TESTS:
        return _trading_domain_targeted_tables()
    return None


def _test_needs_default_trading_users(request) -> bool:
    try:
        name = Path(str(request.node.fspath)).name.lower()
    except Exception:
        return False
    return name in _TRADING_DEFAULT_USER_TESTS


def _seed_default_trading_users() -> None:
    _ensure_app_db_loaded()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO users (id, name)
                VALUES (1, 'Trading Test User'), (99, 'Trading Wrong User')
                ON CONFLICT (id) DO NOTHING
                """
            )
        )
        conn.execute(
            text(
                """
                SELECT setval(
                    pg_get_serial_sequence('users', 'id'),
                    GREATEST((SELECT COALESCE(MAX(id), 0) FROM users), 1),
                    true
                )
                """
            )
        )


def _reset_trading_test_process_state() -> None:
    """Reset in-memory trading safety latches between DB-isolated tests."""
    try:
        from app.services.trading import portfolio_risk

        portfolio_risk._breaker_tripped = False
        portfolio_risk._breaker_reason = None
    except Exception:
        pass


@contextmanager
def _pytest_db_isolation_lock():
    """Serialize DB-backed tests that share the dedicated pytest database.

    The fixture truncates most application tables before each DB test. Running
    multiple pytest processes against the same ``*_test`` database can deadlock
    when two sessions try to TRUNCATE/DELETE overlapping FK graphs at once.
    Hold a session-level advisory lock across setup and the test body so DB
    tests are isolated even when focused pytest commands overlap.
    """
    if os.environ.get("CHILI_PYTEST_DB_LOCK_DISABLED", "").lower() in {"1", "true", "yes"}:
        yield
        return

    lock_engine = _get_pytest_db_lock_engine()
    conn = lock_engine.connect()
    locked = False
    try:
        wait_s = max(5, int(os.environ.get("CHILI_PYTEST_DB_LOCK_WAIT_S", "30")))
        stale_after_s = max(5, int(os.environ.get("CHILI_PYTEST_DB_LOCK_STALE_S", "90")))
        deadline = time.monotonic() + wait_s
        last_reap = 0.0
        while True:
            locked = bool(
                conn.execute(
                    text("SELECT pg_try_advisory_lock(:classid, :objid)"),
                    {
                        "classid": _PYTEST_DB_LOCK_CLASSID,
                        "objid": _PYTEST_DB_LOCK_OBJID,
                    },
                ).scalar()
            )
            conn.commit()
            if locked:
                break
            now = time.monotonic()
            if now - last_reap >= 5.0:
                _terminate_stale_pytest_lock_holders(max_age_s=stale_after_s)
                last_reap = now
            if now >= deadline:
                raise TimeoutError(
                    "Timed out waiting for pytest DB advisory lock. "
                    "Another pytest process is probably still using the shared "
                    "test database; stale holders are now reaped automatically "
                    "after CHILI_PYTEST_DB_LOCK_STALE_S seconds."
                )
            time.sleep(0.25)
        yield
    finally:
        if locked:
            try:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:classid, :objid)"),
                    {
                        "classid": _PYTEST_DB_LOCK_CLASSID,
                        "objid": _PYTEST_DB_LOCK_OBJID,
                    },
                )
                conn.commit()
            except Exception:
                conn.rollback()
        conn.close()


def _get_pytest_db_lock_engine():
    """Reuse the pytest advisory-lock socket instead of opening one per test.

    Windows can temporarily exhaust local TCP ports when a focused DB suite
    rapidly creates short-lived PostgreSQL connections. A one-connection pool
    preserves the session-level advisory lock behavior while avoiding socket
    churn between tests.
    """
    global _pytest_db_lock_engine
    _ensure_app_db_loaded()
    if _pytest_db_lock_engine is None:
        _pytest_db_lock_engine = create_engine(
            engine.url.render_as_string(hide_password=False),
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=True,
            future=True,
        )
    return _pytest_db_lock_engine


@pytest.fixture()
def db(request):
    """Yield a DB session; tables are truncated at test start.

    We do not TRUNCATE again in ``finally``: the session-scoped ASGI ``TestClient`` keeps
    the app lifespan open; post-test truncate races request/engine cleanup and caused
    teardown errors (lock timeout) after PASSED.
    """
    _ensure_test_database_ready()
    _ensure_app_db_loaded()
    lock_cm = _pytest_db_isolation_lock() if _pytest_uses_shared_database() else nullcontext()
    with lock_cm:
        _bootstrap_test_schema()
        _reset_trading_test_process_state()
        _truncate_app_tables(_test_targeted_cleanup_tables(request))
        if _test_needs_default_trading_users(request):
            _seed_default_trading_users()
        SessionTesting = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        session = SessionTesting()
        try:
            yield session
        finally:
            session.close()
            _reset_trading_test_process_state()


@pytest.fixture()
def client(db, fastapi_app, _asgi_test_client):
    """FastAPI TestClient wired to the same PostgreSQL database as ``db``."""
    _ensure_app_db_loaded()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    # #region agent log
    _agent_ndjson(
        hypothesis_id="H2",
        location="conftest.py:client",
        message="client fixture bind db override + clear cookies",
        data={"client_id": id(_asgi_test_client)},
    )
    # #endregion
    fastapi_app.dependency_overrides[get_db] = _override_get_db
    try:
        _asgi_test_client.cookies.clear()
    except Exception:
        pass
    yield _asgi_test_client
    fastapi_app.dependency_overrides.clear()


def _pair_test_client(db, client):
    """TestClient with a cookie representing a paired (non-guest) user."""
    _ensure_app_db_loaded()
    user = User(name="TestUser")
    db.add(user)
    db.flush()
    # #region agent log
    _agent_ndjson(
        hypothesis_id="H4",
        location="conftest.py:paired_client",
        message="user flushed (PK set) without post-commit refresh",
        data={"user_id": getattr(user, "id", None)},
    )
    # #endregion

    token = "test-device-token-abc123"
    db.add(
        Device(
            token=token,
            user_id=user.id,
            label="Test Device",
            client_ip_last="127.0.0.1",
        )
    )
    db.commit()

    client.cookies.set(DEVICE_COOKIE_NAME, token)
    return client, user


@pytest.fixture()
def make_router_client(db):
    """Build a minimal FastAPI app for route tests that do not need app.main."""
    from fastapi import FastAPI

    _ensure_app_db_loaded()
    clients = []

    def _make_router_client(*routers):
        app = FastAPI()
        for router in routers:
            app.include_router(router)

        def _override_get_db():
            try:
                yield db
            finally:
                pass

        app.dependency_overrides[get_db] = _override_get_db
        route_client = TestClient(app)
        clients.append(route_client)
        return route_client

    try:
        yield _make_router_client
    finally:
        for route_client in clients:
            route_client.close()


@pytest.fixture()
def paired_identity(db):
    def _pair(client):
        return _pair_test_client(db, client)

    return _pair


@pytest.fixture()
def paired_client(db, client):
    return _pair_test_client(db, client)
