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
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient


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
    os.environ["DATABASE_URL"] = raw
    return raw


_ensure_postgres_test_url()

# Skip heavy / lock-prone module-level pattern seeding in app.main when the test client
# imports the app (see app.main). Pytest truncates tables per test; momentum tests seed
# variants via ensure_momentum_strategy_variants, not ScanPattern builtins.
os.environ["CHILI_PYTEST"] = "1"
# Avoid APScheduler + most deferred-startup DB maintenance during TestClient runs (reduces
# concurrent DB sessions when pytest shares a dev database with Docker Compose).
os.environ.setdefault("CHILI_SCHEDULER_ROLE", "none")

# Engine + models only — no ``app.main`` at import. The full app loads when
# ``client`` / ``fastapi_app`` is used (routers, scheduler hooks, pattern seeds).
from app.db import Base, engine  # noqa: E402
from app.deps import get_db  # noqa: E402
from app.models import User, Device  # noqa: E402
from app.pairing import DEVICE_COOKIE_NAME  # noqa: E402

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


_schema_initialized = False
_USER_DELETE_BLOCKING_TABLES = frozenset(
    table.name
    for table in Base.metadata.tables.values()
    if any(
        fk.target_fullname == "users.id"
        and (fk.ondelete or "").upper() not in {"CASCADE", "SET NULL"}
        for column in table.columns
        for fk in column.foreign_keys
    )
)
_PROJECT_DOMAIN_TARGETED_TABLES = frozenset(
    {
        "users",
        "devices",
        *_USER_DELETE_BLOCKING_TABLES,
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
_TRADING_DOMAIN_TARGETED_TABLES = frozenset(
    {
        "users",
        "devices",
        *_USER_DELETE_BLOCKING_TABLES,
        "brain_work_events",
        "broker_credentials",
        "broker_sessions",
        "pattern_evidence_corrections",
        "scan_patterns",
        *(
            table.name
            for table in Base.metadata.sorted_tables
            if table.name.startswith(("trading_", "fast_path_", "momentum_"))
        ),
    }
)
_TRADING_DOMAIN_TARGETED_TESTS = (
    "test_alerts_options_skip.py",
    "test_alpha_portfolio_gate.py",
    "test_auto_trader_synergy.py",
    "test_auto_trader_integration.py",
    "test_auto_trader_safety.py",
    "test_auto_trader_monitor.py",
    "test_autotrader_desk_api.py",
    "test_autotrader_payoff_sizing.py",
    "test_autotrader_pdt_soft_warn.py",
    "test_autotrader_position_overrides.py",
    "test_broker_sync.py",
    "test_broker_truth_safety.py",
    "test_brain_runtime_endpoints.py",
    "test_bracket_reconciler_hardening.py",
    "test_brain_work_ledger.py",
    "test_canonical_outcome_layer.py",
    "test_cash_deployment.py",
    "test_composite_reweight.py",
    "test_cost_aware_gate.py",
    "test_cpcv_promotion_gate.py",
    "test_crypto_exit_monitor_pattern_exit_now.py",
    "test_evidence_canonical_writer.py",
    "test_governance_daily_loss.py",
    "test_edge_aware_evolution.py",
    "test_edge_reliability.py",
    "test_emergency_liquidation_no_quote.py",
    "test_execution_cost_builder.py",
    "test_market_data_dead_cache_fallback.py",
    "test_monitor_api_execution_state.py",
    "test_paper_shadow_mode.py",
    "test_phase3_stop_bleed.py",
    "test_portfolio_options_close.py",
    "test_portfolio_risk_options_mtm.py",
    "test_pattern_directional_outcome.py",
    "test_pattern_cohort_promote.py",
    "test_pattern_imminent_alerts.py",
    "test_stop_engine_options_auto_exec.py",
    "test_stuck_order_watchdog.py",
    "test_trade_assign_pattern.py",
    "test_trades_sync.py",
    "test_trading_decision_stack.py",
    "test_triple_barrier_label_anchor.py",
    "test_triple_barrier_labeler.py",
    "test_triple_barrier_scheduler.py",
    "test_venue_robinhood_adapter.py",
)
_TRADING_SCAN_ONLY_TARGETED_TESTS = (
    "test_opportunity_board.py",
    "test_speculative_momentum_surface.py",
)
_TRADING_DEFAULT_USER_TESTS = (
    "test_pattern_imminent_alerts.py",
    "test_signal_to_reconcile_e2e.py",
    "test_trade_assign_pattern.py",
    "test_trades_sync.py",
)


def _bootstrap_test_schema() -> None:
    """Create tables and run versioned migrations (idempotent)."""
    global _schema_initialized
    if _schema_initialized:
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


def _truncate_app_tables(table_names: frozenset[str] | None = None) -> None:
    """Remove row data between tests; keep schema_version so migrations are not re-run."""
    # Static neural mesh topology is seeded by migration 086; keep nodes/edges so tests
    # do not need to re-seed the graph definition every time.
    _skip_truncate = frozenset({"schema_version", "brain_graph_nodes", "brain_graph_edges"})
    if table_names is not None:
        _evict_idle_in_transaction_peers()
        _terminate_stale_truncate_peers()
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                if table.name in _skip_truncate or table.name not in table_names:
                    continue
                conn.execute(text(f'DELETE FROM "{table.name}"'))
        return
    logical_names = [
        t.name
        for t in Base.metadata.sorted_tables
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


def _test_targeted_cleanup_tables(request) -> frozenset[str] | None:
    try:
        name = Path(str(request.node.fspath)).name.lower()
    except Exception:
        return None
    if any(token in name for token in _PROJECT_DOMAIN_TARGETED_TESTS):
        return _PROJECT_DOMAIN_TARGETED_TABLES
    if any(token in name for token in _TRADING_SCAN_ONLY_TARGETED_TESTS):
        return frozenset({"trading_scans"})
    if any(token in name for token in _TRADING_DOMAIN_TARGETED_TESTS):
        return _TRADING_DOMAIN_TARGETED_TABLES
    return None


def _test_needs_default_trading_users(request) -> bool:
    try:
        name = Path(str(request.node.fspath)).name.lower()
    except Exception:
        return False
    return any(token in name for token in _TRADING_DEFAULT_USER_TESTS)


def _seed_default_trading_users() -> None:
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


@pytest.fixture()
def db(request):
    """Yield a DB session; tables are truncated at test start.

    We do not TRUNCATE again in ``finally``: the session-scoped ASGI ``TestClient`` keeps
    the app lifespan open; post-test truncate races request/engine cleanup and caused
    teardown errors (lock timeout) after PASSED.
    """
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


@pytest.fixture()
def paired_client(db, client):
    """TestClient with a cookie representing a paired (non-guest) user."""
    unique_suffix = f"{os.getpid()}-{time.time_ns()}"
    user = User(name=f"TestUser-{unique_suffix}")
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

    token = f"test-device-token-{unique_suffix}"
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
