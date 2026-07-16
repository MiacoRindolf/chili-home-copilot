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
        "broker_symbol_action_claims",
        "captured_paper_post_commit_outbox",
        "captured_paper_post_commit_outbox_events",
        "captured_paper_completed_fill_watch",
        "captured_paper_completed_fill_watch_events",
        "captured_paper_phase_one_handoffs",
        "captured_paper_phase_one_handoff_events",
        "alpaca_paper_fill_activities",
        "alpaca_paper_fill_query_observations",
        "alpaca_paper_fill_page_objects",
        "alpaca_paper_fill_observation_pages",
        "alpaca_paper_fill_observation_activities",
        "alpaca_paper_post_settlement_fill_contradictions",
        "alpaca_paper_terminal_fill_observation_receipts",
        "alpaca_paper_bp_reflection_receipts",
        "alpaca_paper_bp_reflection_items",
        "alpaca_paper_cycle_settlements",
        "alpaca_paper_account_settlement_heads",
        "brain_batch_jobs",
        "pattern_evidence_corrections",
        "scan_patterns",
        *(
            table.name
            for table in Base.metadata.sorted_tables
            if table.name.startswith(
                ("trading_", "fast_path_", "momentum_", "adaptive_risk_")
            )
        ),
    }
)
_TRADING_DOMAIN_TARGETED_TESTS = (
    # Broker-truth recertification suites are trading-only.  Route them through
    # the scoped cleanup path so each invariant test does not TRUNCATE every
    # unrelated application table in the dedicated test database.
    "test_alpaca_account_risk_reservations.py",
    "test_alpaca_fill_activity_capture.py",
    "test_alpaca_buying_power_reflection.py",
    "test_alpaca_close_only_claim_fencing.py",
    "test_alpaca_crypto_paper.py",
    "test_alpaca_deadman_close_handoff.py",
    "test_alpaca_detached_claim_handoff.py",
    "test_alpaca_governed_place_bbo.py",
    "test_alpaca_orphan_outcome_repair.py",
    "test_alpaca_posture_quarantine.py",
    "test_alpaca_replacement_containment_claim.py",
    "test_alpaca_spot_adapter.py",
    "test_adaptive_db_paper_boundary.py",
    "test_auto_arm_causal_frontier.py",
    "test_adopt_on_cancel_fill.py",
    "test_aggregate_risk_cap.py",
    "test_automation_operator_exit_truth.py",
    "test_automation_runner_health.py",
    "test_automation_stale_reaper_account_identity.py",
    "test_broker_symbol_action_claim_concurrency.py",
    "test_concurrency_decouple_helpers.py",
    "test_dup_reference_reconcile.py",
    "test_equity_broker_readiness.py",
    "test_equity_venue_sizing.py",
    "test_fail_closed_risk_and_arm_lock.py",
    "test_iqfeed_trade_bridge_provenance.py",
    "test_lane_health_alert.py",
    "test_live_runner_exit_gating.py",
    "test_live_arm_generation_fence.py",
    "test_live_exit_phantom_reconcile.py",
    "test_live_runner_loop.py",
    "test_captured_paper_admission.py",
    "test_captured_paper_outbox.py",
    "test_captured_paper_phase_one_handoff.py",
    "test_live_runner_non_alpaca_account_identity.py",
    "test_mode_scoped_session_cap.py",
    "test_momentum_atomic_admission.py",
    "test_momentum_arm_pending_ttl.py",
    "test_momentum_bridge_subscribe_on_alert.py",
    "test_momentum_emergency_exit_recovery.py",
    "test_momentum_limit_entry.py",
    "test_momentum_live_runner.py",
    "test_momentum_operator_workflow.py",
    "test_momentum_order_path_dedupe.py",
    "test_momentum_risk_phase6.py",
    "test_momentum_venue_aware_dedup.py",
    "test_momentum_viability_freshness_pair.py",
    "test_non_alpaca_terminalization_truth.py",
    "test_order_truth_bundle.py",
    "test_paper_real_halt_isolation.py",
    "test_per_broker_daily_loss.py",
    "test_premarket_exit_hours_aware.py",
    "test_risk_governance_fail_closed_invariants.py",
    "test_ross_event_admission.py",
    "test_stop_breach_l2_confirm.py",
    "test_trading_scheduler.py",
    "test_verify_momentum_exec_process_health.py",
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
    "test_broker_position_sync.py",
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
    "test_equity_reconcile_partial_list_guard.py",
    "test_market_data_dead_cache_fallback.py",
    "test_monitor_api_execution_state.py",
    "test_paper_shadow_mode.py",
    "test_phase3_stop_bleed.py",
    "test_portfolio_options_close.py",
    "test_portfolio_risk_options_mtm.py",
    "test_pattern_directional_outcome.py",
    "test_pattern_cohort_promote.py",
    "test_pattern_imminent_alerts.py",
    "test_prescreen_artifacts.py",
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
_TRADING_NEURAL_MESH_TARGETED_TABLES = frozenset(
    {
        "brain_activation_events",
        "brain_activation_path_log",
        "brain_fire_log",
        "brain_graph_edge_mutations",
        "brain_graph_metrics",
        "brain_graph_snapshots",
        "brain_node_states",
        "brain_validation_slice_ledger",
    }
)
_TRADING_NEURAL_MESH_TARGETED_TESTS = (
    "test_brain_neural_mesh.py",
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
    _append_only_targeted_delete_tables = frozenset(
        {
            "adaptive_risk_decision_packets",
            "adaptive_risk_reservation_events",
            "adaptive_risk_opportunity_events",
            "captured_paper_post_commit_outbox",
            "captured_paper_post_commit_outbox_events",
            "captured_paper_completed_fill_watch_events",
            "alpaca_paper_fill_activities",
            "alpaca_paper_fill_query_observations",
            "alpaca_paper_fill_page_objects",
            "alpaca_paper_fill_observation_pages",
            "alpaca_paper_fill_observation_activities",
            "alpaca_paper_post_settlement_fill_contradictions",
            "alpaca_paper_terminal_fill_observation_receipts",
            "alpaca_paper_bp_reflection_receipts",
            "alpaca_paper_bp_reflection_items",
            "alpaca_paper_cycle_settlements",
            "alpaca_paper_account_settlement_heads",
            "trading_automation_simulated_fills",
        }
    )
    if table_names is not None:
        _evict_idle_in_transaction_peers()
        _terminate_stale_truncate_peers()
        with engine.begin() as conn:
            # Migration-owned captured-paper ledger tables intentionally have
            # no ORM models.  Clean them explicitly in the guarded *_test DB,
            # child first, while restoring their append-only triggers before
            # this cleanup transaction commits.
            for relation in (
                "captured_paper_completed_fill_watch_events",
                "captured_paper_completed_fill_watch",
                "captured_paper_phase_one_handoff_events",
                "captured_paper_phase_one_handoffs",
            ):
                if relation not in table_names:
                    continue
                present = conn.execute(
                    text("SELECT to_regclass(:name) IS NOT NULL"),
                    {"name": relation},
                ).scalar_one()
                if present:
                    conn.execute(text(f'ALTER TABLE "{relation}" DISABLE TRIGGER USER'))
                    conn.execute(text(f'DELETE FROM "{relation}"'))
                    conn.execute(text(f'ALTER TABLE "{relation}" ENABLE TRIGGER USER'))
            for table in reversed(Base.metadata.sorted_tables):
                if table.name in _skip_truncate or table.name not in table_names:
                    continue
                if table.name in _append_only_targeted_delete_tables:
                    # Migrations 319/324 deliberately reject UPDATE/DELETE of adaptive
                    # lifecycle evidence. Targeted pytest cleanup runs only against the
                    # conftest-guarded ``*_test`` database, so disable USER triggers
                    # transactionally for this cleanup statement and restore them before
                    # commit. A failure rolls the ALTER back with the DELETE; production
                    # and test assertions still see every append-only trigger enabled.
                    conn.execute(text(
                        f'ALTER TABLE "{table.name}" '
                        "DISABLE TRIGGER USER"
                    ))
                    conn.execute(text(
                        f'DELETE FROM "{table.name}"'
                    ))
                    conn.execute(text(
                        f'ALTER TABLE "{table.name}" '
                        "ENABLE TRIGGER USER"
                    ))
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
    statement_s = min(
        max(30, int(os.environ.get("CHILI_PYTEST_TRUNCATE_STATEMENT_TIMEOUT_S", "90"))),
        90,
    )
    for attempt in range(attempts):
        _evict_idle_in_transaction_peers()
        _terminate_stale_truncate_peers()
        try:
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL lock_timeout = '{lock_s}s'"))
                conn.execute(text(f"SET LOCAL statement_timeout = '{statement_s}s'"))
                names = [f'"{name}"' for name in _truncate_relation_names(conn, logical_names)]
                # Production append-only tables also reject TRUNCATE. Test
                # isolation is allowed to bypass USER triggers only inside the
                # guarded *_test database transaction, then restores them
                # before commit. Any failure rolls both ALTERs back.
                # Some append-only evidence relations are migration-owned and
                # therefore absent from ``Base.metadata`` even though they are
                # reached by TRUNCATE ... CASCADE from an ORM-owned parent.
                # Discover every known relation in the guarded test database so
                # its statement-level TRUNCATE trigger is disabled for exactly
                # this cleanup transaction too.
                append_only_for_cleanup = [
                    table_name
                    for table_name in sorted(_append_only_targeted_delete_tables)
                    if conn.execute(
                        text("SELECT to_regclass(:table_name) IS NOT NULL"),
                        {"table_name": f"public.{table_name}"},
                    ).scalar_one()
                ]
                for table_name in append_only_for_cleanup:
                    conn.execute(text(
                        f'ALTER TABLE "{table_name}" DISABLE TRIGGER USER'
                    ))
                stmt = text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE")
                conn.execute(stmt)
                for table_name in append_only_for_cleanup:
                    conn.execute(text(
                        f'ALTER TABLE "{table_name}" ENABLE TRIGGER USER'
                    ))
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
    if any(token in name for token in _TRADING_NEURAL_MESH_TARGETED_TESTS):
        return _TRADING_NEURAL_MESH_TARGETED_TABLES
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


@pytest.fixture()
def stable_non_alpaca_account_identity(monkeypatch):
    """Keep unrelated live-arm tests off real broker account-identity rails."""
    from app.services.trading.momentum_neural import live_runner, operator_actions

    identity = "test-non-alpaca-account-v1"
    monkeypatch.setattr(
        operator_actions,
        "_certified_non_alpaca_account_identity",
        lambda _family: (identity, None),
    )

    def _verify(session, *, adapter=None):
        snapshot = (
            session.risk_snapshot_json
            if isinstance(session.risk_snapshot_json, dict)
            else {}
        )
        frozen = str(
            snapshot.get("non_alpaca_account_identity") or identity
        ).strip()
        return {
            "ok": True,
            "applicable": True,
            "frozen_identity": frozen,
            "current_identity": frozen,
            "reason": None,
        }

    monkeypatch.setattr(
        operator_actions,
        "verify_frozen_non_alpaca_account_identity",
        _verify,
    )
    # live_runner imports the verifier into its own module namespace, so patch
    # that exact call site too. Dedicated account-rotation tests do not request
    # this fixture and continue to exercise the real fail-closed verifier.
    monkeypatch.setattr(
        live_runner,
        "verify_frozen_non_alpaca_account_identity",
        _verify,
    )
    return identity
