"""Version-tracked schema migrations for PostgreSQL. Run at app startup.

## Migration ID contract (Hard Rule 6, CLAUDE.md)

Migration IDs are **sequential and never reused**. This file is effectively
append-only with respect to already-registered IDs:

1. **Never reuse an ID.** Every entry in ``MIGRATIONS`` has a unique
   ``version_id`` string. Reusing an ID that is already in the
   ``schema_version`` table of any deployed environment is effectively
   unrecoverable without backups — the new migration would silently skip
   on that environment while still running on fresh ones, producing a
   silent schema drift.
2. **Never renumber historical IDs.** Downstream ``schema_version`` rows
   reference the literal string. Renaming ``"007_backfill_project_members"``
   to ``"007_backfill_v2"`` would re-execute the migration on every existing
   deployment and double-apply it on fresh ones.
3. **Never delete a migration function.** Once a migration has shipped, its
   code path stays in this file even if the table it touched is later
   dropped. Deleting it breaks the startup import of ``run_migrations``
   in environments where the version_id is still unapplied.
4. **Retirement, not deletion.** If a migration must be decommissioned
   (e.g. it references a dropped table and new environments can skip it),
   add an entry to ``RETIRED_MIGRATIONS`` below with the retirement date,
   but keep its function defined and keep it in ``MIGRATIONS``. Retired
   entries are asserted to never collide with live ``MIGRATIONS`` IDs.
5. **Idempotent bodies.** Every migration body guards every ALTER / INSERT
   with an existence check so re-running on a mis-tracked environment is
   a no-op, not a crash.

The ``_assert_migration_ids_unique`` check below enforces (1) and (4) at
startup: if anyone accidentally introduces a collision the app raises
``RuntimeError`` before any DB writes happen. A standalone verifier lives
at ``scripts/verify-migration-ids.ps1`` for CI / precommit use.

See also: ``docs/PHASE_ROLLBACK_RUNBOOK.md`` for how migrations interact
with the prediction-mirror phase flags.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect as sa_inspect, text

logger = logging.getLogger(__name__)
from sqlalchemy.engine import Engine


# ── Retired migrations ────────────────────────────────────────────────
#
# Map of retired ``version_id`` → short reason + retirement date.
# Entries here are asserted to **not collide** with live ``MIGRATIONS``
# entries. When retiring, keep the ``_migration_NNN_*`` function body in
# this file (so historical ``schema_version`` rows still resolve); just
# remove the tuple from ``MIGRATIONS`` below and add the ID here.
#
# Example:
#     RETIRED_MIGRATIONS: dict[str, str] = {
#         "042_trade_attribution_exit_tca": "superseded by 099; retired 2026-06-01",
#     }
RETIRED_MIGRATIONS: dict[str, str] = {}


def _assert_migration_ids_unique() -> None:
    """Fail fast if ``MIGRATIONS`` has a duplicate ID or collides with retired IDs.

    Called once at the top of ``run_migrations``. Raising here is the
    safest outcome — better to refuse to start the app than to silently
    apply a migration twice or skip it on half the fleet.
    """
    ids = [vid for vid, _fn in MIGRATIONS]
    seen: set[str] = set()
    dupes: list[str] = []
    for vid in ids:
        if vid in seen:
            dupes.append(vid)
        seen.add(vid)
    if dupes:
        raise RuntimeError(
            "Migration ID reuse detected in MIGRATIONS list: "
            f"{sorted(set(dupes))}. Migration IDs are sequential and never "
            "reused — see the Migration ID contract at the top of "
            "app/migrations.py."
        )
    retired_collision = sorted(seen & set(RETIRED_MIGRATIONS))
    if retired_collision:
        raise RuntimeError(
            "Migration ID collides with a retired ID: "
            f"{retired_collision}. A retired ID must not be reused; pick a "
            "new sequential ID. See RETIRED_MIGRATIONS at the top of "
            "app/migrations.py."
        )


def _tables(conn) -> set:
    return set(sa_inspect(conn.engine).get_table_names())


def _columns(conn, table: str) -> set:
    return {c["name"] for c in sa_inspect(conn.engine).get_columns(table)}


def _migration_001_add_email(conn) -> None:
    if "email" not in _columns(conn, "users"):
        conn.execute(text("ALTER TABLE users ADD COLUMN email TEXT"))
        conn.commit()


def _migration_002_add_image_path(conn) -> None:
    if "image_path" not in _columns(conn, "chat_messages"):
        conn.execute(text("ALTER TABLE chat_messages ADD COLUMN image_path TEXT"))
        conn.commit()


def _migration_003_conversations_project_id(conn) -> None:
    if "conversations" in _tables(conn) and "project_id" not in _columns(conn, "conversations"):
        conn.execute(text("ALTER TABLE conversations ADD COLUMN project_id INTEGER REFERENCES projects(id)"))
        conn.commit()


def _migration_004_chore_columns(conn) -> None:
    if "chores" not in _tables(conn):
        return
    chore_cols = _columns(conn, "chores")
    new_chore_cols = {
        "priority": "TEXT DEFAULT 'medium'",
        "due_date": "DATE",
        "recurrence": "TEXT DEFAULT 'none'",
        "assigned_to": "INTEGER REFERENCES users(id)",
        "created_at": "DATETIME",
        "completed_at": "DATETIME",
    }
    for col_name, col_def in new_chore_cols.items():
        if col_name not in chore_cols:
            conn.execute(text(f"ALTER TABLE chores ADD COLUMN {col_name} {col_def}"))
            conn.commit()


def _migration_005_plan_projects_key(conn) -> None:
    if "plan_projects" in _tables(conn) and "key" not in _columns(conn, "plan_projects"):
        conn.execute(text("ALTER TABLE plan_projects ADD COLUMN key TEXT"))
        conn.commit()


def _migration_006_plan_tasks_parent_reporter(conn) -> None:
    if "plan_tasks" not in _tables(conn):
        return
    pt_cols = _columns(conn, "plan_tasks")
    if "parent_id" not in pt_cols:
        conn.execute(text("ALTER TABLE plan_tasks ADD COLUMN parent_id INTEGER REFERENCES plan_tasks(id)"))
        conn.commit()
    if "reporter_id" not in _columns(conn, "plan_tasks"):
        conn.execute(text("ALTER TABLE plan_tasks ADD COLUMN reporter_id INTEGER REFERENCES users(id)"))
        conn.commit()


def _migration_007_backfill_project_members(conn) -> None:
    if "plan_projects" not in _tables(conn) or "project_members" not in _tables(conn):
        return
    rows = conn.execute(text(
        "SELECT pp.id, pp.user_id FROM plan_projects pp "
        "WHERE NOT EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id = pp.id AND pm.user_id = pp.user_id)"
    )).fetchall()
    for row in rows:
        conn.execute(text(
            "INSERT INTO project_members (project_id, user_id, role, joined_at) VALUES (:pid, :uid, 'owner', CURRENT_TIMESTAMP)"
        ), {"pid": row[0], "uid": row[1]})
    if rows:
        conn.commit()


def _migration_008_trade_broker_columns(conn) -> None:
    if "trading_trades" not in _tables(conn):
        return
    cols = _columns(conn, "trading_trades")
    if "broker_source" not in cols:
        conn.execute(text("ALTER TABLE trading_trades ADD COLUMN broker_source TEXT"))
        conn.commit()
    if "broker_order_id" not in cols:
        conn.execute(text("ALTER TABLE trading_trades ADD COLUMN broker_order_id TEXT"))
        conn.commit()


def _migration_009_snapshot_predicted_score(conn) -> None:
    if "trading_snapshots" not in _tables(conn):
        return
    cols = _columns(conn, "trading_snapshots")
    if "predicted_score" not in cols:
        conn.execute(text("ALTER TABLE trading_snapshots ADD COLUMN predicted_score REAL"))
        conn.commit()


def _migration_010_snapshot_extra_columns(conn) -> None:
    if "trading_snapshots" not in _tables(conn):
        return
    cols = _columns(conn, "trading_snapshots")
    for col_name in ("vix_at_snapshot", "future_return_1d", "future_return_3d"):
        if col_name not in cols:
            conn.execute(text(f"ALTER TABLE trading_snapshots ADD COLUMN {col_name} REAL"))
            conn.commit()


def _migration_011_trade_pattern_tags(conn) -> None:
    if "trading_trades" not in _tables(conn):
        return
    cols = _columns(conn, "trading_trades")
    if "pattern_tags" not in cols:
        conn.execute(text("ALTER TABLE trading_trades ADD COLUMN pattern_tags TEXT"))
        conn.commit()


def _migration_012_snapshot_sentiment_fundamentals(conn) -> None:
    if "trading_snapshots" not in _tables(conn):
        return
    cols = _columns(conn, "trading_snapshots")
    for col, typ in [("news_sentiment", "REAL"), ("news_count", "INTEGER"),
                     ("pe_ratio", "REAL"), ("market_cap_b", "REAL")]:
        if col not in cols:
            conn.execute(text(f"ALTER TABLE trading_snapshots ADD COLUMN {col} {typ}"))
    conn.commit()


def _migration_013_trade_order_sync_columns(conn) -> None:
    if "trading_trades" not in _tables(conn):
        return
    cols = _columns(conn, "trading_trades")
    for col, typ in [
        ("broker_status", "TEXT"),
        ("last_broker_sync", "TEXT"),
        ("filled_at", "TEXT"),
        ("avg_fill_price", "REAL"),
    ]:
        if col not in cols:
            conn.execute(text(f"ALTER TABLE trading_trades ADD COLUMN {col} {typ}"))
    conn.commit()


def _migration_014_code_brain_tables(conn) -> None:
    """Create Code Brain tables if they don't exist (SQLAlchemy create_all handles
    this at startup too, but this migration ensures the schema_version record)."""
    tables = _tables(conn)
    if "code_repos" not in tables:
        conn.execute(text("""
            CREATE TABLE code_repos (
                id INTEGER PRIMARY KEY,
                user_id INTEGER, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                language_stats TEXT, framework_tags TEXT,
                file_count INTEGER DEFAULT 0, total_lines INTEGER DEFAULT 0,
                last_indexed TEXT, last_commit_hash TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                active INTEGER DEFAULT 1
            )
        """))
    if "code_insights" not in tables:
        conn.execute(text("""
            CREATE TABLE code_insights (
                id INTEGER PRIMARY KEY,
                repo_id INTEGER, user_id INTEGER, category TEXT NOT NULL,
                description TEXT NOT NULL, confidence REAL DEFAULT 0.5,
                evidence_count INTEGER DEFAULT 1, evidence_files TEXT,
                active INTEGER DEFAULT 1,
                last_seen TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
    if "code_snapshots" not in tables:
        conn.execute(text("""
            CREATE TABLE code_snapshots (
                id INTEGER PRIMARY KEY,
                repo_id INTEGER NOT NULL, file_path TEXT NOT NULL,
                language TEXT, line_count INTEGER DEFAULT 0,
                function_count INTEGER DEFAULT 0, class_count INTEGER DEFAULT 0,
                complexity_score REAL DEFAULT 0.0, last_modified TEXT,
                snapshot_date TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
    if "code_hotspots" not in tables:
        conn.execute(text("""
            CREATE TABLE code_hotspots (
                id INTEGER PRIMARY KEY,
                repo_id INTEGER NOT NULL, file_path TEXT NOT NULL,
                churn_score REAL DEFAULT 0.0, complexity_score REAL DEFAULT 0.0,
                combined_score REAL DEFAULT 0.0, commit_count INTEGER DEFAULT 0,
                last_commit_date TEXT,
                snapshot_date TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
    if "code_learning_events" not in tables:
        conn.execute(text("""
            CREATE TABLE code_learning_events (
                id INTEGER PRIMARY KEY,
                user_id INTEGER, repo_id INTEGER, event_type TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
    conn.commit()


def _migration_015_reasoning_brain_tables(conn) -> None:
    """Create Reasoning Brain tables if they don't exist."""
    tables = _tables(conn)
    if "reasoning_user_models" not in tables:
        conn.execute(
            text(
                """
            CREATE TABLE reasoning_user_models (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                decision_style TEXT,
                risk_tolerance TEXT,
                communication_prefs TEXT,
                active_goals TEXT,
                knowledge_gaps TEXT,
                source_memory_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                active INTEGER NOT NULL DEFAULT 1
            )
        """
            )
        )
    if "reasoning_interests" not in tables:
        conn.execute(
            text(
                """
            CREATE TABLE reasoning_interests (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                topic TEXT NOT NULL,
                category TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 0.0,
                related_topics TEXT,
                source TEXT,
                last_seen TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                active INTEGER NOT NULL DEFAULT 1
            )
        """
            )
        )
    if "reasoning_research" not in tables:
        conn.execute(
            text(
                """
            CREATE TABLE reasoning_research (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                topic TEXT NOT NULL,
                summary TEXT NOT NULL,
                sources TEXT,
                relevance_score REAL NOT NULL DEFAULT 0.0,
                searched_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                stale INTEGER NOT NULL DEFAULT 0
            )
        """
            )
        )
    if "reasoning_anticipations" not in tables:
        conn.execute(
            text(
                """
            CREATE TABLE reasoning_anticipations (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                domain TEXT,
                context TEXT,
                confidence REAL NOT NULL DEFAULT 0.5,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                acted_on INTEGER NOT NULL DEFAULT 0,
                dismissed INTEGER NOT NULL DEFAULT 0
            )
        """
            )
        )
    if "reasoning_events" not in tables:
        conn.execute(
            text(
                """
            CREATE TABLE reasoning_events (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        """
            )
        )
    conn.commit()


def _migration_016_reasoning_learning_structures(conn) -> None:
    """Create Reasoning learning goal / hypothesis / confidence tables."""
    tables = _tables(conn)
    if "reasoning_learning_goals" not in tables:
        conn.execute(
            text(
                """
            CREATE TABLE reasoning_learning_goals (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                dimension TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                confidence_before REAL,
                confidence_after REAL,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                completed_at TEXT
            )
        """
            )
        )
    if "reasoning_hypotheses" not in tables:
        conn.execute(
            text(
                """
            CREATE TABLE reasoning_hypotheses (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                claim TEXT NOT NULL,
                domain TEXT,
                confidence REAL NOT NULL DEFAULT 0.5,
                evidence_for INTEGER NOT NULL DEFAULT 0,
                evidence_against INTEGER NOT NULL DEFAULT 0,
                tested_at TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                active INTEGER NOT NULL DEFAULT 1
            )
        """
            )
        )
    if "reasoning_confidence_snapshots" not in tables:
        conn.execute(
            text(
                """
            CREATE TABLE reasoning_confidence_snapshots (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                dimension TEXT NOT NULL,
                confidence_value REAL NOT NULL DEFAULT 0.0,
                snapshot_date TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        """
            )
        )
    conn.commit()


def _migration_017_code_brain_innovation(conn) -> None:
    """Create tables for Code Brain innovation: graph, trends, reviews, dep alerts, search index."""
    tables = _tables(conn)
    if "code_dependencies" not in tables:
        conn.execute(text("""
            CREATE TABLE code_dependencies (
                id SERIAL PRIMARY KEY,
                repo_id INTEGER NOT NULL,
                source_file VARCHAR(500) NOT NULL,
                target_file VARCHAR(500) NOT NULL,
                import_name VARCHAR(300),
                is_circular BOOLEAN NOT NULL DEFAULT FALSE,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_code_dep_repo ON code_dependencies(repo_id)"))
    if "code_quality_snapshots" not in tables:
        conn.execute(text("""
            CREATE TABLE code_quality_snapshots (
                id SERIAL PRIMARY KEY,
                repo_id INTEGER NOT NULL,
                total_files INTEGER DEFAULT 0,
                total_lines INTEGER DEFAULT 0,
                avg_complexity REAL DEFAULT 0.0,
                max_complexity REAL DEFAULT 0.0,
                test_file_count INTEGER DEFAULT 0,
                test_ratio REAL DEFAULT 0.0,
                hotspot_count INTEGER DEFAULT 0,
                insight_count INTEGER DEFAULT 0,
                recorded_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_code_qs_repo ON code_quality_snapshots(repo_id)"))
    if "code_reviews" not in tables:
        conn.execute(text("""
            CREATE TABLE code_reviews (
                id SERIAL PRIMARY KEY,
                repo_id INTEGER NOT NULL,
                user_id INTEGER,
                commit_hash VARCHAR(50) NOT NULL,
                author VARCHAR(200),
                summary TEXT,
                findings_json TEXT,
                overall_score REAL DEFAULT 5.0,
                reviewed_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_code_rev_repo ON code_reviews(repo_id)"))
        conn.execute(text("CREATE INDEX ix_code_rev_hash ON code_reviews(commit_hash)"))
    if "code_dep_alerts" not in tables:
        conn.execute(text("""
            CREATE TABLE code_dep_alerts (
                id SERIAL PRIMARY KEY,
                repo_id INTEGER NOT NULL,
                package_name VARCHAR(200) NOT NULL,
                current_version VARCHAR(50),
                latest_version VARCHAR(50),
                severity VARCHAR(20) NOT NULL DEFAULT 'info',
                alert_type VARCHAR(30) NOT NULL DEFAULT 'outdated',
                ecosystem VARCHAR(10) NOT NULL DEFAULT 'pip',
                resolved BOOLEAN NOT NULL DEFAULT FALSE,
                detected_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_code_depalert_repo ON code_dep_alerts(repo_id)"))
    if "code_search_index" not in tables:
        conn.execute(text("""
            CREATE TABLE code_search_index (
                id SERIAL PRIMARY KEY,
                repo_id INTEGER NOT NULL,
                file_path VARCHAR(500) NOT NULL,
                symbol_name VARCHAR(300) NOT NULL,
                symbol_type VARCHAR(20) NOT NULL,
                signature TEXT,
                docstring TEXT,
                line_number INTEGER DEFAULT 0,
                indexed_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_code_search_repo ON code_search_index(repo_id)"))
        conn.execute(text("CREATE INDEX ix_code_search_sym ON code_search_index(symbol_name)"))
    conn.commit()


def _migration_018_breakout_alert_outcome_cols(conn) -> None:
    """Add exit-optimization and context columns to trading_breakout_alerts."""
    tables = _tables(conn)
    if "trading_breakout_alerts" not in tables:
        return
    existing = _columns(conn, "trading_breakout_alerts")
    new_cols = [
        ("time_to_peak_hours", "REAL"),
        ("time_to_stop_hours", "REAL"),
        ("price_at_peak", "REAL"),
        ("optimal_exit_pct", "REAL"),
        ("regime_at_alert", "VARCHAR(20)"),
        ("scan_cycle_id", "VARCHAR(40)"),
        ("timeframe", "VARCHAR(10)"),
        ("sector", "VARCHAR(60)"),
        ("news_sentiment_at_alert", "REAL"),
    ]
    for col_name, col_type in new_cols:
        if col_name not in existing:
            conn.execute(text(f"ALTER TABLE trading_breakout_alerts ADD COLUMN {col_name} {col_type}"))
    if "scan_cycle_id" not in existing:
        try:
            conn.execute(text("CREATE INDEX ix_breakout_scan_cycle ON trading_breakout_alerts(scan_cycle_id)"))
        except Exception:
            pass
    conn.commit()


def _migration_019_project_brain_tables(conn) -> None:
    """Create tables for the autonomous Project Brain agent framework."""
    tables = _tables(conn)

    if "project_agent_states" not in tables:
        conn.execute(text("""
            CREATE TABLE project_agent_states (
                id SERIAL PRIMARY KEY,
                agent_name VARCHAR(50) NOT NULL,
                user_id INTEGER,
                state_json TEXT,
                confidence REAL DEFAULT 0.0,
                last_cycle_at DATETIME,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_pas_agent ON project_agent_states(agent_name)"))

    if "agent_findings" not in tables:
        conn.execute(text("""
            CREATE TABLE agent_findings (
                id SERIAL PRIMARY KEY,
                agent_name VARCHAR(50) NOT NULL,
                user_id INTEGER,
                category VARCHAR(50) NOT NULL,
                title VARCHAR(300) NOT NULL,
                description TEXT NOT NULL,
                severity VARCHAR(20) NOT NULL DEFAULT 'info',
                evidence_json TEXT,
                status VARCHAR(20) NOT NULL DEFAULT 'new',
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_af_agent ON agent_findings(agent_name)"))

    if "agent_research" not in tables:
        conn.execute(text("""
            CREATE TABLE agent_research (
                id SERIAL PRIMARY KEY,
                agent_name VARCHAR(50) NOT NULL,
                user_id INTEGER,
                topic VARCHAR(300) NOT NULL,
                query VARCHAR(500) NOT NULL,
                summary TEXT NOT NULL,
                sources_json TEXT,
                relevance_score REAL DEFAULT 0.0,
                searched_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                stale BOOLEAN DEFAULT FALSE
            )
        """))
        conn.execute(text("CREATE INDEX ix_ar_agent ON agent_research(agent_name)"))

    if "agent_goals" not in tables:
        conn.execute(text("""
            CREATE TABLE agent_goals (
                id SERIAL PRIMARY KEY,
                agent_name VARCHAR(50) NOT NULL,
                user_id INTEGER,
                description TEXT NOT NULL,
                goal_type VARCHAR(30) NOT NULL DEFAULT 'learn',
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                progress REAL DEFAULT 0.0,
                evidence_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                completed_at DATETIME
            )
        """))
        conn.execute(text("CREATE INDEX ix_ag_agent ON agent_goals(agent_name)"))

    if "agent_evolutions" not in tables:
        conn.execute(text("""
            CREATE TABLE agent_evolutions (
                id SERIAL PRIMARY KEY,
                agent_name VARCHAR(50) NOT NULL,
                user_id INTEGER,
                dimension VARCHAR(100) NOT NULL,
                description TEXT NOT NULL,
                confidence_before REAL DEFAULT 0.0,
                confidence_after REAL DEFAULT 0.0,
                trigger VARCHAR(200) NOT NULL DEFAULT 'cycle',
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_ae_agent ON agent_evolutions(agent_name)"))

    if "agent_messages" not in tables:
        conn.execute(text("""
            CREATE TABLE agent_messages (
                id SERIAL PRIMARY KEY,
                from_agent VARCHAR(50) NOT NULL,
                to_agent VARCHAR(50) NOT NULL,
                user_id INTEGER,
                message_type VARCHAR(50) NOT NULL,
                content_json TEXT NOT NULL,
                acknowledged BOOLEAN DEFAULT FALSE,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_am_to ON agent_messages(to_agent)"))

    if "po_questions" not in tables:
        conn.execute(text("""
            CREATE TABLE po_questions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                question TEXT NOT NULL,
                context TEXT,
                category VARCHAR(50) NOT NULL DEFAULT 'general',
                priority INTEGER DEFAULT 5,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                answer TEXT,
                asked_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                answered_at DATETIME
            )
        """))

    if "po_requirements" not in tables:
        conn.execute(text("""
            CREATE TABLE po_requirements (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                title VARCHAR(300) NOT NULL,
                description TEXT NOT NULL,
                priority VARCHAR(20) NOT NULL DEFAULT 'medium',
                status VARCHAR(20) NOT NULL DEFAULT 'draft',
                acceptance_criteria TEXT,
                source_questions_json TEXT,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))

    conn.commit()


def _migration_020_user_google_oauth_cols(conn) -> None:
    """Add google_id and avatar_url to users table for Google OAuth SSO."""
    cols = _columns(conn, "users")
    if "google_id" not in cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN google_id TEXT"))
    if "avatar_url" not in cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN avatar_url TEXT"))
    conn.commit()
    try:
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_id ON users(google_id)"))
        conn.commit()
    except Exception:
        pass


def _migration_021_broker_credentials_table(conn) -> None:
    """Create broker_credentials table for per-user encrypted credential storage."""
    tables = _tables(conn)
    if "broker_credentials" not in tables:
        conn.execute(text("""
            CREATE TABLE broker_credentials (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                broker TEXT NOT NULL,
                encrypted_data TEXT NOT NULL,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE(user_id, broker)
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_broker_creds_user ON broker_credentials(user_id)"))
        conn.commit()


def _migration_022_alert_trade_type_cols(conn) -> None:
    """Add trade_type and duration_estimate columns to trading_alerts."""
    cols = _columns(conn, "trading_alerts")
    if "trade_type" not in cols:
        conn.execute(text("ALTER TABLE trading_alerts ADD COLUMN trade_type TEXT"))
    if "duration_estimate" not in cols:
        conn.execute(text("ALTER TABLE trading_alerts ADD COLUMN duration_estimate TEXT"))
    conn.commit()


def _migration_023_qa_engineer_tables(conn) -> None:
    """Create tables for the QA Engineer agent: test cases, test runs, bug reports."""
    tables = _tables(conn)

    if "qa_test_cases" not in tables:
        conn.execute(text("""
            CREATE TABLE qa_test_cases (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                name VARCHAR(300) NOT NULL,
                steps_json TEXT,
                expected_json TEXT,
                priority VARCHAR(20) NOT NULL DEFAULT 'medium',
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                last_run_at DATETIME,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_qa_tc_user ON qa_test_cases(user_id)"))

    if "qa_test_runs" not in tables:
        conn.execute(text("""
            CREATE TABLE qa_test_runs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                test_name VARCHAR(300) NOT NULL,
                passed BOOLEAN NOT NULL DEFAULT FALSE,
                errors_json TEXT,
                duration_ms INTEGER DEFAULT 0,
                screenshot_path VARCHAR(500),
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_qa_tr_user ON qa_test_runs(user_id)"))

    if "qa_bug_reports" not in tables:
        conn.execute(text("""
            CREATE TABLE qa_bug_reports (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                title VARCHAR(300) NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                severity VARCHAR(20) NOT NULL DEFAULT 'warn',
                screenshot_path VARCHAR(500),
                reproduction_steps TEXT,
                status VARCHAR(20) NOT NULL DEFAULT 'open',
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            )
        """))
        conn.execute(text("CREATE INDEX ix_qa_br_user ON qa_bug_reports(user_id)"))

    conn.commit()


def _migration_024_po_question_options(conn) -> None:
    """Add options column to po_questions for multiple-choice interview flow."""
    cols = _columns(conn, "po_questions")
    if "options" not in cols:
        conn.execute(text("ALTER TABLE po_questions ADD COLUMN options TEXT"))
    conn.commit()


def _migration_025_insight_win_loss_counts(conn) -> None:
    """Add win_count and loss_count columns to trading_insights."""
    if "trading_insights" not in _tables(conn):
        return
    cols = _columns(conn, "trading_insights")
    if "win_count" not in cols:
        conn.execute(text("ALTER TABLE trading_insights ADD COLUMN win_count INTEGER NOT NULL DEFAULT 0"))
    if "loss_count" not in cols:
        conn.execute(text("ALTER TABLE trading_insights ADD COLUMN loss_count INTEGER NOT NULL DEFAULT 0"))
    conn.commit()


def _migration_026_reset_backfilled_win_loss(conn) -> None:
    """Reset fake win/loss counts that were backfilled from description text parsing."""
    if "trading_insights" not in _tables(conn):
        return
    conn.execute(text("UPDATE trading_insights SET win_count = 0, loss_count = 0"))
    conn.commit()


def _migration_027_backtest_insight_link(conn) -> None:
    """Add related_insight_id column to trading_backtests for direct linking."""
    if "trading_backtests" not in _tables(conn):
        return
    cols = _columns(conn, "trading_backtests")
    if "related_insight_id" not in cols:
        conn.execute(text("ALTER TABLE trading_backtests ADD COLUMN related_insight_id INTEGER"))
        conn.commit()


def _migration_028_seed_rsi_ema_breakout_pattern(conn) -> None:
    """Seed the RSI>70 + EMA stack + resistance retest breakout pattern."""
    import json as _json

    pat_name = "RSI Overbought + EMA Stack + Resistance Retest Breakout"
    rules = _json.dumps({
        "conditions": [
            {"indicator": "rsi_14", "op": ">", "value": 70},
            {"indicator": "price", "op": ">", "ref": "ema_20"},
            {"indicator": "price", "op": ">", "ref": "ema_50"},
            {"indicator": "price", "op": ">", "ref": "ema_100"},
            {"indicator": "resistance_retests", "op": ">=", "value": 2,
             "params": {"tolerance_pct": 1.5, "lookback": 20}},
            {"indicator": "bb_squeeze", "op": "==", "value": True},
        ]
    })

    if "scan_patterns" in _tables(conn):
        existing = conn.execute(
            text("SELECT id FROM scan_patterns WHERE name = :n"), {"n": pat_name}
        ).fetchone()
        if not existing:
            conn.execute(text(
                "INSERT INTO scan_patterns "
                "(name, description, rules_json, origin, asset_class, timeframe, confidence, "
                " evidence_count, backtest_count, score_boost, min_base_score, "
                " active, generation, ticker_scope, trade_count, backtest_priority, "
                " promotion_status, oos_validation_json, queue_tier, paper_book_json, "
                " regime_affinity_json, lifecycle_stage, pattern_evidence_kind, created_at, updated_at) "
                "VALUES (:name, :desc, :rules, :origin, :ac, '1d', 0.0, 0, 0, 1.5, 4.0, "
                " TRUE, 0, 'universal', 0, 0, "
                " 'legacy', '{}'::jsonb, 'full', '{}'::jsonb, '{}'::jsonb, 'candidate', 'realized_pnl', "
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {
                "name": pat_name,
                "desc": (
                    "RSI overbought (>70) with full bullish EMA stack "
                    "(price > EMA20 > EMA50 > EMA100), at least 2 resistance "
                    "retests, and Bollinger squeeze consolidation before breakout."
                ),
                "rules": rules,
                "origin": "user_seeded",
                "ac": "all",
            })
            conn.commit()

    if "trading_hypotheses" in _tables(conn):
        hyp_desc = (
            "RSI>70 + full EMA stack + resistance retest + BB squeeze "
            "outperforms RSI>70 + EMA stack alone (without consolidation/retest)"
        )
        existing = conn.execute(
            text("SELECT id FROM trading_hypotheses WHERE description = :d"),
            {"d": hyp_desc},
        ).fetchone()
        if not existing:
            cond_a = _json.dumps({
                "rsi_14": ">70", "ema_stack": "bullish",
                "resistance_retests": ">=2", "bb_squeeze": True,
            })
            cond_b = _json.dumps({
                "rsi_14": ">70", "ema_stack": "bullish",
            })
            conn.execute(text(
                "INSERT INTO trading_hypotheses "
                "(description, condition_a, condition_b, expected_winner, "
                " origin, status, times_tested, times_confirmed, "
                " times_rejected, created_at) "
                "VALUES (:desc, :ca, :cb, 'a', 'user_seeded', 'pending', "
                " 0, 0, 0, CURRENT_TIMESTAMP)"
            ), {"desc": hyp_desc, "ca": cond_a, "cb": cond_b})
            conn.commit()


def _migration_029_seed_rsi_ema_insight(conn) -> None:
    """Create a TradingInsight for the seeded RSI+EMA pattern so it shows in the Brain UI."""
    if "trading_insights" not in _tables(conn):
        return
    if "scan_patterns" not in _tables(conn):
        return
    pat_name = "RSI Overbought + EMA Stack + Resistance Retest Breakout"
    pat_row = conn.execute(
        text("SELECT id FROM scan_patterns WHERE name = :n LIMIT 1"),
        {"n": pat_name},
    ).fetchone()
    if not pat_row:
        return
    scan_pattern_id = int(pat_row[0])

    pat_desc = (
        "RSI Overbought + EMA Stack + Resistance Retest Breakout — "
        "RSI overbought (>70) with full bullish EMA stack "
        "(price > EMA20 > EMA50 > EMA100), at least 2 resistance "
        "retests, and Bollinger squeeze consolidation before breakout. "
        "[User-seeded pattern]"
    )

    user_ids = [
        r[0] for r in conn.execute(
            text("SELECT DISTINCT user_id FROM trading_insights")
        ).fetchall()
    ]
    if not user_ids:
        user_ids = [None]

    now_sql = "CURRENT_TIMESTAMP"
    user_match = "user_id IS NOT DISTINCT FROM :uid"
    for uid in user_ids:
        existing = conn.execute(
            text(
                "SELECT id FROM trading_insights "
                "WHERE pattern_description LIKE :pat AND " + user_match
            ),
            {"pat": "RSI Overbought + EMA Stack%", "uid": uid},
        ).fetchone()
        if existing:
            continue
        active_val = "TRUE"
        conn.execute(text(
            "INSERT INTO trading_insights "
            "(user_id, scan_pattern_id, pattern_description, confidence, evidence_count, "
            " last_seen, created_at, active, win_count, loss_count) "
            f"VALUES (:uid, :spid, :desc, 0.5, 1, {now_sql}, {now_sql}, "
            f" {active_val}, 0, 0)"
        ), {"uid": uid, "spid": scan_pattern_id, "desc": pat_desc})
    conn.commit()


def _migration_030_pattern_exit_evolution(conn) -> None:
    """Add columns for exit-strategy evolution: parent_id, exit_config,
    variant_label, generation on scan_patterns."""
    if "scan_patterns" not in _tables(conn):
        return
    cols = _columns(conn, "scan_patterns")
    if "parent_id" not in cols:
        conn.execute(text("ALTER TABLE scan_patterns ADD COLUMN parent_id INTEGER"))
    if "exit_config" not in cols:
        conn.execute(text("ALTER TABLE scan_patterns ADD COLUMN exit_config TEXT"))
    if "variant_label" not in cols:
        conn.execute(text("ALTER TABLE scan_patterns ADD COLUMN variant_label VARCHAR(40)"))
    if "generation" not in cols:
        conn.execute(text(
            "ALTER TABLE scan_patterns ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
        ))
    conn.commit()


def _migration_031_seed_ross_cameron_patterns(conn) -> None:
    """Seed 5 Ross Cameron patterns from Warrior Trading methodology.

    Source: 2025 Small Account Tool Kit PDF + '5 Step Stock Picking Trick'
    YouTube video by Ross Cameron.
    """
    import json as _json

    if "scan_patterns" not in _tables(conn):
        return

    _PATTERNS = [
        {
            "name": "RC 5 Pillars Momentum Scanner",
            "desc": (
                "Ross Cameron's 5 Pillars: relative volume >= 5x, price $1-$20, "
                "daily move >= 10%, gap up > 3%. Identifies 'A quality' small-cap "
                "momentum stocks with high demand and news-driven catalysts. "
                "[User-seeded from Warrior Trading]"
            ),
            "rules": {
                "conditions": [
                    {"indicator": "rel_vol", "op": ">=", "value": 5.0},
                    {"indicator": "price", "op": "between", "value": [1.0, 20.0]},
                    {"indicator": "daily_change_pct", "op": ">=", "value": 10.0},
                    {"indicator": "gap_pct", "op": ">", "value": 3.0},
                ]
            },
        },
        {
            "name": "RC Small Cap Micro-Pullback",
            "desc": (
                "Ross Cameron's primary entry: buy micro-pullbacks on the front "
                "side of momentum in $5-$10 stocks. RSI dipping (40-70) while "
                "price holds above EMA 9 and MACD stays positive. "
                "[User-seeded from Warrior Trading]"
            ),
            "rules": {
                "conditions": [
                    {"indicator": "rel_vol", "op": ">=", "value": 5.0},
                    {"indicator": "price", "op": "between", "value": [5.0, 10.0]},
                    {"indicator": "rsi_14", "op": "between", "value": [40, 70]},
                    {"indicator": "price", "op": ">", "ref": "ema_9"},
                    {"indicator": "macd_hist", "op": ">", "value": 0},
                ]
            },
        },
        {
            "name": "RC Gap and Go",
            "desc": (
                "Ross Cameron's Gap and Go: stock gaps up >= 5% on news with "
                "extreme relative volume (>= 5x), RSI strong (>= 60), confirming "
                "momentum continuation above the gap. Price $1-$20 small-cap range. "
                "[User-seeded from Warrior Trading]"
            ),
            "rules": {
                "conditions": [
                    {"indicator": "gap_pct", "op": ">=", "value": 5.0},
                    {"indicator": "rel_vol", "op": ">=", "value": 5.0},
                    {"indicator": "price", "op": "between", "value": [1.0, 20.0]},
                    {"indicator": "rsi_14", "op": ">=", "value": 60},
                    {"indicator": "daily_change_pct", "op": ">=", "value": 5.0},
                ]
            },
        },
        {
            "name": "RC Bull Flag Breakout",
            "desc": (
                "Ross Cameron's Bull Flag Breakout: Bollinger squeeze firing "
                "(consolidation breaking out) on small-cap stocks with elevated "
                "relative volume (>= 3x), RSI > 50, and price above EMA 20. "
                "[User-seeded from Warrior Trading]"
            ),
            "rules": {
                "conditions": [
                    {"indicator": "rel_vol", "op": ">=", "value": 3.0},
                    {"indicator": "price", "op": "between", "value": [1.0, 20.0]},
                    {"indicator": "bb_squeeze_firing", "op": "==", "value": True},
                    {"indicator": "rsi_14", "op": ">", "value": 50},
                    {"indicator": "price", "op": ">", "ref": "ema_20"},
                ]
            },
        },
        {
            "name": "RC Flat Top Breakout",
            "desc": (
                "Ross Cameron's Flat Top Breakout: price within 1% of resistance "
                "that has been tested >= 2 times, with elevated relative volume "
                "(>= 3x) and RSI 50-75. Horizontal resistance breakout on "
                "small-cap stocks. [User-seeded from Warrior Trading]"
            ),
            "rules": {
                "conditions": [
                    {"indicator": "dist_to_resistance_pct", "op": "<=", "value": 1.0},
                    {"indicator": "resistance_retests", "op": ">=", "value": 2},
                    {"indicator": "rel_vol", "op": ">=", "value": 3.0},
                    {"indicator": "rsi_14", "op": "between", "value": [50, 75]},
                    {"indicator": "price", "op": "between", "value": [1.0, 20.0]},
                ]
            },
        },
    ]

    for pat in _PATTERNS:
        existing = conn.execute(
            text("SELECT id FROM scan_patterns WHERE name = :n"),
            {"n": pat["name"]},
        ).fetchone()
        if existing:
            continue
        conn.execute(text(
            "INSERT INTO scan_patterns "
            "(name, description, rules_json, origin, asset_class, timeframe, confidence, "
            " evidence_count, backtest_count, score_boost, min_base_score, "
            " active, generation, ticker_scope, trade_count, backtest_priority, "
            " promotion_status, oos_validation_json, queue_tier, paper_book_json, "
            " regime_affinity_json, lifecycle_stage, pattern_evidence_kind, created_at, updated_at) "
            "VALUES (:name, :desc, :rules, 'user_seeded', 'stocks', '1d', 0.5, "
            " 0, 0, 1.5, 4.0, TRUE, 0, 'universal', 0, 0, "
            " 'legacy', '{}'::jsonb, 'full', '{}'::jsonb, '{}'::jsonb, 'candidate', 'realized_pnl', "
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {
            "name": pat["name"],
            "desc": pat["desc"],
            "rules": _json.dumps(pat["rules"]),
        })
    conn.commit()

    if "trading_insights" not in _tables(conn):
        return

    user_ids = [
        r[0] for r in conn.execute(
            text("SELECT DISTINCT user_id FROM trading_insights")
        ).fetchall()
    ]
    if not user_ids:
        user_ids = [None]

    user_match = "user_id IS NOT DISTINCT FROM :uid"
    now_sql = "CURRENT_TIMESTAMP"
    active_val = "TRUE"
    for pat in _PATTERNS:
        pat_desc = f"{pat['name']} \u2014 {pat['desc']}"
        sp_row = conn.execute(
            text("SELECT id FROM scan_patterns WHERE name = :n LIMIT 1"),
            {"n": pat["name"]},
        ).fetchone()
        if not sp_row:
            continue
        scan_pattern_id = int(sp_row[0])
        for uid in user_ids:
            existing = conn.execute(
                text(
                    "SELECT id FROM trading_insights "
                    "WHERE pattern_description LIKE :pat AND " + user_match
                ),
                {"pat": f"{pat['name']}%", "uid": uid},
            ).fetchone()
            if existing:
                continue
            conn.execute(text(
                "INSERT INTO trading_insights "
                "(user_id, scan_pattern_id, pattern_description, confidence, evidence_count, "
                " last_seen, created_at, active, win_count, loss_count) "
                f"VALUES (:uid, :spid, :desc, 0.5, 0, {now_sql}, {now_sql}, {active_val}, 0, 0)"
            ), {"uid": uid, "spid": scan_pattern_id, "desc": pat_desc})
    conn.commit()


def _migration_032_seed_candlestick_patterns(conn) -> None:
    """Seed 3 profitable candlestick chart patterns with confirmation rules.

    Source: 'Top 3 MOST Profitable Candlestick Chart Patterns (Full Training)'
    YouTube video + QuantifiedStrategies.com backtest data (56K trades).
    """
    import json as _json

    if "scan_patterns" not in _tables(conn):
        return

    _PATTERNS = [
        {
            "name": "Bullish Engulfing + RSI Oversold + Volume Surge",
            "desc": (
                "Bullish engulfing candle at oversold RSI (< 40) with "
                "elevated relative volume (>= 1.5x) and price above EMA 50 "
                "(uptrend context). Backtested at 60-65% win rate with "
                "confirmation. [User-seeded from candlestick patterns research]"
            ),
            "rules": {
                "conditions": [
                    {"indicator": "bullish_engulfing", "op": "==", "value": True},
                    {"indicator": "rsi_14", "op": "<", "value": 40},
                    {"indicator": "rel_vol", "op": ">=", "value": 1.5},
                    {"indicator": "price", "op": ">", "ref": "ema_50"},
                ]
            },
        },
        {
            "name": "Hammer Reversal at Support with Volume",
            "desc": (
                "Hammer candle (long lower wick, small body near top) at "
                "deeply oversold RSI (< 35), near support (within 2% of "
                "resistance), with volume confirmation (>= 1.5x). "
                "Inverted hammer variant ranks #1 in 56K-trade backtests "
                "with 60% win rate. [User-seeded from candlestick patterns research]"
            ),
            "rules": {
                "conditions": [
                    {"indicator": "hammer", "op": "==", "value": True},
                    {"indicator": "rsi_14", "op": "<", "value": 35},
                    {"indicator": "rel_vol", "op": ">=", "value": 1.5},
                    {"indicator": "price", "op": ">", "ref": "ema_100"},
                ]
            },
        },
        {
            "name": "Morning Star Reversal + Trend Confirmation",
            "desc": (
                "Three-candle morning star reversal pattern (bearish candle, "
                "small-body candle, then bullish candle closing above the "
                "first candle's midpoint). Confirmed by RSI below 45 "
                "(room to run) and MACD histogram turning positive. "
                "Strong in stocks and crypto. "
                "[User-seeded from candlestick patterns research]"
            ),
            "rules": {
                "conditions": [
                    {"indicator": "morning_star", "op": "==", "value": True},
                    {"indicator": "rsi_14", "op": "<", "value": 45},
                    {"indicator": "macd_hist", "op": ">", "value": 0},
                    {"indicator": "price", "op": ">", "ref": "ema_50"},
                ]
            },
        },
    ]

    for pat in _PATTERNS:
        existing = conn.execute(
            text("SELECT id FROM scan_patterns WHERE name = :n"),
            {"n": pat["name"]},
        ).fetchone()
        if existing:
            continue
        conn.execute(text(
            "INSERT INTO scan_patterns "
            "(name, description, rules_json, origin, asset_class, timeframe, confidence, "
            " evidence_count, backtest_count, score_boost, min_base_score, "
            " active, generation, ticker_scope, trade_count, backtest_priority, "
            " promotion_status, oos_validation_json, queue_tier, paper_book_json, "
            " regime_affinity_json, lifecycle_stage, pattern_evidence_kind, created_at, updated_at) "
            "VALUES (:name, :desc, :rules, 'user_seeded', 'all', '1d', 0.5, "
            " 0, 0, 1.5, 4.0, TRUE, 0, 'universal', 0, 0, "
            " 'legacy', '{}'::jsonb, 'full', '{}'::jsonb, '{}'::jsonb, 'candidate', 'realized_pnl', "
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {
            "name": pat["name"],
            "desc": pat["desc"],
            "rules": _json.dumps(pat["rules"]),
        })
    conn.commit()

    if "trading_insights" not in _tables(conn):
        return

    user_ids = [
        r[0] for r in conn.execute(
            text("SELECT DISTINCT user_id FROM trading_insights")
        ).fetchall()
    ]
    if not user_ids:
        user_ids = [None]

    user_match = "user_id IS NOT DISTINCT FROM :uid"
    now_sql = "CURRENT_TIMESTAMP"
    active_val = "TRUE"
    for pat in _PATTERNS:
        pat_desc = f"{pat['name']} \u2014 {pat['desc']}"
        sp_row = conn.execute(
            text("SELECT id FROM scan_patterns WHERE name = :n LIMIT 1"),
            {"n": pat["name"]},
        ).fetchone()
        if not sp_row:
            continue
        scan_pattern_id = int(sp_row[0])
        for uid in user_ids:
            existing = conn.execute(
                text(
                    "SELECT id FROM trading_insights "
                    "WHERE pattern_description LIKE :pat AND " + user_match
                ),
                {"pat": f"{pat['name']}%", "uid": uid},
            ).fetchone()
            if existing:
                continue
            conn.execute(text(
                "INSERT INTO trading_insights "
                "(user_id, scan_pattern_id, pattern_description, confidence, evidence_count, "
                " last_seen, created_at, active, win_count, loss_count) "
                f"VALUES (:uid, :spid, :desc, 0.5, 0, {now_sql}, {now_sql}, {active_val}, 0, 0)"
            ), {"uid": uid, "spid": scan_pattern_id, "desc": pat_desc})
    conn.commit()


def _migration_033_insight_pattern_fk(conn) -> None:
    """Add scan_pattern_id FK to trading_insights and backfill from text matching."""
    if "trading_insights" not in _tables(conn):
        return
    cols = _columns(conn, "trading_insights")
    if "scan_pattern_id" not in cols:
        conn.execute(text(
            "ALTER TABLE trading_insights ADD COLUMN scan_pattern_id INTEGER"
        ))
        conn.commit()

    if "scan_patterns" not in _tables(conn):
        return

    patterns = conn.execute(text(
        "SELECT id, name FROM scan_patterns WHERE name IS NOT NULL"
    )).fetchall()
    if not patterns:
        return

    insights = conn.execute(text(
        "SELECT id, pattern_description FROM trading_insights "
        "WHERE scan_pattern_id IS NULL"
    )).fetchall()

    for ins_id, desc in insights:
        if not desc:
            continue
        name_part = desc.split("\u2014")[0].split(" - ")[0].strip()
        matched_id = None
        for pid, pname in patterns:
            if pname == name_part:
                matched_id = pid
                break
        if not matched_id:
            desc_lower = desc.lower()
            for pid, pname in patterns:
                if pname and pname.lower() in desc_lower:
                    matched_id = pid
                    break
        if matched_id:
            conn.execute(text(
                "UPDATE trading_insights SET scan_pattern_id = :pid WHERE id = :iid"
            ), {"pid": matched_id, "iid": ins_id})

    conn.commit()


def _migration_034_pattern_backtest_queue(conn) -> None:
    """Add backtest queue columns to scan_patterns for priority-based processing."""
    if "scan_patterns" not in _tables(conn):
        return
    sp_cols = _columns(conn, "scan_patterns")
    if "trade_count" not in sp_cols:
        conn.execute(text("ALTER TABLE scan_patterns ADD COLUMN trade_count INTEGER DEFAULT 0"))
        conn.commit()
    if "backtest_priority" not in sp_cols:
        conn.execute(text("ALTER TABLE scan_patterns ADD COLUMN backtest_priority INTEGER DEFAULT 0"))
        conn.commit()
    if "last_backtest_at" not in sp_cols:
        conn.execute(text("ALTER TABLE scan_patterns ADD COLUMN last_backtest_at DATETIME"))
        conn.commit()


def _migration_035_backtest_scan_pattern_fk(conn) -> None:
    """Add scan_pattern_id to trading_backtests; backfill from insights and strategy names."""
    if "trading_backtests" not in _tables(conn):
        return
    bt_cols = _columns(conn, "trading_backtests")
    if "scan_pattern_id" not in bt_cols:
        conn.execute(text("ALTER TABLE trading_backtests ADD COLUMN scan_pattern_id INTEGER"))
        conn.commit()

    if "trading_insights" in _tables(conn):
        conn.execute(text(
            "UPDATE trading_backtests SET scan_pattern_id = ("
            "SELECT ti.scan_pattern_id FROM trading_insights ti "
            "WHERE ti.id = trading_backtests.related_insight_id"
            ") WHERE related_insight_id IS NOT NULL AND scan_pattern_id IS NULL"
        ))
        conn.commit()

    if "scan_patterns" not in _tables(conn) or "trading_insights" not in _tables(conn):
        return

    patterns = conn.execute(text(
        "SELECT id, name FROM scan_patterns WHERE name IS NOT NULL"
    )).fetchall()
    if not patterns:
        return
    name_lower_to_id: dict[str, int] = {}
    for pid, pname in patterns:
        key = (pname or "").strip().lower()
        if key and key not in name_lower_to_id:
            name_lower_to_id[key] = int(pid)

    null_insights = conn.execute(text(
        "SELECT id FROM trading_insights WHERE scan_pattern_id IS NULL"
    )).fetchall()
    for (iid,) in null_insights:
        rows = conn.execute(
            text(
                "SELECT DISTINCT strategy_name FROM trading_backtests "
                "WHERE related_insight_id = :iid AND strategy_name IS NOT NULL"
            ),
            {"iid": iid},
        ).fetchall()
        matched = None
        for (sn,) in rows:
            key = (sn or "").strip().lower()
            matched = name_lower_to_id.get(key)
            if matched:
                break
        if matched:
            conn.execute(
                text("UPDATE trading_insights SET scan_pattern_id = :pid WHERE id = :iid"),
                {"pid": matched, "iid": iid},
            )

    conn.commit()

    conn.execute(text(
        "UPDATE trading_backtests SET scan_pattern_id = ("
        "SELECT ti.scan_pattern_id FROM trading_insights ti "
        "WHERE ti.id = trading_backtests.related_insight_id"
        ") WHERE related_insight_id IS NOT NULL AND scan_pattern_id IS NULL"
    ))
    conn.commit()


def _migration_036_pattern_trade_analytics(conn) -> None:
    """Pattern trade analytics: per-occurrence rows + evidence hypothesis cards."""
    tables = _tables(conn)
    if "trading_pattern_trades" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pattern_trades (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                scan_pattern_id INTEGER,
                related_insight_id INTEGER,
                backtest_result_id INTEGER,
                ticker VARCHAR(20) NOT NULL,
                as_of_ts TIMESTAMP NOT NULL,
                timeframe VARCHAR(10) NOT NULL DEFAULT '1d',
                asset_class VARCHAR(20) NOT NULL DEFAULT 'stock',
                fwd_ret_1b REAL,
                fwd_ret_3b REAL,
                fwd_ret_5b REAL,
                fwd_ret_10b REAL,
                mfe_pct REAL,
                mae_pct REAL,
                hold_bars INTEGER,
                r_multiple REAL,
                outcome_return_pct REAL,
                label_win BOOLEAN,
                features_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                source VARCHAR(40) NOT NULL DEFAULT 'queue_backtest',
                feature_schema_version VARCHAR(20) NOT NULL DEFAULT '1',
                code_version VARCHAR(40),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_ptt_pattern_asof ON trading_pattern_trades (scan_pattern_id, as_of_ts)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_ptt_ticker_asof ON trading_pattern_trades (ticker, as_of_ts)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_ptt_pattern_ticker ON trading_pattern_trades (scan_pattern_id, ticker)"
        ))
        conn.commit()
    if "trading_pattern_evidence_hypotheses" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pattern_evidence_hypotheses (
                id SERIAL PRIMARY KEY,
                scan_pattern_id INTEGER NOT NULL,
                title VARCHAR(200) NOT NULL,
                predicate_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                status VARCHAR(20) NOT NULL DEFAULT 'proposed',
                metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_peh_pattern ON trading_pattern_evidence_hypotheses (scan_pattern_id)"
        ))
        conn.commit()


def _migration_037_learning_cycle_ai_reports(conn) -> None:
    """Persist per-cycle AI deep-study reports for the Brain UI."""
    tables = _tables(conn)
    if "trading_learning_cycle_ai_reports" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_learning_cycle_ai_reports (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                content TEXT NOT NULL,
                metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_tlcai_user_created ON trading_learning_cycle_ai_reports (user_id, created_at DESC)"
        ))
        conn.commit()


def _migration_038_brain_worker_control_stop_heartbeat(conn) -> None:
    """stop_requested + last_heartbeat_at for cooperative stop and stale-PID detection."""
    if "brain_worker_control" not in _tables(conn):
        return
    cols = _columns(conn, "brain_worker_control")
    if "stop_requested" not in cols:
        conn.execute(
            text(
                "ALTER TABLE brain_worker_control ADD COLUMN stop_requested BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        conn.commit()
    if "last_heartbeat_at" not in cols:
        conn.execute(text("ALTER TABLE brain_worker_control ADD COLUMN last_heartbeat_at TIMESTAMP"))
        conn.commit()


def _migration_039_scan_pattern_oos_promotion(conn) -> None:
    """OOS metrics, promotion status, and backtest friction snapshot for ScanPattern."""
    if "scan_patterns" not in _tables(conn):
        return
    cols = _columns(conn, "scan_patterns")
    if "promotion_status" not in cols:
        conn.execute(
            text(
                "ALTER TABLE scan_patterns ADD COLUMN promotion_status VARCHAR(32) NOT NULL DEFAULT 'legacy'"
            )
        )
        conn.commit()
    for col, typ in (
        ("oos_win_rate", "DOUBLE PRECISION"),
        ("oos_avg_return_pct", "DOUBLE PRECISION"),
        ("oos_trade_count", "INTEGER"),
        ("backtest_spread_used", "DOUBLE PRECISION"),
        ("backtest_commission_used", "DOUBLE PRECISION"),
    ):
        if col not in _columns(conn, "scan_patterns"):
            conn.execute(text(f"ALTER TABLE scan_patterns ADD COLUMN {col} {typ}"))
            conn.commit()
    if "oos_evaluated_at" not in _columns(conn, "scan_patterns"):
        conn.execute(text("ALTER TABLE scan_patterns ADD COLUMN oos_evaluated_at TIMESTAMP"))
        conn.commit()


def _migration_040_scan_pattern_bench_walk_forward(conn) -> None:
    """JSON summary of benchmark walk-forward evaluation on scan_patterns."""
    if "scan_patterns" not in _tables(conn):
        return
    if "bench_walk_forward_json" not in _columns(conn, "scan_patterns"):
        conn.execute(
            text("ALTER TABLE scan_patterns ADD COLUMN bench_walk_forward_json JSONB")
        )
        conn.commit()


def _migration_041_trade_tca_columns(conn) -> None:
    """TCA: reference entry price and computed slippage (bps) on trading_trades."""
    if "trading_trades" not in _tables(conn):
        return
    tt = _columns(conn, "trading_trades")
    if "tca_reference_entry_price" not in tt:
        conn.execute(
            text("ALTER TABLE trading_trades ADD COLUMN tca_reference_entry_price DOUBLE PRECISION")
        )
        conn.commit()
    if "tca_entry_slippage_bps" not in tt:
        conn.execute(
            text("ALTER TABLE trading_trades ADD COLUMN tca_entry_slippage_bps DOUBLE PRECISION")
        )
        conn.commit()


def _migration_042_trade_attribution_exit_tca(conn) -> None:
    """Trade proposal/pattern attribution + exit TCA columns; proposal.scan_pattern_id."""
    if "trading_trades" in _tables(conn):
        tt = _columns(conn, "trading_trades")
        for col, typ in (
            ("tca_reference_exit_price", "DOUBLE PRECISION"),
            ("tca_exit_slippage_bps", "DOUBLE PRECISION"),
            ("strategy_proposal_id", "INTEGER"),
            ("scan_pattern_id", "INTEGER"),
        ):
            if col not in tt:
                conn.execute(text(f"ALTER TABLE trading_trades ADD COLUMN {col} {typ}"))
                conn.commit()
        # Indexes (idempotent names)
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS ix_trading_trades_strategy_proposal_id ON trading_trades (strategy_proposal_id)",
            "CREATE INDEX IF NOT EXISTS ix_trading_trades_scan_pattern_id ON trading_trades (scan_pattern_id)",
        ):
            try:
                conn.execute(text(idx_sql))
                conn.commit()
            except Exception:
                conn.rollback()
    if "trading_proposals" in _tables(conn):
        tp = _columns(conn, "trading_proposals")
        if "scan_pattern_id" not in tp:
            conn.execute(text("ALTER TABLE trading_proposals ADD COLUMN scan_pattern_id INTEGER"))
            conn.commit()
        try:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_trading_proposals_scan_pattern_id "
                    "ON trading_proposals (scan_pattern_id)"
                )
            )
            conn.commit()
        except Exception:
            conn.rollback()


def _migration_043_insight_scan_pattern_required(conn) -> None:
    """Backfill trading_insights.scan_pattern_id, sentinel for orphans, NOT NULL + FK.

    ID-first model: linkage is only by integer FK; name-parsing is used once here for backfill.
    """
    if "trading_insights" not in _tables(conn) or "scan_patterns" not in _tables(conn):
        return
    ti_cols = _columns(conn, "trading_insights")
    if "scan_pattern_id" not in ti_cols:
        conn.execute(text("ALTER TABLE trading_insights ADD COLUMN scan_pattern_id INTEGER"))
        conn.commit()

    sent_name = "[Unlinked legacy insight]"
    row = conn.execute(
        text(
            "SELECT id FROM scan_patterns WHERE name = :n AND origin = 'legacy_unlinked' LIMIT 1"
        ),
        {"n": sent_name},
    ).fetchone()
    if not row:
        conn.execute(
            text(
                "INSERT INTO scan_patterns (name, description, rules_json, origin, asset_class, "
                "timeframe, confidence, evidence_count, backtest_count, score_boost, min_base_score, "
                "active, generation, ticker_scope, trade_count, backtest_priority, promotion_status, "
                "oos_validation_json, queue_tier, paper_book_json, regime_affinity_json, lifecycle_stage, "
                "pattern_evidence_kind, created_at, updated_at) "
                "VALUES (:name, :desc, '{}', 'legacy_unlinked', 'all', '1d', 0.0, 0, 0, 0.0, 0.0, "
                "FALSE, 0, 'universal', 0, 0, 'legacy', "
                "'{}'::jsonb, 'full', '{}'::jsonb, '{}'::jsonb, 'candidate', 'realized_pnl', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "name": sent_name,
                "desc": "Placeholder for insights that could not be linked to a real ScanPattern.",
            },
        )
        conn.commit()
        row = conn.execute(
            text(
                "SELECT id FROM scan_patterns WHERE name = :n AND origin = 'legacy_unlinked' LIMIT 1"
            ),
            {"n": sent_name},
        ).fetchone()
    sentinel_id = int(row[0])

    # Orphan FKs → sentinel
    conn.execute(
        text(
            "UPDATE trading_insights ti SET scan_pattern_id = :sid "
            "WHERE ti.scan_pattern_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM scan_patterns sp WHERE sp.id = ti.scan_pattern_id)"
        ),
        {"sid": sentinel_id},
    )
    conn.commit()

    # A) Majority scan_pattern_id from backtests (tie-break higher backtest id)
    if "trading_backtests" in _tables(conn):
        null_rows = conn.execute(
            text("SELECT id FROM trading_insights WHERE scan_pattern_id IS NULL")
        ).fetchall()
        for (ins_id,) in null_rows:
            bt_row = conn.execute(
                text(
                    "SELECT scan_pattern_id, COUNT(*) AS c FROM trading_backtests "
                    "WHERE related_insight_id = :iid AND scan_pattern_id IS NOT NULL "
                    "GROUP BY scan_pattern_id "
                    "ORDER BY c DESC, MAX(id) DESC LIMIT 1"
                ),
                {"iid": ins_id},
            ).fetchone()
            if bt_row and bt_row[0]:
                spid = int(bt_row[0])
                exists = conn.execute(
                    text("SELECT 1 FROM scan_patterns WHERE id = :id LIMIT 1"),
                    {"id": spid},
                ).fetchone()
                if exists:
                    conn.execute(
                        text("UPDATE trading_insights SET scan_pattern_id = :p WHERE id = :i"),
                        {"p": spid, "i": ins_id},
                    )
        conn.commit()

    # B) Exact name prefix match, C) fuzzy name-in-desc (same as legacy 033)
    patterns = conn.execute(
        text("SELECT id, name FROM scan_patterns WHERE name IS NOT NULL ORDER BY id")
    ).fetchall()
    null_rows = conn.execute(
        text("SELECT id, pattern_description FROM trading_insights WHERE scan_pattern_id IS NULL")
    ).fetchall()
    for ins_id, desc in null_rows:
        if not desc:
            continue
        name_part = desc.split("\u2014")[0].split(" - ")[0].strip()
        matched_id = None
        for pid, pname in patterns:
            if pname == name_part:
                matched_id = int(pid)
                break
        if not matched_id:
            desc_lower = desc.lower()
            for pid, pname in patterns:
                if pname and pname.lower() in desc_lower:
                    matched_id = int(pid)
                    break
        if matched_id:
            conn.execute(
                text("UPDATE trading_insights SET scan_pattern_id = :p WHERE id = :i"),
                {"p": matched_id, "i": ins_id},
            )
    conn.commit()

    conn.execute(
        text(
            "UPDATE trading_insights SET scan_pattern_id = :sid WHERE scan_pattern_id IS NULL"
        ),
        {"sid": sentinel_id},
    )
    conn.commit()

    # NOT NULL
    conn.execute(
        text("ALTER TABLE trading_insights ALTER COLUMN scan_pattern_id SET NOT NULL")
    )
    conn.commit()

    # FK (idempotent)
    exists_fk = conn.execute(
        text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_name = 'trading_insights' "
            "AND constraint_type = 'FOREIGN KEY' "
            "AND constraint_name = 'fk_trading_insights_scan_pattern_id'"
        )
    ).fetchone()
    if not exists_fk:
        conn.execute(
            text(
                "ALTER TABLE trading_insights "
                "ADD CONSTRAINT fk_trading_insights_scan_pattern_id "
                "FOREIGN KEY (scan_pattern_id) REFERENCES scan_patterns (id)"
            )
        )
        conn.commit()


def _migration_044_trading_insight_scan_pattern_constraints(conn) -> None:
    """Idempotent repair: NOT NULL + FK + index on trading_insights.scan_pattern_id.

    Catches databases where 043 did not run, failed partway, or were created from
    SQLAlchemy ``create_all`` without PostgreSQL constraints.
    """
    if "trading_insights" not in _tables(conn) or "scan_patterns" not in _tables(conn):
        return

    ti_cols = _columns(conn, "trading_insights")
    if "scan_pattern_id" not in ti_cols:
        conn.execute(text("ALTER TABLE trading_insights ADD COLUMN scan_pattern_id INTEGER"))
        conn.commit()
        ti_cols = _columns(conn, "trading_insights")

    sent_name = "[Unlinked legacy insight]"
    row = conn.execute(
        text(
            "SELECT id FROM scan_patterns WHERE name = :n AND origin = 'legacy_unlinked' LIMIT 1"
        ),
        {"n": sent_name},
    ).fetchone()
    if not row:
        conn.execute(
            text(
                "INSERT INTO scan_patterns (name, description, rules_json, origin, asset_class, "
                "timeframe, confidence, evidence_count, backtest_count, score_boost, min_base_score, "
                "active, generation, ticker_scope, trade_count, backtest_priority, promotion_status, "
                "oos_validation_json, queue_tier, paper_book_json, regime_affinity_json, lifecycle_stage, "
                "pattern_evidence_kind, created_at, updated_at) "
                "VALUES (:name, :desc, '{}', 'legacy_unlinked', 'all', '1d', 0.0, 0, 0, 0.0, 0.0, "
                "FALSE, 0, 'universal', 0, 0, 'legacy', "
                "'{}'::jsonb, 'full', '{}'::jsonb, '{}'::jsonb, 'candidate', 'realized_pnl', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "name": sent_name,
                "desc": "Placeholder for insights that could not be linked to a real ScanPattern.",
            },
        )
        conn.commit()
        row = conn.execute(
            text(
                "SELECT id FROM scan_patterns WHERE name = :n AND origin = 'legacy_unlinked' LIMIT 1"
            ),
            {"n": sent_name},
        ).fetchone()
    sentinel_id = int(row[0])

    conn.execute(
        text(
            "UPDATE trading_insights ti SET scan_pattern_id = :sid "
            "WHERE ti.scan_pattern_id IS NULL"
        ),
        {"sid": sentinel_id},
    )
    conn.execute(
        text(
            "UPDATE trading_insights ti SET scan_pattern_id = :sid "
            "WHERE ti.scan_pattern_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM scan_patterns sp WHERE sp.id = ti.scan_pattern_id)"
        ),
        {"sid": sentinel_id},
    )
    conn.commit()

    null_row = conn.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'trading_insights' "
            "AND column_name = 'scan_pattern_id'"
        )
    ).fetchone()
    if null_row and str(null_row[0]).upper() == "YES":
        conn.execute(
            text("ALTER TABLE trading_insights ALTER COLUMN scan_pattern_id SET NOT NULL")
        )
        conn.commit()

    fk_row = conn.execute(
        text(
            "SELECT tc.constraint_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_schema = kcu.constraint_schema "
            "AND tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema "
            "WHERE tc.table_schema = current_schema() "
            "AND tc.table_name = 'trading_insights' "
            "AND tc.constraint_type = 'FOREIGN KEY' "
            "AND kcu.column_name = 'scan_pattern_id'"
        )
    ).fetchone()
    if not fk_row:
        conn.execute(
            text(
                "ALTER TABLE trading_insights "
                "ADD CONSTRAINT fk_trading_insights_scan_pattern_id "
                "FOREIGN KEY (scan_pattern_id) REFERENCES scan_patterns (id) "
                "ON DELETE RESTRICT"
            )
        )
        conn.commit()

    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_trading_insights_scan_pattern_id "
            "ON trading_insights (scan_pattern_id)"
        )
    )
    conn.commit()


def _migration_045_trading_alert_scan_pattern_id(conn) -> None:
    """Optional FK context on trading_alerts for pattern-tied messages (dedupe/UI)."""
    if "trading_alerts" not in _tables(conn):
        return
    cols = _columns(conn, "trading_alerts")
    if "scan_pattern_id" not in cols:
        conn.execute(text("ALTER TABLE trading_alerts ADD COLUMN scan_pattern_id INTEGER"))
        conn.commit()
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_trading_alerts_scan_pattern_id "
            "ON trading_alerts (scan_pattern_id)"
        )
    )
    conn.commit()


def _migration_046_hypothesis_family_columns(conn) -> None:
    """ScanPattern + TradingInsight hypothesis_family for miner/evolution taxonomy (OOS gates, UI)."""
    if "scan_patterns" in _tables(conn):
        if "hypothesis_family" not in _columns(conn, "scan_patterns"):
            conn.execute(
                text("ALTER TABLE scan_patterns ADD COLUMN hypothesis_family VARCHAR(32)")
            )
            conn.commit()
    if "trading_insights" in _tables(conn):
        if "hypothesis_family" not in _columns(conn, "trading_insights"):
            conn.execute(
                text("ALTER TABLE trading_insights ADD COLUMN hypothesis_family VARCHAR(32)")
            )
            conn.commit()


def _migration_047_scan_pattern_research_quant_columns(conn) -> None:
    """OOS validation JSON, queue tier for two-stage backtests, optional paper book."""
    if "scan_patterns" not in _tables(conn):
        return
    cols = _columns(conn, "scan_patterns")
    if "oos_validation_json" not in cols:
        conn.execute(
            text(
                "ALTER TABLE scan_patterns ADD COLUMN oos_validation_json JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
        )
        conn.commit()
    if "queue_tier" not in cols:
        conn.execute(
            text(
                "ALTER TABLE scan_patterns ADD COLUMN queue_tier VARCHAR(16) NOT NULL DEFAULT 'full'"
            )
        )
        conn.commit()
    if "paper_book_json" not in cols:
        conn.execute(
            text(
                "ALTER TABLE scan_patterns ADD COLUMN paper_book_json JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
        )
        conn.commit()


def _migration_048_brain_learning_cycle_tables(conn) -> None:
    """Trading-brain Phase 1: learning cycle run + stage job tables."""
    tables = _tables(conn)
    if "brain_learning_cycle_run" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE brain_learning_cycle_run (
                    id SERIAL PRIMARY KEY,
                    correlation_id VARCHAR(64) NOT NULL,
                    universe_id VARCHAR(64),
                    status VARCHAR(24) NOT NULL,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    meta_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_brain_lcr_correlation_id ON brain_learning_cycle_run (correlation_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_brain_lcr_universe_id ON brain_learning_cycle_run (universe_id)"
            )
        )
        conn.commit()
    if "brain_stage_job" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE brain_stage_job (
                    id SERIAL PRIMARY KEY,
                    cycle_run_id INTEGER NOT NULL
                        REFERENCES brain_learning_cycle_run(id) ON DELETE CASCADE,
                    stage_key VARCHAR(64) NOT NULL,
                    ordinal INTEGER NOT NULL,
                    status VARCHAR(24) NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    lease_until TIMESTAMP,
                    worker_id VARCHAR(128),
                    input_artifact_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
                    output_artifact_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
                    error_detail TEXT,
                    skip_reason VARCHAR(255),
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    CONSTRAINT uq_brain_stage_job_cycle_ordinal UNIQUE (cycle_run_id, ordinal)
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_brain_stage_job_cycle_run_id ON brain_stage_job (cycle_run_id)"
            )
        )
        conn.commit()


def _migration_049_brain_cycle_lease(conn) -> None:
    """Single-flight lease row (seed scope_key=global)."""
    tables = _tables(conn)
    if "brain_cycle_lease" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE brain_cycle_lease (
                    scope_key VARCHAR(64) PRIMARY KEY,
                    cycle_run_id INTEGER REFERENCES brain_learning_cycle_run(id) ON DELETE SET NULL,
                    holder_id VARCHAR(128) NOT NULL,
                    acquired_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
                """
            )
        )
        conn.commit()
    conn.execute(
        text(
            """
            INSERT INTO brain_cycle_lease (scope_key, holder_id)
            VALUES ('global', '')
            ON CONFLICT (scope_key) DO NOTHING
            """
        )
    )
    conn.commit()


def _migration_050_brain_integration_event(conn) -> None:
    """Inbound integration event idempotency store (Phase 1: table only)."""
    tables = _tables(conn)
    if "brain_integration_event" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE brain_integration_event (
                    idempotency_key VARCHAR(256) PRIMARY KEY,
                    event_id VARCHAR(64) NOT NULL,
                    event_type VARCHAR(64) NOT NULL,
                    payload_hash VARCHAR(128) NOT NULL,
                    payload_json JSONB NOT NULL,
                    received_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP,
                    status VARCHAR(24) NOT NULL
                )
                """
            )
        )
        conn.commit()


def _migration_051_brain_prediction_snapshot(conn) -> None:
    """Phase 4: append-only mirror of legacy get_current_predictions (dual-write; not read-authoritative)."""
    tables = _tables(conn)
    if "brain_prediction_snapshot" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE brain_prediction_snapshot (
                    id BIGSERIAL PRIMARY KEY,
                    as_of_ts TIMESTAMP NOT NULL,
                    universe_fingerprint VARCHAR(64) NOT NULL,
                    ticker_count INTEGER NOT NULL,
                    source_tag VARCHAR(64) NOT NULL DEFAULT 'legacy_get_current_predictions',
                    correlation_id VARCHAR(40) NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_brain_prediction_snapshot_universe_fp ON brain_prediction_snapshot (universe_fingerprint)"
            )
        )
        conn.commit()
    if "brain_prediction_line" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE brain_prediction_line (
                    id BIGSERIAL PRIMARY KEY,
                    snapshot_id BIGINT NOT NULL REFERENCES brain_prediction_snapshot(id) ON DELETE CASCADE,
                    sort_rank INTEGER NOT NULL,
                    ticker VARCHAR(32) NOT NULL,
                    score DOUBLE PRECISION NOT NULL,
                    confidence INTEGER,
                    direction VARCHAR(32),
                    price DOUBLE PRECISION,
                    meta_ml_probability DOUBLE PRECISION,
                    vix_regime VARCHAR(32),
                    signals_json JSONB NOT NULL DEFAULT '[]',
                    matched_patterns_json JSONB NOT NULL DEFAULT '[]',
                    suggested_stop DOUBLE PRECISION,
                    suggested_target DOUBLE PRECISION,
                    risk_reward DOUBLE PRECISION,
                    position_size_pct DOUBLE PRECISION
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_brain_prediction_line_snapshot_id ON brain_prediction_line (snapshot_id)"
            )
        )
        conn.commit()


def _migration_052_planner_coding_task_layer(conn) -> None:
    """Phase 1: minimal plan_tasks coding columns + adjacent coding/PO-v2/validation tables."""
    if "plan_tasks" not in _tables(conn):
        return
    pt_cols = _columns(conn, "plan_tasks")
    if "coding_workflow_mode" not in pt_cols:
        conn.execute(
            text(
                "ALTER TABLE plan_tasks ADD COLUMN coding_workflow_mode VARCHAR(32) "
                "NOT NULL DEFAULT 'tracked'"
            )
        )
        conn.commit()
        pt_cols = _columns(conn, "plan_tasks")
    if "coding_readiness_state" not in pt_cols:
        conn.execute(
            text(
                "ALTER TABLE plan_tasks ADD COLUMN coding_readiness_state VARCHAR(40) "
                "NOT NULL DEFAULT 'not_started'"
            )
        )
        conn.commit()

    tables = _tables(conn)

    if "plan_task_coding_profile" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE plan_task_coding_profile (
                    task_id INTEGER PRIMARY KEY REFERENCES plan_tasks(id) ON DELETE CASCADE,
                    repo_index INTEGER NOT NULL DEFAULT 0,
                    sub_path TEXT NOT NULL DEFAULT '',
                    brief_approved_at TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.commit()

    if "task_clarification" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE task_clarification (
                    id SERIAL PRIMARY KEY,
                    task_id INTEGER NOT NULL REFERENCES plan_tasks(id) ON DELETE CASCADE,
                    question TEXT NOT NULL,
                    answer TEXT,
                    status VARCHAR(20) NOT NULL DEFAULT 'open',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_task_clarification_task_id ON task_clarification(task_id)"))
        conn.commit()

    if "coding_task_brief" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE coding_task_brief (
                    id SERIAL PRIMARY KEY,
                    task_id INTEGER NOT NULL REFERENCES plan_tasks(id) ON DELETE CASCADE,
                    body TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_coding_task_brief_task_id ON coding_task_brief(task_id)"))
        conn.commit()

    if "coding_task_validation_run" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE coding_task_validation_run (
                    id SERIAL PRIMARY KEY,
                    task_id INTEGER NOT NULL REFERENCES plan_tasks(id) ON DELETE CASCADE,
                    trigger_source VARCHAR(24) NOT NULL DEFAULT 'manual',
                    status VARCHAR(24) NOT NULL DEFAULT 'pending',
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    exit_code INTEGER,
                    timed_out BOOLEAN NOT NULL DEFAULT FALSE,
                    error_message TEXT
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_ctvr_task_id ON coding_task_validation_run(task_id)"))
        conn.commit()

    if "coding_validation_artifact" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE coding_validation_artifact (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES coding_task_validation_run(id) ON DELETE CASCADE,
                    step_key VARCHAR(64) NOT NULL,
                    kind VARCHAR(32) NOT NULL,
                    content TEXT,
                    byte_length INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_cva_run_id ON coding_validation_artifact(run_id)"))
        conn.commit()

    if "coding_blocker_report" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE coding_blocker_report (
                    id SERIAL PRIMARY KEY,
                    task_id INTEGER NOT NULL REFERENCES plan_tasks(id) ON DELETE CASCADE,
                    run_id INTEGER REFERENCES coding_task_validation_run(id) ON DELETE SET NULL,
                    category VARCHAR(64) NOT NULL DEFAULT 'validation',
                    severity VARCHAR(24) NOT NULL DEFAULT 'error',
                    summary TEXT NOT NULL,
                    detail_json TEXT
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_cbr_task_id ON coding_blocker_report(task_id)"))
        conn.commit()

    try:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_plan_tasks_project_coding_mode "
                "ON plan_tasks (project_id, coding_workflow_mode)"
            )
        )
        conn.commit()
    except Exception:
        conn.rollback()


def _migration_053_coding_agent_suggestion(conn) -> None:
    """Phase 16: durable bounded snapshots of Phase 15 agent-suggest success (append-only)."""
    tables = _tables(conn)
    if "coding_agent_suggestion" in tables:
        return
    if "plan_tasks" not in tables:
        return
    conn.execute(
        text(
            """
            CREATE TABLE coding_agent_suggestion (
                id SERIAL PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES plan_tasks(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model VARCHAR(200) NOT NULL DEFAULT '',
                response_text TEXT NOT NULL DEFAULT '',
                diffs_json TEXT NOT NULL DEFAULT '[]',
                files_changed_json TEXT NOT NULL DEFAULT '[]',
                validation_json TEXT NOT NULL DEFAULT '[]',
                context_used_json TEXT NOT NULL DEFAULT '{}',
                truncation_flags_json TEXT
            )
            """
        )
    )
    conn.execute(text("CREATE INDEX ix_cas_task_id ON coding_agent_suggestion(task_id)"))
    conn.execute(text("CREATE INDEX ix_cas_user_id ON coding_agent_suggestion(user_id)"))
    conn.commit()


def _migration_054_coding_agent_suggestion_apply(conn) -> None:
    """Phase 17: append-only apply audit rows (references snapshot RESTRICT)."""
    tables = _tables(conn)
    if "coding_agent_suggestion_apply" in tables:
        return
    if "coding_agent_suggestion" not in tables:
        return
    conn.execute(
        text(
            """
            CREATE TABLE coding_agent_suggestion_apply (
                id SERIAL PRIMARY KEY,
                suggestion_id INTEGER NOT NULL REFERENCES coding_agent_suggestion(id) ON DELETE RESTRICT,
                task_id INTEGER NOT NULL REFERENCES plan_tasks(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                dry_run BOOLEAN NOT NULL DEFAULT FALSE,
                status VARCHAR(24) NOT NULL,
                message TEXT NOT NULL DEFAULT ''
            )
            """
        )
    )
    conn.execute(text("CREATE INDEX ix_casa_suggestion_id ON coding_agent_suggestion_apply(suggestion_id)"))
    conn.execute(text("CREATE INDEX ix_casa_task_id ON coding_agent_suggestion_apply(task_id)"))
    conn.execute(text("CREATE INDEX ix_casa_user_id ON coding_agent_suggestion_apply(user_id)"))
    conn.commit()


def _migration_055_brain_worker_ui_digest_json(conn) -> None:
    """Cross-process JSON blobs for Brain UI: last cycle digest + proposal skip rollup."""
    if "brain_worker_control" not in _tables(conn):
        return
    cols = _columns(conn, "brain_worker_control")
    if "last_cycle_digest_json" not in cols:
        conn.execute(text("ALTER TABLE brain_worker_control ADD COLUMN last_cycle_digest_json TEXT"))
    if "last_proposal_skips_json" not in cols:
        conn.execute(text("ALTER TABLE brain_worker_control ADD COLUMN last_proposal_skips_json TEXT"))
    conn.commit()


def _migration_056_snapshot_bar_key(conn) -> None:
    """Canonical bar identity for snapshots: interval + bar open UTC; legacy flag for pre-migration rows."""
    if "trading_snapshots" not in _tables(conn):
        return
    cols = _columns(conn, "trading_snapshots")
    if "bar_interval" not in cols:
        conn.execute(text("ALTER TABLE trading_snapshots ADD COLUMN bar_interval VARCHAR(16)"))
    if "bar_start_at" not in cols:
        conn.execute(text("ALTER TABLE trading_snapshots ADD COLUMN bar_start_at TIMESTAMP"))
    if "snapshot_legacy" not in cols:
        conn.execute(text(
            "ALTER TABLE trading_snapshots ADD COLUMN snapshot_legacy BOOLEAN NOT NULL DEFAULT TRUE"
        ))
    conn.commit()
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_trading_snapshots_bar_key "
        "ON trading_snapshots (ticker, bar_interval, bar_start_at) "
        "WHERE bar_start_at IS NOT NULL AND bar_interval IS NOT NULL"
    ))
    conn.commit()


def _migration_057_trading_insight_evidence(conn) -> None:
    """One row per (insight, ticker, interval, bar) for auditable reinforcement credits."""
    if "trading_insight_evidence" in _tables(conn):
        return
    if "trading_insights" not in _tables(conn):
        return
    conn.execute(
        text(
            """
            CREATE TABLE trading_insight_evidence (
                id SERIAL PRIMARY KEY,
                insight_id INTEGER NOT NULL REFERENCES trading_insights(id) ON DELETE CASCADE,
                ticker VARCHAR(20) NOT NULL,
                bar_interval VARCHAR(16) NOT NULL,
                bar_start_utc TIMESTAMP NOT NULL,
                source VARCHAR(24) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_tie_insight_bar "
            "ON trading_insight_evidence (insight_id, ticker, bar_interval, bar_start_utc)"
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tie_insight_id ON trading_insight_evidence (insight_id)"))
    conn.commit()


def _migration_058_trading_prescreen_artifacts(conn) -> None:
    """Daily prescreen snapshots + durable candidate rows (global + optional per-user)."""
    if "trading_prescreen_snapshots" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE trading_prescreen_snapshots (
                    id BIGSERIAL PRIMARY KEY,
                    run_id VARCHAR(64) NOT NULL UNIQUE,
                    run_started_at TIMESTAMP NOT NULL,
                    run_finished_at TIMESTAMP,
                    timezone_label VARCHAR(64) NOT NULL DEFAULT 'America/Los_Angeles',
                    settings_json JSONB,
                    status_json JSONB,
                    source_map_json JSONB,
                    inclusion_summary_json JSONB,
                    candidate_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_tps_run_started ON trading_prescreen_snapshots (run_started_at DESC)"))
        conn.commit()

    if "trading_prescreen_candidates" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE trading_prescreen_candidates (
                    id BIGSERIAL PRIMARY KEY,
                    snapshot_id BIGINT REFERENCES trading_prescreen_snapshots(id) ON DELETE SET NULL,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    ticker VARCHAR(32) NOT NULL,
                    ticker_norm VARCHAR(36) NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    modified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    entry_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
                    sources_json JSONB
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_trading_prescreen_candidate_global "
                "ON trading_prescreen_candidates (ticker_norm) WHERE user_id IS NULL"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_trading_prescreen_candidate_user "
                "ON trading_prescreen_candidates (user_id, ticker_norm) WHERE user_id IS NOT NULL"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_tpc_active_global_norm ON trading_prescreen_candidates (active, ticker_norm) "
                "WHERE user_id IS NULL"
            )
        )
        conn.commit()


def _migration_059_prescreen_asset_universe(conn) -> None:
    """Prescreen candidate row: crypto vs stock universe (analytics, filtering)."""
    if "trading_prescreen_candidates" not in _tables(conn):
        return
    cols = _columns(conn, "trading_prescreen_candidates")
    if "asset_universe" not in cols:
        conn.execute(
            text(
                "ALTER TABLE trading_prescreen_candidates "
                "ADD COLUMN asset_universe VARCHAR(16) NOT NULL DEFAULT 'stock'"
            )
        )
        conn.commit()
    conn.execute(
        text(
            "UPDATE trading_prescreen_candidates SET asset_universe = 'crypto' "
            "WHERE ticker_norm LIKE '%-USD'"
        )
    )
    conn.commit()


def _migration_060_brain_batch_jobs(conn) -> None:
    """Audit log for scheduled / batch brain jobs (prescreen, market scan, …)."""
    if "brain_batch_jobs" in _tables(conn):
        return
    conn.execute(
        text(
            """
            CREATE TABLE brain_batch_jobs (
                id VARCHAR(36) PRIMARY KEY,
                job_type VARCHAR(64) NOT NULL,
                status VARCHAR(24) NOT NULL DEFAULT 'running',
                started_at TIMESTAMP NOT NULL,
                ended_at TIMESTAMP,
                error_message TEXT,
                meta_json JSONB,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE INDEX ix_brain_batch_jobs_type_started ON brain_batch_jobs (job_type, started_at DESC)"
        )
    )
    conn.commit()


def _migration_061_brain_batch_jobs_payload_json(conn) -> None:
    """Large scan results (crypto/stock breakout lists) for cross-process reads."""
    if "brain_batch_jobs" not in _tables(conn):
        return
    cols = _columns(conn, "brain_batch_jobs")
    if "payload_json" not in cols:
        conn.execute(text("ALTER TABLE brain_batch_jobs ADD COLUMN payload_json JSONB"))
        conn.commit()


def _migration_062_breakout_alert_feedback_cols(conn) -> None:
    """Add user_id, scan_pattern_id, related_insight_id to trading_breakout_alerts for feedback linkage."""
    if "trading_breakout_alerts" not in _tables(conn):
        return
    cols = _columns(conn, "trading_breakout_alerts")
    for col_name, col_type in [
        ("user_id", "INTEGER"),
        ("scan_pattern_id", "INTEGER"),
        ("related_insight_id", "INTEGER"),
    ]:
        if col_name not in cols:
            conn.execute(text(f"ALTER TABLE trading_breakout_alerts ADD COLUMN {col_name} {col_type}"))
    for idx_name, idx_col in [
        ("ix_breakout_alert_user_id", "user_id"),
        ("ix_breakout_alert_scan_pattern_id", "scan_pattern_id"),
        ("ix_breakout_alert_related_insight_id", "related_insight_id"),
    ]:
        try:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON trading_breakout_alerts({idx_col})"))
        except Exception:
            pass
    conn.commit()


def _migration_063_composite_indexes_performance(conn) -> None:
    """Add composite and partial indexes for hot query paths."""
    _idx_defs = [
        ("ix_scans_user_scanned", "trading_scans", "(user_id, scanned_at DESC)"),
        ("ix_snapshots_future_ret_5d", "trading_snapshots", "(future_return_5d) WHERE future_return_5d IS NOT NULL"),
        ("ix_snapshots_ticker_bar", "trading_snapshots", "(ticker, bar_interval, bar_start_at DESC)"),
        ("ix_scan_patterns_active", "scan_patterns", "(active) WHERE active = true"),
        ("ix_insights_user_active", "trading_insights", "(user_id) WHERE active = true"),
    ]
    for idx_name, tbl, expr in _idx_defs:
        if tbl not in _tables(conn):
            continue
        try:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {tbl} {expr}"))
        except Exception:
            pass
    conn.commit()


def _migration_064_backtest_oos_fields(conn) -> None:
    """Add OOS walk-forward columns to trading_backtests."""
    tbl = "trading_backtests"
    if tbl not in _tables(conn):
        return
    cols = _columns(conn, tbl)
    _new = [
        ("oos_win_rate", "FLOAT"),
        ("oos_return_pct", "FLOAT"),
        ("oos_trade_count", "INTEGER"),
        ("oos_holdout_fraction", "FLOAT"),
        ("in_sample_bars", "INTEGER"),
        ("out_of_sample_bars", "INTEGER"),
    ]
    for col, typ in _new:
        if col not in cols:
            conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}"))
    conn.commit()


def _migration_065_scan_pattern_lifecycle(conn) -> None:
    """Add lifecycle_stage + lifecycle_changed_at to scan_patterns; backfill from promotion_status."""
    tbl = "scan_patterns"
    if tbl not in _tables(conn):
        return
    cols = _columns(conn, tbl)
    if "lifecycle_stage" not in cols:
        conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN lifecycle_stage VARCHAR(20) NOT NULL DEFAULT 'candidate'"))
    if "lifecycle_changed_at" not in cols:
        conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN lifecycle_changed_at TIMESTAMP"))
    conn.commit()
    # Backfill: promoted patterns → 'live', rejected → 'retired', others → 'candidate'
    try:
        conn.execute(text(f"""
            UPDATE {tbl} SET lifecycle_stage = 'live', lifecycle_changed_at = NOW()
            WHERE promotion_status = 'promoted' AND lifecycle_stage = 'candidate'
        """))
        conn.execute(text(f"""
            UPDATE {tbl} SET lifecycle_stage = 'retired', lifecycle_changed_at = NOW()
            WHERE promotion_status LIKE 'rejected%' AND lifecycle_stage = 'candidate' AND active = false
        """))
        conn.execute(text(f"""
            UPDATE {tbl} SET lifecycle_stage = 'backtested', lifecycle_changed_at = NOW()
            WHERE last_backtest_at IS NOT NULL AND lifecycle_stage = 'candidate' AND active = true
        """))
        conn.commit()
    except Exception:
        conn.rollback()


def _migration_066_foreign_keys(conn) -> None:
    """Add soft FK constraints where both tables exist. Nullify orphans first."""
    _fk_defs = [
        ("trading_backtests", "scan_pattern_id", "scan_patterns", "id", "fk_bt_scan_pattern"),
        ("trading_backtests", "related_insight_id", "trading_insights", "id", "fk_bt_insight"),
        ("trading_trades", "scan_pattern_id", "scan_patterns", "id", "fk_trade_scan_pattern"),
        ("trading_breakout_alerts", "scan_pattern_id", "scan_patterns", "id", "fk_alert_scan_pattern"),
    ]
    tables = _tables(conn)
    for child_tbl, child_col, parent_tbl, parent_col, fk_name in _fk_defs:
        if child_tbl not in tables or parent_tbl not in tables:
            continue
        if child_col not in _columns(conn, child_tbl):
            continue
        try:
            conn.execute(text(f"""
                UPDATE {child_tbl} SET {child_col} = NULL
                WHERE {child_col} IS NOT NULL
                  AND {child_col} NOT IN (SELECT {parent_col} FROM {parent_tbl})
            """))
            conn.execute(text(f"""
                ALTER TABLE {child_tbl}
                ADD CONSTRAINT {fk_name}
                FOREIGN KEY ({child_col}) REFERENCES {parent_tbl}({parent_col})
                ON DELETE SET NULL
            """))
        except Exception:
            pass
    conn.commit()


def _migration_067_data_retention_columns(conn) -> None:
    """Add archived_at column to large tables for soft-archive retention."""
    for tbl in ("trading_snapshots", "trading_backtests", "brain_batch_jobs"):
        if tbl not in _tables(conn):
            continue
        cols = _columns(conn, tbl)
        if "archived_at" not in cols:
            try:
                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN archived_at TIMESTAMP"))
            except Exception:
                pass
    conn.commit()


def _migration_069_supporting_tables(conn) -> None:
    """Create supporting tables for risk state, daily performance, playbooks, and ML model versions."""
    tables = _tables(conn)

    if "trading_risk_state" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_risk_state (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                snapshot_date DATE NOT NULL,
                open_positions INTEGER NOT NULL DEFAULT 0,
                total_heat_pct FLOAT NOT NULL DEFAULT 0,
                breaker_tripped BOOLEAN NOT NULL DEFAULT FALSE,
                breaker_reason VARCHAR(256),
                capital FLOAT NOT NULL DEFAULT 100000,
                regime VARCHAR(32),
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text("CREATE INDEX ix_risk_state_user_date ON trading_risk_state (user_id, snapshot_date DESC)"))

    if "trading_brain_performance_daily" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_brain_performance_daily (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                perf_date DATE NOT NULL,
                total_pnl FLOAT NOT NULL DEFAULT 0,
                trade_count INTEGER NOT NULL DEFAULT 0,
                win_count INTEGER NOT NULL DEFAULT 0,
                loss_count INTEGER NOT NULL DEFAULT 0,
                win_rate FLOAT,
                avg_pnl FLOAT,
                max_win FLOAT,
                max_loss FLOAT,
                patterns_active INTEGER,
                patterns_promoted INTEGER,
                signals_generated INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX uix_perf_daily_user_date ON trading_brain_performance_daily (user_id, perf_date)"))

    if "trading_daily_playbooks" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_daily_playbooks (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                playbook_date DATE NOT NULL,
                regime VARCHAR(32),
                regime_guidance TEXT,
                max_new_trades INTEGER,
                ideas_json JSONB,
                watchlist_json JSONB,
                risk_snapshot_json JSONB,
                performance_json JSONB,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX uix_playbooks_user_date ON trading_daily_playbooks (user_id, playbook_date)"))
        conn.execute(text("CREATE INDEX ix_playbooks_user_date ON trading_daily_playbooks (user_id, playbook_date DESC)"))

    if "trading_ml_model_versions" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_ml_model_versions (
                id SERIAL PRIMARY KEY,
                version_id VARCHAR(128) NOT NULL UNIQUE,
                model_type VARCHAR(64) NOT NULL,
                trained_at TIMESTAMP NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT FALSE,
                is_shadow BOOLEAN NOT NULL DEFAULT FALSE,
                metrics_json JSONB,
                file_path VARCHAR(512),
                parent_version VARCHAR(128),
                notes TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text("CREATE INDEX ix_ml_versions_type_active ON trading_ml_model_versions (model_type, is_active)"))

    conn.commit()


def _migration_068_paper_trades_table(conn) -> None:
    """Create paper trades table for simulated trading."""
    if "trading_paper_trades" in _tables(conn):
        return
    conn.execute(text("""
        CREATE TABLE trading_paper_trades (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            scan_pattern_id INTEGER,
            ticker VARCHAR(32) NOT NULL,
            direction VARCHAR(8) NOT NULL DEFAULT 'long',
            entry_price FLOAT NOT NULL,
            stop_price FLOAT,
            target_price FLOAT,
            quantity INTEGER NOT NULL DEFAULT 1,
            status VARCHAR(16) NOT NULL DEFAULT 'open',
            entry_date TIMESTAMP NOT NULL DEFAULT NOW(),
            exit_date TIMESTAMP,
            exit_price FLOAT,
            exit_reason VARCHAR(32),
            pnl FLOAT,
            pnl_pct FLOAT,
            signal_json JSONB,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(text("CREATE INDEX ix_paper_trades_user ON trading_paper_trades (user_id)"))
    conn.execute(text("CREATE INDEX ix_paper_trades_status ON trading_paper_trades (status)"))
    conn.execute(text("CREATE INDEX ix_paper_trades_pattern ON trading_paper_trades (scan_pattern_id)"))
    conn.commit()


def _migration_070_normalize_win_rate(conn) -> None:
    """Normalize win_rate/oos_win_rate from 0-100 percent to 0-1 fraction where needed."""
    tables = _tables(conn)

    if "scan_patterns" in tables:
        conn.execute(text(
            "UPDATE scan_patterns SET win_rate = win_rate / 100.0 "
            "WHERE win_rate IS NOT NULL AND win_rate > 1.0"
        ))
        conn.execute(text(
            "UPDATE scan_patterns SET oos_win_rate = oos_win_rate / 100.0 "
            "WHERE oos_win_rate IS NOT NULL AND oos_win_rate > 1.0"
        ))

    if "trading_backtests" in tables:
        conn.execute(text(
            "UPDATE trading_backtests SET win_rate = win_rate / 100.0 "
            "WHERE win_rate IS NOT NULL AND win_rate > 1.0"
        ))
        conn.execute(text(
            "UPDATE trading_backtests SET oos_win_rate = oos_win_rate / 100.0 "
            "WHERE oos_win_rate IS NOT NULL AND oos_win_rate > 1.0"
        ))

    conn.commit()


def _migration_071_reconcile_backtest_links(conn) -> None:
    """Re-link orphaned backtests by strategy_name and null out dangling FKs."""
    tables = _tables(conn)

    if "trading_backtests" not in tables:
        return

    # 1. Re-link orphaned backtests whose strategy_name matches an active scan_pattern
    if "scan_patterns" in tables:
        conn.execute(text(
            "UPDATE trading_backtests bt "
            "SET scan_pattern_id = sp.id "
            "FROM scan_patterns sp "
            "WHERE bt.scan_pattern_id IS NULL "
            "  AND bt.strategy_name = sp.name "
            "  AND sp.active = true"
        ))

    # 2. Null out dangling scan_pattern_id references
    if "scan_patterns" in tables:
        conn.execute(text(
            "UPDATE trading_backtests "
            "SET scan_pattern_id = NULL "
            "WHERE scan_pattern_id IS NOT NULL "
            "  AND scan_pattern_id NOT IN (SELECT id FROM scan_patterns)"
        ))

    # 3. Null out dangling related_insight_id references
    if "trading_insights" in tables:
        conn.execute(text(
            "UPDATE trading_backtests "
            "SET related_insight_id = NULL "
            "WHERE related_insight_id IS NOT NULL "
            "  AND related_insight_id NOT IN (SELECT id FROM trading_insights)"
        ))

    conn.commit()


def _migration_072_recompute_pattern_stats(conn) -> None:
    """Recompute ScanPattern stats (backtest_count, trade_count, win_rate)
    from actual DB rows so counters match reality after cleanup migrations."""
    tables = _tables(conn)

    if "scan_patterns" not in tables:
        return

    # 1. backtest_count from actual backtest rows
    if "trading_backtests" in tables:
        conn.execute(text(
            "UPDATE scan_patterns sp "
            "SET backtest_count = sub.cnt "
            "FROM ("
            "    SELECT scan_pattern_id, COUNT(*) AS cnt "
            "    FROM trading_backtests "
            "    WHERE scan_pattern_id IS NOT NULL "
            "    GROUP BY scan_pattern_id"
            ") sub "
            "WHERE sp.id = sub.scan_pattern_id "
            "  AND (sp.backtest_count IS NULL OR sp.backtest_count != sub.cnt)"
        ))
        # Zero out patterns with no matching backtests
        conn.execute(text(
            "UPDATE scan_patterns "
            "SET backtest_count = 0 "
            "WHERE id NOT IN ("
            "    SELECT DISTINCT scan_pattern_id FROM trading_backtests "
            "    WHERE scan_pattern_id IS NOT NULL"
            ") AND backtest_count > 0"
        ))

    # 2. trade_count from actual trade rows
    if "trading_trades" in tables:
        conn.execute(text(
            "UPDATE scan_patterns sp "
            "SET trade_count = sub.cnt "
            "FROM ("
            "    SELECT scan_pattern_id, COUNT(*) AS cnt "
            "    FROM trading_trades "
            "    WHERE scan_pattern_id IS NOT NULL "
            "    GROUP BY scan_pattern_id"
            ") sub "
            "WHERE sp.id = sub.scan_pattern_id "
            "  AND (sp.trade_count IS NULL OR sp.trade_count != sub.cnt)"
        ))
        # Zero out patterns with no matching trades
        conn.execute(text(
            "UPDATE scan_patterns "
            "SET trade_count = 0 "
            "WHERE id NOT IN ("
            "    SELECT DISTINCT scan_pattern_id FROM trading_trades "
            "    WHERE scan_pattern_id IS NOT NULL"
            ") AND trade_count > 0"
        ))

    # 3. win_rate from closed trades (0-1 fraction, post-normalization)
    if "trading_trades" in tables:
        conn.execute(text(
            "UPDATE scan_patterns sp "
            "SET win_rate = sub.wr "
            "FROM ("
            "    SELECT scan_pattern_id, "
            "           CASE WHEN COUNT(*) > 0 "
            "                THEN COUNT(*) FILTER (WHERE pnl > 0)::float / COUNT(*) "
            "                ELSE 0 END AS wr "
            "    FROM trading_trades "
            "    WHERE scan_pattern_id IS NOT NULL "
            "      AND status = 'closed' "
            "    GROUP BY scan_pattern_id"
            ") sub "
            "WHERE sp.id = sub.scan_pattern_id "
            "  AND sp.trade_count >= 5"
        ))

    conn.commit()


def _migration_073_scan_patterns_user_id(conn) -> None:
    """Optional owner for scan patterns (per-user scoping for stats/decay)."""
    if "scan_patterns" not in _tables(conn):
        return
    cols = _columns(conn, "scan_patterns")
    if "user_id" not in cols:
        conn.execute(
            text(
                "ALTER TABLE scan_patterns ADD COLUMN user_id INTEGER "
                "REFERENCES users(id) ON DELETE SET NULL"
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_scan_patterns_user_id ON scan_patterns (user_id)"))
    conn.commit()


def _migration_074_backfill_pattern_trade_asset_class(conn) -> None:
    """Backfill asset_class='crypto' for pattern_trade_rows with crypto tickers."""
    if "pattern_trade_rows" not in _tables(conn):
        return
    conn.execute(text(
        "UPDATE pattern_trade_rows "
        "SET asset_class = 'crypto' "
        "WHERE asset_class = 'stock' "
        "  AND (ticker LIKE '%-USD' OR ticker LIKE '%-USDT' "
        "       OR ticker LIKE '%USDT' OR ticker LIKE '%BUSD' "
        "       OR ticker LIKE '%-BTC' OR ticker LIKE '%-ETH' "
        "       OR UPPER(SPLIT_PART(ticker, '-', 1)) IN "
        "         ('BTC','ETH','SOL','DOGE','XRP','ADA','AVAX','MATIC','DOT','LINK','SHIB','BNB'))"
    ))
    conn.commit()


def _migration_075_text_to_jsonb(conn) -> None:
    """Convert JSON-stored Text columns to JSONB."""
    conversions = [
        ("trading_trades", "indicator_snapshot"),
        ("trading_insights", "indicator_snapshot"),
        ("trading_top_picks", "indicator_data"),
        ("trading_backtests", "params"),
        ("trading_backtests", "equity_curve"),
        ("trading_snapshots", "indicator_data"),
        ("trading_breakout_alerts", "indicator_snapshot"),
        ("trading_breakout_alerts", "signals_snapshot"),
        ("trading_strategy_proposals", "signals_json"),
        ("trading_strategy_proposals", "indicator_json"),
        ("scan_patterns", "rules_json"),
        ("scan_patterns", "exit_config"),
        ("trading_hypothesis_ab_tests", "last_result_json"),
    ]
    existing = _tables(conn)
    for tbl, col in conversions:
        if tbl not in existing:
            continue
        try:
            row = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = :tbl AND column_name = :col"
            ), {"tbl": tbl, "col": col}).fetchone()
            if not row or row[0] == "jsonb":
                continue
            conn.execute(text(f"UPDATE {tbl} SET {col} = NULL WHERE {col} IS NOT NULL AND {col} !~ :pat"), {"pat": r"^\s*[\[\{]"})
            conn.execute(text(f"ALTER TABLE {tbl} ALTER COLUMN {col} TYPE JSONB USING {col}::jsonb"))
        except Exception:
            conn.rollback()


def _migration_076_check_constraints(conn) -> None:
    """Add CHECK constraints for value ranges and enum columns."""
    checks = [
        ("scan_patterns", "chk_sp_win_rate", "win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 1)"),
        ("scan_patterns", "chk_sp_oos_win_rate", "oos_win_rate IS NULL OR (oos_win_rate >= 0 AND oos_win_rate <= 1)"),
        ("scan_patterns", "chk_sp_confidence", "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"),
        ("scan_patterns", "chk_sp_lifecycle", "lifecycle_stage IN ('candidate','backtested','validated','promoted','live','decayed','retired')"),
        ("trading_backtests", "chk_bt_win_rate", "win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 1)"),
        ("brain_batch_jobs", "chk_bbj_status", "status IN ('queued','running','ok','error','timeout')"),
        ("trading_paper_trades", "chk_pt_status", "status IN ('open','closed','expired','cancelled')"),
    ]
    existing = _tables(conn)
    for tbl, name, expr in checks:
        if tbl not in existing:
            continue
        try:
            conn.execute(text(f"ALTER TABLE {tbl} ADD CONSTRAINT {name} CHECK ({expr}) NOT VALID"))
            conn.execute(text(f"ALTER TABLE {tbl} VALIDATE CONSTRAINT {name}"))
        except Exception:
            conn.rollback()


def _migration_077_composite_indexes(conn) -> None:
    """Add composite indexes for hot query paths."""
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_bt_sp_id_ran_at ON trading_backtests(scan_pattern_id, ran_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_trades_sp_status ON trading_trades(scan_pattern_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_sp_active_lifecycle ON scan_patterns(active, lifecycle_stage)",
        "CREATE INDEX IF NOT EXISTS idx_sp_origin_active ON scan_patterns(origin, active)",
        "CREATE INDEX IF NOT EXISTS idx_insights_sp_id ON trading_insights(scan_pattern_id) WHERE scan_pattern_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_alerts_status_created ON trading_alerts(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_snapshots_date_ticker ON trading_snapshots(ticker, snapshot_date)",
        "CREATE INDEX IF NOT EXISTS idx_batch_jobs_status_started ON brain_batch_jobs(status, started_at)",
        "CREATE INDEX IF NOT EXISTS idx_paper_trades_sp_status ON trading_paper_trades(scan_pattern_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_learning_events_created ON trading_learning_events(created_at)",
    ]
    existing = _tables(conn)
    for stmt in indexes:
        tbl = stmt.split(" ON ")[-1].split("(")[0].strip()
        if tbl not in existing:
            continue
        try:
            conn.execute(text(stmt))
        except Exception:
            conn.rollback()


def _migration_078_foreign_keys_phase2(conn) -> None:
    """Add missing FK constraints after orphan cleanup."""
    existing = _tables(conn)
    if "trading_backtests" in existing and "trading_insights" in existing:
        try:
            conn.execute(text("UPDATE trading_backtests SET related_insight_id = NULL WHERE related_insight_id IS NOT NULL AND related_insight_id NOT IN (SELECT id FROM trading_insights)"))
        except Exception:
            conn.rollback()
    if "trading_strategy_proposals" in existing and "scan_patterns" in existing:
        try:
            conn.execute(text("DELETE FROM trading_strategy_proposals WHERE scan_pattern_id IS NOT NULL AND scan_pattern_id NOT IN (SELECT id FROM scan_patterns)"))
        except Exception:
            conn.rollback()
    if "trading_paper_trades" in existing and "scan_patterns" in existing:
        try:
            conn.execute(text("DELETE FROM trading_paper_trades WHERE scan_pattern_id IS NOT NULL AND scan_pattern_id NOT IN (SELECT id FROM scan_patterns)"))
        except Exception:
            conn.rollback()
    fks = [
        ("trading_backtests", "fk_bt_insight", "related_insight_id", "trading_insights", "id", "SET NULL"),
        ("trading_strategy_proposals", "fk_proposal_sp", "scan_pattern_id", "scan_patterns", "id", "CASCADE"),
        ("trading_paper_trades", "fk_paper_sp", "scan_pattern_id", "scan_patterns", "id", "CASCADE"),
    ]
    for tbl, name, col, ref_tbl, ref_col, on_del in fks:
        if tbl not in existing or ref_tbl not in existing:
            continue
        try:
            conn.execute(text(f"ALTER TABLE {tbl} ADD CONSTRAINT {name} FOREIGN KEY ({col}) REFERENCES {ref_tbl}({ref_col}) ON DELETE {on_del}"))
        except Exception:
            conn.rollback()


def _migration_079_unique_constraints(conn) -> None:
    """Add partial unique indexes to prevent duplicates."""
    existing = _tables(conn)
    if "scan_patterns" in existing:
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_sp_name_origin_active ON scan_patterns(name, origin) WHERE active = true"))
        except Exception:
            conn.rollback()
    if "brain_batch_jobs" in existing:
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_bbj_running ON brain_batch_jobs(job_type, batch_key) WHERE status = 'running'"))
        except Exception:
            conn.rollback()


def _migration_080_orphan_cleanup(conn) -> None:
    """Clean up dead patterns, stuck jobs, and orphan references."""
    existing = _tables(conn)
    if "scan_patterns" in existing:
        try:
            conn.execute(text("UPDATE scan_patterns SET active = false WHERE active = true AND backtest_count = 0 AND trade_count = 0 AND evidence_count = 0 AND lifecycle_stage = 'candidate' AND created_at < NOW() - INTERVAL '30 days'"))
        except Exception:
            conn.rollback()
    if "brain_batch_jobs" in existing:
        try:
            conn.execute(text("UPDATE brain_batch_jobs SET status = 'timeout' WHERE status = 'running' AND started_at < NOW() - INTERVAL '4 hours'"))
        except Exception:
            conn.rollback()


def _migration_081_graduate_startup_repairs(conn) -> None:
    """One-time migration for repairs previously run on every startup."""
    existing = _tables(conn)
    if "trading_backtests" in existing:
        try:
            conn.execute(text("DELETE FROM trading_backtests WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY strategy_name, ticker ORDER BY ran_at DESC) AS rn FROM trading_backtests) sub WHERE rn > 1)"))
        except Exception:
            conn.rollback()
    if "scan_patterns" in existing:
        try:
            conn.execute(text("UPDATE scan_patterns SET ticker_scope = 'universal' WHERE ticker_scope IS NULL"))
        except Exception:
            conn.rollback()


def _migration_082_breakout_alert_outcome_notes(conn) -> None:
    """Optional free-text notes on breakout alert outcomes (e.g. auto-expire reason)."""
    if "trading_breakout_alerts" not in _tables(conn):
        return
    cols = _columns(conn, "trading_breakout_alerts")
    if "outcome_notes" not in cols:
        conn.execute(text("ALTER TABLE trading_breakout_alerts ADD COLUMN outcome_notes TEXT"))
        conn.commit()


def _migration_083_backtest_win_rate_scale_cleanup(conn) -> None:
    """Normalize win_rate/oos_win_rate stored as percent (>1.0) to fraction in trading_backtests."""
    if "trading_backtests" not in _tables(conn):
        return
    cols = _columns(conn, "trading_backtests")
    if "win_rate" in cols:
        for _ in range(12):
            r = conn.execute(
                text(
                    "UPDATE trading_backtests SET win_rate = win_rate / 100.0 "
                    "WHERE win_rate IS NOT NULL AND win_rate > 1.0"
                )
            )
            if (r.rowcount or 0) == 0:
                break
    if "oos_win_rate" in cols:
        for _ in range(12):
            r = conn.execute(
                text(
                    "UPDATE trading_backtests SET oos_win_rate = oos_win_rate / 100.0 "
                    "WHERE oos_win_rate IS NOT NULL AND oos_win_rate > 1.0"
                )
            )
            if (r.rowcount or 0) == 0:
                break
    cols2 = _columns(conn, "trading_backtests")
    if "win_rate" in cols2:
        conn.execute(
            text(
                "UPDATE trading_backtests SET win_rate = NULL "
                "WHERE win_rate IS NOT NULL "
                "AND NOT (win_rate >= 0 AND win_rate <= 1)"
            )
        )
    if "oos_win_rate" in cols2:
        conn.execute(
            text(
                "UPDATE trading_backtests SET oos_win_rate = NULL "
                "WHERE oos_win_rate IS NOT NULL "
                "AND NOT (oos_win_rate >= 0 AND oos_win_rate <= 1)"
            )
        )
    conn.commit()


def _migration_084_align_backtest_scan_pattern_from_insight(conn) -> None:
    """Align ``trading_backtests.scan_pattern_id`` with authoritative ``TradingInsight.scan_pattern_id``.

    Policy (pattern-evidence plan): trust the insight when both are linked; backfill NULL
    from the insight and fix disagreements. Does not delete rows.
    """
    if "trading_backtests" not in _tables(conn) or "trading_insights" not in _tables(conn):
        return
    cols_bt = _columns(conn, "trading_backtests")
    cols_ti = _columns(conn, "trading_insights")
    if (
        "scan_pattern_id" not in cols_bt
        or "scan_pattern_id" not in cols_ti
        or "related_insight_id" not in cols_bt
    ):
        return
    conn.execute(
        text(
            "UPDATE trading_backtests bt "
            "SET scan_pattern_id = ti.scan_pattern_id "
            "FROM trading_insights ti "
            "WHERE bt.related_insight_id = ti.id "
            "  AND ti.scan_pattern_id IS NOT NULL "
            "  AND (bt.scan_pattern_id IS NULL OR bt.scan_pattern_id != ti.scan_pattern_id)"
        )
    )
    conn.commit()


def _migration_085_brain_worker_learning_live_json(conn) -> None:
    """Cross-process live learning cycle snapshot for Brain UI (Network tab graph, scan/status)."""
    if "brain_worker_control" not in _tables(conn):
        return
    cols = _columns(conn, "brain_worker_control")
    if "learning_live_json" not in cols:
        conn.execute(text("ALTER TABLE brain_worker_control ADD COLUMN learning_live_json TEXT"))
    conn.commit()


def _brain_graph_edge_type_for_seed(source_node_id: str, signal_type: str, polarity: str) -> str:
    """Classify edge_type for mesh seed INSERTs; matches migration 103 backfill rules."""
    if polarity == "inhibitory":
        return "veto"
    if source_node_id.startswith("nm_evidence"):
        return "evidence"
    if source_node_id.startswith("nm_meta"):
        return "feedback"
    if signal_type in ("cluster_chain", "step_completed"):
        return "control"
    return "dataflow"


def _brain_node_state_activation_ts_column(conn) -> str:
    """SQLAlchemy create_all uses last_activated_at; migration 086 DDL used staleness_at until 103 renames."""
    cols = _columns(conn, "brain_node_states")
    if "last_activated_at" in cols:
        return "last_activated_at"
    return "staleness_at"


def _migration_086_trading_brain_neural_mesh(conn) -> None:
    """Trading Brain v2: Postgres-backed neural mesh (nodes, edges, activation queue, states)."""
    if "brain_graph_nodes" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE brain_graph_nodes (
                    id VARCHAR(80) PRIMARY KEY,
                    domain TEXT NOT NULL DEFAULT 'trading',
                    graph_version INTEGER NOT NULL DEFAULT 1,
                    node_type TEXT NOT NULL,
                    layer INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    fire_threshold DOUBLE PRECISION NOT NULL DEFAULT 0.55,
                    cooldown_seconds INTEGER NOT NULL DEFAULT 120,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    version INTEGER NOT NULL DEFAULT 1,
                    is_observer BOOLEAN NOT NULL DEFAULT FALSE,
                    display_meta JSONB,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_brain_graph_nodes_domain_ver_type "
                "ON brain_graph_nodes (domain, graph_version, node_type)"
            )
        )
        conn.execute(text("CREATE INDEX ix_brain_graph_nodes_layer ON brain_graph_nodes (layer)"))
        conn.commit()

    if "brain_graph_edges" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE brain_graph_edges (
                    id SERIAL PRIMARY KEY,
                    source_node_id VARCHAR(80) NOT NULL REFERENCES brain_graph_nodes(id) ON DELETE CASCADE,
                    target_node_id VARCHAR(80) NOT NULL REFERENCES brain_graph_nodes(id) ON DELETE CASCADE,
                    signal_type TEXT NOT NULL DEFAULT '*',
                    weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                    polarity TEXT NOT NULL DEFAULT 'excitatory',
                    delay_ms INTEGER NOT NULL DEFAULT 0,
                    decay_half_life_seconds INTEGER,
                    gate_config JSONB,
                    min_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    graph_version INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT ck_brain_graph_edges_polarity CHECK (polarity IN ('excitatory', 'inhibitory'))
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_brain_graph_edges_src_en "
                "ON brain_graph_edges (source_node_id, enabled)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_brain_graph_edges_tgt_en "
                "ON brain_graph_edges (target_node_id, enabled)"
            )
        )
        conn.commit()

    if "brain_node_states" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE brain_node_states (
                    node_id VARCHAR(80) PRIMARY KEY REFERENCES brain_graph_nodes(id) ON DELETE CASCADE,
                    activation_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                    local_state JSONB,
                    last_fired_at TIMESTAMP,
                    staleness_at TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.commit()

    if "brain_activation_events" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE brain_activation_events (
                    id BIGSERIAL PRIMARY KEY,
                    source_node_id VARCHAR(80) REFERENCES brain_graph_nodes(id) ON DELETE SET NULL,
                    cause TEXT NOT NULL,
                    payload JSONB,
                    confidence_delta DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    propagation_depth INTEGER NOT NULL DEFAULT 0,
                    correlation_id TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP,
                    status TEXT NOT NULL DEFAULT 'pending',
                    CONSTRAINT ck_brain_activation_events_status CHECK (
                        status IN ('pending', 'processing', 'done', 'dead')
                    )
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_brain_activation_events_status_created "
                "ON brain_activation_events (status, created_at)"
            )
        )
        conn.execute(
            text("CREATE INDEX ix_brain_activation_events_correlation ON brain_activation_events (correlation_id)")
        )
        conn.execute(
            text(
                "CREATE INDEX ix_brain_activation_events_pending_partial ON brain_activation_events (created_at) "
                "WHERE status = 'pending'"
            )
        )
        conn.commit()

    if "brain_fire_log" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE brain_fire_log (
                    id BIGSERIAL PRIMARY KEY,
                    node_id VARCHAR(80) NOT NULL REFERENCES brain_graph_nodes(id) ON DELETE CASCADE,
                    fired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    activation_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    correlation_id TEXT,
                    summary TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_brain_fire_log_node_fired ON brain_fire_log (node_id, fired_at DESC)"
            )
        )
        conn.commit()

    if "brain_graph_snapshots" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE brain_graph_snapshots (
                    id BIGSERIAL PRIMARY KEY,
                    graph_version INTEGER NOT NULL,
                    domain TEXT NOT NULL,
                    snapshot_json JSONB NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text("CREATE INDEX ix_brain_graph_snapshots_domain_ver ON brain_graph_snapshots (domain, graph_version)")
        )
        conn.commit()

    if "brain_graph_metrics" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE brain_graph_metrics (
                    domain TEXT NOT NULL,
                    graph_version INTEGER NOT NULL,
                    metric_key TEXT NOT NULL,
                    value_num DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    extra JSONB,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (domain, graph_version, metric_key)
                )
                """
            )
        )
        conn.commit()

    # Idempotent seed for v1 trading mesh (ON CONFLICT DO NOTHING on nodes)
    gv = 1
    dom = "trading"
    existing = conn.execute(
        text("SELECT COUNT(*) FROM brain_graph_nodes WHERE domain = :d AND graph_version = :g"),
        {"d": dom, "g": gv},
    ).scalar()
    if existing and int(existing) > 0:
        conn.commit()
        return

    nodes_seed = [
        ("nm_snap_daily", 1, "sensory_snapshot", "Market snapshots (daily)", False, 0.45, 30),
        ("nm_snap_intraday", 1, "sensory_snapshot", "Intraday snapshots", False, 0.45, 30),
        ("nm_snap_crypto", 1, "sensory_snapshot", "Crypto snapshots", False, 0.45, 30),
        ("nm_universe_scan", 1, "sensory_universe", "Universe / prescreen", False, 0.5, 300),
        ("nm_volatility", 2, "feature_volatility", "Volatility state", False, 0.5, 60),
        ("nm_momentum", 2, "feature_momentum", "Momentum state", False, 0.5, 60),
        ("nm_anomaly", 2, "feature_anomaly", "Anomaly detectors", False, 0.55, 120),
        ("nm_event_bus", 3, "latent_event_bus", "Event bus / activation router", False, 0.35, 15),
        ("nm_working_memory", 3, "latent_working_memory", "Working memory", False, 0.5, 45),
        ("nm_regime", 3, "latent_regime", "Regime inference", False, 0.5, 90),
        ("nm_contradiction", 3, "latent_contradiction", "Contradiction tracker", False, 0.5, 60),
        ("nm_pattern_disc", 4, "pattern_discovery", "Pattern discovery", False, 0.55, 180),
        ("nm_similarity", 4, "pattern_similarity", "Similarity search", False, 0.6, 300),
        ("nm_evidence_bt", 5, "evidence_backtest", "Backtest evidence", False, 0.55, 120),
        ("nm_evidence_replay", 5, "evidence_replay", "Scenario replay", False, 0.6, 240),
        ("nm_action_signals", 6, "action_signals", "Signal surfacing", False, 0.6, 30),
        ("nm_action_alerts", 6, "action_alerts", "Alert candidates", False, 0.6, 30),
        ("nm_observer_journal", 6, "observer_journal", "Journal observer", True, 0.4, 60),
        ("nm_observer_playbook", 6, "observer_playbook", "Playbook observer", True, 0.4, 120),
        ("nm_meta_reweight", 7, "meta_reweight", "Edge / threshold tuning", False, 0.65, 600),
        ("nm_meta_decay", 7, "meta_decay_policy", "Decay policy", False, 0.5, 300),
    ]
    for nid, layer, ntype, label, is_obs, fth, cd in nodes_seed:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, :ntype, :layer, :label,
                    :fth, :cd, TRUE, 1, :is_obs,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "id": nid,
                "domain": dom,
                "gv": gv,
                "ntype": ntype,
                "layer": layer,
                "label": label,
                "fth": fth,
                "cd": cd,
                "is_obs": is_obs,
            },
        )

    edges_seed = [
        ("nm_snap_daily", "nm_event_bus", "snapshot_refresh", 1.0, "excitatory", None),
        ("nm_snap_intraday", "nm_event_bus", "snapshot_refresh", 1.0, "excitatory", None),
        ("nm_snap_crypto", "nm_event_bus", "snapshot_refresh", 0.9, "excitatory", None),
        ("nm_event_bus", "nm_volatility", "state_tick", 0.85, "excitatory", None),
        ("nm_volatility", "nm_regime", "feature_signal", 0.9, "excitatory", None),
        ("nm_momentum", "nm_regime", "feature_signal", 0.75, "excitatory", None),
        ("nm_regime", "nm_contradiction", "state_tick", 0.6, "excitatory", None),
        ("nm_regime", "nm_pattern_disc", "regime_shift", 0.8, "excitatory", None),
        ("nm_pattern_disc", "nm_evidence_bt", "pattern_candidate", 0.85, "excitatory", None),
        ("nm_evidence_bt", "nm_action_signals", "evidence_ok", 0.9, "excitatory", None),
        ("nm_contradiction", "nm_action_signals", "contradict", 0.95, "inhibitory", None),
        ("nm_regime", "nm_observer_journal", "regime_shift", 0.4, "excitatory", None),
        ("nm_meta_decay", "nm_working_memory", "decay_tick", 0.5, "excitatory", '{"apply_decay_strength": 0.15}'),
        ("nm_universe_scan", "nm_event_bus", "universe_tick", 0.7, "excitatory", None),
    ]
    for src, tgt, sig, w, pol, gcfg in edges_seed:
        etype = _brain_graph_edge_type_for_seed(src, sig, pol)
        if gcfg is None:
            conn.execute(
                text(
                    """
                    INSERT INTO brain_graph_edges (
                        source_node_id, target_node_id, signal_type, weight, polarity,
                        edge_type, delay_ms, min_confidence, min_source_confidence, enabled, graph_version, gate_config,
                        created_at, updated_at
                    ) VALUES (
                        :src, :tgt, :sig, :w, :pol,
                        :etype, 0, 0.0, 0.0, TRUE, :gv, NULL,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "etype": etype, "gv": gv},
            )
        else:
            conn.execute(
                text(
                    """
                    INSERT INTO brain_graph_edges (
                        source_node_id, target_node_id, signal_type, weight, polarity,
                        edge_type, delay_ms, min_confidence, min_source_confidence, enabled, graph_version, gate_config,
                        created_at, updated_at
                    ) VALUES (
                        :src, :tgt, :sig, :w, :pol,
                        :etype, 0, 0.0, 0.0, TRUE, :gv, CAST(:gcfg AS jsonb),
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "etype": etype, "gv": gv, "gcfg": gcfg},
            )

    ts_col = _brain_node_state_activation_ts_column(conn)
    for nid, _, _, _, _, _, _ in nodes_seed:
        conn.execute(
            text(
                f"""
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state, {ts_col}, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{{}}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": nid},
        )

    conn.execute(
        text(
            """
            UPDATE brain_graph_nodes SET enabled = FALSE WHERE id IN (
                'nm_universe_scan', 'nm_anomaly', 'nm_similarity', 'nm_evidence_replay', 'nm_action_alerts'
            )
            """
        )
    )

    conn.commit()


def _migration_087_neural_mesh_seed_expand_v15(conn) -> None:
    """Add focused v1.5 neural mesh nodes (high-value coverage); idempotent."""
    if "brain_graph_nodes" not in _tables(conn):
        return
    gv = 1
    dom = "trading"
    nodes = [
        ("nm_liquidity_state", 2, "feature_liquidity", "Liquidity state", False, 0.5, 90),
        ("nm_breadth_state", 2, "feature_breadth", "Breadth state", False, 0.5, 90),
        ("nm_intermarket_state", 2, "feature_intermarket", "Intermarket state", False, 0.5, 120),
        ("nm_active_thesis_state", 3, "latent_thesis", "Active thesis state", False, 0.52, 120),
        ("nm_confidence_accumulator", 3, "latent_confidence", "Confidence accumulator", False, 0.5, 60),
        ("nm_memory_freshness", 3, "latent_memory_fresh", "Memory freshness state", False, 0.48, 90),
        ("nm_evidence_quality", 5, "evidence_quality", "Evidence quality scorer", False, 0.55, 120),
        ("nm_counterfactual_challenger", 5, "evidence_counterfactual", "Counterfactual challenger", False, 0.52, 180),
        ("nm_contradiction_verifier", 5, "evidence_contradiction", "Contradiction verifier", False, 0.54, 90),
        ("nm_risk_gate", 6, "action_risk_gate", "Risk gate", False, 0.58, 45),
        ("nm_sizing_policy", 6, "action_sizing", "Sizing policy", False, 0.56, 60),
        ("nm_threshold_tuner", 7, "meta_threshold", "Threshold tuner", False, 0.62, 600),
        ("nm_promotion_demotion_monitor", 7, "meta_promotion", "Promotion/demotion monitor", False, 0.55, 300),
    ]
    for nid, layer, ntype, label, is_obs, fth, cd in nodes:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, :ntype, :layer, :label,
                    :fth, :cd, TRUE, 1, :is_obs,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": nid,
                "domain": dom,
                "gv": gv,
                "ntype": ntype,
                "layer": layer,
                "label": label,
                "fth": fth,
                "cd": cd,
                "is_obs": is_obs,
            },
        )

    ts_col = _brain_node_state_activation_ts_column(conn)
    for nid, _, _, _, _, _, _ in nodes:
        conn.execute(
            text(
                f"""
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state, {ts_col}, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{{}}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": nid},
        )

    edges = [
        ("nm_event_bus", "nm_liquidity_state", "state_tick", 0.72, "excitatory"),
        ("nm_event_bus", "nm_breadth_state", "state_tick", 0.72, "excitatory"),
        ("nm_event_bus", "nm_intermarket_state", "state_tick", 0.7, "excitatory"),
        ("nm_liquidity_state", "nm_regime", "feature_signal", 0.78, "excitatory"),
        ("nm_breadth_state", "nm_regime", "feature_signal", 0.78, "excitatory"),
        ("nm_intermarket_state", "nm_regime", "feature_signal", 0.76, "excitatory"),
        ("nm_regime", "nm_active_thesis_state", "state_tick", 0.7, "excitatory"),
        ("nm_working_memory", "nm_confidence_accumulator", "state_tick", 0.65, "excitatory"),
        ("nm_meta_decay", "nm_memory_freshness", "decay_tick", 0.45, "excitatory"),
        ("nm_pattern_disc", "nm_evidence_quality", "pattern_candidate", 0.55, "excitatory"),
        ("nm_evidence_bt", "nm_evidence_quality", "evidence_ok", 0.52, "excitatory"),
        ("nm_evidence_bt", "nm_counterfactual_challenger", "state_tick", 0.5, "excitatory"),
        ("nm_contradiction", "nm_contradiction_verifier", "state_tick", 0.72, "excitatory"),
        ("nm_evidence_bt", "nm_contradiction_verifier", "state_tick", 0.55, "excitatory"),
        ("nm_evidence_bt", "nm_risk_gate", "evidence_ok", 0.76, "excitatory"),
        ("nm_risk_gate", "nm_action_signals", "state_tick", 0.82, "excitatory"),
        ("nm_regime", "nm_sizing_policy", "state_tick", 0.62, "excitatory"),
        ("nm_sizing_policy", "nm_action_signals", "state_tick", 0.68, "excitatory"),
        ("nm_meta_reweight", "nm_threshold_tuner", "state_tick", 0.58, "excitatory"),
        ("nm_threshold_tuner", "nm_meta_decay", "state_tick", 0.42, "excitatory"),
        ("nm_pattern_disc", "nm_promotion_demotion_monitor", "pattern_candidate", 0.46, "excitatory"),
        ("nm_evidence_bt", "nm_promotion_demotion_monitor", "evidence_ok", 0.42, "excitatory"),
    ]
    for src, tgt, sig, w, pol in edges:
        etype = _brain_graph_edge_type_for_seed(src, sig, pol)
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    edge_type, delay_ms, min_confidence, min_source_confidence, enabled, graph_version, gate_config,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    :etype, 0, 0.0, 0.0, TRUE, :gv, NULL,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.graph_version = :gv AND e.signal_type = :sig
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "etype": etype, "gv": gv},
        )

    conn.commit()


def _migration_088_backtest_param_sets(conn) -> None:
    """Deduplicated param/provenance payloads (hash-keyed); optional FK from trading_backtests."""
    tables = _tables(conn)
    if "trading_backtest_param_sets" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE trading_backtest_param_sets (
                    id SERIAL PRIMARY KEY,
                    param_hash VARCHAR(64) NOT NULL UNIQUE,
                    params_json JSONB NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trading_backtest_param_sets_created "
                "ON trading_backtest_param_sets (created_at DESC)"
            )
        )
    if "trading_backtests" in _tables(conn):
        bt_cols = _columns(conn, "trading_backtests")
        if "param_set_id" not in bt_cols:
            conn.execute(
                text(
                    """
                    ALTER TABLE trading_backtests
                    ADD COLUMN param_set_id INTEGER
                    REFERENCES trading_backtest_param_sets(id) ON DELETE SET NULL
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_trading_backtests_param_set_id "
                    "ON trading_backtests (param_set_id)"
                )
            )
    conn.commit()


def _migration_089_momentum_neural_mesh(conn) -> None:
    """Neural-mesh nodes/edges for Coinbase/crypto momentum intelligence (Phase 1). Idempotent."""
    import json

    if "brain_graph_nodes" not in _tables(conn):
        conn.commit()
        return
    gv = 1
    dom = "trading"
    nodes = [
        (
            "nm_momentum_crypto_intel",
            4,
            "momentum_crypto_intel",
            "Crypto momentum intelligence",
            False,
            0.52,
            45,
            {"role": "momentum_intel_hub", "execution_family": "coinbase_spot"},
        ),
        (
            "nm_momentum_viability_pool",
            5,
            "momentum_viability",
            "Momentum viability pool",
            True,
            0.5,
            90,
            {"role": "momentum_viability", "observer": True},
        ),
        (
            "nm_momentum_evolution_trace",
            7,
            "momentum_evolution",
            "Momentum evolution trace",
            True,
            0.52,
            300,
            {"role": "momentum_evolution", "observer": True},
        ),
    ]
    ts_col = _brain_node_state_activation_ts_column(conn)
    for nid, layer, ntype, label, is_obs, fth, cd, dmeta in nodes:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    display_meta, created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, :ntype, :layer, :label,
                    :fth, :cd, TRUE, 1, :is_obs,
                    CAST(:dmeta AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": nid,
                "domain": dom,
                "gv": gv,
                "ntype": ntype,
                "layer": layer,
                "label": label,
                "fth": fth,
                "cd": cd,
                "is_obs": is_obs,
                "dmeta": json.dumps(dmeta),
            },
        )
        conn.execute(
            text(
                f"""
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state, {ts_col}, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{{}}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": nid},
        )

    edges = [
        ("nm_event_bus", "nm_momentum_crypto_intel", "momentum_context_refresh", 0.88, "excitatory"),
        ("nm_momentum_crypto_intel", "nm_momentum_viability_pool", "momentum_scored", 0.82, "excitatory"),
        ("nm_momentum_crypto_intel", "nm_momentum_evolution_trace", "momentum_scored", 0.55, "excitatory"),
        ("nm_momentum", "nm_momentum_crypto_intel", "feature_signal", 0.7, "excitatory"),
    ]
    for src, tgt, sig, w, pol in edges:
        etype = _brain_graph_edge_type_for_seed(src, sig, pol)
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    edge_type, delay_ms, min_confidence, min_source_confidence, enabled, graph_version, gate_config,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    :etype, 0, 0.0, 0.0, TRUE, :gv, NULL,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.signal_type = :sig AND e.graph_version = :gv
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "etype": etype, "gv": gv},
        )

    conn.commit()


def _migration_092_speculative_momentum_neural_subgraph(conn) -> None:
    """Neural-mesh observer subgraph for speculative momentum engine (graph-native identity)."""
    import json

    if "brain_graph_nodes" not in _tables(conn):
        conn.commit()
        return
    gv = 1
    dom = "trading"
    nodes = [
        (
            "nm_speculative_momentum_hub",
            4,
            "speculative_momentum_hub",
            "Speculative momentum hub",
            False,
            0.52,
            60,
            {"role": "speculative_momentum_hub", "engine": "speculative_momentum"},
        ),
        (
            "nm_sm_volume_expansion",
            5,
            "speculative_signal",
            "Abnormal volume expansion",
            True,
            0.5,
            90,
            {"role": "speculative_signal", "engine": "speculative_momentum", "signal": "volume"},
        ),
        (
            "nm_sm_squeeze_pressure",
            5,
            "speculative_signal",
            "Squeeze / halt pressure",
            True,
            0.5,
            90,
            {"role": "speculative_signal", "engine": "speculative_momentum", "signal": "squeeze"},
        ),
        (
            "nm_sm_event_impulse",
            5,
            "speculative_signal",
            "Event / flow impulse",
            True,
            0.5,
            120,
            {"role": "speculative_signal", "engine": "speculative_momentum", "signal": "event"},
        ),
        (
            "nm_sm_extension_risk",
            5,
            "speculative_signal",
            "Extension / blow-off risk",
            True,
            0.5,
            90,
            {"role": "speculative_signal", "engine": "speculative_momentum", "signal": "extension"},
        ),
        (
            "nm_sm_execution_risk",
            5,
            "speculative_signal",
            "Execution / liquidity stress",
            True,
            0.5,
            90,
            {"role": "speculative_signal", "engine": "speculative_momentum", "signal": "execution"},
        ),
        (
            "nm_sm_vwap_pullback",
            5,
            "speculative_signal",
            "VWAP / pullback structure",
            True,
            0.5,
            120,
            {"role": "speculative_signal", "engine": "speculative_momentum", "signal": "vwap_pullback"},
        ),
        (
            "nm_sm_exhaustion",
            5,
            "speculative_signal",
            "Exhaustion / failed continuation",
            True,
            0.5,
            120,
            {"role": "speculative_signal", "engine": "speculative_momentum", "signal": "exhaustion"},
        ),
    ]
    ts_col = _brain_node_state_activation_ts_column(conn)
    for nid, layer, ntype, label, is_obs, fth, cd, dmeta in nodes:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    display_meta, created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, :ntype, :layer, :label,
                    :fth, :cd, TRUE, 1, :is_obs,
                    CAST(:dmeta AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": nid,
                "domain": dom,
                "gv": gv,
                "ntype": ntype,
                "layer": layer,
                "label": label,
                "fth": fth,
                "cd": cd,
                "is_obs": is_obs,
                "dmeta": json.dumps(dmeta),
            },
        )
        conn.execute(
            text(
                f"""
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state, {ts_col}, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{{}}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": nid},
        )

    edges = [
        ("nm_event_bus", "nm_speculative_momentum_hub", "speculative_context_tick", 0.75, "excitatory"),
        ("nm_sm_volume_expansion", "nm_speculative_momentum_hub", "speculative_signal", 0.72, "excitatory"),
        ("nm_sm_squeeze_pressure", "nm_speculative_momentum_hub", "speculative_signal", 0.78, "excitatory"),
        ("nm_sm_event_impulse", "nm_speculative_momentum_hub", "speculative_signal", 0.74, "excitatory"),
        ("nm_sm_extension_risk", "nm_speculative_momentum_hub", "speculative_signal", 0.76, "excitatory"),
        ("nm_sm_execution_risk", "nm_speculative_momentum_hub", "speculative_signal", 0.7, "excitatory"),
        ("nm_sm_vwap_pullback", "nm_speculative_momentum_hub", "speculative_signal", 0.65, "excitatory"),
        ("nm_sm_exhaustion", "nm_speculative_momentum_hub", "speculative_signal", 0.68, "excitatory"),
    ]
    for src, tgt, sig, w, pol in edges:
        etype = _brain_graph_edge_type_for_seed(src, sig, pol)
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    edge_type, delay_ms, min_confidence, min_source_confidence, enabled, graph_version, gate_config,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    :etype, 0, 0.0, 0.0, TRUE, :gv, NULL,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.signal_type = :sig AND e.graph_version = :gv
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "etype": etype, "gv": gv},
        )

    conn.commit()


def _migration_093_automation_session_promotion_lineage(conn) -> None:
    """FK from live-candidate sessions back to originating paper session (audit lineage)."""
    if "trading_automation_sessions" not in _tables(conn):
        conn.commit()
        return
    cols = _columns(conn, "trading_automation_sessions")
    if "source_paper_session_id" not in cols:
        conn.execute(
            text(
                """
                ALTER TABLE trading_automation_sessions
                ADD COLUMN source_paper_session_id INTEGER
                REFERENCES trading_automation_sessions(id) ON DELETE SET NULL
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tas_source_paper "
                "ON trading_automation_sessions (source_paper_session_id)"
            )
        )
    conn.commit()


def _migration_090_momentum_neural_persistence(conn) -> None:
    """Momentum strategy variants, symbol viability, automation session/event (Phase 2 neural backing)."""
    if "momentum_strategy_variants" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE momentum_strategy_variants (
                    id SERIAL PRIMARY KEY,
                    family VARCHAR(64) NOT NULL,
                    variant_key VARCHAR(64) NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    label VARCHAR(256) NOT NULL,
                    params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    execution_family VARCHAR(32) NOT NULL DEFAULT 'coinbase_spot',
                    scan_pattern_id INTEGER REFERENCES scan_patterns(id) ON DELETE SET NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_momentum_strategy_variant_fkv UNIQUE (family, variant_key, version)
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_msv_family ON momentum_strategy_variants (family)"))
        conn.execute(text("CREATE INDEX ix_msv_variant_key ON momentum_strategy_variants (variant_key)"))
        conn.execute(
            text("CREATE INDEX ix_msv_scan_pattern ON momentum_strategy_variants (scan_pattern_id)")
        )

    if "momentum_symbol_viability" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE momentum_symbol_viability (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(36) NOT NULL,
                    scope VARCHAR(16) NOT NULL DEFAULT 'symbol',
                    variant_id INTEGER NOT NULL REFERENCES momentum_strategy_variants(id) ON DELETE CASCADE,
                    viability_score DOUBLE PRECISION NOT NULL,
                    paper_eligible BOOLEAN NOT NULL DEFAULT TRUE,
                    live_eligible BOOLEAN NOT NULL DEFAULT FALSE,
                    freshness_ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    regime_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    execution_readiness_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    explain_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    evidence_window_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    source_node_id VARCHAR(80),
                    correlation_id VARCHAR(64),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_momentum_symbol_viability_sym_var UNIQUE (symbol, variant_id)
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_msvi_symbol_updated ON momentum_symbol_viability (symbol, updated_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_msvi_variant_updated ON momentum_symbol_viability (variant_id, updated_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_msvi_symbol_live_updated "
                "ON momentum_symbol_viability (symbol, live_eligible, updated_at DESC)"
            )
        )
        conn.execute(text("CREATE INDEX ix_msvi_freshness ON momentum_symbol_viability (freshness_ts)"))
        conn.execute(text("CREATE INDEX ix_msvi_corr ON momentum_symbol_viability (correlation_id)"))
        conn.execute(
            text("CREATE INDEX ix_msvi_scope_freshness ON momentum_symbol_viability (scope, freshness_ts DESC)")
        )

    if "trading_automation_sessions" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE trading_automation_sessions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    venue VARCHAR(32) NOT NULL DEFAULT 'coinbase',
                    execution_family VARCHAR(32) NOT NULL DEFAULT 'coinbase_spot',
                    mode VARCHAR(16) NOT NULL DEFAULT 'paper',
                    symbol VARCHAR(36) NOT NULL,
                    variant_id INTEGER NOT NULL REFERENCES momentum_strategy_variants(id) ON DELETE RESTRICT,
                    state VARCHAR(32) NOT NULL DEFAULT 'idle',
                    risk_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    correlation_id VARCHAR(64),
                    source_node_id VARCHAR(80),
                    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ended_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_tas_user ON trading_automation_sessions (user_id)"))
        conn.execute(text("CREATE INDEX ix_tas_symbol ON trading_automation_sessions (symbol)"))
        conn.execute(text("CREATE INDEX ix_tas_variant ON trading_automation_sessions (variant_id)"))
        conn.execute(text("CREATE INDEX ix_tas_corr ON trading_automation_sessions (correlation_id)"))

    if "trading_automation_events" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE trading_automation_events (
                    id BIGSERIAL PRIMARY KEY,
                    session_id INTEGER NOT NULL REFERENCES trading_automation_sessions(id) ON DELETE CASCADE,
                    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    event_type VARCHAR(64) NOT NULL,
                    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    correlation_id VARCHAR(64),
                    source_node_id VARCHAR(80)
                )
                """
            )
        )
        conn.execute(
            text("CREATE INDEX ix_tae_session_ts ON trading_automation_events (session_id, ts)")
        )
        conn.execute(
            text("CREATE INDEX ix_tae_event_type_ts ON trading_automation_events (event_type, ts)")
        )

    # Idempotent seed: Phase 1 family slugs (variant_key == family for v1).
    seed = [
        ("impulse_breakout", "Impulse breakout"),
        ("micro_pullback_continuation", "1m micro pullback continuation"),
        ("rolling_range_high_breakout", "Rolling range high breakout"),
        ("breakout_reclaim", "Breakout reclaim"),
        ("vwap_reclaim_continuation", "VWAP reclaim continuation"),
        ("ema_reclaim_continuation", "EMA reclaim continuation"),
        ("compression_expansion_breakout", "Compression to expansion breakout"),
        ("momentum_follow_through_scalp", "Momentum follow-through scalp"),
        ("failed_breakout_bailout", "Failed breakout bailout"),
        ("no_follow_through_exit", "No-follow-through / exhaustion exit"),
    ]
    for family, label in seed:
        conn.execute(
            text(
                """
                INSERT INTO momentum_strategy_variants (
                    family, variant_key, version, label, params_json, is_active, execution_family,
                    refinement_meta_json,
                    created_at, updated_at
                ) VALUES (
                    :family, :family, 1, :label, '{}'::jsonb, TRUE, 'coinbase_spot',
                    '{}'::jsonb,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (family, variant_key, version) DO NOTHING
                """
            ),
            {"family": family, "label": label},
        )

    # If SQLAlchemy create_all created bare tables before migrations, ensure indexes exist.
    if "momentum_strategy_variants" in _tables(conn):
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_msv_family ON momentum_strategy_variants (family)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_msv_variant_key ON momentum_strategy_variants (variant_key)")
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_msv_scan_pattern ON momentum_strategy_variants (scan_pattern_id)"
            )
        )
    if "momentum_symbol_viability" in _tables(conn):
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_msvi_symbol_updated "
                "ON momentum_symbol_viability (symbol, updated_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_msvi_variant_updated "
                "ON momentum_symbol_viability (variant_id, updated_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_msvi_symbol_live_updated "
                "ON momentum_symbol_viability (symbol, live_eligible, updated_at DESC)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_msvi_freshness ON momentum_symbol_viability (freshness_ts)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_msvi_corr ON momentum_symbol_viability (correlation_id)")
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_msvi_scope_freshness "
                "ON momentum_symbol_viability (scope, freshness_ts DESC)"
            )
        )
    if "trading_automation_sessions" in _tables(conn):
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tas_user ON trading_automation_sessions (user_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tas_symbol ON trading_automation_sessions (symbol)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tas_variant ON trading_automation_sessions (variant_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_tas_corr ON trading_automation_sessions (correlation_id)"))
    if "trading_automation_events" in _tables(conn):
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_tae_session_ts ON trading_automation_events (session_id, ts)")
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tae_event_type_ts ON trading_automation_events (event_type, ts)"
            )
        )

    conn.commit()


def _migration_091_momentum_automation_outcomes(conn) -> None:
    """Closed-loop automation outcomes for neural evolution (Phase 9)."""
    if "momentum_automation_outcomes" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE momentum_automation_outcomes (
                    id SERIAL PRIMARY KEY,
                    session_id INTEGER NOT NULL UNIQUE
                        REFERENCES trading_automation_sessions(id) ON DELETE CASCADE,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    variant_id INTEGER NOT NULL REFERENCES momentum_strategy_variants(id) ON DELETE RESTRICT,
                    symbol VARCHAR(36) NOT NULL,
                    mode VARCHAR(16) NOT NULL,
                    execution_family VARCHAR(32) NOT NULL DEFAULT 'coinbase_spot',
                    terminal_state VARCHAR(32) NOT NULL,
                    terminal_at TIMESTAMP NOT NULL,
                    outcome_class VARCHAR(48) NOT NULL,
                    realized_pnl_usd DOUBLE PRECISION,
                    return_bps DOUBLE PRECISION,
                    hold_seconds INTEGER,
                    exit_reason VARCHAR(64),
                    regime_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    readiness_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    admission_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    governance_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    extracted_summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    evidence_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                    contributes_to_evolution BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_mao_variant_created ON momentum_automation_outcomes (variant_id, created_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_mao_symbol_mode_created ON momentum_automation_outcomes (symbol, mode, created_at)"
            )
        )
        conn.execute(
            text("CREATE INDEX ix_mao_user_created ON momentum_automation_outcomes (user_id, created_at)")
        )
        conn.execute(
            text("CREATE INDEX ix_mao_terminal_at ON momentum_automation_outcomes (terminal_at)")
        )
        conn.execute(
            text("CREATE INDEX ix_mao_outcome_class ON momentum_automation_outcomes (outcome_class)")
        )
    conn.commit()


def _migration_094_trading_autopilot_runtime(conn) -> None:
    """Autopilot runtime read models and simulated fill audit (additive, production-safe)."""
    tables = _tables(conn)

    if "trading_automation_runtime_snapshots" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE trading_automation_runtime_snapshots (
                    id SERIAL PRIMARY KEY,
                    session_id INTEGER NOT NULL UNIQUE
                        REFERENCES trading_automation_sessions(id) ON DELETE CASCADE,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    symbol VARCHAR(36) NOT NULL,
                    mode VARCHAR(16) NOT NULL DEFAULT 'paper',
                    lane VARCHAR(24) NOT NULL DEFAULT 'simulation',
                    state VARCHAR(32) NOT NULL DEFAULT 'idle',
                    strategy_family VARCHAR(64),
                    strategy_label VARCHAR(256),
                    thesis TEXT,
                    confidence DOUBLE PRECISION,
                    conviction DOUBLE PRECISION,
                    current_position_state VARCHAR(24),
                    last_action VARCHAR(64),
                    runtime_seconds INTEGER,
                    simulated_pnl_usd DOUBLE PRECISION,
                    trade_count INTEGER NOT NULL DEFAULT 0,
                    last_price DOUBLE PRECISION,
                    execution_readiness_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    latest_levels_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_tars_user_updated "
                "ON trading_automation_runtime_snapshots (user_id, updated_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_tars_lane_state "
                "ON trading_automation_runtime_snapshots (lane, state)"
            )
        )

    if "trading_automation_session_bindings" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE trading_automation_session_bindings (
                    id SERIAL PRIMARY KEY,
                    session_id INTEGER NOT NULL UNIQUE
                        REFERENCES trading_automation_sessions(id) ON DELETE CASCADE,
                    discovery_provider VARCHAR(32),
                    chart_provider VARCHAR(32),
                    signal_provider VARCHAR(32),
                    source_of_truth_provider VARCHAR(32),
                    source_of_truth_exchange VARCHAR(32),
                    bar_builder VARCHAR(48),
                    latency_class VARCHAR(48),
                    simulation_fidelity VARCHAR(48),
                    gating_reason TEXT,
                    meta_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_tasb_truth_provider "
                "ON trading_automation_session_bindings (source_of_truth_provider, source_of_truth_exchange)"
            )
        )

    if "trading_automation_simulated_fills" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE trading_automation_simulated_fills (
                    id BIGSERIAL PRIMARY KEY,
                    session_id INTEGER NOT NULL
                        REFERENCES trading_automation_sessions(id) ON DELETE CASCADE,
                    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    symbol VARCHAR(36) NOT NULL,
                    lane VARCHAR(24) NOT NULL DEFAULT 'simulation',
                    side VARCHAR(16),
                    action VARCHAR(32) NOT NULL,
                    fill_type VARCHAR(32),
                    quantity DOUBLE PRECISION,
                    price DOUBLE PRECISION,
                    reference_price DOUBLE PRECISION,
                    fees_usd DOUBLE PRECISION,
                    pnl_usd DOUBLE PRECISION,
                    position_state_before VARCHAR(24),
                    position_state_after VARCHAR(24),
                    reason VARCHAR(64),
                    marker_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_tasf_session_ts "
                "ON trading_automation_simulated_fills (session_id, ts DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_tasf_symbol_ts "
                "ON trading_automation_simulated_fills (symbol, ts DESC)"
            )
        )

    conn.commit()


def _migration_095_task_workspace_binding(conn) -> None:
    """Canonical planner task workspace binding via code_repo_id with legacy repo_index backfill."""
    tables = _tables(conn)
    if "plan_task_coding_profile" not in tables or "code_repos" not in tables:
        return

    cols = _columns(conn, "plan_task_coding_profile")
    if "code_repo_id" not in cols:
        conn.execute(text("ALTER TABLE plan_task_coding_profile ADD COLUMN code_repo_id INTEGER"))
        conn.commit()

    try:
        conn.execute(
            text(
                """
                ALTER TABLE plan_task_coding_profile
                ADD CONSTRAINT fk_plan_task_coding_profile_code_repo_id
                FOREIGN KEY (code_repo_id) REFERENCES code_repos(id) ON DELETE SET NULL
                """
            )
        )
        conn.commit()
    except Exception:
        conn.rollback()

    try:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_plan_task_coding_profile_code_repo_id "
                "ON plan_task_coding_profile (code_repo_id)"
            )
        )
        conn.commit()
    except Exception:
        conn.rollback()

    try:
        from pathlib import Path

        from .config import settings

        raw = (getattr(settings, "code_brain_repos", "") or "").strip()
        roots = []
        if raw:
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    roots.append(Path(part).resolve())
                except OSError:
                    continue

        if not roots:
            return

        repo_rows = conn.execute(
            text("SELECT id, path FROM code_repos WHERE active IS TRUE")
        ).fetchall()
        path_to_repo_id: dict[str, int] = {}
        for repo_id, path in repo_rows:
            try:
                path_to_repo_id[str(Path(path).resolve())] = int(repo_id)
            except OSError:
                continue

        prof_rows = conn.execute(
            text(
                "SELECT task_id, repo_index FROM plan_task_coding_profile "
                "WHERE code_repo_id IS NULL"
            )
        ).fetchall()
        changed = False
        for task_id, repo_index in prof_rows:
            try:
                idx = int(repo_index)
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(roots):
                continue
            repo_id = path_to_repo_id.get(str(roots[idx]))
            if repo_id is None:
                continue
            conn.execute(
                text(
                    "UPDATE plan_task_coding_profile "
                    "SET code_repo_id = :repo_id "
                    "WHERE task_id = :task_id"
                ),
                {"repo_id": repo_id, "task_id": task_id},
            )
            changed = True
        if changed:
            conn.commit()
    except Exception:
        conn.rollback()


def _migration_096_momentum_variant_refinement(conn) -> None:
    """Momentum variant lineage + refinement metadata columns."""
    tables = _tables(conn)
    if "momentum_strategy_variants" not in tables:
        return

    cols = _columns(conn, "momentum_strategy_variants")
    if "parent_variant_id" not in cols:
        conn.execute(text("ALTER TABLE momentum_strategy_variants ADD COLUMN parent_variant_id INTEGER"))
        conn.commit()
    if "refinement_meta_json" not in cols:
        conn.execute(
            text(
                "ALTER TABLE momentum_strategy_variants "
                "ADD COLUMN refinement_meta_json JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
        )
        conn.commit()

    try:
        conn.execute(
            text(
                """
                ALTER TABLE momentum_strategy_variants
                ADD CONSTRAINT fk_momentum_strategy_variants_parent_variant
                FOREIGN KEY (parent_variant_id)
                REFERENCES momentum_strategy_variants(id)
                ON DELETE SET NULL
                """
            )
        )
        conn.commit()
    except Exception:
        conn.rollback()

    try:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_momentum_strategy_variants_parent_variant_id "
                "ON momentum_strategy_variants (parent_variant_id)"
            )
        )
        conn.commit()
    except Exception:
        conn.rollback()


def _migration_097_scan_pattern_lifecycle_challenged(conn) -> None:
    """Allow lifecycle_stage 'challenged' (edge-vs-luck research gate; not live-eligible)."""
    tbl = "scan_patterns"
    if tbl not in _tables(conn):
        return
    try:
        conn.execute(text(f"ALTER TABLE {tbl} DROP CONSTRAINT IF EXISTS chk_sp_lifecycle"))
        conn.commit()
    except Exception:
        conn.rollback()
    try:
        conn.execute(
            text(
                f"ALTER TABLE {tbl} ADD CONSTRAINT chk_sp_lifecycle CHECK (lifecycle_stage IN ("
                f"'candidate','backtested','validated','challenged','promoted','live','decayed','retired'"
                f")) NOT VALID"
            )
        )
        conn.execute(text(f"ALTER TABLE {tbl} VALIDATE CONSTRAINT chk_sp_lifecycle"))
        conn.commit()
    except Exception:
        conn.rollback()


def _migration_098_brain_validation_slice_ledger(conn) -> None:
    """Selection-bias accounting: dedupe by research_run_key, aggregate by slice_key."""
    if "brain_validation_slice_ledger" in _tables(conn):
        return
    conn.execute(
        text(
            """
            CREATE TABLE brain_validation_slice_ledger (
                id SERIAL PRIMARY KEY,
                research_run_key VARCHAR(64) NOT NULL,
                slice_key VARCHAR(64) NOT NULL,
                scan_pattern_id INTEGER NOT NULL,
                rules_fingerprint VARCHAR(32),
                param_hash VARCHAR(64),
                recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_bvsl_research_run_key "
            "ON brain_validation_slice_ledger (research_run_key)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_bvsl_slice_key ON brain_validation_slice_ledger (slice_key)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_bvsl_scan_pattern_id "
            "ON brain_validation_slice_ledger (scan_pattern_id)"
        )
    )
    conn.commit()


def _migration_099_execution_audit_and_allocator(conn) -> None:
    """Execution audit events, trade fill-state columns, and allocator snapshots."""
    tables = _tables(conn)

    if "trading_trades" in tables:
        cols = _columns(conn, "trading_trades")
        trade_cols = {
            "filled_quantity": "ALTER TABLE trading_trades ADD COLUMN filled_quantity DOUBLE PRECISION",
            "remaining_quantity": "ALTER TABLE trading_trades ADD COLUMN remaining_quantity DOUBLE PRECISION",
            "submitted_at": "ALTER TABLE trading_trades ADD COLUMN submitted_at TIMESTAMP",
            "acknowledged_at": "ALTER TABLE trading_trades ADD COLUMN acknowledged_at TIMESTAMP",
            "first_fill_at": "ALTER TABLE trading_trades ADD COLUMN first_fill_at TIMESTAMP",
            "last_fill_at": "ALTER TABLE trading_trades ADD COLUMN last_fill_at TIMESTAMP",
        }
        for name, ddl in trade_cols.items():
            if name not in cols:
                conn.execute(text(ddl))
    conn.commit()

    if "trading_proposals" in tables:
        cols = _columns(conn, "trading_proposals")
        if "allocation_decision_json" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE trading_proposals "
                    "ADD COLUMN allocation_decision_json JSONB NOT NULL DEFAULT '{}'::jsonb"
                )
            )
            conn.commit()

    if "trading_automation_sessions" in tables:
        cols = _columns(conn, "trading_automation_sessions")
        if "allocation_decision_json" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE trading_automation_sessions "
                    "ADD COLUMN allocation_decision_json JSONB NOT NULL DEFAULT '{}'::jsonb"
                )
            )
            conn.commit()

    if "trading_execution_events" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE trading_execution_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id INTEGER,
                    trade_id INTEGER REFERENCES trading_trades(id) ON DELETE CASCADE,
                    proposal_id INTEGER REFERENCES trading_proposals(id) ON DELETE SET NULL,
                    automation_session_id INTEGER REFERENCES trading_automation_sessions(id) ON DELETE SET NULL,
                    scan_pattern_id INTEGER REFERENCES scan_patterns(id) ON DELETE SET NULL,
                    ticker VARCHAR(36),
                    venue VARCHAR(32),
                    execution_family VARCHAR(32),
                    broker_source VARCHAR(32),
                    order_id VARCHAR(128),
                    client_order_id VARCHAR(128),
                    product_id VARCHAR(64),
                    event_type VARCHAR(32) NOT NULL,
                    status VARCHAR(32),
                    requested_quantity DOUBLE PRECISION,
                    cumulative_filled_quantity DOUBLE PRECISION,
                    last_fill_quantity DOUBLE PRECISION,
                    average_fill_price DOUBLE PRECISION,
                    submitted_at TIMESTAMP,
                    acknowledged_at TIMESTAMP,
                    first_fill_at TIMESTAMP,
                    last_fill_at TIMESTAMP,
                    event_at TIMESTAMP,
                    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reference_price DOUBLE PRECISION,
                    best_bid DOUBLE PRECISION,
                    best_ask DOUBLE PRECISION,
                    spread_bps DOUBLE PRECISION,
                    expected_slippage_bps DOUBLE PRECISION,
                    realized_slippage_bps DOUBLE PRECISION,
                    submit_to_ack_ms DOUBLE PRECISION,
                    ack_to_first_fill_ms DOUBLE PRECISION,
                    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trading_execution_events_trade_ts "
                "ON trading_execution_events (trade_id, recorded_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trading_execution_events_order_ts "
                "ON trading_execution_events (broker_source, order_id, recorded_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trading_execution_events_pattern_ts "
                "ON trading_execution_events (scan_pattern_id, recorded_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trading_execution_events_event_type "
                "ON trading_execution_events (event_type)"
            )
        )
        conn.commit()


def _migration_100_momentum_viability_scope(conn) -> None:
    """Add explicit symbol-vs-aggregate scope to durable viability rows."""
    if "momentum_symbol_viability" not in _tables(conn):
        return
    cols = _columns(conn, "momentum_symbol_viability")
    if "scope" not in cols:
        conn.execute(
            text(
                "ALTER TABLE momentum_symbol_viability "
                "ADD COLUMN scope VARCHAR(16) NOT NULL DEFAULT 'symbol'"
            )
        )
    conn.execute(
        text(
            "UPDATE momentum_symbol_viability "
            "SET scope = 'aggregate' "
            "WHERE UPPER(COALESCE(symbol, '')) = '__AGGREGATE__'"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_msvi_scope_freshness "
            "ON momentum_symbol_viability (scope, freshness_ts DESC)"
        )
    )
    conn.commit()


def _migration_101_coding_execution_iteration(conn) -> None:
    if "coding_execution_iteration" in _tables(conn):
        return
    conn.execute(text("""
        CREATE TABLE coding_execution_iteration (
            id SERIAL PRIMARY KEY,
            run_id VARCHAR(64) NOT NULL,
            iteration INTEGER NOT NULL DEFAULT 0,
            state VARCHAR(32) NOT NULL DEFAULT 'planning',
            prompt TEXT,
            plan_json TEXT,
            diffs_json TEXT,
            files_changed_json TEXT,
            apply_status VARCHAR(24),
            test_exit_code INTEGER,
            test_output TEXT,
            diagnosis TEXT,
            error_category VARCHAR(64),
            model_used VARCHAR(200),
            duration_ms INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX ix_cei_run_id ON coding_execution_iteration (run_id)"))
    conn.commit()


def _migration_102_learning_cycle_neural_nodes(conn) -> None:
    """Wire learning-cycle clusters + steps as real neural mesh nodes (layers 8-9)."""
    import json

    if "brain_graph_nodes" not in _tables(conn):
        conn.commit()
        return

    gv = 1
    dom = "trading"

    # --- 7 cluster nodes (layer 8) ---
    cluster_nodes = [
        ("nm_lc_c_state", 8, "learning_cluster", "Market state & memory", False, 0.40, 30,
         {"role": "learning_cluster", "cluster_id": "c_state"}),
        ("nm_lc_c_discovery", 8, "learning_cluster", "Pattern discovery", False, 0.40, 30,
         {"role": "learning_cluster", "cluster_id": "c_discovery"}),
        ("nm_lc_c_validation", 8, "learning_cluster", "Evidence & backtests", False, 0.40, 30,
         {"role": "learning_cluster", "cluster_id": "c_validation"}),
        ("nm_lc_c_evolution", 8, "learning_cluster", "Evolution & hypotheses", False, 0.40, 30,
         {"role": "learning_cluster", "cluster_id": "c_evolution"}),
        ("nm_lc_c_secondary", 8, "learning_cluster", "Secondary miners", False, 0.40, 30,
         {"role": "learning_cluster", "cluster_id": "c_secondary"}),
        ("nm_lc_c_journal", 8, "learning_cluster", "Journal & signals", False, 0.40, 30,
         {"role": "learning_cluster", "cluster_id": "c_journal"}),
        ("nm_lc_c_meta", 8, "learning_cluster", "Meta-learning & close", False, 0.40, 30,
         {"role": "learning_cluster", "cluster_id": "c_meta"}),
    ]

    # --- 27 step nodes (layer 9) ---
    step_nodes = [
        # c_state (4)
        ("nm_lc_snapshots_daily", 9, "learning_step", "Daily market snapshots", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_state", "step_sid": "snapshots_daily"}),
        ("nm_lc_snapshots_intraday", 9, "learning_step", "Intraday snapshots", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_state", "step_sid": "snapshots_intraday"}),
        ("nm_lc_backfill", 9, "learning_step", "Backfilling future returns", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_state", "step_sid": "backfill"}),
        ("nm_lc_decay", 9, "learning_step", "Decaying stale insights", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_state", "step_sid": "decay"}),
        # c_discovery (2)
        ("nm_lc_mine", 9, "learning_step", "Mining patterns", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_discovery", "step_sid": "mine"}),
        ("nm_lc_seek", 9, "learning_step", "Active pattern seeking", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_discovery", "step_sid": "seek"}),
        # c_validation (2)
        ("nm_lc_bt_insights", 9, "learning_step", "Backtesting insights", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_validation", "step_sid": "bt_insights"}),
        ("nm_lc_bt_queue", 9, "learning_step", "Backtesting pattern queue", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_validation", "step_sid": "bt_queue"}),
        # c_evolution (3)
        ("nm_lc_variants", 9, "learning_step", "Evolving pattern variants", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_evolution", "step_sid": "variants"}),
        ("nm_lc_hypotheses", 9, "learning_step", "Testing hypotheses", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_evolution", "step_sid": "hypotheses"}),
        ("nm_lc_breakout", 9, "learning_step", "Learning from breakouts", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_evolution", "step_sid": "breakout"}),
        # c_secondary (8)
        ("nm_lc_intraday_hv", 9, "learning_step", "Intraday breakout patterns", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_secondary", "step_sid": "intraday_hv"}),
        ("nm_lc_refine", 9, "learning_step", "Refining patterns", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_secondary", "step_sid": "refine"}),
        ("nm_lc_exit", 9, "learning_step", "Exit optimization", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_secondary", "step_sid": "exit"}),
        ("nm_lc_fakeout", 9, "learning_step", "Mining fakeout patterns", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_secondary", "step_sid": "fakeout"}),
        ("nm_lc_sizing", 9, "learning_step", "Position sizing tuning", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_secondary", "step_sid": "sizing"}),
        ("nm_lc_inter_alert", 9, "learning_step", "Inter-alert patterns", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_secondary", "step_sid": "inter_alert"}),
        ("nm_lc_timeframe", 9, "learning_step", "Timeframe performance", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_secondary", "step_sid": "timeframe"}),
        ("nm_lc_synergy", 9, "learning_step", "Signal synergies", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_secondary", "step_sid": "synergy"}),
        # c_journal (2)
        ("nm_lc_journal", 9, "learning_step", "Writing market journal", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_journal", "step_sid": "journal"}),
        ("nm_lc_signals", 9, "learning_step", "Checking signal events", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_journal", "step_sid": "signals"}),
        # c_meta (6)
        ("nm_lc_ml", 9, "learning_step", "Training meta-learner", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_meta", "step_sid": "ml"}),
        ("nm_lc_pattern_engine", 9, "learning_step", "Pattern discovery engine", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_meta", "step_sid": "pattern_engine"}),
        ("nm_lc_proposals", 9, "learning_step", "Strategy proposals", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_meta", "step_sid": "proposals"}),
        ("nm_lc_cycle_report", 9, "learning_step", "Cycle AI report", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_meta", "step_sid": "cycle_report"}),
        ("nm_lc_depromote", 9, "learning_step", "Live depromotion", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_meta", "step_sid": "depromote"}),
        ("nm_lc_finalize", 9, "learning_step", "Finalizing cycle", False, 0.35, 15,
         {"role": "learning_step", "cluster_id": "c_meta", "step_sid": "finalize"}),
    ]

    all_nodes = cluster_nodes + step_nodes
    ts_col = _brain_node_state_activation_ts_column(conn)
    for nid, layer, ntype, label, is_obs, fth, cd, dmeta in all_nodes:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    display_meta, created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, :ntype, :layer, :label,
                    :fth, :cd, TRUE, 1, :is_obs,
                    CAST(:dmeta AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": nid,
                "domain": dom,
                "gv": gv,
                "ntype": ntype,
                "layer": layer,
                "label": label,
                "fth": fth,
                "cd": cd,
                "is_obs": is_obs,
                "dmeta": json.dumps(dmeta),
            },
        )
        conn.execute(
            text(
                f"""
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state, {ts_col}, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{{}}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": nid},
        )

    # --- Edges ---
    edges = [
        # Sequential cluster chain
        ("nm_lc_c_state", "nm_lc_c_discovery", "cluster_chain", 0.7, "excitatory"),
        ("nm_lc_c_discovery", "nm_lc_c_validation", "cluster_chain", 0.7, "excitatory"),
        ("nm_lc_c_validation", "nm_lc_c_evolution", "cluster_chain", 0.7, "excitatory"),
        ("nm_lc_c_evolution", "nm_lc_c_secondary", "cluster_chain", 0.7, "excitatory"),
        ("nm_lc_c_secondary", "nm_lc_c_journal", "cluster_chain", 0.7, "excitatory"),
        ("nm_lc_c_journal", "nm_lc_c_meta", "cluster_chain", 0.7, "excitatory"),
        # Cluster → first step in cluster
        ("nm_lc_c_state", "nm_lc_snapshots_daily", "step_completed", 0.7, "excitatory"),
        ("nm_lc_c_discovery", "nm_lc_mine", "step_completed", 0.7, "excitatory"),
        ("nm_lc_c_validation", "nm_lc_bt_insights", "step_completed", 0.7, "excitatory"),
        ("nm_lc_c_evolution", "nm_lc_variants", "step_completed", 0.7, "excitatory"),
        ("nm_lc_c_secondary", "nm_lc_intraday_hv", "step_completed", 0.7, "excitatory"),
        ("nm_lc_c_journal", "nm_lc_journal", "step_completed", 0.7, "excitatory"),
        ("nm_lc_c_meta", "nm_lc_ml", "step_completed", 0.7, "excitatory"),
        # Last step → cluster completion
        ("nm_lc_decay", "nm_lc_c_state", "step_completed", 0.7, "excitatory"),
        ("nm_lc_seek", "nm_lc_c_discovery", "step_completed", 0.7, "excitatory"),
        ("nm_lc_bt_queue", "nm_lc_c_validation", "step_completed", 0.7, "excitatory"),
        ("nm_lc_breakout", "nm_lc_c_evolution", "step_completed", 0.7, "excitatory"),
        ("nm_lc_synergy", "nm_lc_c_secondary", "step_completed", 0.7, "excitatory"),
        ("nm_lc_signals", "nm_lc_c_journal", "step_completed", 0.7, "excitatory"),
        ("nm_lc_finalize", "nm_lc_c_meta", "step_completed", 0.7, "excitatory"),
        # Cross-connects to existing spine
        ("nm_lc_mine", "nm_pattern_disc", "step_completed", 0.55, "excitatory"),
        ("nm_lc_bt_queue", "nm_evidence_bt", "step_completed", 0.55, "excitatory"),
        ("nm_lc_finalize", "nm_event_bus", "step_completed", 0.50, "excitatory"),
        ("nm_lc_snapshots_daily", "nm_snap_daily", "step_completed", 0.50, "excitatory"),
    ]
    for src, tgt, sig, w, pol in edges:
        etype = _brain_graph_edge_type_for_seed(src, sig, pol)
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    edge_type, delay_ms, min_confidence, min_source_confidence, enabled, graph_version, gate_config,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    :etype, 0, 0.0, 0.0, TRUE, :gv, NULL,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.signal_type = :sig AND e.graph_version = :gv
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "etype": etype, "gv": gv},
        )

    conn.commit()


def _migration_103_unified_neural_mesh(conn) -> None:
    """Backfill display_meta on all mesh nodes, add edge_type + min_source_confidence,
    rename staleness_at → last_activated_at.  Unified neural-only graph."""
    import json

    if "brain_graph_nodes" not in _tables(conn):
        conn.commit()
        return

    # ── 1. Core spine display_meta (layers 1-7) ────────────────────────
    spine_meta: dict[str, dict] = {
        "nm_snap_daily": {
            "role": "sensory_snapshot",
            "description": "Captures daily OHLCV market snapshots into the database for downstream feature extraction and pattern mining.",
            "remarks": "Layer 1 sensory node. Fires on scheduler-driven snapshot refresh. Feeds event bus and feature extractors.",
        },
        "nm_snap_intraday": {
            "role": "sensory_snapshot",
            "description": "Captures intraday bar snapshots (e.g. 15m) for crypto symbols when enabled.",
            "remarks": "Layer 1 sensory node. Enables shorter-timeframe pattern mining and compression/HV analysis.",
        },
        "nm_snap_crypto": {
            "role": "sensory_snapshot",
            "description": "Captures crypto-specific market snapshots with exchange-native data.",
            "remarks": "Layer 1 sensory node. Feeds crypto-specific feature extraction paths.",
        },
        "nm_universe_scan": {
            "role": "sensory_universe",
            "description": "Universe prescreen and full market scan to identify the tradeable candidate pool.",
            "remarks": "Layer 1 sensory node. Daily prescreen (2:00) and full scan (2:30) populate candidate tables.",
        },
        "nm_volatility": {
            "role": "feature_volatility",
            "description": "Extracts and tracks volatility regime features (HV, IV rank, compression, expansion).",
            "remarks": "Layer 2 feature node. Feeds regime inference and risk gating downstream.",
        },
        "nm_momentum": {
            "role": "feature_momentum",
            "description": "Extracts momentum state features (trend strength, rate of change, relative strength).",
            "remarks": "Layer 2 feature node. Core input to regime inference and pattern discovery.",
        },
        "nm_anomaly": {
            "role": "feature_anomaly",
            "description": "Detects statistical anomalies in price, volume, or feature distributions.",
            "remarks": "Layer 2 feature node. Currently disabled in default seed. Fires on distribution breaks.",
        },
        "nm_liquidity_state": {
            "role": "feature_liquidity",
            "description": "Tracks liquidity conditions (spread quality, depth, volume profile).",
            "remarks": "Layer 2 feature node. Feeds execution readiness and risk gating.",
        },
        "nm_breadth_state": {
            "role": "feature_breadth",
            "description": "Tracks market breadth metrics (advance/decline, new highs/lows, sector rotation).",
            "remarks": "Layer 2 feature node. Contextualizes individual signals against broad market health.",
        },
        "nm_intermarket_state": {
            "role": "feature_intermarket",
            "description": "Tracks cross-asset correlations and intermarket signals (bonds, currencies, commodities).",
            "remarks": "Layer 2 feature node. Provides macro context for equity/crypto positioning.",
        },
        "nm_event_bus": {
            "role": "latent_event_bus",
            "description": "Central activation router that distributes events across the neural mesh.",
            "remarks": "Layer 3 latent node. Hub node with low fire threshold (0.35). Routes snapshot refreshes, cycle completions, and momentum ticks.",
        },
        "nm_working_memory": {
            "role": "latent_working_memory",
            "description": "Short-term working memory for active hypotheses, recent signals, and context.",
            "remarks": "Layer 3 latent node. Receives decay policy signals. Maintains active thesis context.",
        },
        "nm_regime": {
            "role": "latent_regime",
            "description": "Infers the current market regime (trending, mean-reverting, volatile, quiet).",
            "remarks": "Layer 3 latent node. Key routing decision — regime shifts trigger pattern discovery and journal observation.",
        },
        "nm_contradiction": {
            "role": "latent_contradiction",
            "description": "Tracks contradictory signals that should suppress action (conflicting indicators, regime ambiguity).",
            "remarks": "Layer 3 latent node. Inhibitory edge to action signals — prevents trading when evidence conflicts.",
        },
        "nm_active_thesis_state": {
            "role": "latent_thesis",
            "description": "Tracks the active trading thesis and its current validity.",
            "remarks": "Layer 3 latent node. Maintains thesis coherence across regime shifts.",
        },
        "nm_confidence_accumulator": {
            "role": "latent_confidence",
            "description": "Accumulates confidence from multiple evidence sources before gating action.",
            "remarks": "Layer 3 latent node. Prevents premature action from single-source signals.",
        },
        "nm_memory_freshness": {
            "role": "latent_memory_fresh",
            "description": "Tracks the freshness of working memory and feature data.",
            "remarks": "Layer 3 latent node. Signals when stored state is too old for reliable decisions.",
        },
        "nm_pattern_disc": {
            "role": "pattern_discovery",
            "description": "Pattern discovery and candidate generation from market structure.",
            "remarks": "Layer 4 pattern node. Receives regime shift signals. Feeds evidence/backtest layer.",
        },
        "nm_similarity": {
            "role": "pattern_similarity",
            "description": "Similarity search across pattern library to find related setups and analogues.",
            "remarks": "Layer 4 pattern node. Currently disabled in default seed. Cross-references new candidates.",
        },
        "nm_evidence_bt": {
            "role": "evidence_backtest",
            "description": "Backtest evidence evaluation — validates pattern candidates against historical data.",
            "remarks": "Layer 5 evidence node. Core evidence gate before action signals.",
        },
        "nm_evidence_replay": {
            "role": "evidence_replay",
            "description": "Scenario replay evidence — tests pattern robustness under alternative market paths.",
            "remarks": "Layer 5 evidence node. Currently disabled. Complements backtest with Monte Carlo / replay approaches.",
        },
        "nm_evidence_quality": {
            "role": "evidence_quality",
            "description": "Scores the overall quality and reliability of accumulated evidence.",
            "remarks": "Layer 5 evidence node. Meta-evidence: rates backtest sample size, OOS consistency, regime coverage.",
        },
        "nm_counterfactual_challenger": {
            "role": "evidence_counterfactual",
            "description": "Challenges pattern evidence with counterfactual scenarios and devil's advocate analysis.",
            "remarks": "Layer 5 evidence node. Reduces confirmation bias in pattern promotion.",
        },
        "nm_contradiction_verifier": {
            "role": "evidence_contradiction",
            "description": "Verifies whether contradictory evidence should suppress or merely discount a pattern.",
            "remarks": "Layer 5 evidence node. Graduated contradiction response instead of binary veto.",
        },
        "nm_action_signals": {
            "role": "action_signals",
            "description": "Signal surfacing — aggregates validated patterns into actionable trading signals.",
            "remarks": "Layer 6 action node. Receives evidence-ok signals, inhibited by contradiction tracker.",
        },
        "nm_action_alerts": {
            "role": "action_alerts",
            "description": "Alert candidate generation for user notification and desk display.",
            "remarks": "Layer 6 action node. Currently disabled. Converts signals into user-facing alerts.",
        },
        "nm_observer_journal": {
            "role": "observer_journal",
            "description": "Passive observer that logs regime shifts and significant events to the market journal.",
            "remarks": "Layer 6 observer node. Does not propagate downstream (is_observer=True).",
        },
        "nm_observer_playbook": {
            "role": "observer_playbook",
            "description": "Passive observer that tracks playbook-relevant events for operator review.",
            "remarks": "Layer 6 observer node. Does not propagate downstream (is_observer=True).",
        },
        "nm_risk_gate": {
            "role": "action_risk_gate",
            "description": "Risk gate that can block or attenuate action signals based on portfolio risk limits.",
            "remarks": "Layer 6 action node. Final safety check before operator surface.",
        },
        "nm_sizing_policy": {
            "role": "action_sizing",
            "description": "Position sizing policy that modulates trade size based on confidence and risk.",
            "remarks": "Layer 6 action node. Translates signal strength into appropriate position sizes.",
        },
        "nm_meta_reweight": {
            "role": "meta_reweight",
            "description": "Edge and threshold tuning — adapts mesh weights from realized performance feedback.",
            "remarks": "Layer 7 meta node. Learns which edges and thresholds produce good outcomes.",
        },
        "nm_meta_decay": {
            "role": "meta_decay_policy",
            "description": "Decay policy — governs how quickly stale activations and confidence drain.",
            "remarks": "Layer 7 meta node. Sends decay ticks to working memory. Configures half-life behavior.",
        },
        "nm_threshold_tuner": {
            "role": "meta_threshold",
            "description": "Dynamically tunes fire thresholds based on recent activation patterns and outcomes.",
            "remarks": "Layer 7 meta node. Prevents threshold drift from causing over- or under-firing.",
        },
        "nm_promotion_demotion_monitor": {
            "role": "meta_promotion",
            "description": "Monitors pattern promotion/demotion rates and flags anomalous churn.",
            "remarks": "Layer 7 meta node. Integrity check on the promotion pipeline.",
        },
    }

    for nid, meta in spine_meta.items():
        conn.execute(
            text(
                """
                UPDATE brain_graph_nodes
                SET display_meta = COALESCE(display_meta, '{}'::jsonb) || CAST(:meta AS jsonb),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :nid
                """
            ),
            {"nid": nid, "meta": json.dumps(meta)},
        )

    # ── 2. Learning-cycle cluster + step display_meta (layers 8-9) ─────
    # Import canonical definitions to avoid duplicating long text.
    from app.services.trading.learning_cycle_architecture import TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS

    for cdef in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS:
        cluster_nid = f"nm_lc_{cdef.id}"
        cmeta = {
            "role": "learning_cluster",
            "cluster_id": cdef.id,
            "description": cdef.description,
            "remarks": cdef.remarks,
            "phase_summary": cdef.phase_summary,
            "inputs": list(cdef.inputs),
            "outputs": list(cdef.outputs),
            "code_ref": f"run_learning_cycle \u2192 {cdef.id}",
        }
        conn.execute(
            text(
                """
                UPDATE brain_graph_nodes
                SET display_meta = CAST(:meta AS jsonb),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :nid
                """
            ),
            {"nid": cluster_nid, "meta": json.dumps(cmeta)},
        )

        for st in cdef.steps:
            step_nid = f"nm_lc_{st.sid}"
            smeta = {
                "role": "learning_step",
                "cluster_id": cdef.id,
                "step_sid": st.sid,
                "description": st.description,
                "remarks": st.remarks,
                "code_ref": st.code_ref,
                "runner_phase": st.runner_phase,
                "inputs": list(st.inputs),
                "outputs": list(st.outputs),
            }
            conn.execute(
                text(
                    """
                    UPDATE brain_graph_nodes
                    SET display_meta = CAST(:meta AS jsonb),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :nid
                    """
                ),
                {"nid": step_nid, "meta": json.dumps(smeta)},
            )

    # ── 3. Add edge_type column ────────────────────────────────────────
    cols = _columns(conn, "brain_graph_edges")
    if "edge_type" not in cols:
        conn.execute(text(
            "ALTER TABLE brain_graph_edges "
            "ADD COLUMN edge_type VARCHAR(32) NOT NULL DEFAULT 'dataflow'"
        ))
        conn.execute(text(
            "ALTER TABLE brain_graph_edges "
            "ADD COLUMN min_source_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0"
        ))
        # Backfill existing edges by semantic type
        conn.execute(text(
            "UPDATE brain_graph_edges SET edge_type = 'veto' "
            "WHERE polarity = 'inhibitory'"
        ))
        conn.execute(text(
            "UPDATE brain_graph_edges SET edge_type = 'evidence' "
            "WHERE source_node_id LIKE 'nm_evidence%'"
        ))
        conn.execute(text(
            "UPDATE brain_graph_edges SET edge_type = 'feedback' "
            "WHERE source_node_id LIKE 'nm_meta%'"
        ))
        conn.execute(text(
            "UPDATE brain_graph_edges SET edge_type = 'control' "
            "WHERE signal_type IN ('cluster_chain', 'step_completed')"
        ))
        conn.execute(text(
            "ALTER TABLE brain_graph_edges "
            "ADD CONSTRAINT ck_brain_graph_edges_edge_type "
            "CHECK (edge_type IN ('dataflow','evidence','veto','feedback','control','operator_output'))"
        ))

    # ── 4. Rename staleness_at → last_activated_at ─────────────────────
    state_cols = _columns(conn, "brain_node_states")
    if "staleness_at" in state_cols and "last_activated_at" not in state_cols:
        conn.execute(text(
            "ALTER TABLE brain_node_states "
            "RENAME COLUMN staleness_at TO last_activated_at"
        ))

    conn.commit()


def _migration_104_split_c_meta_cluster(conn) -> None:
    """Split c_meta into c_meta_learning, c_decisioning, c_control."""
    import json

    if "brain_graph_nodes" not in _tables(conn):
        conn.commit()
        return

    gv = 1
    dom = "trading"

    # ── 1. Insert new cluster nodes ────────────────────────────────────
    new_clusters = [
        ("nm_lc_c_meta_learning", 8, "learning_cluster", "Meta-learning & reweighting", False, 0.40, 30, {
            "role": "learning_cluster",
            "cluster_id": "c_meta_learning",
            "description": "Trains the pattern meta-learner and applies feedback boosts or penalties to pattern scores.",
            "remarks": "Split from c_meta. Focuses on ML model training and confidence reweighting.",
            "phase_summary": "ML training",
            "code_ref": "run_learning_cycle \u2192 c_meta_learning",
        }),
        ("nm_lc_c_decisioning", 8, "learning_cluster", "Decisioning & promotion", False, 0.40, 30, {
            "role": "learning_cluster",
            "cluster_id": "c_decisioning",
            "description": "Runs the pattern engine sub-cycle and generates strategy proposals from high-confidence patterns.",
            "remarks": "Split from c_meta. Bridges research patterns to actionable proposals.",
            "phase_summary": "pattern engine \u2192 proposals",
            "code_ref": "run_learning_cycle \u2192 c_decisioning",
        }),
        ("nm_lc_c_control", 8, "learning_cluster", "Control & audit close", False, 0.40, 30, {
            "role": "learning_cluster",
            "cluster_id": "c_control",
            "description": "Generates the cycle AI report, applies live depromotion gates, and finalizes the cycle.",
            "remarks": "Split from c_meta. Integrity, audit, and close-the-books operations.",
            "phase_summary": "report \u2192 depromote \u2192 finalize",
            "code_ref": "run_learning_cycle \u2192 c_control",
        }),
    ]

    for nid, layer, ntype, label, is_obs, fth, cd, dmeta in new_clusters:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    display_meta, created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, :ntype, :layer, :label,
                    :fth, :cd, TRUE, 1, :is_obs,
                    CAST(:dmeta AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": nid, "domain": dom, "gv": gv, "ntype": ntype,
                "layer": layer, "label": label, "fth": fth, "cd": cd,
                "is_obs": is_obs, "dmeta": json.dumps(dmeta),
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state,
                    last_activated_at, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": nid},
        )

    # ── 2. Reassign step nodes to new clusters ─────────────────────────
    step_reassign = {
        "nm_lc_ml": "c_meta_learning",
        "nm_lc_pattern_engine": "c_decisioning",
        "nm_lc_proposals": "c_decisioning",
        "nm_lc_cycle_report": "c_control",
        "nm_lc_depromote": "c_control",
        "nm_lc_finalize": "c_control",
    }
    for step_nid, new_cluster_id in step_reassign.items():
        conn.execute(
            text(
                """
                UPDATE brain_graph_nodes
                SET display_meta = jsonb_set(
                    COALESCE(display_meta, '{}'::jsonb),
                    '{cluster_id}',
                    cast(:cid_json as jsonb)
                ),
                updated_at = CURRENT_TIMESTAMP
                WHERE id = :nid
                """
            ),
            {"nid": step_nid, "cid_json": f'"{new_cluster_id}"'},
        )

    # ── 3. Disable old nm_lc_c_meta (preserve for FK integrity) ────────
    conn.execute(
        text(
            "UPDATE brain_graph_nodes SET enabled = FALSE, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = 'nm_lc_c_meta'"
        )
    )

    # ── 4. Add pipeline edges for new clusters ─────────────────────────
    new_edges = [
        # Chain: journal → meta_learning → decisioning → control
        ("nm_lc_c_journal", "nm_lc_c_meta_learning", "cluster_chain", 0.7, "excitatory", "control"),
        ("nm_lc_c_meta_learning", "nm_lc_c_decisioning", "cluster_chain", 0.7, "excitatory", "control"),
        ("nm_lc_c_decisioning", "nm_lc_c_control", "cluster_chain", 0.7, "excitatory", "control"),
        # Cluster → first step
        ("nm_lc_c_meta_learning", "nm_lc_ml", "step_completed", 0.7, "excitatory", "control"),
        ("nm_lc_c_decisioning", "nm_lc_pattern_engine", "step_completed", 0.7, "excitatory", "control"),
        ("nm_lc_c_control", "nm_lc_cycle_report", "step_completed", 0.7, "excitatory", "control"),
        # Last step → cluster completion
        ("nm_lc_ml", "nm_lc_c_meta_learning", "step_completed", 0.7, "excitatory", "control"),
        ("nm_lc_proposals", "nm_lc_c_decisioning", "step_completed", 0.7, "excitatory", "control"),
        ("nm_lc_finalize", "nm_lc_c_control", "step_completed", 0.7, "excitatory", "control"),
    ]
    for src, tgt, sig, w, pol, etype in new_edges:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    delay_ms, min_confidence, enabled, graph_version, gate_config,
                    edge_type, min_source_confidence,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    0, 0.0, TRUE, :gv, NULL,
                    :etype, 0.0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.signal_type = :sig AND e.graph_version = :gv
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "gv": gv, "etype": etype},
        )

    # ── 5. Disable old edges from/to nm_lc_c_meta ─────────────────────
    conn.execute(
        text(
            "UPDATE brain_graph_edges SET enabled = FALSE, updated_at = CURRENT_TIMESTAMP "
            "WHERE (source_node_id = 'nm_lc_c_meta' OR target_node_id = 'nm_lc_c_meta') "
            "AND enabled = TRUE"
        )
    )

    conn.commit()


def _migration_105_execution_context_venue_nodes(conn) -> None:
    """Add provider truth (Coinbase, Robinhood) and execution context nodes to neural mesh."""
    import json

    if "brain_graph_nodes" not in _tables(conn):
        conn.commit()
        return

    gv = 1
    dom = "trading"

    nodes = [
        ("nm_venue_truth_coinbase", 2, "feature_venue", "Coinbase venue truth", False, 0.45, 60, {
            "role": "venue_provider_truth",
            "venue": "coinbase",
            "execution_family": "coinbase_spot",
            "description": "Live Coinbase exchange state: spread, liquidity, tradability, product status.",
            "remarks": "Layer 2 venue node. Publishes execution readiness metadata from the Coinbase spot adapter.",
        }),
        ("nm_venue_truth_robinhood", 2, "feature_venue", "Robinhood venue truth", False, 0.45, 60, {
            "role": "venue_provider_truth",
            "venue": "robinhood",
            "execution_family": "robinhood_spot",
            "description": "Live Robinhood exchange state: quotes, tradability, market hours, product status.",
            "remarks": "Layer 2 venue node. Publishes execution readiness metadata from the Robinhood spot adapter.",
        }),
        ("nm_exec_liquidity_regime", 3, "latent_liquidity_regime", "Liquidity regime", False, 0.50, 90, {
            "role": "execution_context",
            "description": "Infers current liquidity regime from venue truth signals (thin, normal, deep).",
            "remarks": "Layer 3 execution context node. Aggregates venue signals into a regime classification.",
        }),
        ("nm_exec_spread_quality", 5, "evidence_execution", "Spread / slippage quality", False, 0.50, 60, {
            "role": "execution_context",
            "description": "Evaluates whether current spread and estimated slippage are within acceptable bounds.",
            "remarks": "Layer 5 evidence node. Gates execution readiness based on microstructure quality.",
        }),
        ("nm_exec_readiness_gate", 6, "action_exec_gate", "Execution readiness gate", False, 0.55, 30, {
            "role": "execution_context",
            "description": "Final execution readiness check: blocks action signals when venue conditions are unfavorable.",
            "remarks": "Layer 6 action gate. Inhibits action_signals and risk_gate when execution quality is poor.",
        }),
    ]

    for nid, layer, ntype, label, is_obs, fth, cd, dmeta in nodes:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    display_meta, created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, :ntype, :layer, :label,
                    :fth, :cd, TRUE, 1, :is_obs,
                    CAST(:dmeta AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": nid, "domain": dom, "gv": gv, "ntype": ntype,
                "layer": layer, "label": label, "fth": fth, "cd": cd,
                "is_obs": is_obs, "dmeta": json.dumps(dmeta),
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state,
                    last_activated_at, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": nid},
        )

    # Edges (typed)
    edges = [
        ("nm_venue_truth_coinbase", "nm_exec_liquidity_regime", "venue_refresh", 0.8, "excitatory", "dataflow"),
        ("nm_venue_truth_robinhood", "nm_exec_liquidity_regime", "venue_refresh", 0.8, "excitatory", "dataflow"),
        ("nm_exec_liquidity_regime", "nm_exec_spread_quality", "liquidity_update", 0.75, "excitatory", "evidence"),
        ("nm_exec_spread_quality", "nm_exec_readiness_gate", "spread_ok", 0.85, "excitatory", "evidence"),
        ("nm_exec_readiness_gate", "nm_action_signals", "exec_ready", 0.7, "excitatory", "control"),
        ("nm_exec_readiness_gate", "nm_risk_gate", "exec_not_ready", 0.9, "inhibitory", "veto"),
        # Connect existing liquidity_state to the new regime node
        ("nm_liquidity_state", "nm_exec_liquidity_regime", "feature_signal", 0.6, "excitatory", "dataflow"),
    ]

    for src, tgt, sig, w, pol, etype in edges:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    delay_ms, min_confidence, enabled, graph_version, gate_config,
                    edge_type, min_source_confidence,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    0, 0.0, TRUE, :gv, NULL,
                    :etype, 0.0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.signal_type = :sig AND e.graph_version = :gv
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "gv": gv, "etype": etype},
        )

    conn.commit()


def _migration_106_split_c_secondary_cluster(conn) -> None:
    """Split c_secondary into c_secondary_structure, c_secondary_outcomes, c_secondary_signals."""
    import json

    if "brain_graph_nodes" not in _tables(conn):
        conn.commit()
        return

    gv = 1
    dom = "trading"

    # ── 1. Insert new cluster nodes ────────────────────────────────────
    new_clusters = [
        ("nm_lc_c_secondary_structure", 8, "learning_cluster", "Pattern structure miners", False, 0.40, 30, {
            "role": "learning_cluster",
            "cluster_id": "c_secondary_structure",
            "description": "Mines intraday/HV patterns and refines candidate parameters.",
            "remarks": "Split from c_secondary. Covers structural pattern discovery and parameter polish.",
            "phase_summary": "brain_secondary_miners_on_cycle (structure)",
            "code_ref": "run_learning_cycle → c_secondary_structure",
        }),
        ("nm_lc_c_secondary_outcomes", 8, "learning_cluster", "Trade outcome learning", False, 0.40, 30, {
            "role": "learning_cluster",
            "cluster_id": "c_secondary_outcomes",
            "description": "Learns exit rules, fakeout filters, and position sizing from realized outcomes.",
            "remarks": "Split from c_secondary. Feeds back trade results into pattern scoring and risk hints.",
            "phase_summary": "brain_secondary_miners_on_cycle (outcomes)",
            "code_ref": "run_learning_cycle → c_secondary_outcomes",
        }),
        ("nm_lc_c_secondary_signals", 8, "learning_cluster", "Signal correlation miners", False, 0.40, 30, {
            "role": "learning_cluster",
            "cluster_id": "c_secondary_signals",
            "description": "Mines inter-alert sequences, timeframe attribution, and signal synergies.",
            "remarks": "Split from c_secondary. Temporal and portfolio-level signal correlation analysis.",
            "phase_summary": "brain_secondary_miners_on_cycle (signals)",
            "code_ref": "run_learning_cycle → c_secondary_signals",
        }),
    ]

    for nid, layer, ntype, label, is_obs, fth, cd, dmeta in new_clusters:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    display_meta, created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, :ntype, :layer, :label,
                    :fth, :cd, TRUE, 1, :is_obs,
                    CAST(:dmeta AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": nid, "domain": dom, "gv": gv, "ntype": ntype,
                "layer": layer, "label": label, "fth": fth, "cd": cd,
                "is_obs": is_obs, "dmeta": json.dumps(dmeta),
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state,
                    last_activated_at, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": nid},
        )

    # ── 2. Reassign step nodes to new clusters ─────────────────────────
    step_reassign = {
        "nm_lc_intraday_hv": "c_secondary_structure",
        "nm_lc_refine": "c_secondary_structure",
        "nm_lc_exit": "c_secondary_outcomes",
        "nm_lc_fakeout": "c_secondary_outcomes",
        "nm_lc_sizing": "c_secondary_outcomes",
        "nm_lc_inter_alert": "c_secondary_signals",
        "nm_lc_timeframe": "c_secondary_signals",
        "nm_lc_synergy": "c_secondary_signals",
    }
    for step_nid, new_cluster_id in step_reassign.items():
        conn.execute(
            text(
                """
                UPDATE brain_graph_nodes
                SET display_meta = jsonb_set(
                    COALESCE(display_meta, '{}'::jsonb),
                    '{cluster_id}',
                    cast(:cid_json as jsonb)
                ),
                updated_at = CURRENT_TIMESTAMP
                WHERE id = :nid
                """
            ),
            {"nid": step_nid, "cid_json": f'"{new_cluster_id}"'},
        )

    # ── 3. Disable old nm_lc_c_secondary (preserve for FK integrity) ──
    conn.execute(
        text(
            "UPDATE brain_graph_nodes SET enabled = FALSE, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = 'nm_lc_c_secondary'"
        )
    )

    # ── 4. Add pipeline edges for new clusters ─────────────────────────
    new_edges = [
        # Chain: evolution → secondary_structure → secondary_outcomes → secondary_signals → journal
        ("nm_lc_c_evolution", "nm_lc_c_secondary_structure", "cluster_chain", 0.7, "excitatory", "control"),
        ("nm_lc_c_secondary_structure", "nm_lc_c_secondary_outcomes", "cluster_chain", 0.7, "excitatory", "control"),
        ("nm_lc_c_secondary_outcomes", "nm_lc_c_secondary_signals", "cluster_chain", 0.7, "excitatory", "control"),
        ("nm_lc_c_secondary_signals", "nm_lc_c_journal", "cluster_chain", 0.7, "excitatory", "control"),
        # Cluster → first step
        ("nm_lc_c_secondary_structure", "nm_lc_intraday_hv", "step_completed", 0.7, "excitatory", "control"),
        ("nm_lc_c_secondary_outcomes", "nm_lc_exit", "step_completed", 0.7, "excitatory", "control"),
        ("nm_lc_c_secondary_signals", "nm_lc_inter_alert", "step_completed", 0.7, "excitatory", "control"),
        # Last step → cluster completion
        ("nm_lc_refine", "nm_lc_c_secondary_structure", "step_completed", 0.7, "excitatory", "control"),
        ("nm_lc_sizing", "nm_lc_c_secondary_outcomes", "step_completed", 0.7, "excitatory", "control"),
        ("nm_lc_synergy", "nm_lc_c_secondary_signals", "step_completed", 0.7, "excitatory", "control"),
    ]
    for src, tgt, sig, w, pol, etype in new_edges:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    delay_ms, min_confidence, enabled, graph_version, gate_config,
                    edge_type, min_source_confidence,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    0, 0.0, TRUE, :gv, NULL,
                    :etype, 0.0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.signal_type = :sig AND e.graph_version = :gv
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "gv": gv, "etype": etype},
        )

    # ── 5. Disable old edges from/to nm_lc_c_secondary ────────────────
    conn.execute(
        text(
            "UPDATE brain_graph_edges SET enabled = FALSE, updated_at = CURRENT_TIMESTAMP "
            "WHERE (source_node_id = 'nm_lc_c_secondary' OR target_node_id = 'nm_lc_c_secondary') "
            "AND enabled = TRUE"
        )
    )

    conn.commit()


def _migration_108_neural_mesh_lc_causal_edges(conn) -> None:
    """Interpretive causal_feedback edges from learning-cycle step nodes that emit mesh activations."""
    if "brain_graph_edges" not in _tables(conn):
        conn.commit()
        return
    # Extend edge_type check (103 seeded: dataflow, evidence, veto, feedback, control, operator_output).
    eg_cols = _columns(conn, "brain_graph_edges")
    if "edge_type" in eg_cols:
        conn.execute(text("ALTER TABLE brain_graph_edges DROP CONSTRAINT IF EXISTS ck_brain_graph_edges_edge_type"))
        conn.execute(
            text(
                "ALTER TABLE brain_graph_edges ADD CONSTRAINT ck_brain_graph_edges_edge_type "
                "CHECK (edge_type IN ("
                "'dataflow','evidence','veto','feedback','control','operator_output','causal_feedback'"
                "))"
            )
        )
    gv = 1
    edges = [
        ("nm_lc_depromote", "nm_evidence_quality", "lc_causal_depromote", 0.55, "excitatory", "causal_feedback"),
        ("nm_lc_bt_queue", "nm_evidence_quality", "lc_causal_bt_evidence", 0.5, "excitatory", "causal_feedback"),
    ]
    for src, tgt, sig, w, pol, etype in edges:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    delay_ms, min_confidence, enabled, graph_version, gate_config,
                    edge_type, min_source_confidence,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    0, 0.0, TRUE, :gv, NULL,
                    :etype, 0.0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.signal_type = :sig AND e.graph_version = :gv
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "gv": gv, "etype": etype},
        )
    conn.commit()


def _migration_107_scan_pattern_regime_affinity(conn) -> None:
    """Add regime_affinity_json JSONB column to scan_patterns."""
    if "scan_patterns" not in _tables(conn):
        conn.commit()
        return
    cols = _columns(conn, "scan_patterns")
    if "regime_affinity_json" not in cols:
        conn.execute(text(
            "ALTER TABLE scan_patterns "
            "ADD COLUMN regime_affinity_json JSONB NOT NULL DEFAULT '{}'"
        ))
    conn.commit()


def _migration_109_brain_work_events(conn) -> None:
    """Durable work ledger for event-first Trading Brain (separate from brain_activation_events)."""
    if "brain_work_events" in _tables(conn):
        conn.commit()
        return
    conn.execute(
        text(
            """
            CREATE TABLE brain_work_events (
                id BIGSERIAL PRIMARY KEY,
                domain TEXT NOT NULL DEFAULT 'trading',
                event_type TEXT NOT NULL,
                event_kind TEXT NOT NULL DEFAULT 'work',
                payload JSONB,
                dedupe_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                next_run_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                lease_holder TEXT,
                lease_expires_at TIMESTAMP,
                last_error TEXT,
                correlation_id TEXT,
                parent_event_id BIGINT REFERENCES brain_work_events(id) ON DELETE SET NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                CONSTRAINT ck_brain_work_events_status CHECK (
                    status IN ('pending', 'processing', 'retry_wait', 'done', 'dead')
                ),
                CONSTRAINT ck_brain_work_events_kind CHECK (event_kind IN ('work', 'outcome'))
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE INDEX ix_brain_work_events_domain_status_next "
            "ON brain_work_events (domain, status, next_run_at)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX ix_brain_work_events_domain_type_created "
            "ON brain_work_events (domain, event_type, created_at DESC)"
        )
    )
    conn.execute(
        text(
            """
            CREATE UNIQUE INDEX uq_brain_work_events_open_dedupe
            ON brain_work_events (dedupe_key)
            WHERE status IN ('pending', 'processing', 'retry_wait')
            """
        )
    )
    conn.commit()


def _migration_110_brain_work_lease_scope(conn) -> None:
    """Lease scope column for brain_work_events (handler-family visibility + future claim filters)."""
    if "brain_work_events" not in _tables(conn):
        conn.commit()
        return
    cols = _columns(conn, "brain_work_events")
    if "lease_scope" not in cols:
        conn.execute(
            text(
                "ALTER TABLE brain_work_events "
                "ADD COLUMN lease_scope TEXT NOT NULL DEFAULT 'general'"
            )
        )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_brain_work_events_scope_status_next "
            "ON brain_work_events (lease_scope, status, next_run_at)"
        )
    )
    conn.commit()


def _migration_111_trading_decision_stack(conn) -> None:
    """Decision packets, candidates, deployment ladder state; link simulated fills to packets."""
    if "trading_decision_packets" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE trading_decision_packets (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    automation_session_id INTEGER REFERENCES trading_automation_sessions(id) ON DELETE SET NULL,
                    scan_pattern_id INTEGER REFERENCES scan_patterns(id) ON DELETE SET NULL,
                    chosen_ticker VARCHAR(36),
                    decision_type VARCHAR(24) NOT NULL DEFAULT 'trade',
                    execution_mode VARCHAR(16) NOT NULL DEFAULT 'paper',
                    deployment_stage VARCHAR(24) NOT NULL DEFAULT 'paper',
                    regime_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    allocator_input_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    allocator_output_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    portfolio_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    expected_edge_gross DOUBLE PRECISION,
                    expected_edge_net DOUBLE PRECISION,
                    expected_slippage_bps DOUBLE PRECISION,
                    expected_fill_probability DOUBLE PRECISION,
                    expected_partial_fill_probability DOUBLE PRECISION,
                    expected_missed_fill_probability DOUBLE PRECISION,
                    risk_budget_pct DOUBLE PRECISION,
                    size_notional DOUBLE PRECISION,
                    size_shares_or_qty DOUBLE PRECISION,
                    abstain_reason_code VARCHAR(64),
                    abstain_reason_text TEXT,
                    selected_candidate_rank INTEGER,
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    capacity_blocked BOOLEAN NOT NULL DEFAULT FALSE,
                    capacity_reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    correlation_penalty DOUBLE PRECISION,
                    uncertainty_haircut DOUBLE PRECISION,
                    execution_penalty DOUBLE PRECISION,
                    final_score DOUBLE PRECISION,
                    source_surface VARCHAR(32) NOT NULL DEFAULT 'autopilot',
                    research_vs_live_context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    linked_trade_id INTEGER REFERENCES trading_trades(id) ON DELETE SET NULL,
                    outcome_status VARCHAR(24) NOT NULL DEFAULT 'pending',
                    shadow_advisory_only BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tdp_user_created ON trading_decision_packets (user_id, created_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tdp_session_created ON trading_decision_packets (automation_session_id, created_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tdp_ticker_created ON trading_decision_packets (chosen_ticker, created_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tdp_pattern_created ON trading_decision_packets (scan_pattern_id, created_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tdp_mode_stage ON trading_decision_packets (execution_mode, deployment_stage)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tdp_outcome ON trading_decision_packets (outcome_status, created_at DESC)"
            )
        )
    if "trading_decision_candidates" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE trading_decision_candidates (
                    id BIGSERIAL PRIMARY KEY,
                    decision_packet_id BIGINT NOT NULL REFERENCES trading_decision_packets(id) ON DELETE CASCADE,
                    rank INTEGER NOT NULL DEFAULT 0,
                    ticker VARCHAR(36) NOT NULL,
                    scan_pattern_id INTEGER REFERENCES scan_patterns(id) ON DELETE SET NULL,
                    candidate_score_raw DOUBLE PRECISION,
                    candidate_score_net DOUBLE PRECISION,
                    expected_edge_gross DOUBLE PRECISION,
                    expected_edge_net DOUBLE PRECISION,
                    expected_slippage_bps DOUBLE PRECISION,
                    expected_fill_probability DOUBLE PRECISION,
                    size_cap_notional DOUBLE PRECISION,
                    was_selected BOOLEAN NOT NULL DEFAULT FALSE,
                    reject_reason_code VARCHAR(64),
                    reject_reason_text TEXT,
                    reject_detail_json JSONB NOT NULL DEFAULT '{}'::jsonb
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tdc_packet_rank ON trading_decision_candidates (decision_packet_id, rank)"
            )
        )
    if "trading_deployment_states" not in _tables(conn):
        conn.execute(
            text(
                """
                CREATE TABLE trading_deployment_states (
                    id SERIAL PRIMARY KEY,
                    scope_type VARCHAR(32) NOT NULL,
                    scope_key VARCHAR(256) NOT NULL,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    current_stage VARCHAR(24) NOT NULL DEFAULT 'paper',
                    promoted_at TIMESTAMP,
                    degraded_at TIMESTAMP,
                    disabled_at TIMESTAMP,
                    stage_metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    live_trade_count INTEGER NOT NULL DEFAULT 0,
                    paper_trade_count INTEGER NOT NULL DEFAULT 0,
                    rolling_win_rate DOUBLE PRECISION,
                    rolling_expectancy_net DOUBLE PRECISION,
                    rolling_slippage_bps DOUBLE PRECISION,
                    rolling_drawdown_pct DOUBLE PRECISION,
                    rolling_missed_fill_rate DOUBLE PRECISION,
                    rolling_partial_fill_rate DOUBLE PRECISION,
                    last_reason_code VARCHAR(64),
                    last_reason_text TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_trading_deployment_scope UNIQUE (scope_type, scope_key)
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tds_user_stage ON trading_deployment_states (user_id, current_stage)"
            )
        )
    if "trading_automation_simulated_fills" in _tables(conn):
        sf_cols = _columns(conn, "trading_automation_simulated_fills")
        if "decision_packet_id" not in sf_cols:
            conn.execute(
                text(
                    "ALTER TABLE trading_automation_simulated_fills "
                    "ADD COLUMN decision_packet_id BIGINT REFERENCES trading_decision_packets(id) ON DELETE SET NULL"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_tasf_decision_packet ON trading_automation_simulated_fills (decision_packet_id)"
                )
            )
    conn.commit()


def _migration_112_trade_sector_and_governance_approvals(conn) -> None:
    """Add Trade.sector and persistent governance approvals table."""
    tables = _tables(conn)
    if "trading_trades" in tables:
        cols = _columns(conn, "trading_trades")
        if "sector" not in cols:
            conn.execute(text("ALTER TABLE trading_trades ADD COLUMN sector VARCHAR(80)"))
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_trading_trades_sector ON trading_trades (sector)")
            )
    if "trading_governance_approvals" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE trading_governance_approvals (
                    id BIGSERIAL PRIMARY KEY,
                    action_type VARCHAR(64) NOT NULL,
                    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    submitted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    status VARCHAR(24) NOT NULL DEFAULT 'pending',
                    decision VARCHAR(24),
                    decided_at TIMESTAMP,
                    notes TEXT NOT NULL DEFAULT ''
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tga_status_submitted "
                "ON trading_governance_approvals (status, submitted_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tga_action_status "
                "ON trading_governance_approvals (action_type, status)"
            )
        )
    conn.commit()


def _migration_113_trade_stop_columns(conn) -> None:
    """First-class stop/target/trail columns on trading_trades for the stop engine."""
    if "trading_trades" not in _tables(conn):
        conn.commit()
        return
    cols = _columns(conn, "trading_trades")
    for col, typ in [
        ("stop_loss", "DOUBLE PRECISION"),
        ("take_profit", "DOUBLE PRECISION"),
        ("trail_stop", "DOUBLE PRECISION"),
        ("high_watermark", "DOUBLE PRECISION"),
        ("stop_model", "VARCHAR(30)"),
        ("exit_reason", "VARCHAR(50)"),
    ]:
        if col not in cols:
            conn.execute(text(f"ALTER TABLE trading_trades ADD COLUMN {col} {typ}"))
    conn.commit()


def _migration_114_stop_decisions_and_delivery(conn) -> None:
    """Audit table for stop-engine decisions and alert delivery attempts."""
    tables = _tables(conn)
    if "trading_stop_decisions" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE trading_stop_decisions (
                    id BIGSERIAL PRIMARY KEY,
                    trade_id INTEGER NOT NULL REFERENCES trading_trades(id) ON DELETE CASCADE,
                    as_of_ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    state VARCHAR(24) NOT NULL,
                    old_stop DOUBLE PRECISION,
                    new_stop DOUBLE PRECISION,
                    trigger VARCHAR(50),
                    inputs_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    reason TEXT NOT NULL DEFAULT '',
                    executed BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tsd_trade_ts "
                "ON trading_stop_decisions (trade_id, as_of_ts DESC)"
            )
        )
    if "trading_alert_delivery_attempts" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE trading_alert_delivery_attempts (
                    id BIGSERIAL PRIMARY KEY,
                    alert_id INTEGER NOT NULL REFERENCES trading_alerts(id) ON DELETE CASCADE,
                    channel VARCHAR(30) NOT NULL,
                    provider_msg_id VARCHAR(200),
                    status VARCHAR(20) NOT NULL DEFAULT 'queued',
                    attempt_n INTEGER NOT NULL DEFAULT 1,
                    next_retry_at TIMESTAMP,
                    last_error TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_tada_alert_status "
                "ON trading_alert_delivery_attempts (alert_id, status)"
            )
        )
    conn.commit()


def _migration_115_schema_hardening_fks(conn) -> None:
    """Add missing FK constraints across all domains after data repair."""
    tables = _tables(conn)

    fks_to_add: list[tuple[str, str, str, str, str, str]] = [
        # (constraint_name, source_table, source_col, target_table, target_col, on_delete)
        # --- Trading domain ---
        ("fk_trades_user", "trading_trades", "user_id", "users", "id", "SET NULL"),
        ("fk_trades_proposal", "trading_trades", "strategy_proposal_id", "trading_proposals", "id", "SET NULL"),
        ("fk_journal_trade", "trading_journal", "trade_id", "trading_trades", "id", "CASCADE"),
        ("fk_journal_user", "trading_journal", "user_id", "users", "id", "SET NULL"),
        ("fk_paper_user", "trading_paper_trades", "user_id", "users", "id", "SET NULL"),
        ("fk_watchlist_user", "trading_watchlist", "user_id", "users", "id", "SET NULL"),
        ("fk_scans_user", "trading_scans", "user_id", "users", "id", "SET NULL"),
        ("fk_alerts_sp", "trading_alerts", "scan_pattern_id", "scan_patterns", "id", "SET NULL"),
        ("fk_alerts_user", "trading_alerts", "user_id", "users", "id", "SET NULL"),
        ("fk_breakout_insight", "trading_breakout_alerts", "related_insight_id", "trading_insights", "id", "SET NULL"),
        ("fk_breakout_user", "trading_breakout_alerts", "user_id", "users", "id", "SET NULL"),
        ("fk_proposals_sp", "trading_proposals", "scan_pattern_id", "scan_patterns", "id", "SET NULL"),
        ("fk_proposals_trade", "trading_proposals", "trade_id", "trading_trades", "id", "SET NULL"),
        ("fk_proposals_user", "trading_proposals", "user_id", "users", "id", "SET NULL"),
        ("fk_ptrades_sp", "trading_pattern_trades", "scan_pattern_id", "scan_patterns", "id", "SET NULL"),
        ("fk_ptrades_insight", "trading_pattern_trades", "related_insight_id", "trading_insights", "id", "SET NULL"),
        ("fk_ptrades_bt", "trading_pattern_trades", "backtest_result_id", "trading_backtests", "id", "SET NULL"),
        ("fk_peh_sp", "trading_pattern_evidence_hypotheses", "scan_pattern_id", "scan_patterns", "id", "SET NULL"),
        ("fk_levents_insight", "trading_learning_events", "related_insight_id", "trading_insights", "id", "SET NULL"),
        ("fk_levents_user", "trading_learning_events", "user_id", "users", "id", "SET NULL"),
        ("fk_bvsl_sp", "brain_validation_slice_ledger", "scan_pattern_id", "scan_patterns", "id", "CASCADE"),
        ("fk_sp_parent", "scan_patterns", "parent_id", "scan_patterns", "id", "SET NULL"),
        # --- Code brain ---
        ("fk_cinsight_repo", "code_insights", "repo_id", "code_repos", "id", "CASCADE"),
        ("fk_cinsight_user", "code_insights", "user_id", "users", "id", "SET NULL"),
        ("fk_csnapshot_repo", "code_snapshots", "repo_id", "code_repos", "id", "CASCADE"),
        ("fk_chotspot_repo", "code_hotspots", "repo_id", "code_repos", "id", "CASCADE"),
        ("fk_clevent_user", "code_learning_events", "user_id", "users", "id", "SET NULL"),
        ("fk_cdep_repo", "code_dependencies", "repo_id", "code_repos", "id", "CASCADE"),
        ("fk_cqsnap_repo", "code_quality_snapshots", "repo_id", "code_repos", "id", "CASCADE"),
        ("fk_creview_repo", "code_reviews", "repo_id", "code_repos", "id", "CASCADE"),
        ("fk_creview_user", "code_reviews", "user_id", "users", "id", "SET NULL"),
        ("fk_cdepalert_repo", "code_dep_alerts", "repo_id", "code_repos", "id", "CASCADE"),
        ("fk_csearch_repo", "code_search_index", "repo_id", "code_repos", "id", "CASCADE"),
        # --- Reasoning brain ---
        ("fk_rum_user", "reasoning_user_models", "user_id", "users", "id", "CASCADE"),
        ("fk_rint_user", "reasoning_interests", "user_id", "users", "id", "CASCADE"),
        ("fk_rres_user", "reasoning_research", "user_id", "users", "id", "CASCADE"),
        ("fk_rant_user", "reasoning_anticipations", "user_id", "users", "id", "CASCADE"),
        ("fk_revt_user", "reasoning_events", "user_id", "users", "id", "SET NULL"),
        ("fk_rlg_user", "reasoning_learning_goals", "user_id", "users", "id", "CASCADE"),
        ("fk_rhyp_user", "reasoning_hypotheses", "user_id", "users", "id", "CASCADE"),
        ("fk_rconf_user", "reasoning_confidence_snapshots", "user_id", "users", "id", "CASCADE"),
        # --- Project brain ---
        ("fk_pas_user", "project_agent_states", "user_id", "users", "id", "SET NULL"),
        ("fk_afind_user", "agent_findings", "user_id", "users", "id", "SET NULL"),
        ("fk_ares_user", "agent_research", "user_id", "users", "id", "SET NULL"),
        ("fk_agoal_user", "agent_goals", "user_id", "users", "id", "SET NULL"),
        ("fk_aevo_user", "agent_evolutions", "user_id", "users", "id", "SET NULL"),
        ("fk_amsg_user", "agent_messages", "user_id", "users", "id", "SET NULL"),
        ("fk_poq_user", "po_questions", "user_id", "users", "id", "SET NULL"),
        ("fk_poreq_user", "po_requirements", "user_id", "users", "id", "SET NULL"),
        ("fk_qatc_user", "qa_test_cases", "user_id", "users", "id", "SET NULL"),
        ("fk_qatr_user", "qa_test_runs", "user_id", "users", "id", "SET NULL"),
        ("fk_qabr_user", "qa_bug_reports", "user_id", "users", "id", "SET NULL"),
        # --- Core (missing despite model FK declarations) ---
        ("fk_devices_user", "devices", "user_id", "users", "id", "CASCADE"),
        ("fk_paircodes_user", "pair_codes", "user_id", "users", "id", "CASCADE"),
    ]

    existing_constraints: set[str] = set()
    try:
        rows = conn.execute(text(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE constraint_type = 'FOREIGN KEY' AND table_schema = 'public'"
        )).fetchall()
        existing_constraints = {r[0] for r in rows}
    except Exception:
        pass

    for cname, src_table, src_col, tgt_table, tgt_col, on_del in fks_to_add:
        if cname in existing_constraints:
            continue
        if src_table not in tables or tgt_table not in tables:
            continue
        try:
            conn.execute(text(
                f"ALTER TABLE {src_table} ADD CONSTRAINT {cname} "
                f"FOREIGN KEY ({src_col}) REFERENCES {tgt_table}({tgt_col}) "
                f"ON DELETE {on_del}"
            ))
        except Exception:
            conn.rollback()

    try:
        conn.execute(text(
            "UPDATE scan_patterns SET win_rate = NULL WHERE win_rate = 'NaN'::float"
        ))
        conn.execute(text(
            "UPDATE scan_patterns SET oos_win_rate = NULL WHERE oos_win_rate = 'NaN'::float"
        ))
    except Exception:
        pass

    conn.commit()


def _migration_118_dynamic_trade_plan_monitor(conn) -> None:
    """Add BreakoutAlert.trade_plan JSONB; insert position-monitor mesh nodes and edges."""
    cols = _columns(conn, "trading_breakout_alerts")
    if "trade_plan" not in cols:
        conn.execute(text(
            "ALTER TABLE trading_breakout_alerts ADD COLUMN trade_plan JSONB"
        ))

    # Neural mesh: position monitor spine node (action tier, layer 6).
    conn.execute(text("""
        INSERT INTO brain_graph_nodes (id, domain, graph_version, node_type, layer, label,
                                       fire_threshold, cooldown_seconds, enabled, version,
                                       is_observer, display_meta, created_at, updated_at)
        VALUES
            ('nm_position_monitor', 'trading', 1, 'action_position_monitor', 6,
             'Position monitor', 0.55, 60, true, 1, false,
             '{"role":"action_position_monitor","desc":"Pattern-aware live position management"}',
             NOW(), NOW()),
            ('nm_lc_monitor_review', 'trading', 1, 'learning_step', 9,
             'Monitor decision review', 0.5, 120, true, 1, false,
             '{"role":"learning_step","cluster_id":"c_secondary_outcomes","step_sid":"monitor_review",'
             '"desc":"Reviews pattern-monitor decision outcomes for threshold evolution"}',
             NOW(), NOW())
        ON CONFLICT (id) DO NOTHING
    """))

    for _nid in ("nm_position_monitor", "nm_lc_monitor_review"):
        conn.execute(text("""
            INSERT INTO brain_node_states (
                node_id, activation_score, confidence, local_state, updated_at
            )
            VALUES (:nid, 0.0, 0.5, '{}'::jsonb, CURRENT_TIMESTAMP)
            ON CONFLICT (node_id) DO NOTHING
        """), {"nid": _nid})

    for src, tgt, sig, w, pol in [
        ("nm_latent_regime", "nm_position_monitor", "regime_shift", 0.6, "excitatory"),
        ("nm_evidence_quality", "nm_position_monitor", "evidence_ok", 0.5, "excitatory"),
        ("nm_position_monitor", "nm_risk_gate", "position_health", 0.7, "excitatory"),
        ("nm_position_monitor", "nm_action_alerts", "monitor_alert", 0.65, "excitatory"),
        ("nm_lc_monitor_review", "nm_evidence_quality", "monitor_feedback", 0.5, "excitatory"),
    ]:
        etype = _brain_graph_edge_type_for_seed(src, sig, pol)
        conn.execute(text("""
            INSERT INTO brain_graph_edges
                (source_node_id, target_node_id, signal_type, weight, polarity,
                 delay_ms, min_confidence, enabled, graph_version,
                 edge_type, min_source_confidence, created_at, updated_at)
            SELECT :src, :tgt, :sig, :w, :pol,
                   0, 0.0, true, 1,
                   :etype, 0.0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            WHERE EXISTS (SELECT 1 FROM brain_graph_nodes WHERE id = :src)
              AND EXISTS (SELECT 1 FROM brain_graph_nodes WHERE id = :tgt)
              AND NOT EXISTS (
                  SELECT 1 FROM brain_graph_edges
                  WHERE source_node_id = :src AND target_node_id = :tgt AND signal_type = :sig
              )
        """), {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "etype": etype})

    conn.commit()


def _migration_117_pattern_position_monitor(conn) -> None:
    """Add Trade.related_alert_id FK and create pattern monitor decisions table."""
    cols = _columns(conn, "trading_trades")
    if "related_alert_id" not in cols:
        conn.execute(text(
            "ALTER TABLE trading_trades ADD COLUMN related_alert_id INTEGER "
            "REFERENCES trading_breakout_alerts(id) ON DELETE SET NULL"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_trading_trades_related_alert "
            "ON trading_trades (related_alert_id) WHERE related_alert_id IS NOT NULL"
        ))

    tables = _tables(conn)
    if "trading_pattern_monitor_decisions" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pattern_monitor_decisions (
                id SERIAL PRIMARY KEY,
                trade_id INTEGER REFERENCES trading_trades(id) ON DELETE CASCADE NOT NULL,
                breakout_alert_id INTEGER REFERENCES trading_breakout_alerts(id) ON DELETE SET NULL,
                scan_pattern_id INTEGER REFERENCES scan_patterns(id) ON DELETE SET NULL,
                health_score FLOAT NOT NULL,
                health_delta FLOAT,
                conditions_snapshot JSONB,
                action VARCHAR(30) NOT NULL,
                old_stop FLOAT,
                new_stop FLOAT,
                old_target FLOAT,
                new_target FLOAT,
                llm_confidence FLOAT,
                llm_reasoning TEXT,
                price_at_decision FLOAT,
                price_after_1h FLOAT,
                price_after_4h FLOAT,
                was_beneficial BOOLEAN,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_pmd_trade_created ON trading_pattern_monitor_decisions (trade_id, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pmd_pattern_created ON trading_pattern_monitor_decisions (scan_pattern_id, created_at DESC)"
        ))
    conn.commit()


def _migration_116_trade_type_column(conn) -> None:
    """Add trade_type column to trading_trades for daytrade/scalp classification."""
    cols = _columns(conn, "trading_trades")
    if "trade_type" not in cols:
        conn.execute(text("ALTER TABLE trading_trades ADD COLUMN trade_type VARCHAR(30)"))
    conn.commit()


def _migration_119_broker_sessions_table(conn) -> None:
    """Create broker_sessions table for storing API session tokens in PostgreSQL."""
    tables = _tables(conn)
    if "broker_sessions" not in tables:
        conn.execute(text("""
            CREATE TABLE broker_sessions (
                id SERIAL PRIMARY KEY,
                broker VARCHAR NOT NULL,
                username VARCHAR NOT NULL,
                token_data JSONB NOT NULL,
                device_token VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_broker_session UNIQUE (broker, username)
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_broker_sessions_broker ON broker_sessions (broker)"
        ))
    conn.commit()


def _migration_120_monitor_learning_engine(conn) -> None:
    """Tables for the self-learning monitor: decision rules, plan accuracy, and
    new columns on existing tables for dual-path (mechanical vs LLM) tracking.
    Also inserts c_monitor_learning neural mesh cluster (3 nodes + 4 edges)."""
    tables = _tables(conn)

    # ── MonitorDecisionRule ──
    if "trading_monitor_decision_rules" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_monitor_decision_rules (
                id SERIAL PRIMARY KEY,
                pattern_type VARCHAR(120) NOT NULL,
                signal_signature VARCHAR(200) NOT NULL,
                action VARCHAR(30) NOT NULL,
                stop_ratio FLOAT,
                target_ratio FLOAT,
                sample_count INTEGER NOT NULL DEFAULT 0,
                benefit_rate FLOAT NOT NULL DEFAULT 0,
                llm_agreement_rate FLOAT NOT NULL DEFAULT 0,
                graduation_status VARCHAR(20) NOT NULL DEFAULT 'bootstrap',
                rolling_benefit JSONB,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_mdr_pattern_type ON trading_monitor_decision_rules (pattern_type)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_mdr_signal_sig ON trading_monitor_decision_rules (signal_signature)"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_mdr_pt_sig ON trading_monitor_decision_rules (pattern_type, signal_signature)"
        ))

    # ── MonitorPlanAccuracy ──
    if "trading_monitor_plan_accuracy" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_monitor_plan_accuracy (
                id SERIAL PRIMARY KEY,
                pattern_type VARCHAR(120) NOT NULL,
                complexity_band VARCHAR(20) NOT NULL DEFAULT 'simple',
                llm_correct_count INTEGER NOT NULL DEFAULT 0,
                mechanical_correct_count INTEGER NOT NULL DEFAULT 0,
                agreement_count INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                graduation_status VARCHAR(20) NOT NULL DEFAULT 'bootstrap',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_mpa_pattern_type ON trading_monitor_plan_accuracy (pattern_type)"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_mpa_pt_cb ON trading_monitor_plan_accuracy (pattern_type, complexity_band)"
        ))

    # ── New columns on PatternMonitorDecision for dual-path tracking ──
    pmd_cols = _columns(conn, "trading_pattern_monitor_decisions")
    for col, typedef in [
        ("mechanical_action", "VARCHAR(30)"),
        ("mechanical_stop", "FLOAT"),
        ("mechanical_target", "FLOAT"),
        ("decision_source", "VARCHAR(20)"),
    ]:
        if col not in pmd_cols:
            conn.execute(text(
                f"ALTER TABLE trading_pattern_monitor_decisions ADD COLUMN {col} {typedef}"
            ))

    # ── New column on BreakoutAlert for mechanical trade plan ──
    ba_cols = _columns(conn, "trading_breakout_alerts")
    if "trade_plan_mechanical" not in ba_cols:
        conn.execute(text(
            "ALTER TABLE trading_breakout_alerts ADD COLUMN trade_plan_mechanical JSONB"
        ))

    # ── Neural mesh: c_monitor_learning cluster (3 nodes + 4 edges) ──
    _existing_nodes = set()
    try:
        rows = conn.execute(text(
            "SELECT id FROM brain_graph_nodes WHERE id LIKE 'nm_monitor_%'"
        )).fetchall()
        _existing_nodes = {r[0] for r in rows}
    except Exception:
        pass

    _nodes = [
        ("nm_monitor_rules_learner", "learning_step",
         "Monitor rules learner", "c_monitor_learning",
         "Aggregates decision outcomes into learned rules per signal signature"),
        ("nm_plan_accuracy_tracker", "learning_step",
         "Plan accuracy tracker", "c_monitor_learning",
         "Compares mechanical vs LLM trade plan accuracy per pattern type"),
        ("nm_monitor_graduation", "learning_step",
         "Monitor graduation manager", "c_monitor_learning",
         "Manages per-pattern-type graduation lifecycle"),
    ]
    for nid, ntype, label, cluster, desc in _nodes:
        if nid not in _existing_nodes:
            conn.execute(text("""
                INSERT INTO brain_graph_nodes
                    (id, domain, graph_version, node_type, layer, label,
                     fire_threshold, cooldown_seconds, enabled, version,
                     is_observer, display_meta, created_at, updated_at)
                VALUES
                    (:nid, 'trading', 1, :ntype, 9, :label,
                     0.5, 120, true, 1, false,
                     :meta, NOW(), NOW())
            """), {
                "nid": nid, "ntype": ntype, "label": label,
                "meta": (
                    '{"role":"' + ntype + '","cluster_id":"' + cluster + '",'
                    '"desc":"' + desc + '"}'
                ),
            })
            _state_exists = conn.execute(text(
                "SELECT 1 FROM brain_node_states WHERE node_id = :nid"
            ), {"nid": nid}).fetchone()
            if not _state_exists:
                conn.execute(text("""
                    INSERT INTO brain_node_states
                        (node_id, activation_score, confidence, local_state, updated_at)
                    VALUES (:nid, 0.0, 0.5, '{}'::jsonb, CURRENT_TIMESTAMP)
                """), {"nid": nid})

    _edges = [
        ("nm_lc_monitor_review", "nm_monitor_rules_learner", "outcome_data_ready"),
        ("nm_monitor_rules_learner", "nm_plan_accuracy_tracker", "rules_updated"),
        ("nm_plan_accuracy_tracker", "nm_monitor_graduation", "accuracy_computed"),
        ("nm_monitor_graduation", "nm_position_monitor", "graduation_status_changed"),
    ]
    for src, tgt, sig in _edges:
        exists = conn.execute(text(
            "SELECT 1 FROM brain_graph_edges "
            "WHERE source_node_id = :src AND target_node_id = :tgt AND signal_type = :sig"
        ), {"src": src, "tgt": tgt, "sig": sig}).fetchone()
        if not exists:
            etype = _brain_graph_edge_type_for_seed(src, sig, "excitatory")
            conn.execute(text("""
                INSERT INTO brain_graph_edges
                    (source_node_id, target_node_id, signal_type, weight, polarity,
                     delay_ms, min_confidence, enabled, graph_version,
                     edge_type, min_source_confidence, created_at, updated_at)
                SELECT :src, :tgt, :sig, 0.5, 'excitatory',
                       0, 0.0, true, 1,
                       :etype, 0.0, NOW(), NOW()
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes WHERE id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes WHERE id = :tgt)
            """), {"src": src, "tgt": tgt, "sig": sig, "etype": etype})

    conn.commit()


def _migration_121_autopilot_profitability_outcomes(conn) -> None:
    """Entry/exit regime snapshots on momentum outcomes; supports family-regime analytics."""
    if "momentum_automation_outcomes" not in _tables(conn):
        return
    cols = _columns(conn, "momentum_automation_outcomes")
    if "entry_regime_snapshot_json" not in cols:
        conn.execute(
            text(
                "ALTER TABLE momentum_automation_outcomes "
                "ADD COLUMN entry_regime_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
        )
    if "exit_regime_snapshot_json" not in cols:
        conn.execute(
            text(
                "ALTER TABLE momentum_automation_outcomes "
                "ADD COLUMN exit_regime_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
        )
    conn.commit()


def _migration_122_position_plans_table(conn) -> None:
    """Table for cached LLM-generated position evaluation plans."""
    if "trading_position_plans" not in _tables(conn):
        conn.execute(
            text(
                "CREATE TABLE trading_position_plans ("
                "  id SERIAL PRIMARY KEY,"
                "  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,"
                "  trade_ids JSONB NOT NULL DEFAULT '[]'::jsonb,"
                "  plan_json JSONB NOT NULL DEFAULT '{}'::jsonb,"
                "  generated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trading_position_plans_user_gen "
                "ON trading_position_plans (user_id, generated_at DESC)"
            )
        )
    conn.commit()


def _migration_123_setup_vitals_engine(conn) -> None:
    """Ticker vitals cache, per-setup vitals history, PatternMonitorDecision.vitals_composite, mesh node."""
    tables = _tables(conn)

    if "trading_ticker_vitals" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE trading_ticker_vitals (
                    id SERIAL PRIMARY KEY,
                    ticker VARCHAR(32) NOT NULL,
                    bar_interval VARCHAR(16) NOT NULL DEFAULT '1d',
                    momentum_score DOUBLE PRECISION,
                    volume_score DOUBLE PRECISION,
                    trend_score DOUBLE PRECISION,
                    overextension_risk DOUBLE PRECISION,
                    composite_health DOUBLE PRECISION,
                    trajectory_json JSONB,
                    divergences_json JSONB,
                    computed_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uix_ticker_vitals_ticker_interval "
                "ON trading_ticker_vitals (ticker, bar_interval)"
            )
        )
        conn.execute(text("CREATE INDEX ix_ticker_vitals_computed ON trading_ticker_vitals (computed_at)"))

    if "trading_setup_vitals_history" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE trading_setup_vitals_history (
                    id SERIAL PRIMARY KEY,
                    trade_id INTEGER REFERENCES trading_trades(id) ON DELETE CASCADE,
                    breakout_alert_id INTEGER REFERENCES trading_breakout_alerts(id) ON DELETE SET NULL,
                    momentum_score DOUBLE PRECISION,
                    volume_score DOUBLE PRECISION,
                    trend_score DOUBLE PRECISION,
                    overextension_risk DOUBLE PRECISION,
                    composite_health DOUBLE PRECISION,
                    price_at_check DOUBLE PRECISION,
                    degradation_flags JSONB,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_setup_vitals_hist_trade_created "
                "ON trading_setup_vitals_history (trade_id, created_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_setup_vitals_hist_created ON trading_setup_vitals_history (created_at)"
            )
        )

    cols_pmd = _columns(conn, "trading_pattern_monitor_decisions")
    if "vitals_composite" not in cols_pmd:
        try:
            conn.execute(
                text(
                    "ALTER TABLE trading_pattern_monitor_decisions "
                    "ADD COLUMN vitals_composite DOUBLE PRECISION"
                )
            )
        except Exception:
            pass

    # Neural mesh: setup health observer node (idempotent)
    try:
        r = conn.execute(
            text("SELECT 1 FROM brain_graph_nodes WHERE id = 'nm_setup_health'")
        ).fetchone()
        if not r:
            conn.execute(
                text(
                    """
                    INSERT INTO brain_graph_nodes
                        (id, domain, graph_version, node_type, layer, label,
                         fire_threshold, cooldown_seconds, enabled, version, is_observer, display_meta)
                    VALUES
                        ('nm_setup_health', 'trading', 1, 'observer_setup_health', 6,
                         'Setup vitals / trajectory health', 0.55, 90, TRUE, 1, TRUE,
                         '{"role":"setup_vitals","description":"Indicator trajectory and setup health monitoring"}'::jsonb)
                    """
                )
            )
    except Exception:
        pass

    conn.commit()


def _migration_124_alert_content_signature(conn) -> None:
    """Dedup pattern_monitor Telegram: persist content hash on trading_alerts."""
    cols = _columns(conn, "trading_alerts")
    if "content_signature" not in cols:
        conn.execute(
            text(
                "ALTER TABLE trading_alerts ADD COLUMN content_signature VARCHAR(512)"
            )
        )
    conn.commit()


def _migration_125_mesh_reactive_sensors(conn) -> None:
    """Phase 0+1 of mesh-driven alert architecture:
    - Postgres NOTIFY trigger on brain_activation_events for instant reactivity
    - Sensor nodes (nm_stop_eval, nm_pattern_health) + edges to nm_action_signals
    """
    import json
    tables = _tables(conn)

    # ── 1. Postgres NOTIFY trigger for reactive mesh ──
    if "brain_activation_events" in tables:
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION mesh_activation_notify()
            RETURNS trigger AS $$
            BEGIN
                PERFORM pg_notify('mesh_activation', NEW.id::text);
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """))
        conn.execute(text("""
            DROP TRIGGER IF EXISTS trg_mesh_activation_notify
            ON brain_activation_events
        """))
        conn.execute(text("""
            CREATE TRIGGER trg_mesh_activation_notify
            AFTER INSERT ON brain_activation_events
            FOR EACH ROW
            WHEN (NEW.status = 'pending')
            EXECUTE FUNCTION mesh_activation_notify()
        """))

    # ── 2. Sensor nodes ──
    if "brain_graph_nodes" in tables:
        sensor_nodes = [
            ("nm_stop_eval", 2, "sensor_stop", "Stop engine sensor", False, 0.50, 30, {
                "role": "sensor",
                "description": "Publishes stop engine evaluation results (stop tightened, hit, approaching).",
                "remarks": "Layer 2 sensor. Writes structured StopDecisionResult to local_state.",
            }),
            ("nm_pattern_health", 2, "sensor_pattern_health", "Pattern health sensor", False, 0.50, 30, {
                "role": "sensor",
                "description": "Publishes pattern monitor health evaluations and adjustment recommendations.",
                "remarks": "Layer 2 sensor. Writes health score, action, reasoning to local_state.",
            }),
            ("nm_imminent_eval", 2, "sensor_imminent", "Imminent breakout sensor", False, 0.55, 60, {
                "role": "sensor",
                "description": "Publishes imminent breakout evaluations (composite score, readiness, ETA).",
                "remarks": "Layer 2 sensor. Writes composite score and readiness to local_state.",
            }),
            ("nm_trade_context", 4, "aggregator_trade", "Trade context aggregator", False, 0.50, 45, {
                "role": "aggregator",
                "description": "Aggregates stop, pattern health, and imminent signals into unified trade context. "
                               "Self-graduating: GPT-5.4 teacher -> mechanical rules.",
                "remarks": "Layer 4 aggregator. Bridges sensors to action_signals/risk_gate.",
            }),
        ]
        for nid, layer, ntype, label, observer, threshold, cooldown, meta in sensor_nodes:
            conn.execute(text("""
                INSERT INTO brain_graph_nodes (id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer, display_meta,
                    created_at, updated_at)
                VALUES (:id, 'trading', 1, :ntype, :layer, :label,
                    :threshold, :cooldown, true, 1, :observer, :meta,
                    NOW(), NOW())
                ON CONFLICT (id) DO NOTHING
            """), {
                "id": nid, "layer": layer, "ntype": ntype, "label": label,
                "observer": observer, "threshold": threshold, "cooldown": cooldown,
                "meta": json.dumps(meta),
            })

    # ── 3. Edges: sensors → nm_action_signals (through risk gate) ──
    if "brain_graph_edges" in tables:
        sensor_edges = [
            ("nm_stop_eval", "nm_risk_gate", "stop_eval", 0.85, "excitatory", "dataflow"),
            ("nm_stop_eval", "nm_action_signals", "stop_hit", 0.90, "excitatory", "evidence"),
            ("nm_pattern_health", "nm_risk_gate", "pattern_health", 0.80, "excitatory", "dataflow"),
            ("nm_pattern_health", "nm_action_signals", "exit_now", 0.90, "excitatory", "evidence"),
            ("nm_imminent_eval", "nm_risk_gate", "imminent_eval", 0.70, "excitatory", "dataflow"),
            ("nm_imminent_eval", "nm_action_signals", "imminent_breakout", 0.75, "excitatory", "evidence"),
            # Sensors → nm_trade_context aggregator
            ("nm_stop_eval", "nm_trade_context", "stop_eval", 0.85, "excitatory", "dataflow"),
            ("nm_pattern_health", "nm_trade_context", "pattern_health", 0.80, "excitatory", "dataflow"),
            ("nm_imminent_eval", "nm_trade_context", "imminent_eval", 0.70, "excitatory", "dataflow"),
            # nm_trade_context → decision layer
            ("nm_trade_context", "nm_risk_gate", "trade_context", 0.85, "excitatory", "dataflow"),
            ("nm_trade_context", "nm_action_signals", "trade_context", 0.80, "excitatory", "evidence"),
        ]
        for src, tgt, sig, weight, pol, etype in sensor_edges:
            conn.execute(text("""
                INSERT INTO brain_graph_edges
                    (source_node_id, target_node_id, signal_type, weight, polarity, edge_type,
                     delay_ms, enabled, graph_version, min_confidence, min_source_confidence,
                     created_at, updated_at)
                SELECT :src, :tgt, :sig, :w, :pol, :etype,
                       0, true, 1, 0.0, 0.0, NOW(), NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges
                    WHERE source_node_id = :src AND target_node_id = :tgt AND signal_type = :sig
                )
            """), {"src": src, "tgt": tgt, "sig": sig, "w": weight, "pol": pol, "etype": etype})

    conn.commit()


def _migration_126_mesh_dependency_edges(conn) -> None:
    """Rewire learning-cycle mesh from sequential cluster_chain to real data-dependency edges.

    - Disables 12 cluster_chain edges (false linear pipeline)
    - Inserts 17 data-flow dependency edges based on actual function reads
    - Adds 2 spine-to-LC trigger edges (snapshot -> backfill, snapshot -> intraday_hv)
    - Adds schedule metadata on self-triggering nodes (decay, journal, outcome nodes, depromote)
    - Disables bt_insights node (no-op since legacy insight BT was removed)
    """
    import json
    tables = _tables(conn)
    if "brain_graph_edges" not in tables or "brain_graph_nodes" not in tables:
        return

    # ── 1. Disable cluster_chain edges (the false linear pipeline) ──
    conn.execute(text("""
        UPDATE brain_graph_edges
        SET enabled = false, updated_at = NOW()
        WHERE signal_type = 'cluster_chain'
          AND graph_version = 1
          AND enabled = true
    """))

    # ── 2. Disable bt_insights node (no-op since legacy insight BT removed) ──
    conn.execute(text("""
        UPDATE brain_graph_nodes
        SET enabled = false, updated_at = NOW()
        WHERE id = 'nm_lc_bt_insights' AND enabled = true
    """))
    conn.execute(text("""
        UPDATE brain_graph_edges
        SET enabled = false, updated_at = NOW()
        WHERE (source_node_id = 'nm_lc_bt_insights' OR target_node_id = 'nm_lc_bt_insights')
          AND graph_version = 1 AND enabled = true
    """))

    # ── 3. Insert real data-dependency edges ──
    dep_edges = [
        # Spine -> LC triggers
        ("nm_snap_daily", "nm_lc_backfill", "snapshot_refresh", 0.6, "excitatory", "dataflow"),
        ("nm_snap_intraday", "nm_lc_intraday_hv", "snapshot_refresh", 0.6, "excitatory", "dataflow"),
        # Tier 1 -> Tier 2: backfill labels snapshots that mine needs
        ("nm_lc_backfill", "nm_lc_mine", "node_completed", 0.7, "excitatory", "dataflow"),
        # Tier 2 -> Tier 3: mining produces patterns/insights
        ("nm_lc_mine", "nm_lc_seek", "node_completed", 0.6, "excitatory", "dataflow"),
        ("nm_lc_mine", "nm_lc_bt_queue", "node_completed", 0.7, "excitatory", "dataflow"),
        ("nm_lc_mine", "nm_lc_refine", "node_completed", 0.5, "excitatory", "dataflow"),
        ("nm_lc_mine", "nm_lc_hypotheses", "node_completed", 0.6, "excitatory", "dataflow"),
        # hypotheses can spawn patterns that need backtesting
        ("nm_lc_hypotheses", "nm_lc_bt_queue", "node_completed", 0.5, "excitatory", "dataflow"),
        # Tier 3 -> Tier 4: backtests produce results for evolution
        ("nm_lc_bt_queue", "nm_lc_variants", "node_completed", 0.7, "excitatory", "dataflow"),
        ("nm_lc_bt_queue", "nm_lc_ml", "node_completed", 0.6, "excitatory", "dataflow"),
        ("nm_lc_bt_queue", "nm_lc_depromote", "node_completed", 0.5, "excitatory", "dataflow"),
        # Tier 4 -> Tier 5: ML + pattern engine -> proposals
        ("nm_lc_ml", "nm_lc_pattern_engine", "node_completed", 0.6, "excitatory", "dataflow"),
        ("nm_lc_pattern_engine", "nm_lc_proposals", "node_completed", 0.7, "excitatory", "dataflow"),
        ("nm_lc_pattern_engine", "nm_lc_signals", "node_completed", 0.5, "excitatory", "dataflow"),
        # Terminal: proposals + depromote -> report -> finalize
        ("nm_lc_proposals", "nm_lc_cycle_report", "node_completed", 0.6, "excitatory", "dataflow"),
        ("nm_lc_depromote", "nm_lc_finalize", "node_completed", 0.5, "excitatory", "control"),
        ("nm_lc_cycle_report", "nm_lc_finalize", "node_completed", 0.5, "excitatory", "control"),
    ]

    for src, tgt, sig, weight, pol, etype in dep_edges:
        conn.execute(text("""
            INSERT INTO brain_graph_edges
                (source_node_id, target_node_id, signal_type, weight, polarity, edge_type,
                 delay_ms, enabled, graph_version, min_confidence, min_source_confidence,
                 created_at, updated_at)
            SELECT :src, :tgt, :sig, :w, :pol, :etype,
                   0, true, 1, 0.0, 0.0, NOW(), NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM brain_graph_edges
                WHERE source_node_id = :src AND target_node_id = :tgt
                  AND signal_type = :sig AND graph_version = 1
            )
        """), {"src": src, "tgt": tgt, "sig": sig, "w": weight, "pol": pol, "etype": etype})

    # ── 4. Schedule metadata on self-triggering nodes ──
    schedule_meta = {
        "nm_lc_decay": {"trigger": "cycle_start", "description": "Fires at start of each reconcile cycle"},
        "nm_lc_journal": {"trigger": "schedule", "cron": "16:05 ET Mon-Fri", "description": "Daily at market close"},
        "nm_lc_depromote": {"trigger": "schedule", "cron": "03:00 UTC daily", "description": "Daily depromotion check"},
        "nm_lc_breakout": {"trigger": "alert_resolved", "description": "Fires when BreakoutAlerts resolve"},
        "nm_lc_exit": {"trigger": "alert_resolved", "description": "Fires when BreakoutAlerts resolve"},
        "nm_lc_fakeout": {"trigger": "alert_resolved", "description": "Fires when BreakoutAlerts resolve"},
        "nm_lc_sizing": {"trigger": "alert_resolved", "description": "Fires when BreakoutAlerts resolve"},
        "nm_lc_inter_alert": {"trigger": "alert_resolved", "description": "Fires when BreakoutAlerts resolve"},
        "nm_lc_timeframe": {"trigger": "alert_resolved", "description": "Fires when BreakoutAlerts resolve"},
        "nm_lc_synergy": {"trigger": "alert_resolved", "description": "Fires when BreakoutAlerts resolve"},
        "nm_lc_monitor_review": {"trigger": "monitor_decision", "description": "Fires when PatternMonitorDecision rows appear"},
    }

    for node_id, sched in schedule_meta.items():
        meta_json = json.dumps({"schedule": sched})
        conn.execute(text("""
            UPDATE brain_graph_nodes
            SET display_meta = COALESCE(display_meta, '{}'::jsonb) || CAST(:meta AS jsonb),
                updated_at = NOW()
            WHERE id = :nid
        """), {"nid": node_id, "meta": meta_json})

    conn.commit()


def _migration_127_net_edge_ranker(conn) -> None:
    """Phase E: NetEdgeRanker shadow rollout.

    Tables:
      * trading_net_edge_scores - per-decision log of NetEdgeRanker score vs heuristic
      * trading_net_edge_calibration_snapshots - daily per-regime calibrator state

    Idempotent. Shadow-safe: tables can exist and be empty with zero runtime impact
    while brain_net_edge_ranker_mode = 'off'.
    """
    tables = _tables(conn)

    if "trading_net_edge_scores" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_net_edge_scores (
                id BIGSERIAL PRIMARY KEY,
                decision_id TEXT NOT NULL,
                scan_pattern_id INTEGER NULL,
                ticker VARCHAR(32) NOT NULL,
                asset_class VARCHAR(16) NULL,
                regime VARCHAR(32) NULL,
                ctx_hash VARCHAR(64) NULL,
                calibrated_prob DOUBLE PRECISION NULL,
                expected_payoff DOUBLE PRECISION NULL,
                spread_cost DOUBLE PRECISION NULL,
                slippage_cost DOUBLE PRECISION NULL,
                fees_cost DOUBLE PRECISION NULL,
                miss_prob_cost DOUBLE PRECISION NULL,
                partial_fill_cost DOUBLE PRECISION NULL,
                expected_net_pnl DOUBLE PRECISION NULL,
                heuristic_score DOUBLE PRECISION NULL,
                disagree_flag BOOLEAN NOT NULL DEFAULT FALSE,
                mode VARCHAR(16) NOT NULL,
                provenance_json JSONB NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_net_edge_scores_ticker_created "
            "ON trading_net_edge_scores (ticker, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_net_edge_scores_pattern_created "
            "ON trading_net_edge_scores (scan_pattern_id, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_net_edge_scores_regime_created "
            "ON trading_net_edge_scores (regime, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_net_edge_scores_mode_created "
            "ON trading_net_edge_scores (mode, created_at DESC)"
        ))
        conn.commit()

    if "trading_net_edge_calibration_snapshots" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_net_edge_calibration_snapshots (
                id BIGSERIAL PRIMARY KEY,
                version_id VARCHAR(64) NOT NULL,
                asset_class VARCHAR(16) NULL,
                regime VARCHAR(32) NULL,
                method VARCHAR(32) NOT NULL,
                sample_count INTEGER NOT NULL DEFAULT 0,
                reliability_json JSONB NULL,
                brier_score DOUBLE PRECISION NULL,
                log_loss DOUBLE PRECISION NULL,
                disagreement_rate DOUBLE PRECISION NULL,
                params_json JSONB NULL,
                fitted_at TIMESTAMP NOT NULL DEFAULT NOW(),
                is_active BOOLEAN NOT NULL DEFAULT FALSE
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_net_edge_cal_regime_fitted "
            "ON trading_net_edge_calibration_snapshots (regime, fitted_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_net_edge_cal_active "
            "ON trading_net_edge_calibration_snapshots (is_active, fitted_at DESC)"
        ))
        conn.commit()


def _migration_128_exit_evaluator_parity(conn) -> None:
    """Phase B: Exit-engine unification shadow rollout.

    Table:
      * trading_exit_parity_log - per-bar, per-position disagreement record
        between the legacy exit paths (backtest DynamicPatternStrategy +
        live_exit_engine.compute_live_exit_levels) and the new canonical
        ExitEvaluator. Shadow-only until a later cutover phase.

    Idempotent. Shadow-safe: table can exist and be empty with zero runtime
    impact while brain_exit_engine_mode = 'off'.
    """
    tables = _tables(conn)

    if "trading_exit_parity_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_exit_parity_log (
                id BIGSERIAL PRIMARY KEY,
                source VARCHAR(16) NOT NULL,
                position_id BIGINT NULL,
                scan_pattern_id INTEGER NULL,
                ticker VARCHAR(32) NOT NULL,
                bar_ts TIMESTAMP NULL,
                legacy_action VARCHAR(32) NOT NULL,
                legacy_exit_price DOUBLE PRECISION NULL,
                canonical_action VARCHAR(32) NOT NULL,
                canonical_exit_price DOUBLE PRECISION NULL,
                pnl_diff_pct DOUBLE PRECISION NULL,
                agree_bool BOOLEAN NOT NULL DEFAULT FALSE,
                mode VARCHAR(16) NOT NULL,
                config_hash VARCHAR(64) NULL,
                provenance_json JSONB NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_exit_parity_source_created "
            "ON trading_exit_parity_log (source, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_exit_parity_ticker_created "
            "ON trading_exit_parity_log (ticker, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_exit_parity_mode_created "
            "ON trading_exit_parity_log (mode, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_exit_parity_agree_created "
            "ON trading_exit_parity_log (agree_bool, created_at DESC)"
        ))
        conn.commit()


def _migration_129_economic_ledger(conn) -> None:
    """Phase A: Economic-truth ledger shadow rollout.

    Tables:
      * trading_economic_ledger - append-only economic events
        (entry_fill / exit_fill / partial_fill / fee / adjustment) with
        explicit cash_delta and realized_pnl_delta. Parallel to Trade /
        PaperTrade rows; legacy pnl columns remain authoritative until a
        later cutover phase.
      * trading_ledger_parity_log - per-closed-trade reconciliation record
        between ledger-derived PnL and legacy Trade/PaperTrade PnL.

    Idempotent. Shadow-safe: tables can exist and be empty with zero runtime
    impact while brain_economic_ledger_mode = 'off'.
    """
    tables = _tables(conn)

    if "trading_economic_ledger" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_economic_ledger (
                id BIGSERIAL PRIMARY KEY,
                source VARCHAR(16) NOT NULL,
                trade_id BIGINT NULL,
                paper_trade_id BIGINT NULL,
                user_id INTEGER NULL,
                scan_pattern_id INTEGER NULL,
                ticker VARCHAR(32) NOT NULL,
                event_type VARCHAR(32) NOT NULL,
                direction VARCHAR(8) NULL,
                quantity DOUBLE PRECISION NULL,
                price DOUBLE PRECISION NULL,
                fee DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                cash_delta DOUBLE PRECISION NOT NULL,
                realized_pnl_delta DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                position_qty_after DOUBLE PRECISION NULL,
                position_cost_basis_after DOUBLE PRECISION NULL,
                venue VARCHAR(32) NULL,
                broker_source VARCHAR(32) NULL,
                event_ts TIMESTAMP NULL,
                mode VARCHAR(16) NOT NULL,
                provenance_json JSONB NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_economic_ledger_source_created "
            "ON trading_economic_ledger (source, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_economic_ledger_trade_created "
            "ON trading_economic_ledger (trade_id, created_at) "
            "WHERE trade_id IS NOT NULL"
        ))
        conn.execute(text(
            "CREATE INDEX ix_economic_ledger_paper_trade_created "
            "ON trading_economic_ledger (paper_trade_id, created_at) "
            "WHERE paper_trade_id IS NOT NULL"
        ))
        conn.execute(text(
            "CREATE INDEX ix_economic_ledger_ticker_created "
            "ON trading_economic_ledger (ticker, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_economic_ledger_event_type_created "
            "ON trading_economic_ledger (event_type, created_at DESC)"
        ))
        # Idempotency: at most one entry_fill / exit_fill per trade ref.
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_economic_ledger_paper_entry "
            "ON trading_economic_ledger (paper_trade_id, event_type) "
            "WHERE paper_trade_id IS NOT NULL AND event_type IN ('entry_fill','exit_fill')"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_economic_ledger_trade_entry "
            "ON trading_economic_ledger (trade_id, event_type) "
            "WHERE trade_id IS NOT NULL AND event_type IN ('entry_fill','exit_fill')"
        ))
        conn.commit()

    if "trading_ledger_parity_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_ledger_parity_log (
                id BIGSERIAL PRIMARY KEY,
                source VARCHAR(16) NOT NULL,
                trade_id BIGINT NULL,
                paper_trade_id BIGINT NULL,
                user_id INTEGER NULL,
                scan_pattern_id INTEGER NULL,
                ticker VARCHAR(32) NOT NULL,
                legacy_pnl DOUBLE PRECISION NULL,
                ledger_pnl DOUBLE PRECISION NULL,
                delta_pnl DOUBLE PRECISION NULL,
                delta_abs DOUBLE PRECISION NULL,
                agree_bool BOOLEAN NOT NULL DEFAULT FALSE,
                tolerance_usd DOUBLE PRECISION NULL,
                mode VARCHAR(16) NOT NULL,
                provenance_json JSONB NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_ledger_parity_source_created "
            "ON trading_ledger_parity_log (source, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_ledger_parity_agree_created "
            "ON trading_ledger_parity_log (agree_bool, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_ledger_parity_ticker_created "
            "ON trading_ledger_parity_log (ticker, created_at DESC)"
        ))
        conn.commit()


def _migration_130_pit_hygiene(conn) -> None:
    """Phase C: PIT hygiene + historical universe snapshot.

    Tables:
      * trading_pit_audit_log - per-audit record of a ScanPattern's condition
        fields classified as PIT / non_pit / unknown. History is preserved;
        multiple passes per pattern are allowed.
      * trading_universe_snapshots - per-day, per-ticker active/halted/delisted
        snapshot with UNIQUE (as_of_date, ticker) for idempotent upsert.

    Idempotent. Shadow-safe: both tables can exist empty with zero runtime
    impact while brain_pit_audit_mode = 'off'.
    """
    tables = _tables(conn)

    if "trading_pit_audit_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pit_audit_log (
                id BIGSERIAL PRIMARY KEY,
                pattern_id INTEGER NOT NULL,
                name VARCHAR(200) NULL,
                origin VARCHAR(32) NULL,
                lifecycle_stage VARCHAR(32) NULL,
                pit_count INTEGER NOT NULL,
                non_pit_count INTEGER NOT NULL,
                unknown_count INTEGER NOT NULL,
                pit_fields JSONB NOT NULL DEFAULT '[]',
                non_pit_fields JSONB NOT NULL DEFAULT '[]',
                unknown_fields JSONB NOT NULL DEFAULT '[]',
                agree_bool BOOLEAN NOT NULL,
                mode VARCHAR(16) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_pit_audit_pattern_created "
            "ON trading_pit_audit_log (pattern_id, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pit_audit_agree_created "
            "ON trading_pit_audit_log (agree_bool, created_at DESC)"
        ))
        conn.commit()

    if "trading_universe_snapshots" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_universe_snapshots (
                id BIGSERIAL PRIMARY KEY,
                as_of_date DATE NOT NULL,
                ticker VARCHAR(32) NOT NULL,
                asset_class VARCHAR(16) NOT NULL,
                status VARCHAR(16) NOT NULL,
                primary_exchange VARCHAR(32) NULL,
                source VARCHAR(32) NULL,
                provenance_json JSONB NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_universe_snapshot_date_ticker "
            "ON trading_universe_snapshots (as_of_date, ticker)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_universe_snapshot_date "
            "ON trading_universe_snapshots (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_universe_snapshot_ticker_date "
            "ON trading_universe_snapshots (ticker, as_of_date DESC)"
        ))
        conn.commit()


def _migration_131_triple_barrier(conn) -> None:
    """Phase D: Triple-barrier label store + economic promotion metric (shadow rollout).

    Table:
      * trading_triple_barrier_labels - one row per (ticker, label_date, side,
        tp_pct, sl_pct, max_bars) tuple, labelling the outcome of a trade
        entered at that bar's close against configured barriers. Idempotent
        on the configured UNIQUE key so re-running the labeler is safe.

    Shadow-safe: the table can be empty with zero runtime impact until
    brain_triple_barrier_mode != 'off'. Promotion behaviour is unchanged
    until brain_promotion_metric_mode == 'economic'.
    """
    tables = _tables(conn)

    if "trading_triple_barrier_labels" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_triple_barrier_labels (
                id BIGSERIAL PRIMARY KEY,
                snapshot_id INTEGER NULL,
                ticker VARCHAR(32) NOT NULL,
                label_date DATE NOT NULL,
                side VARCHAR(8) NOT NULL,
                tp_pct DOUBLE PRECISION NOT NULL,
                sl_pct DOUBLE PRECISION NOT NULL,
                max_bars INTEGER NOT NULL,
                entry_close DOUBLE PRECISION NOT NULL,
                tp_price DOUBLE PRECISION NOT NULL,
                sl_price DOUBLE PRECISION NOT NULL,
                label SMALLINT NOT NULL,
                barrier_hit VARCHAR(16) NOT NULL,
                exit_bar_idx INTEGER NOT NULL,
                realized_return_pct DOUBLE PRECISION NOT NULL,
                mode VARCHAR(16) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_triple_barrier_labels "
            "ON trading_triple_barrier_labels "
            "(ticker, label_date, side, tp_pct, sl_pct, max_bars)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_triple_barrier_label_date "
            "ON trading_triple_barrier_labels (label_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_triple_barrier_ticker_date "
            "ON trading_triple_barrier_labels (ticker, label_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_triple_barrier_snapshot "
            "ON trading_triple_barrier_labels (snapshot_id)"
        ))
        conn.commit()


def _migration_132_execution_cost_model(conn) -> None:
    """Phase F: Execution-cost model + venue-truth telemetry (shadow rollout).

    Tables:
      * trading_execution_cost_estimates - per-(ticker, side, window_days)
        rolling cost profile: median/p90 spread in bps, median/p90 slippage
        in bps, avg daily volume in USD, sample counts, last refresh
        timestamp. Idempotent on UNIQUE (ticker, side, window_days) so the
        estimator can be re-run safely.

      * trading_venue_truth_log - one row per fill observation comparing
        expected vs realized execution costs. Powers the /brain/venue-truth/
        diagnostics endpoint and the release-blocker script.

    Shadow-safe: both tables stay empty until brain_execution_cost_mode /
    brain_venue_truth_mode flip from 'off'. No existing code path reads
    these tables in this phase.
    """
    tables = _tables(conn)

    if "trading_execution_cost_estimates" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_execution_cost_estimates (
                id BIGSERIAL PRIMARY KEY,
                ticker VARCHAR(32) NOT NULL,
                side VARCHAR(8) NOT NULL,
                window_days INTEGER NOT NULL,
                median_spread_bps DOUBLE PRECISION NOT NULL,
                p90_spread_bps DOUBLE PRECISION NOT NULL,
                median_slippage_bps DOUBLE PRECISION NOT NULL,
                p90_slippage_bps DOUBLE PRECISION NOT NULL,
                avg_daily_volume_usd DOUBLE PRECISION NOT NULL,
                sample_trades INTEGER NOT NULL,
                last_updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_execution_cost_estimates "
            "ON trading_execution_cost_estimates "
            "(ticker, side, window_days)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_execution_cost_estimates_updated "
            "ON trading_execution_cost_estimates (last_updated_at DESC)"
        ))
        conn.commit()

    if "trading_venue_truth_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_venue_truth_log (
                id BIGSERIAL PRIMARY KEY,
                trade_id INTEGER NULL,
                ticker VARCHAR(32) NOT NULL,
                side VARCHAR(8) NOT NULL,
                notional_usd DOUBLE PRECISION NOT NULL,
                expected_spread_bps DOUBLE PRECISION NULL,
                realized_spread_bps DOUBLE PRECISION NULL,
                expected_slippage_bps DOUBLE PRECISION NULL,
                realized_slippage_bps DOUBLE PRECISION NULL,
                expected_cost_fraction DOUBLE PRECISION NULL,
                realized_cost_fraction DOUBLE PRECISION NULL,
                paper_bool BOOLEAN NOT NULL DEFAULT TRUE,
                mode VARCHAR(16) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_venue_truth_log_created "
            "ON trading_venue_truth_log (created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_venue_truth_log_ticker_created "
            "ON trading_venue_truth_log (ticker, created_at DESC)"
        ))
        conn.commit()


def _migration_133_live_brackets_reconciliation(conn) -> None:
    """Phase G: Live brackets + stop reconciliation (shadow rollout).

    Tables:
      * trading_bracket_intents - one row per live Trade recording the
        stop/target bracket we would have placed at the broker. Keyed
        uniquely on trade_id so repeated shadow emits are idempotent.
        Broker order ids stay NULL in shadow mode.

      * trading_bracket_reconciliation_log - append-only sweep log
        comparing local trade state + bracket intent vs broker-reported
        open orders and positions. Every sweep writes at minimum one
        row per scanned trade (kind='agree' is valid).

    Shadow-safe: both tables stay empty until
    brain_live_brackets_mode flips from 'off'. No existing code path
    reads these tables in this phase; the Phase G reconciliation
    service + diagnostics endpoints are the first consumers.
    """
    tables = _tables(conn)

    if "trading_bracket_intents" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_bracket_intents (
                id BIGSERIAL PRIMARY KEY,
                trade_id INTEGER NOT NULL REFERENCES trading_trades(id) ON DELETE CASCADE,
                user_id INTEGER NULL,
                ticker VARCHAR(32) NOT NULL,
                direction VARCHAR(8) NOT NULL,
                quantity DOUBLE PRECISION NOT NULL,
                entry_price DOUBLE PRECISION NOT NULL,
                stop_price DOUBLE PRECISION NULL,
                target_price DOUBLE PRECISION NULL,
                stop_model VARCHAR(32) NULL,
                pattern_id INTEGER NULL,
                regime VARCHAR(32) NULL,
                intent_state VARCHAR(32) NOT NULL DEFAULT 'intent',
                shadow_mode BOOLEAN NOT NULL DEFAULT TRUE,
                broker_source VARCHAR(32) NULL,
                broker_stop_order_id VARCHAR(128) NULL,
                broker_target_order_id VARCHAR(128) NULL,
                last_observed_at TIMESTAMP NULL,
                last_diff_reason VARCHAR(128) NULL,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_bracket_intents_trade_id "
            "ON trading_bracket_intents (trade_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_bracket_intents_ticker_state "
            "ON trading_bracket_intents (ticker, intent_state)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_bracket_intents_updated_at "
            "ON trading_bracket_intents (updated_at DESC)"
        ))
        conn.commit()

    if "trading_bracket_reconciliation_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_bracket_reconciliation_log (
                id BIGSERIAL PRIMARY KEY,
                sweep_id VARCHAR(64) NOT NULL,
                trade_id INTEGER NULL REFERENCES trading_trades(id) ON DELETE SET NULL,
                bracket_intent_id BIGINT NULL REFERENCES trading_bracket_intents(id) ON DELETE SET NULL,
                ticker VARCHAR(32) NULL,
                broker_source VARCHAR(32) NULL,
                kind VARCHAR(32) NOT NULL,
                severity VARCHAR(16) NOT NULL,
                local_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                broker_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                delta_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_bracket_reconciliation_sweep "
            "ON trading_bracket_reconciliation_log (sweep_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_bracket_reconciliation_trade "
            "ON trading_bracket_reconciliation_log (trade_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_bracket_reconciliation_kind_ts "
            "ON trading_bracket_reconciliation_log (kind, observed_at DESC)"
        ))
        conn.commit()


def _migration_134_position_sizer_log(conn) -> None:
    """Phase H: Canonical PositionSizer + portfolio optimizer (shadow rollout).

    Table:
      * trading_position_sizer_log - append-only shadow log. For every
        actionable pick (alerts, paper/live runner, manual, backtest)
        the canonical sizer emits exactly one proposal row containing
        the NetEdgeRanker score it consumed, the proposed notional /
        quantity / risk, which caps triggered, and the legacy sizer's
        notional for divergence tracking.

    Shadow-safe: Phase H never changes the value returned by legacy
    sizers. This table is write-only from the canonical sizer and
    read-only from the diagnostics endpoint + release-blocker script.
    Authoritative cutover is Phase H.2.
    """
    tables = _tables(conn)

    if "trading_position_sizer_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_position_sizer_log (
                id BIGSERIAL PRIMARY KEY,
                proposal_id VARCHAR(64) NOT NULL,
                source VARCHAR(32) NOT NULL,
                ticker VARCHAR(32) NOT NULL,
                direction VARCHAR(8) NOT NULL,
                user_id INTEGER NULL,
                pattern_id INTEGER NULL,
                asset_class VARCHAR(16) NULL,
                regime VARCHAR(32) NULL,
                entry_price DOUBLE PRECISION NOT NULL,
                stop_price DOUBLE PRECISION NULL,
                target_price DOUBLE PRECISION NULL,
                capital DOUBLE PRECISION NULL,
                calibrated_prob DOUBLE PRECISION NULL,
                payoff_fraction DOUBLE PRECISION NULL,
                cost_fraction DOUBLE PRECISION NULL,
                expected_net_pnl DOUBLE PRECISION NULL,
                kelly_fraction DOUBLE PRECISION NULL,
                kelly_scaled_fraction DOUBLE PRECISION NULL,
                proposed_notional DOUBLE PRECISION NULL,
                proposed_quantity DOUBLE PRECISION NULL,
                proposed_risk_pct DOUBLE PRECISION NULL,
                correlation_cap_triggered BOOLEAN NOT NULL DEFAULT FALSE,
                correlation_bucket VARCHAR(64) NULL,
                max_bucket_notional DOUBLE PRECISION NULL,
                notional_cap_triggered BOOLEAN NOT NULL DEFAULT FALSE,
                legacy_notional DOUBLE PRECISION NULL,
                legacy_quantity DOUBLE PRECISION NULL,
                legacy_source VARCHAR(48) NULL,
                divergence_bps DOUBLE PRECISION NULL,
                mode VARCHAR(16) NOT NULL,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_position_sizer_log_proposal "
            "ON trading_position_sizer_log (proposal_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_position_sizer_log_source_ts "
            "ON trading_position_sizer_log (source, observed_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_position_sizer_log_ticker_ts "
            "ON trading_position_sizer_log (ticker, observed_at DESC)"
        ))
        conn.commit()


def _migration_135_risk_dial_capital_reweight(conn) -> None:
    """Phase I: Risk dial + weekly capital re-weighting (shadow rollout).

    Tables:
      * trading_risk_dial_state - append-only log of risk-dial values.
        A row represents a resolved dial for (user_id, regime) with
        the source attribution ('config' | 'regime_default' |
        'manual' | 'drift_override'). The current dial is the
        latest row per (user_id) order by observed_at DESC.
      * trading_capital_reweight_log - append-only weekly sweep log.
        One row per (user_id, as_of_date) captures the proposed
        bucket weights vs. the current book weights; read by the
        diagnostics endpoint.

    Column:
      * trading_position_sizer_log.risk_dial_multiplier - nullable
        record of which dial value was in effect when the Phase H
        proposal was generated. Never read by the sizer in Phase I;
        Phase I.2 will consume it authoritatively.

    Shadow-safe: no existing behaviour changes, no Phase H math
    changes, no live trade is resized by these tables. Authoritative
    cutover (dial applied inside compute_proposal + rebalance
    orders from the weekly sweep) is Phase I.2.
    """
    tables = _tables(conn)

    if "trading_risk_dial_state" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_risk_dial_state (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NULL,
                dial_value DOUBLE PRECISION NOT NULL,
                regime VARCHAR(32) NULL,
                source VARCHAR(32) NOT NULL,
                reason VARCHAR(256) NULL,
                mode VARCHAR(16) NOT NULL,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_risk_dial_user_ts "
            "ON trading_risk_dial_state (user_id, observed_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_risk_dial_regime_ts "
            "ON trading_risk_dial_state (regime, observed_at DESC)"
        ))
        conn.commit()

    if "trading_capital_reweight_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_capital_reweight_log (
                id BIGSERIAL PRIMARY KEY,
                reweight_id VARCHAR(64) NOT NULL,
                user_id INTEGER NULL,
                as_of_date DATE NOT NULL,
                regime VARCHAR(32) NULL,
                total_capital DOUBLE PRECISION NOT NULL,
                proposed_allocations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                current_allocations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                drift_bucket_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mean_drift_bps DOUBLE PRECISION NULL,
                p90_drift_bps DOUBLE PRECISION NULL,
                cap_triggers_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_capital_reweight_user_date "
            "ON trading_capital_reweight_log (user_id, as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_capital_reweight_id "
            "ON trading_capital_reweight_log (reweight_id)"
        ))
        conn.commit()

    cols = _columns(conn, "trading_position_sizer_log")
    if "risk_dial_multiplier" not in cols:
        conn.execute(text(
            "ALTER TABLE trading_position_sizer_log "
            "ADD COLUMN risk_dial_multiplier DOUBLE PRECISION NULL"
        ))
        conn.commit()


def _migration_136_drift_monitor_recert(conn) -> None:
    """Phase J: Drift monitor + re-certification queue (shadow rollout).

    Tables:
      * trading_pattern_drift_log - append-only drift score log. One
        row per (scan_pattern_id, sweep_at). Records Brier-delta and
        CUSUM statistics against the pattern's backtest baseline,
        plus a bucketed severity ('green' | 'yellow' | 'red').
      * trading_pattern_recert_log - append-only re-cert proposal log.
        One row per (scan_pattern_id, as_of_date) when the drift
        monitor crosses red severity or a user manually queues a
        re-cert. Status starts as 'proposed'; Phase J.2 will consume
        these rows and trigger the backtest + promotion gate.

    Shadow-safe: no existing lifecycle transitions, no backtest
    triggers, no scanner/alerts/playbook consumer changes. Both
    tables are write-only in J.1.
    """
    tables = _tables(conn)

    if "trading_pattern_drift_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pattern_drift_log (
                id BIGSERIAL PRIMARY KEY,
                drift_id VARCHAR(64) NOT NULL,
                scan_pattern_id INTEGER NOT NULL,
                pattern_name VARCHAR(256) NULL,
                baseline_win_prob DOUBLE PRECISION NULL,
                observed_win_prob DOUBLE PRECISION NULL,
                brier_delta DOUBLE PRECISION NULL,
                cusum_statistic DOUBLE PRECISION NULL,
                cusum_threshold DOUBLE PRECISION NULL,
                sample_size INTEGER NOT NULL DEFAULT 0,
                severity VARCHAR(16) NOT NULL,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                sweep_at TIMESTAMP NOT NULL DEFAULT NOW(),
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_pattern_drift_pattern_ts "
            "ON trading_pattern_drift_log (scan_pattern_id, sweep_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pattern_drift_severity_ts "
            "ON trading_pattern_drift_log (severity, sweep_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pattern_drift_id "
            "ON trading_pattern_drift_log (drift_id)"
        ))
        conn.commit()

    if "trading_pattern_recert_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pattern_recert_log (
                id BIGSERIAL PRIMARY KEY,
                recert_id VARCHAR(64) NOT NULL,
                scan_pattern_id INTEGER NOT NULL,
                pattern_name VARCHAR(256) NULL,
                as_of_date DATE NOT NULL,
                source VARCHAR(32) NOT NULL,
                severity VARCHAR(16) NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'proposed',
                reason VARCHAR(256) NULL,
                drift_log_id BIGINT NULL,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_pattern_recert_pattern_ts "
            "ON trading_pattern_recert_log (scan_pattern_id, observed_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pattern_recert_status_ts "
            "ON trading_pattern_recert_log (status, observed_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pattern_recert_id "
            "ON trading_pattern_recert_log (recert_id)"
        ))
        conn.commit()


def _migration_137_divergence_panel(conn) -> None:
    """Phase K: canonical divergence panel log (shadow rollout).

    Creates ``trading_pattern_divergence_log``, an append-only aggregation
    of per-pattern divergence signals sourced from existing Phase A/B/F/G/H
    divergence-bearing tables:

    * ``trading_ledger_parity_log`` (Phase A)
    * ``trading_exit_parity_log`` (Phase B)
    * ``trading_venue_truth_log`` (Phase F)
    * ``trading_bracket_reconciliation_log`` (Phase G)
    * ``trading_position_sizer_log`` (Phase H)

    One row per pattern per daily sweep. Per-layer severities + a hysteresis
    overall severity are stored so operators can triage cross-layer
    drift quickly. Shadow-only: K.1 never mutates lifecycle state or
    writes to ``scan_patterns``.
    """
    tables = _tables(conn)

    if "trading_pattern_divergence_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pattern_divergence_log (
                id BIGSERIAL PRIMARY KEY,
                divergence_id VARCHAR(64) NOT NULL,
                scan_pattern_id INTEGER NOT NULL,
                pattern_name VARCHAR(256) NULL,
                as_of_date DATE NOT NULL,
                ledger_severity VARCHAR(16) NULL,
                exit_severity VARCHAR(16) NULL,
                venue_severity VARCHAR(16) NULL,
                bracket_severity VARCHAR(16) NULL,
                sizer_severity VARCHAR(16) NULL,
                severity VARCHAR(16) NOT NULL,
                score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                layers_sampled INTEGER NOT NULL DEFAULT 0,
                layers_agreed INTEGER NOT NULL DEFAULT 0,
                layers_total INTEGER NOT NULL DEFAULT 5,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                sweep_at TIMESTAMP NOT NULL DEFAULT NOW(),
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_pattern_divergence_pattern_ts "
            "ON trading_pattern_divergence_log (scan_pattern_id, sweep_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pattern_divergence_severity_ts "
            "ON trading_pattern_divergence_log (severity, sweep_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pattern_divergence_id "
            "ON trading_pattern_divergence_log (divergence_id)"
        ))
        conn.commit()


def _migration_138_macro_regime_snapshot(conn) -> None:
    """Phase L.17: macro regime expansion snapshot (shadow rollout).

    Creates ``trading_macro_regime_snapshots``, an append-only daily
    record of the extended macro regime surface: existing SPY/VIX
    composite plus rates (IEF/SHY/TLT), credit (HYG/LQD), and USD (UUP)
    features. Shadow-only: L.17.1 does not mutate any existing regime
    consumer. The ``get_market_regime()`` return shape in
    ``market_data.py`` is bit-for-bit unchanged.
    """
    tables = _tables(conn)

    if "trading_macro_regime_snapshots" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_macro_regime_snapshots (
                id BIGSERIAL PRIMARY KEY,
                regime_id VARCHAR(64) NOT NULL,
                as_of_date DATE NOT NULL,
                -- equity block (mirrors get_market_regime())
                spy_direction VARCHAR(16) NULL,
                spy_momentum_5d DOUBLE PRECISION NULL,
                vix DOUBLE PRECISION NULL,
                vix_regime VARCHAR(16) NULL,
                volatility_percentile DOUBLE PRECISION NULL,
                composite VARCHAR(16) NULL,
                regime_numeric INTEGER NULL,
                -- rates block
                ief_trend VARCHAR(16) NULL,
                shy_trend VARCHAR(16) NULL,
                tlt_trend VARCHAR(16) NULL,
                yield_curve_slope_proxy DOUBLE PRECISION NULL,
                rates_regime VARCHAR(16) NULL,
                -- credit block
                hyg_trend VARCHAR(16) NULL,
                lqd_trend VARCHAR(16) NULL,
                credit_spread_proxy DOUBLE PRECISION NULL,
                credit_regime VARCHAR(16) NULL,
                -- usd block
                uup_trend VARCHAR(16) NULL,
                uup_momentum_20d DOUBLE PRECISION NULL,
                usd_regime VARCHAR(16) NULL,
                -- composite-macro block
                macro_numeric INTEGER NOT NULL DEFAULT 0,
                macro_label VARCHAR(32) NOT NULL,
                -- coverage block
                symbols_sampled INTEGER NOT NULL DEFAULT 0,
                symbols_missing INTEGER NOT NULL DEFAULT 0,
                coverage_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                -- raw per-symbol readings + config echoes
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                computed_at TIMESTAMP NOT NULL DEFAULT NOW(),
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_macro_regime_as_of "
            "ON trading_macro_regime_snapshots (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_macro_regime_id "
            "ON trading_macro_regime_snapshots (regime_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_macro_regime_label_computed "
            "ON trading_macro_regime_snapshots (macro_label, computed_at DESC)"
        ))
        conn.commit()


def _migration_139_breadth_relstr_snapshot(conn) -> None:
    """Phase L.18: breadth + cross-sectional relative-strength snapshot
    (shadow rollout).

    Creates ``trading_breadth_relstr_snapshots``, an append-only daily
    record of:
      * breadth block: how many members of a fixed reference basket
        (11 US sector SPDRs plus SPY/QQQ/IWM benchmarks) are
        advancing vs declining (ETF-basket proxy for A/D);
      * per-sector trend + relative strength vs SPY (stored as JSONB
        to avoid 33+ flat columns);
      * benchmark trends + momenta (SPY/QQQ/IWM);
      * composite breadth label (broad_risk_on / mixed / broad_risk_off)
        and leader / laggard sector by 20d RS.

    Shadow-only: L.18.1 does not mutate any existing consumer.
    ``market_data.get_market_regime()`` and Phase L.17's
    ``trading_macro_regime_snapshots`` are bit-for-bit unchanged.
    Authoritative consumption is deferred to L.18.2.
    """
    tables = _tables(conn)

    if "trading_breadth_relstr_snapshots" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_breadth_relstr_snapshots (
                id BIGSERIAL PRIMARY KEY,
                snapshot_id VARCHAR(64) NOT NULL,
                as_of_date DATE NOT NULL,
                -- breadth block (ETF-basket A/D proxy)
                members_sampled INTEGER NOT NULL DEFAULT 0,
                members_advancing INTEGER NOT NULL DEFAULT 0,
                members_declining INTEGER NOT NULL DEFAULT 0,
                members_flat INTEGER NOT NULL DEFAULT 0,
                advance_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                new_highs_count INTEGER NOT NULL DEFAULT 0,
                new_lows_count INTEGER NOT NULL DEFAULT 0,
                -- sector block (JSONB: {sector: {trend, momentum_20d, rs_vs_spy_20d}})
                sector_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                -- benchmark block
                spy_trend VARCHAR(16) NULL,
                spy_momentum_20d DOUBLE PRECISION NULL,
                qqq_trend VARCHAR(16) NULL,
                qqq_momentum_20d DOUBLE PRECISION NULL,
                iwm_trend VARCHAR(16) NULL,
                iwm_momentum_20d DOUBLE PRECISION NULL,
                -- tilt block
                size_tilt DOUBLE PRECISION NULL,
                style_tilt DOUBLE PRECISION NULL,
                -- composite block
                breadth_numeric INTEGER NOT NULL DEFAULT 0,
                breadth_label VARCHAR(32) NOT NULL,
                leader_sector VARCHAR(32) NULL,
                laggard_sector VARCHAR(32) NULL,
                -- coverage block
                symbols_sampled INTEGER NOT NULL DEFAULT 0,
                symbols_missing INTEGER NOT NULL DEFAULT 0,
                coverage_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                -- raw per-symbol readings + config echo
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                computed_at TIMESTAMP NOT NULL DEFAULT NOW(),
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_breadth_relstr_as_of "
            "ON trading_breadth_relstr_snapshots (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_breadth_relstr_id "
            "ON trading_breadth_relstr_snapshots (snapshot_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_breadth_relstr_label_computed "
            "ON trading_breadth_relstr_snapshots (breadth_label, computed_at DESC)"
        ))
        conn.commit()


def _migration_140_cross_asset_snapshot(conn) -> None:
    """Phase L.19: cross-asset signals snapshot (shadow rollout).

    Append-only daily snapshot of cross-asset lead/lag features:
    bond vs equity, credit vs equity, USD vs crypto, VIX shock vs
    breadth, BTC-SPY rolling beta. No downstream consumer reads this
    in L.19.1; authoritative consumption deferred to L.19.2.
    """
    tables = _tables(conn)

    if "trading_cross_asset_snapshots" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_cross_asset_snapshots (
                id BIGSERIAL PRIMARY KEY,
                snapshot_id VARCHAR(64) NOT NULL,
                as_of_date DATE NOT NULL,
                -- bond vs equity lead (TLT vs SPY)
                bond_equity_lead_5d DOUBLE PRECISION NULL,
                bond_equity_lead_20d DOUBLE PRECISION NULL,
                bond_equity_label VARCHAR(32) NULL,
                -- credit vs equity lead (Δ(HYG-LQD) vs SPY)
                credit_equity_lead_5d DOUBLE PRECISION NULL,
                credit_equity_lead_20d DOUBLE PRECISION NULL,
                credit_equity_label VARCHAR(32) NULL,
                -- USD vs crypto lead (UUP vs BTC)
                usd_crypto_lead_5d DOUBLE PRECISION NULL,
                usd_crypto_lead_20d DOUBLE PRECISION NULL,
                usd_crypto_label VARCHAR(32) NULL,
                -- VIX shock vs breadth divergence
                vix_level DOUBLE PRECISION NULL,
                vix_percentile DOUBLE PRECISION NULL,
                breadth_advance_ratio DOUBLE PRECISION NULL,
                vix_breadth_divergence_score DOUBLE PRECISION NULL,
                vix_breadth_label VARCHAR(32) NULL,
                -- BTC-SPY rolling beta (window configurable)
                crypto_equity_beta DOUBLE PRECISION NULL,
                crypto_equity_beta_window_days INTEGER NULL,
                crypto_equity_correlation DOUBLE PRECISION NULL,
                -- composite block
                cross_asset_numeric INTEGER NOT NULL DEFAULT 0,
                cross_asset_label VARCHAR(32) NOT NULL,
                -- coverage block
                symbols_sampled INTEGER NOT NULL DEFAULT 0,
                symbols_missing INTEGER NOT NULL DEFAULT 0,
                coverage_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                -- raw per-symbol readings + macro/breadth context echo + config
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                computed_at TIMESTAMP NOT NULL DEFAULT NOW(),
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_cross_asset_as_of "
            "ON trading_cross_asset_snapshots (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_cross_asset_id "
            "ON trading_cross_asset_snapshots (snapshot_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_cross_asset_label_computed "
            "ON trading_cross_asset_snapshots (cross_asset_label, computed_at DESC)"
        ))
        conn.commit()


def _migration_141_ticker_regime_snapshot(conn) -> None:
    """Phase L.20: per-ticker mean-reversion vs trend regime (shadow rollout).

    Append-only daily per-ticker snapshot of pure time-series regime
    features (AC(1), variance-ratio, Hurst R/S, ADX proxy) and a
    composite label in {trend_up, trend_down, mean_revert, choppy,
    neutral}. No downstream consumer reads this in L.20.1;
    authoritative consumption deferred to L.20.2.
    """
    tables = _tables(conn)

    if "trading_ticker_regime_snapshots" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_ticker_regime_snapshots (
                id BIGSERIAL PRIMARY KEY,
                snapshot_id VARCHAR(64) NOT NULL,
                as_of_date DATE NOT NULL,
                ticker VARCHAR(32) NOT NULL,
                asset_class VARCHAR(16) NULL,
                -- raw features
                last_close DOUBLE PRECISION NULL,
                sigma_20d DOUBLE PRECISION NULL,
                ac1 DOUBLE PRECISION NULL,
                vr_5 DOUBLE PRECISION NULL,
                vr_20 DOUBLE PRECISION NULL,
                hurst DOUBLE PRECISION NULL,
                adx_proxy DOUBLE PRECISION NULL,
                -- composite scores + label
                trend_score DOUBLE PRECISION NULL,
                mean_revert_score DOUBLE PRECISION NULL,
                ticker_regime_numeric INTEGER NOT NULL DEFAULT 0,
                ticker_regime_label VARCHAR(32) NOT NULL,
                -- coverage block
                bars_used INTEGER NOT NULL DEFAULT 0,
                bars_missing INTEGER NOT NULL DEFAULT 0,
                coverage_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                -- raw readings + config echo
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                computed_at TIMESTAMP NOT NULL DEFAULT NOW(),
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_ticker_regime_as_of "
            "ON trading_ticker_regime_snapshots (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_ticker_regime_id "
            "ON trading_ticker_regime_snapshots (snapshot_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_ticker_regime_ticker_as_of "
            "ON trading_ticker_regime_snapshots (ticker, as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_ticker_regime_label_computed "
            "ON trading_ticker_regime_snapshots (ticker_regime_label, computed_at DESC)"
        ))
        conn.commit()


def _migration_142_vol_dispersion_snapshot(conn) -> None:
    """Phase L.21: volatility term structure + cross-sectional dispersion
    snapshot (shadow rollout).

    Append-only daily market-wide snapshot capturing VIX term structure
    (VIXY/VIXM/VXZ), SPY realised-vol windows, cross-sectional return
    dispersion, mean pairwise correlation, and sector-leadership churn.
    Composite labels for vol-regime, dispersion, and correlation are
    shadow-only; no consumer reads this table in L.21.1 —
    authoritative consumption deferred to L.21.2.
    """
    tables = _tables(conn)

    if "trading_vol_dispersion_snapshots" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_vol_dispersion_snapshots (
                id BIGSERIAL PRIMARY KEY,
                snapshot_id VARCHAR(64) NOT NULL,
                as_of_date DATE NOT NULL,
                -- VIX term structure
                vixy_close DOUBLE PRECISION NULL,
                vixm_close DOUBLE PRECISION NULL,
                vxz_close DOUBLE PRECISION NULL,
                vix_slope_4m_1m DOUBLE PRECISION NULL,
                vix_slope_7m_1m DOUBLE PRECISION NULL,
                -- SPY realised vol (annualised)
                spy_realized_vol_5d DOUBLE PRECISION NULL,
                spy_realized_vol_20d DOUBLE PRECISION NULL,
                spy_realized_vol_60d DOUBLE PRECISION NULL,
                vix_realized_gap DOUBLE PRECISION NULL,
                -- cross-sectional dispersion + correlation
                cross_section_return_std_5d DOUBLE PRECISION NULL,
                cross_section_return_std_20d DOUBLE PRECISION NULL,
                mean_abs_corr_20d DOUBLE PRECISION NULL,
                corr_sample_size INTEGER NOT NULL DEFAULT 0,
                -- sector leadership churn (Spearman 1-rho^2)
                sector_leadership_churn_20d DOUBLE PRECISION NULL,
                -- composite labels
                vol_regime_numeric INTEGER NOT NULL DEFAULT 0,
                vol_regime_label VARCHAR(32) NOT NULL,
                dispersion_numeric INTEGER NOT NULL DEFAULT 0,
                dispersion_label VARCHAR(32) NOT NULL,
                correlation_numeric INTEGER NOT NULL DEFAULT 0,
                correlation_label VARCHAR(32) NOT NULL,
                -- coverage block
                universe_size INTEGER NOT NULL DEFAULT 0,
                tickers_missing INTEGER NOT NULL DEFAULT 0,
                coverage_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                -- raw readings + config echo
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                computed_at TIMESTAMP NOT NULL DEFAULT NOW(),
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_vol_dispersion_as_of "
            "ON trading_vol_dispersion_snapshots (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_vol_dispersion_id "
            "ON trading_vol_dispersion_snapshots (snapshot_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_vol_dispersion_vol_label "
            "ON trading_vol_dispersion_snapshots (vol_regime_label, computed_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_vol_dispersion_disp_label "
            "ON trading_vol_dispersion_snapshots (dispersion_label, computed_at DESC)"
        ))
        conn.commit()


def _migration_143_intraday_session_snapshot(conn) -> None:
    """Phase L.22: intraday session regime snapshot (shadow rollout).

    Append-only daily market-wide snapshot derived from SPY 5-minute
    bars that captures opening-range, midday compression, power-hour,
    gap-open magnitude and the resulting session composite label
    (trending / range / reversal / gap-and-go / gap-fade / compressed /
    neutral). Shadow-only in L.22.1: no consumer reads this table;
    authoritative consumption deferred to L.22.2.
    """
    tables = _tables(conn)

    if "trading_intraday_session_snapshots" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_intraday_session_snapshots (
                id BIGSERIAL PRIMARY KEY,
                snapshot_id VARCHAR(64) NOT NULL,
                as_of_date DATE NOT NULL,
                source_symbol VARCHAR(16) NOT NULL DEFAULT 'SPY',
                -- session anchors
                open_price DOUBLE PRECISION NULL,
                close_price DOUBLE PRECISION NULL,
                session_high DOUBLE PRECISION NULL,
                session_low DOUBLE PRECISION NULL,
                session_range_pct DOUBLE PRECISION NULL,
                -- gap features
                prev_close DOUBLE PRECISION NULL,
                gap_open DOUBLE PRECISION NULL,
                gap_open_pct DOUBLE PRECISION NULL,
                -- opening range (first 30 min by default)
                or_high DOUBLE PRECISION NULL,
                or_low DOUBLE PRECISION NULL,
                or_range_pct DOUBLE PRECISION NULL,
                or_volume_ratio DOUBLE PRECISION NULL,
                -- midday window (12:00-14:00 ET)
                midday_range_pct DOUBLE PRECISION NULL,
                midday_compression_ratio DOUBLE PRECISION NULL,
                -- power hour (last 30 min)
                ph_range_pct DOUBLE PRECISION NULL,
                ph_volume_ratio DOUBLE PRECISION NULL,
                close_vs_or_mid_pct DOUBLE PRECISION NULL,
                -- intraday realised vol (annualised)
                intraday_rv DOUBLE PRECISION NULL,
                -- composite label
                session_numeric INTEGER NOT NULL DEFAULT 0,
                session_label VARCHAR(32) NOT NULL,
                -- coverage block
                bars_observed INTEGER NOT NULL DEFAULT 0,
                coverage_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                -- raw readings + config echo
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                computed_at TIMESTAMP NOT NULL DEFAULT NOW(),
                observed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_intraday_session_as_of "
            "ON trading_intraday_session_snapshots (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_intraday_session_id "
            "ON trading_intraday_session_snapshots (snapshot_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_intraday_session_label "
            "ON trading_intraday_session_snapshots (session_label, computed_at DESC)"
        ))
        conn.commit()


def _migration_144_pattern_regime_performance_ledger(conn) -> None:
    """Phase M.1: pattern x regime performance ledger (shadow rollout).

    Append-only daily aggregate of closed paper-trade performance
    stratified by pattern_id and regime dimension/label. First
    consumer of L.17 - L.22 snapshots: reads them read-only to
    resolve the regime label at each trade's entry_date. Writes
    one row per (as_of_date, pattern_id, regime_dimension,
    regime_label, window_days) tuple per run.

    Shadow-only in M.1: no downstream consumer (scanner,
    promotion, sizing, alerts) reads this table. Authoritative
    consumption (e.g. NetEdgeRanker reading per-regime expectancy
    to tilt sizing) is deferred to M.2 behind governance.
    """
    tables = _tables(conn)

    if "trading_pattern_regime_performance_daily" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pattern_regime_performance_daily (
                id BIGSERIAL PRIMARY KEY,
                ledger_run_id VARCHAR(64) NOT NULL,
                as_of_date DATE NOT NULL,
                window_days INTEGER NOT NULL DEFAULT 90,
                pattern_id INTEGER NOT NULL,
                regime_dimension VARCHAR(32) NOT NULL,
                regime_label VARCHAR(48) NOT NULL,
                n_trades INTEGER NOT NULL DEFAULT 0,
                n_wins INTEGER NOT NULL DEFAULT 0,
                hit_rate DOUBLE PRECISION NULL,
                mean_pnl_pct DOUBLE PRECISION NULL,
                median_pnl_pct DOUBLE PRECISION NULL,
                sum_pnl DOUBLE PRECISION NULL,
                expectancy DOUBLE PRECISION NULL,
                mean_win_pct DOUBLE PRECISION NULL,
                mean_loss_pct DOUBLE PRECISION NULL,
                profit_factor DOUBLE PRECISION NULL,
                sharpe_proxy DOUBLE PRECISION NULL,
                avg_hold_days DOUBLE PRECISION NULL,
                has_confidence BOOLEAN NOT NULL DEFAULT FALSE,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                mode VARCHAR(16) NOT NULL,
                computed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_pattern_regime_perf_as_of "
            "ON trading_pattern_regime_performance_daily (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pattern_regime_perf_lookup "
            "ON trading_pattern_regime_performance_daily "
            "(pattern_id, regime_dimension, regime_label, as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pattern_regime_perf_run "
            "ON trading_pattern_regime_performance_daily (ledger_run_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pattern_regime_perf_confident "
            "ON trading_pattern_regime_performance_daily "
            "(pattern_id, regime_dimension) "
            "WHERE has_confidence"
        ))
        conn.commit()


def _migration_145_pattern_regime_m2_consumers(conn) -> None:
    """Phase M.2: pattern x regime authoritative consumers.

    Adds three append-only decision-log tables (tilt, promotion,
    kill-switch) and two additive columns to
    ``trading_position_sizer_log`` for M.2.a's sizing tilt
    multiplier. Also extends ``trading_governance_approvals`` with
    a nullable ``expires_at`` column so M.2 can gate ``authoritative``
    mode behind time-bounded approvals.

    Shadow/compare-first rollout: the new tables accept rows in any
    mode, but ``authoritative`` refusal (with a ``refused`` event)
    triggers when the matching approval row is missing or expired.
    """
    tables = _tables(conn)

    # --- Extend trading_governance_approvals (additive) -------------
    if "trading_governance_approvals" in tables:
        cols = _columns(conn, "trading_governance_approvals")
        if "expires_at" not in cols:
            conn.execute(text(
                "ALTER TABLE trading_governance_approvals "
                "ADD COLUMN expires_at TIMESTAMP NULL"
            ))

    # --- Extend trading_position_sizer_log (additive) ---------------
    if "trading_position_sizer_log" in tables:
        cols = _columns(conn, "trading_position_sizer_log")
        if "pattern_regime_tilt_multiplier" not in cols:
            conn.execute(text(
                "ALTER TABLE trading_position_sizer_log "
                "ADD COLUMN pattern_regime_tilt_multiplier "
                "DOUBLE PRECISION NULL"
            ))
        if "pattern_regime_tilt_reason" not in cols:
            conn.execute(text(
                "ALTER TABLE trading_position_sizer_log "
                "ADD COLUMN pattern_regime_tilt_reason VARCHAR(48) NULL"
            ))

    # --- Tilt log ---------------------------------------------------
    if "trading_pattern_regime_tilt_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pattern_regime_tilt_log (
                id BIGSERIAL PRIMARY KEY,
                evaluation_id VARCHAR(32) NOT NULL,
                as_of_date DATE NOT NULL,
                pattern_id INTEGER NOT NULL,
                ticker VARCHAR(32) NULL,
                source VARCHAR(48) NULL,
                mode VARCHAR(16) NOT NULL,
                applied BOOLEAN NOT NULL DEFAULT FALSE,
                baseline_size_dollars DOUBLE PRECISION NULL,
                consumer_size_dollars DOUBLE PRECISION NULL,
                multiplier DOUBLE PRECISION NOT NULL,
                reason_code VARCHAR(48) NOT NULL,
                diff_category VARCHAR(16) NULL,
                contributing_dimensions JSONB NOT NULL DEFAULT '{}'::jsonb,
                n_confident_dimensions INTEGER NOT NULL DEFAULT 0,
                fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
                context_hash VARCHAR(16) NULL,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                computed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_pr_tilt_as_of "
            "ON trading_pattern_regime_tilt_log (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pr_tilt_pattern "
            "ON trading_pattern_regime_tilt_log "
            "(pattern_id, as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pr_tilt_auth "
            "ON trading_pattern_regime_tilt_log (pattern_id, as_of_date DESC) "
            "WHERE mode = 'authoritative'"
        ))

    # --- Promotion log ----------------------------------------------
    if "trading_pattern_regime_promotion_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pattern_regime_promotion_log (
                id BIGSERIAL PRIMARY KEY,
                evaluation_id VARCHAR(32) NOT NULL,
                as_of_date DATE NOT NULL,
                pattern_id INTEGER NOT NULL,
                mode VARCHAR(16) NOT NULL,
                applied BOOLEAN NOT NULL DEFAULT FALSE,
                baseline_allow BOOLEAN NULL,
                consumer_allow BOOLEAN NOT NULL,
                reason_code VARCHAR(48) NOT NULL,
                diff_category VARCHAR(16) NULL,
                blocking_dimensions JSONB NOT NULL DEFAULT '{}'::jsonb,
                n_confident_dimensions INTEGER NOT NULL DEFAULT 0,
                fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
                source VARCHAR(48) NULL,
                context_hash VARCHAR(16) NULL,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                computed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_pr_prom_as_of "
            "ON trading_pattern_regime_promotion_log (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pr_prom_pattern "
            "ON trading_pattern_regime_promotion_log "
            "(pattern_id, as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pr_prom_auth "
            "ON trading_pattern_regime_promotion_log (pattern_id, as_of_date DESC) "
            "WHERE mode = 'authoritative'"
        ))

    # --- Kill-switch log --------------------------------------------
    if "trading_pattern_regime_killswitch_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pattern_regime_killswitch_log (
                id BIGSERIAL PRIMARY KEY,
                evaluation_id VARCHAR(32) NOT NULL,
                as_of_date DATE NOT NULL,
                pattern_id INTEGER NOT NULL,
                mode VARCHAR(16) NOT NULL,
                applied BOOLEAN NOT NULL DEFAULT FALSE,
                baseline_status VARCHAR(24) NULL,
                consumer_quarantine BOOLEAN NOT NULL,
                reason_code VARCHAR(48) NOT NULL,
                diff_category VARCHAR(16) NULL,
                consecutive_days_negative INTEGER NOT NULL DEFAULT 0,
                worst_dimension VARCHAR(32) NULL,
                worst_expectancy DOUBLE PRECISION NULL,
                n_confident_dimensions INTEGER NOT NULL DEFAULT 0,
                fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
                context_hash VARCHAR(16) NULL,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                computed_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_pr_kill_as_of "
            "ON trading_pattern_regime_killswitch_log (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pr_kill_pattern "
            "ON trading_pattern_regime_killswitch_log "
            "(pattern_id, as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pr_kill_auth "
            "ON trading_pattern_regime_killswitch_log (pattern_id, as_of_date DESC) "
            "WHERE mode = 'authoritative'"
        ))

    conn.commit()


def _migration_146_m2_autopilot(conn) -> None:
    """Phase M.2-autopilot: auto-advance engine for M.2 slices.

    Adds two tables:

    * ``trading_brain_runtime_modes`` — single-row-per-slice override
      table. Each M.2 slice (``tilt`` / ``promotion`` / ``killswitch``)
      consults this before falling back to ``settings.brain_pattern_regime_*_mode``.
      Allows the autopilot to advance / revert modes without mutating
      ``.env`` or restarting services.
    * ``trading_pattern_regime_autopilot_log`` — append-only audit
      trail of every advance / hold / revert / weekly-summary decision,
      with gate evaluation payload for forensic analysis.

    Both tables are additive. Absence of a row in runtime_modes is a
    valid state (slice uses env fallback). No existing rows are touched.
    """
    tables = _tables(conn)

    if "trading_brain_runtime_modes" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_brain_runtime_modes (
                slice_name VARCHAR(64) PRIMARY KEY,
                mode VARCHAR(16) NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_by VARCHAR(64) NOT NULL DEFAULT 'unknown',
                reason VARCHAR(200) NULL,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_brain_runtime_modes_updated "
            "ON trading_brain_runtime_modes (updated_at DESC)"
        ))

    if "trading_pattern_regime_autopilot_log" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_pattern_regime_autopilot_log (
                id BIGSERIAL PRIMARY KEY,
                as_of_date DATE NOT NULL,
                evaluated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                slice_name VARCHAR(64) NOT NULL,
                event VARCHAR(32) NOT NULL,
                from_mode VARCHAR(16) NULL,
                to_mode VARCHAR(16) NULL,
                reason_code VARCHAR(64) NOT NULL,
                gates_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                approval_id INTEGER NULL,
                days_in_stage INTEGER NULL,
                ops_log_excerpt TEXT NULL
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_pr_autopilot_as_of "
            "ON trading_pattern_regime_autopilot_log (as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pr_autopilot_slice_event "
            "ON trading_pattern_regime_autopilot_log "
            "(slice_name, event, as_of_date DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_pr_autopilot_evaluated "
            "ON trading_pattern_regime_autopilot_log (evaluated_at DESC)"
        ))

    conn.commit()


def _migration_147_autotrader_audit(conn) -> None:
    """AutoTrader v1: audit table + Trade columns for scale-in and filtering."""
    tables = _tables(conn)

    if "trading_autotrader_runs" not in tables:
        conn.execute(text("""
            CREATE TABLE trading_autotrader_runs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                breakout_alert_id INTEGER REFERENCES trading_breakout_alerts(id) ON DELETE SET NULL,
                scan_pattern_id INTEGER REFERENCES scan_patterns(id) ON DELETE SET NULL,
                ticker VARCHAR(32) NOT NULL DEFAULT '',
                decision VARCHAR(24) NOT NULL,
                reason TEXT,
                rule_snapshot JSONB,
                llm_snapshot JSONB,
                trade_id INTEGER REFERENCES trading_trades(id) ON DELETE SET NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_autotrader_runs_user_created ON trading_autotrader_runs (user_id, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_autotrader_runs_breakout_alert ON trading_autotrader_runs (breakout_alert_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_autotrader_runs_trade ON trading_autotrader_runs (trade_id)"
        ))

    if "trading_trades" in _tables(conn):
        cols = _columns(conn, "trading_trades")
        if "scale_in_count" not in cols:
            conn.execute(text(
                "ALTER TABLE trading_trades ADD COLUMN scale_in_count INTEGER NOT NULL DEFAULT 0"
            ))
        if "auto_trader_version" not in cols:
            conn.execute(text(
                "ALTER TABLE trading_trades ADD COLUMN auto_trader_version VARCHAR(32)"
            ))
            conn.execute(text(
                "CREATE INDEX ix_trading_trades_auto_trader_version "
                "ON trading_trades (auto_trader_version) "
                "WHERE auto_trader_version IS NOT NULL"
            ))

    conn.commit()


def _migration_148_trade_pending_exit_columns(conn) -> None:
    """Pending live-exit state for Robinhood off-hours equity liquidation."""
    if "trading_trades" not in _tables(conn):
        conn.commit()
        return
    cols = _columns(conn, "trading_trades")
    for col, typ in [
        ("pending_exit_order_id", "VARCHAR(100)"),
        ("pending_exit_status", "VARCHAR(30)"),
        ("pending_exit_requested_at", "TIMESTAMP"),
        ("pending_exit_reason", "VARCHAR(50)"),
        ("pending_exit_limit_price", "DOUBLE PRECISION"),
    ]:
        if col not in cols:
            conn.execute(text(f"ALTER TABLE trading_trades ADD COLUMN {col} {typ}"))
    conn.commit()


def _migration_150_venue_order_idempotency(conn) -> None:
    """Durable DB-backed client_order_id guard for venue adapters.

    Previously the per-venue duplicate guards lived only in RAM
    (``_recent_client_orders`` OrderedDict in robinhood_spot.py /
    coinbase_spot.py). That resets on restart, leaving a window where a
    crash + redeploy during an in-flight order could re-submit.

    This table is the durable backing for
    ``app/services/trading/venue/idempotency_store.py``. The in-memory
    guard remains as a hot path; the DB is the source of truth across
    restarts.

    TTL-based eviction: rows stay until ``ttl_expires_at`` passes, then
    ``idempotency_store.gc_expired`` (optional scheduler) can reap them.
    Lookups filter on TTL directly, so an untended table is still
    correct — just larger.
    """
    if "venue_order_idempotency" not in _tables(conn):
        conn.execute(text("""
            CREATE TABLE venue_order_idempotency (
                client_order_id VARCHAR(128) PRIMARY KEY,
                venue VARCHAR(32) NOT NULL,
                symbol VARCHAR(32) NOT NULL,
                side VARCHAR(8) NOT NULL,
                qty DOUBLE PRECISION NOT NULL,
                broker_order_id VARCHAR(128) NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'submitted',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                ttl_expires_at TIMESTAMP NOT NULL
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_voi_venue_created_at "
            "ON venue_order_idempotency (venue, created_at DESC)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_voi_ttl "
            "ON venue_order_idempotency (ttl_expires_at)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_voi_broker_order_id "
            "ON venue_order_idempotency (broker_order_id) "
            "WHERE broker_order_id IS NOT NULL"
        ))
    conn.commit()


def _migration_149_schema_drift_repairs(conn) -> None:
    """Repair additive trading columns when schema_version drift skipped old migrations."""
    tables = _tables(conn)

    if "trading_pattern_monitor_decisions" in tables:
        cols = _columns(conn, "trading_pattern_monitor_decisions")
        if "vitals_composite" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE trading_pattern_monitor_decisions "
                    "ADD COLUMN vitals_composite DOUBLE PRECISION"
                )
            )

    if "trading_trades" in tables:
        cols = _columns(conn, "trading_trades")
        for col, typ in [
            ("pending_exit_order_id", "VARCHAR(100)"),
            ("pending_exit_status", "VARCHAR(30)"),
            ("pending_exit_requested_at", "TIMESTAMP"),
            ("pending_exit_reason", "VARCHAR(50)"),
            ("pending_exit_limit_price", "DOUBLE PRECISION"),
        ]:
            if col not in cols:
                conn.execute(text(f"ALTER TABLE trading_trades ADD COLUMN {col} {typ}"))

    conn.commit()


def _migration_151_trade_management_scope(conn) -> None:
    """Add explicit provenance/management scope to live trade and audit rows."""
    tables = _tables(conn)

    if "trading_trades" in tables:
        cols = _columns(conn, "trading_trades")
        if "management_scope" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE trading_trades "
                    "ADD COLUMN management_scope VARCHAR(40)"
                )
            )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trading_trades_management_scope "
                "ON trading_trades (management_scope)"
            )
        )
        conn.execute(
            text(
                """
                UPDATE trading_trades
                   SET management_scope = CASE
                       WHEN COALESCE(auto_trader_version, '') = 'v1' THEN 'auto_trader_v1'
                       WHEN COALESCE(tags, '') ILIKE '%sync%' AND broker_source IS NOT NULL THEN 'broker_sync'
                       WHEN broker_source IS NOT NULL AND broker_source IN ('robinhood', 'coinbase') THEN 'broker_sync'
                       ELSE 'manual'
                   END
                 WHERE management_scope IS NULL
                """
            )
        )

    if "trading_autotrader_runs" in tables:
        cols = _columns(conn, "trading_autotrader_runs")
        if "management_scope" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE trading_autotrader_runs "
                    "ADD COLUMN management_scope VARCHAR(40)"
                )
            )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trading_autotrader_runs_management_scope "
                "ON trading_autotrader_runs (management_scope)"
            )
        )
        conn.execute(
            text(
                """
                UPDATE trading_autotrader_runs
                   SET management_scope = CASE
                       WHEN decision IN ('adopt_manual', 'unadopt_manual') THEN 'adopted_position'
                       ELSE 'auto_trader_v1'
                   END
                 WHERE management_scope IS NULL
                """
            )
        )

    conn.commit()


def _migration_159_order_state_log(conn) -> None:
    """P1.1 — formal order state machine log.

    Canonical states:
      DRAFT → SUBMITTING → ACK → PARTIAL → FILLED | CANCELLED | REJECTED | EXPIRED

    Written by ``app.services.trading.venue.order_state_machine.record_transition``
    whenever the venue poll loop or the reconciler observes a new state. The
    writer enforces the allowed-transition table so only legal transitions land.

    Indexes support the three access patterns we need:
      * "current state for this order" — (order_id, recorded_at DESC)
      * "resolve client_order_id before broker fills it in" —
        (client_order_id, recorded_at DESC)
      * "recent transitions per venue" — (venue, recorded_at DESC)
      * "how many orders are in ACK right now" — (to_state, recorded_at DESC)
    """
    if "trading_order_state_log" not in _tables(conn):
        conn.execute(text("""
            CREATE TABLE trading_order_state_log (
                id BIGSERIAL PRIMARY KEY,
                order_id VARCHAR(128) NULL,
                client_order_id VARCHAR(128) NULL,
                venue VARCHAR(32) NOT NULL,
                from_state VARCHAR(16) NULL,
                to_state VARCHAR(16) NOT NULL,
                source VARCHAR(32) NOT NULL,
                broker_status VARCHAR(32) NULL,
                raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                recorded_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_order_state_log_order "
            "ON trading_order_state_log (order_id, recorded_at)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_order_state_log_client "
            "ON trading_order_state_log (client_order_id, recorded_at)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_order_state_log_venue_ts "
            "ON trading_order_state_log (venue, recorded_at)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_order_state_log_to_state_ts "
            "ON trading_order_state_log (to_state, recorded_at)"
        ))
    conn.commit()


def _migration_161_trade_broker_order_id_unique(conn) -> None:
    """P0.3 — partial UNIQUE index on ``trading_trades.broker_order_id``.

    Without this, two concurrent AutoTrader ticks (or a tick racing with
    broker_position_sync) can INSERT two Trade rows pointing at the same
    live broker order. Once that happens the reconciler can't tell which
    row is authoritative and exits may fire twice — the idempotency_store
    blocks the duplicate *order submission* at the venue adapter, but the
    Trade row is written AFTER the broker ACK, so a retry of just the DB
    write is still possible.

    Partial because legacy rows have empty or NULL broker_order_id —
    those must remain unconstrained. We additionally dedupe existing
    conflicts by appending ``#dup{id}`` to collisions (keeping the
    lowest-id row untouched) so the index can be created cleanly. Any
    touched rows are logged so an operator can investigate.
    """
    if "trading_trades" not in _tables(conn):
        conn.commit()
        return

    # Find duplicate broker_order_id groups. Keep the smallest id as the
    # canonical row; rename later duplicates so the unique index can land.
    dupes = conn.execute(text(
        """
        WITH dup_groups AS (
            SELECT broker_order_id
            FROM trading_trades
            WHERE broker_order_id IS NOT NULL AND broker_order_id <> ''
            GROUP BY broker_order_id
            HAVING COUNT(*) > 1
        ),
        ranked AS (
            SELECT t.id, t.broker_order_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY t.broker_order_id ORDER BY t.id ASC
                   ) AS rn
            FROM trading_trades t
            JOIN dup_groups g ON g.broker_order_id = t.broker_order_id
        )
        SELECT id, broker_order_id FROM ranked WHERE rn > 1
        """
    )).fetchall()

    if dupes:
        dup_ids = [int(r[0]) for r in dupes]
        logger.warning(
            "[migration_161] dedup quarantined %d trading_trades with duplicate "
            "broker_order_id; ids=%s",
            len(dup_ids), dup_ids,
        )
        conn.execute(
            text(
                "UPDATE trading_trades "
                "SET broker_order_id = broker_order_id || '#dup' || id "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": dup_ids},
        )

    existing = {
        row[0] for row in conn.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' "
            "  AND tablename = 'trading_trades'"
        )).fetchall()
    }
    if "ix_trading_trades_broker_order_id_unique" not in existing:
        conn.execute(text(
            "CREATE UNIQUE INDEX ix_trading_trades_broker_order_id_unique "
            "ON trading_trades (broker_order_id) "
            "WHERE broker_order_id IS NOT NULL AND broker_order_id <> ''"
        ))
    conn.commit()


def _migration_160_exec_events_venue_ts_index(conn) -> None:
    """P1.2 — composite ``(venue, recorded_at)`` index on execution events.

    The venue-health circuit breaker queries ``trading_execution_events``
    with both a venue filter AND a rolling-window time filter
    (``recorded_at >= NOW() - INTERVAL '5 minutes'``). The existing single-
    column indexes on ``venue`` and ``recorded_at`` each individually
    satisfy only half the predicate; PostgreSQL would bitmap-AND them or
    fall back to a sequential scan under load. This composite index serves
    the breaker's hot path directly.

    Note: the venue column is already indexed alone (from the ORM's
    ``index=True`` on the column def); this index is complementary, not a
    replacement. We keep both so queries that filter on venue alone (e.g.
    ops-health snapshot) still hit the single-column index.
    """
    existing = {
        row[0] for row in conn.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' "
            "  AND tablename = 'trading_execution_events'"
        )).fetchall()
    }
    if "ix_trading_execution_events_venue_ts" not in existing:
        conn.execute(text(
            "CREATE INDEX ix_trading_execution_events_venue_ts "
            "ON trading_execution_events (venue, recorded_at)"
        ))
    conn.commit()


# (version_id, callable that receives conn and runs migration)


def _migration_162_project_domain_recovery(conn) -> None:
    """Project-domain recovery: runtime-aware repos, durable runs, and analysis snapshots."""
    tables = _tables(conn)

    if "code_repos" in tables:
        cols = _columns(conn, "code_repos")
        additions = {
            "host_path": "TEXT",
            "container_path": "TEXT",
            "reachable_in_web": "BOOLEAN NOT NULL DEFAULT FALSE",
            "reachable_in_scheduler": "BOOLEAN NOT NULL DEFAULT FALSE",
            "last_index_error": "TEXT",
            "last_successful_indexed_at": "TIMESTAMP",
            "last_successful_file_count": "INTEGER",
        }
        for col_name, col_type in additions.items():
            if col_name not in cols:
                conn.execute(text(f"ALTER TABLE code_repos ADD COLUMN {col_name} {col_type}"))
        conn.execute(
            text(
                "UPDATE code_repos "
                "SET host_path = COALESCE(host_path, path), "
                "container_path = COALESCE(container_path, CASE WHEN path LIKE '/%' THEN path ELSE NULL END), "
                "last_successful_indexed_at = COALESCE(last_successful_indexed_at, last_indexed), "
                "last_successful_file_count = COALESCE(last_successful_file_count, CASE WHEN file_count > 0 THEN file_count ELSE NULL END)"
            )
        )
        conn.commit()

    if "plan_tasks" in tables:
        cols = _columns(conn, "plan_tasks")
        if "coding_workflow_state" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE plan_tasks ADD COLUMN coding_workflow_state "
                    "VARCHAR(40) NOT NULL DEFAULT 'unbound'"
                )
            )
        if "coding_workflow_state_updated_at" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE plan_tasks ADD COLUMN coding_workflow_state_updated_at "
                    "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
                )
            )
        conn.commit()

    if "project_domain_runs" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE project_domain_runs (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    task_id INTEGER NULL REFERENCES plan_tasks(id) ON DELETE SET NULL,
                    repo_id INTEGER NULL REFERENCES code_repos(id) ON DELETE SET NULL,
                    run_kind VARCHAR(32) NOT NULL,
                    status VARCHAR(24) NOT NULL DEFAULT 'running',
                    trigger_source VARCHAR(32) NOT NULL DEFAULT 'manual',
                    title VARCHAR(200) NULL,
                    detail_json TEXT NULL,
                    error_message TEXT NULL,
                    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMP NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_project_domain_runs_kind ON project_domain_runs(run_kind)"))
        conn.execute(text("CREATE INDEX ix_project_domain_runs_status ON project_domain_runs(status)"))
        conn.execute(text("CREATE INDEX ix_project_domain_runs_user_created ON project_domain_runs(user_id, created_at DESC)"))
        conn.commit()

    if "project_analysis_snapshots" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE project_analysis_snapshots (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    task_id INTEGER NULL REFERENCES plan_tasks(id) ON DELETE SET NULL,
                    repo_id INTEGER NULL REFERENCES code_repos(id) ON DELETE SET NULL,
                    source_run_id INTEGER NULL REFERENCES project_domain_runs(id) ON DELETE SET NULL,
                    status VARCHAR(24) NOT NULL DEFAULT 'completed',
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    perspectives_json TEXT NOT NULL DEFAULT '{}',
                    timeline_json TEXT NOT NULL DEFAULT '[]',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_project_analysis_snapshots_user_created ON project_analysis_snapshots(user_id, created_at DESC)"))
        conn.execute(text("CREATE INDEX ix_project_analysis_snapshots_task_created ON project_analysis_snapshots(task_id, created_at DESC)"))
        conn.commit()


def _migration_152_operational_clusters(conn) -> None:
    """Phase 2A: Add operational clusters for the L1-L7 spine.

    Mirrors the L8 learning-cycle cluster pattern on the operational side so
    every operational node belongs to a named cluster. Additive:
    - Inserts 8 operational cluster nodes at layer 8, node_type='operational_cluster'.
    - Stamps display_meta.operational_cluster_id on each member node.
    - Adds structural edges (member<->cluster) with unique signal_types
      ('cluster_roll_up' / 'cluster_wake') that no existing code emits, so
      propagation behavior is unchanged. Future phases may start emitting them.
    """
    import json

    if "brain_graph_nodes" not in _tables(conn):
        conn.commit()
        return

    gv = 1
    dom = "trading"

    # (cluster_id, label, description, member_node_ids)
    operational_clusters: list[tuple[str, str, str, list[str]]] = [
        (
            "nm_c_sensing",
            "Sensing",
            "Market-data ingestion and per-venue provider truth.",
            [
                "nm_snap_daily", "nm_snap_intraday", "nm_snap_crypto", "nm_universe_scan",
                "nm_venue_truth_coinbase", "nm_venue_truth_robinhood",
            ],
        ),
        (
            "nm_c_features",
            "Feature Extraction",
            "Technical features derived from snapshots (volatility, momentum, liquidity, breadth, intermarket).",
            [
                "nm_volatility", "nm_momentum", "nm_anomaly",
                "nm_liquidity_state", "nm_breadth_state", "nm_intermarket_state",
            ],
        ),
        (
            "nm_c_market_state",
            "Latent Market State",
            "Working memory, regime, thesis, and confidence accumulators.",
            [
                "nm_event_bus", "nm_working_memory", "nm_regime", "nm_contradiction",
                "nm_active_thesis_state", "nm_confidence_accumulator",
                "nm_memory_freshness", "nm_exec_liquidity_regime",
            ],
        ),
        (
            "nm_c_pattern_inference",
            "Pattern Inference",
            "Pattern discovery, similarity, and trade-context association.",
            ["nm_pattern_disc", "nm_similarity", "nm_trade_context"],
        ),
        (
            "nm_c_evidence",
            "Evidence & Verification",
            "Backtest, replay, counterfactual, and execution-quality verification.",
            [
                "nm_evidence_bt", "nm_evidence_replay", "nm_evidence_quality",
                "nm_counterfactual_challenger", "nm_contradiction_verifier",
                "nm_exec_spread_quality",
            ],
        ),
        (
            "nm_c_decision",
            "Decision & Expression",
            "Action signals, alerts, risk gating, sizing, and observer outputs.",
            [
                "nm_action_signals", "nm_action_alerts", "nm_risk_gate",
                "nm_sizing_policy", "nm_exec_readiness_gate",
                "nm_observer_journal", "nm_observer_playbook",
            ],
        ),
        (
            "nm_c_reactive_sensors",
            "Reactive Sensors",
            "Real-time trading sensors (stop eval, pattern health, imminent eval).",
            ["nm_stop_eval", "nm_pattern_health", "nm_imminent_eval"],
        ),
        (
            "nm_c_meta_ops",
            "Operational Meta",
            "Ops-side meta (reweight, decay, threshold tuner, promotion monitor). Distinct from L8 learning-cycle meta.",
            [
                "nm_meta_reweight", "nm_meta_decay",
                "nm_threshold_tuner", "nm_promotion_demotion_monitor",
            ],
        ),
    ]

    # 1. Insert cluster nodes (layer 8, high fire_threshold so they effectively never fire on their own)
    for cid, label, desc, _members in operational_clusters:
        dmeta = {
            "role": "operational_cluster",
            "cluster_kind": "operational",
            "operational_cluster_id": cid,
            "description": desc,
            "remarks": "Phase 2A operational cluster. Groups L1-L7 spine nodes for audit and future cluster-level activation.",
        }
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    display_meta, created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, 'operational_cluster', 8, :label,
                    0.99, 60, TRUE, 1, FALSE,
                    CAST(:dmeta AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": cid, "domain": dom, "gv": gv, "label": label, "dmeta": json.dumps(dmeta)},
        )
        conn.execute(
            text(
                """
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state,
                    last_activated_at, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": cid},
        )

    # 2. Stamp operational_cluster_id on member nodes (skip missing nodes silently)
    for cid, _label, _desc, members in operational_clusters:
        for mid in members:
            conn.execute(
                text(
                    """
                    UPDATE brain_graph_nodes
                    SET display_meta = jsonb_set(
                        COALESCE(display_meta, '{}'::jsonb),
                        '{operational_cluster_id}',
                        cast(:cid_json as jsonb)
                    ),
                    updated_at = CURRENT_TIMESTAMP
                    WHERE id = :nid
                    """
                ),
                {"nid": mid, "cid_json": f'"{cid}"'},
            )

    # 3. Structural edges: member -> cluster (roll_up) and cluster -> member (wake).
    #    signal_types are unique to this migration so no existing emitter activates them.
    for cid, _label, _desc, members in operational_clusters:
        for mid in members:
            for (src, tgt, sig) in (
                (mid, cid, "cluster_roll_up"),
                (cid, mid, "cluster_wake"),
            ):
                conn.execute(
                    text(
                        """
                        INSERT INTO brain_graph_edges (
                            source_node_id, target_node_id, signal_type, weight, polarity,
                            delay_ms, min_confidence, enabled, graph_version, gate_config,
                            edge_type, min_source_confidence,
                            created_at, updated_at
                        )
                        SELECT :src, :tgt, :sig, 0.3, 'excitatory',
                            0, 0.0, TRUE, :gv, NULL,
                            'control', 0.0,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                          AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                          AND NOT EXISTS (
                            SELECT 1 FROM brain_graph_edges e
                            WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                              AND e.signal_type = :sig AND e.graph_version = :gv
                          )
                        """
                    ),
                    {"src": src, "tgt": tgt, "sig": sig, "gv": gv},
                )

    conn.commit()


def _migration_153_portfolio_and_exit_clusters(conn) -> None:
    """Phase 2B: Portfolio and exit execution sub-graph.

    Adds two operational clusters plus their member nodes so the mesh has a
    first-class representation of portfolio state and exit lifecycle:

    - nm_c_portfolio: portfolio_state, exposure_heat, trade_lifecycle_hub,
      pending_orders, pdt_state.
    - nm_c_exit_execution: exit_policy, trail_engine, target_engine, exit_trigger.

    Adds behavioral edge nm_exposure_heat -> nm_risk_gate (veto) and
    nm_pdt_state -> nm_risk_gate (veto). These activate only when callers start
    publishing exposure_update / trade_lifecycle events, so propagation stays
    quiet until Phase B integration hooks are wired in.
    """
    import json

    if "brain_graph_nodes" not in _tables(conn):
        conn.commit()
        return

    gv = 1
    dom = "trading"

    # 1. New cluster nodes (layer 8, operational_cluster)
    new_clusters = [
        ("nm_c_portfolio", "Portfolio State",
         "Open trades, exposure, pending orders, regulatory status. Bridge between decisions and their outcomes."),
        ("nm_c_exit_execution", "Exit Execution",
         "Exit policy, trail/target engines, terminal exit trigger. Symmetric to entry execution."),
    ]
    for cid, label, desc in new_clusters:
        dmeta = {
            "role": "operational_cluster",
            "cluster_kind": "operational",
            "operational_cluster_id": cid,
            "description": desc,
            "remarks": "Phase 2B operational cluster (portfolio / exit axis).",
        }
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    display_meta, created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, 'operational_cluster', 8, :label,
                    0.99, 60, TRUE, 1, FALSE,
                    CAST(:dmeta AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": cid, "domain": dom, "gv": gv, "label": label, "dmeta": json.dumps(dmeta)},
        )
        conn.execute(
            text(
                """
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state,
                    last_activated_at, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": cid},
        )

    # 2. Member nodes: (id, layer, node_type, label, fire_th, cooldown_s, cluster_id, description, remarks)
    member_nodes = [
        # nm_c_portfolio members
        ("nm_portfolio_state", 3, "portfolio_aggregator", "Portfolio state", 0.45, 30,
         "nm_c_portfolio",
         "Rolls up open trades: count, notional, per-venue split, unrealized pnl.",
         "L3 latent state aggregating live Trade rows. Fed by trade_lifecycle updates."),
        ("nm_exposure_heat", 5, "portfolio_risk", "Exposure heat", 0.50, 30,
         "nm_c_portfolio",
         "Sector / venue / correlation heat vs. configured budget. Fires when over limit.",
         "L5 evidence. Inhibits risk_gate when exposure exceeds budget."),
        ("nm_trade_lifecycle_hub", 6, "trade_lifecycle", "Trade lifecycle hub", 0.40, 15,
         "nm_c_portfolio",
         "Fires on any open trade transition (entry, scale-in, scale-out, close).",
         "L6 action. Central source of trade-state activation for the mesh."),
        ("nm_pending_orders", 3, "order_book_state", "Pending orders", 0.45, 30,
         "nm_c_portfolio",
         "Working / unfilled orders per venue. Tracks broker order-book commitments.",
         "L3 latent state. Fed by venue_truth refresh + order placement events."),
        ("nm_pdt_state", 2, "regulatory_state", "PDT state", 0.60, 120,
         "nm_c_portfolio",
         "Pattern day trader rule status (equities). Inhibits new entries when at the PDT limit.",
         "L2 feature. Equities-only; crypto venues are unaffected."),
        # nm_c_exit_execution members
        ("nm_exit_policy", 6, "exit_policy", "Exit policy", 0.45, 30,
         "nm_c_exit_execution",
         "Aggregates stop model / target / trail rules per open trade.",
         "L6 action. Reads stop_eval and pattern_health, emits to trail/target engines."),
        ("nm_trail_engine", 5, "exit_trail", "Trail engine", 0.50, 30,
         "nm_c_exit_execution",
         "Computes trailing-stop moves from high-watermark and configured trail logic.",
         "L5 evidence. Fires when trail should move; feeds exit_trigger."),
        ("nm_target_engine", 5, "exit_target", "Target engine", 0.50, 30,
         "nm_c_exit_execution",
         "Fires when take-profit thresholds cross.",
         "L5 evidence. Fires on TP hit; feeds exit_trigger."),
        ("nm_exit_trigger", 6, "exit_decision", "Exit trigger", 0.55, 15,
         "nm_c_exit_execution",
         "Terminal: fires exit order; signals action_signals with exit intent.",
         "L6 action. Downstream of trail/target engines and exit policy."),
    ]
    for nid, layer, ntype, label, fth, cd, cid, desc, remarks in member_nodes:
        dmeta = {
            "role": ntype,
            "operational_cluster_id": cid,
            "description": desc,
            "remarks": remarks,
        }
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    display_meta, created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, :ntype, :layer, :label,
                    :fth, :cd, TRUE, 1, FALSE,
                    CAST(:dmeta AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": nid, "domain": dom, "gv": gv, "ntype": ntype,
                "layer": layer, "label": label, "fth": fth, "cd": cd,
                "dmeta": json.dumps(dmeta),
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state,
                    last_activated_at, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": nid},
        )

    # 3. Edges
    edges = [
        # Portfolio flow
        ("nm_trade_lifecycle_hub", "nm_portfolio_state", "trade_lifecycle", 0.75, "excitatory", "dataflow"),
        ("nm_portfolio_state", "nm_exposure_heat", "portfolio_update", 0.75, "excitatory", "evidence"),
        ("nm_exposure_heat", "nm_risk_gate", "exposure_exceeded", 0.90, "inhibitory", "veto"),
        ("nm_pdt_state", "nm_risk_gate", "pdt_blocked", 0.90, "inhibitory", "veto"),
        # Venue truth informs pending orders (tradability, quotes)
        ("nm_venue_truth_coinbase", "nm_pending_orders", "venue_refresh", 0.60, "excitatory", "dataflow"),
        ("nm_venue_truth_robinhood", "nm_pending_orders", "venue_refresh", 0.60, "excitatory", "dataflow"),
        # Exit execution chain
        ("nm_stop_eval", "nm_exit_policy", "stop_eval", 0.70, "excitatory", "dataflow"),
        ("nm_pattern_health", "nm_exit_policy", "pattern_health_change", 0.65, "excitatory", "evidence"),
        ("nm_trade_lifecycle_hub", "nm_exit_policy", "trade_lifecycle", 0.55, "excitatory", "dataflow"),
        ("nm_exit_policy", "nm_trail_engine", "exit_policy", 0.70, "excitatory", "control"),
        ("nm_exit_policy", "nm_target_engine", "exit_policy", 0.70, "excitatory", "control"),
        ("nm_trail_engine", "nm_exit_trigger", "trail_move", 0.75, "excitatory", "evidence"),
        ("nm_target_engine", "nm_exit_trigger", "target_hit", 0.80, "excitatory", "evidence"),
        ("nm_exit_trigger", "nm_action_signals", "exit_intent", 0.75, "excitatory", "control"),
    ]
    for src, tgt, sig, w, pol, etype in edges:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    delay_ms, min_confidence, enabled, graph_version, gate_config,
                    edge_type, min_source_confidence,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    0, 0.0, TRUE, :gv, NULL,
                    :etype, 0.0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.signal_type = :sig AND e.graph_version = :gv
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "gv": gv, "etype": etype},
        )

    # 4. Structural cluster edges (roll_up / wake) for the new clusters' members.
    cluster_members = {
        "nm_c_portfolio": [
            "nm_portfolio_state", "nm_exposure_heat", "nm_trade_lifecycle_hub",
            "nm_pending_orders", "nm_pdt_state",
        ],
        "nm_c_exit_execution": [
            "nm_exit_policy", "nm_trail_engine", "nm_target_engine", "nm_exit_trigger",
        ],
    }
    for cid, members in cluster_members.items():
        for mid in members:
            for (src, tgt, sig) in (
                (mid, cid, "cluster_roll_up"),
                (cid, mid, "cluster_wake"),
            ):
                conn.execute(
                    text(
                        """
                        INSERT INTO brain_graph_edges (
                            source_node_id, target_node_id, signal_type, weight, polarity,
                            delay_ms, min_confidence, enabled, graph_version, gate_config,
                            edge_type, min_source_confidence,
                            created_at, updated_at
                        )
                        SELECT :src, :tgt, :sig, 0.3, 'excitatory',
                            0, 0.0, TRUE, :gv, NULL,
                            'control', 0.0,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                          AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                          AND NOT EXISTS (
                            SELECT 1 FROM brain_graph_edges e
                            WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                              AND e.signal_type = :sig AND e.graph_version = :gv
                          )
                        """
                    ),
                    {"src": src, "tgt": tgt, "sig": sig, "gv": gv},
                )

    conn.commit()


def _migration_154_spine_to_learning_feedback_edges(conn) -> None:
    """Phase 2D: feedback edges from operational spine into the learning cycle.

    Migration 108 added causal edges from learning-cycle step nodes to the spine;
    this migration closes the loop the other direction so the learning cycle
    ingests real operational state.

    All edges are ``feedback`` type with modest weight (0.5). They only activate
    when the source nodes fire — they do not create new pipeline hops.
    """
    if "brain_graph_nodes" not in _tables(conn):
        conn.commit()
        return

    gv = 1

    edges = [
        # Trade outcomes feed meta-learning (every close is training signal).
        ("nm_trade_lifecycle_hub", "nm_lc_c_meta_learning", "trade_lifecycle",
         0.50, "excitatory", "feedback"),
        # Exposure heat informs cycle control (risk status can gate cycle progress).
        ("nm_exposure_heat", "nm_lc_c_control", "exposure_exceeded",
         0.50, "excitatory", "feedback"),
        # Decisioning outputs can adjust edge thresholds via the meta-reweight node.
        ("nm_lc_c_decisioning", "nm_meta_reweight", "cluster_completed",
         0.50, "excitatory", "feedback"),
    ]
    for src, tgt, sig, w, pol, etype in edges:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    delay_ms, min_confidence, enabled, graph_version, gate_config,
                    edge_type, min_source_confidence,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    0, 0.0, TRUE, :gv, NULL,
                    :etype, 0.0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.signal_type = :sig AND e.graph_version = :gv
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "gv": gv, "etype": etype},
        )

    conn.commit()


def _migration_155_plasticity_tables_and_nodes(conn) -> None:
    """Phase 2C: plasticity audit tables + engine/budget mesh nodes.

    Creates:
    - brain_graph_edge_mutations: append-only audit of every (proposed or applied)
      edge weight / gate change.
    - brain_activation_path_log: per-hop record of correlation paths that
      terminated at an action node; enables plasticity to find which edges
      carried a trade's signal.
    - nm_plasticity_engine, nm_plasticity_budget mesh nodes (in nm_c_meta_ops).

    The engine runs behind a feature flag (chili_mesh_plasticity_enabled) with
    dry_run defaulting to True, so this migration does not on its own change any
    edge weights.
    """
    import json

    tables = _tables(conn)

    # 1. brain_graph_edge_mutations
    if "brain_graph_edge_mutations" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE brain_graph_edge_mutations (
                    id BIGSERIAL PRIMARY KEY,
                    edge_id INTEGER NOT NULL REFERENCES brain_graph_edges(id) ON DELETE CASCADE,
                    old_weight DOUBLE PRECISION NOT NULL,
                    new_weight DOUBLE PRECISION NOT NULL,
                    old_min_source_confidence DOUBLE PRECISION NULL,
                    new_min_source_confidence DOUBLE PRECISION NULL,
                    reason VARCHAR(40) NOT NULL,
                    evidence_ref JSONB NULL,
                    delta_source VARCHAR(40) NOT NULL,
                    applied BOOLEAN NOT NULL DEFAULT TRUE,
                    dry_run BOOLEAN NOT NULL DEFAULT FALSE,
                    correlation_id VARCHAR(64) NULL,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_bgem_edge ON brain_graph_edge_mutations(edge_id)"))
        conn.execute(text("CREATE INDEX ix_bgem_applied_at ON brain_graph_edge_mutations(applied_at)"))
        conn.execute(text("CREATE INDEX ix_bgem_correlation ON brain_graph_edge_mutations(correlation_id)"))

    # 2. brain_activation_path_log
    if "brain_activation_path_log" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE brain_activation_path_log (
                    correlation_id VARCHAR(64) NOT NULL,
                    hop_idx INTEGER NOT NULL,
                    source_node_id VARCHAR(80) NOT NULL,
                    target_node_id VARCHAR(80) NOT NULL,
                    edge_id INTEGER NULL REFERENCES brain_graph_edges(id) ON DELETE SET NULL,
                    activation_before DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    activation_after DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    confidence_at_hop DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (correlation_id, hop_idx)
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_bapl_src ON brain_activation_path_log(source_node_id)"))
        conn.execute(text("CREATE INDEX ix_bapl_tgt ON brain_activation_path_log(target_node_id)"))
        conn.execute(text("CREATE INDEX ix_bapl_edge ON brain_activation_path_log(edge_id)"))

    # 3. Plasticity nodes (in nm_c_meta_ops)
    if "brain_graph_nodes" not in tables:
        conn.commit()
        return
    gv = 1
    dom = "trading"

    nodes = [
        ("nm_plasticity_engine", 7, "plasticity_engine", "Plasticity engine",
         0.55, 60, "nm_c_meta_ops",
         "Owns the Hebbian update: on trade close, reweight edges along the activation path.",
         "Fires only when chili_mesh_plasticity_enabled=True. Dry-run writes audit rows without mutating weights."),
        ("nm_plasticity_budget", 7, "plasticity_budget", "Plasticity budget",
         0.70, 300, "nm_c_meta_ops",
         "Caps total |Δw| per day per edge-type. Fires (inhibitory) when budget is exhausted.",
         "Hard safety net. Daily cap defaults to 0.5 (chili_mesh_plasticity_daily_budget)."),
    ]
    for nid, layer, ntype, label, fth, cd, cid, desc, remarks in nodes:
        dmeta = {
            "role": ntype,
            "operational_cluster_id": cid,
            "description": desc,
            "remarks": remarks,
        }
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_nodes (
                    id, domain, graph_version, node_type, layer, label,
                    fire_threshold, cooldown_seconds, enabled, version, is_observer,
                    display_meta, created_at, updated_at
                ) VALUES (
                    :id, :domain, :gv, :ntype, :layer, :label,
                    :fth, :cd, TRUE, 1, FALSE,
                    CAST(:dmeta AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": nid, "domain": dom, "gv": gv, "ntype": ntype,
                "layer": layer, "label": label, "fth": fth, "cd": cd,
                "dmeta": json.dumps(dmeta),
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state,
                    last_activated_at, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (node_id) DO NOTHING
                """
            ),
            {"nid": nid},
        )

    # 4. Structural cluster membership: add to nm_c_meta_ops
    for mid in ("nm_plasticity_engine", "nm_plasticity_budget"):
        for (src, tgt, sig) in (
            (mid, "nm_c_meta_ops", "cluster_roll_up"),
            ("nm_c_meta_ops", mid, "cluster_wake"),
        ):
            conn.execute(
                text(
                    """
                    INSERT INTO brain_graph_edges (
                        source_node_id, target_node_id, signal_type, weight, polarity,
                        delay_ms, min_confidence, enabled, graph_version, gate_config,
                        edge_type, min_source_confidence,
                        created_at, updated_at
                    )
                    SELECT :src, :tgt, :sig, 0.3, 'excitatory',
                        0, 0.0, TRUE, :gv, NULL,
                        'control', 0.0,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                      AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                      AND NOT EXISTS (
                        SELECT 1 FROM brain_graph_edges e
                        WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                          AND e.signal_type = :sig AND e.graph_version = :gv
                      )
                    """
                ),
                {"src": src, "tgt": tgt, "sig": sig, "gv": gv},
            )

    # 5. Plasticity behavioral edges:
    #    - nm_trade_lifecycle_hub -> nm_plasticity_engine (close events trigger updates)
    #    - nm_plasticity_budget -> nm_plasticity_engine (inhibitory veto when over budget)
    #    - nm_plasticity_engine -> nm_meta_reweight (informs downstream reweight node)
    edges = [
        ("nm_trade_lifecycle_hub", "nm_plasticity_engine", "trade_lifecycle",
         0.70, "excitatory", "feedback"),
        ("nm_plasticity_budget", "nm_plasticity_engine", "budget_exhausted",
         0.90, "inhibitory", "veto"),
        ("nm_plasticity_engine", "nm_meta_reweight", "plasticity_applied",
         0.60, "excitatory", "control"),
    ]
    for src, tgt, sig, w, pol, etype in edges:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    delay_ms, min_confidence, enabled, graph_version, gate_config,
                    edge_type, min_source_confidence,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    0, 0.0, TRUE, :gv, NULL,
                    :etype, 0.0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.signal_type = :sig AND e.graph_version = :gv
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "gv": gv, "etype": etype},
        )

    conn.commit()


def _migration_156_observers_and_momentum_bridge(conn) -> None:
    """Phase 2E: Observer feedback + momentum-neural bridge to main spine.

    Two small changes:
    1. Observers (nm_observer_journal, nm_observer_playbook) are no longer
       write-only: clear is_observer and add feedback edges to nm_lc_c_journal.
    2. Bridge momentum-neural hub (nm_momentum_crypto_intel) to the main-spine
       momentum node (nm_momentum) so crypto momentum intelligence informs the
       mainline regime/momentum signals instead of staying parallel.
    """
    if "brain_graph_nodes" not in _tables(conn):
        conn.commit()
        return

    gv = 1

    # 1. Observers: clear is_observer so they can participate in propagation downstream
    for observer_id in ("nm_observer_journal", "nm_observer_playbook"):
        conn.execute(
            text(
                "UPDATE brain_graph_nodes "
                "SET is_observer = FALSE, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = :id AND is_observer = TRUE"
            ),
            {"id": observer_id},
        )

    # 2. Observer feedback edges into the learning-cycle journal cluster
    #    and bridge edges from momentum hub to main-spine nm_momentum.
    edges = [
        # Observers → learning-cycle journal (so observations feed the market journal).
        ("nm_observer_journal", "nm_lc_c_journal", "observer_note",
         0.40, "excitatory", "feedback"),
        ("nm_observer_playbook", "nm_lc_c_journal", "observer_note",
         0.40, "excitatory", "feedback"),
        # Momentum-neural hub → main-spine momentum feature. Typed dataflow so
        # crypto momentum deltas reach the regime/pattern inference layers.
        ("nm_momentum_crypto_intel", "nm_momentum", "momentum_context_refresh",
         0.55, "excitatory", "dataflow"),
        # And momentum-neural hub → regime (so crypto regime mapping informs main regime)
        ("nm_momentum_crypto_intel", "nm_regime", "momentum_context_refresh",
         0.40, "excitatory", "evidence"),
    ]
    for src, tgt, sig, w, pol, etype in edges:
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    delay_ms, min_confidence, enabled, graph_version, gate_config,
                    edge_type, min_source_confidence,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    0, 0.0, TRUE, :gv, NULL,
                    :etype, 0.0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                WHERE EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :src)
                  AND EXISTS (SELECT 1 FROM brain_graph_nodes n WHERE n.id = :tgt)
                  AND NOT EXISTS (
                    SELECT 1 FROM brain_graph_edges e
                    WHERE e.source_node_id = :src AND e.target_node_id = :tgt
                      AND e.signal_type = :sig AND e.graph_version = :gv
                  )
                """
            ),
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "gv": gv, "etype": etype},
        )

    conn.commit()


def _migration_157_trade_mesh_entry_correlation(conn) -> None:
    """Phase 2C integration: add Trade.mesh_entry_correlation_id.

    The neural mesh correlation_id for the entry signal that triggered a trade.
    When the trade closes, plasticity uses this to look up the activation path
    in brain_activation_path_log and reinforce/attenuate the edges that carried
    the signal.
    """
    if "trading_trades" not in _tables(conn):
        conn.commit()
        return
    if "mesh_entry_correlation_id" in _columns(conn, "trading_trades"):
        conn.commit()
        return
    conn.execute(
        text("ALTER TABLE trading_trades ADD COLUMN mesh_entry_correlation_id VARCHAR(64)")
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_trading_trades_mesh_corr "
            "ON trading_trades(mesh_entry_correlation_id)"
        )
    )
    conn.commit()


def _migration_158_plasticity_pre_live_snapshot(conn) -> None:
    """Phase 2C go-live: capture baseline edge weights before plasticity mutates anything.

    Creates brain_graph_edge_weight_baseline with every enabled edge's weight at
    the moment this migration runs. Plasticity reads this baseline for the
    weight-drift circuit breaker (chili_mesh_plasticity_drift_cap). Also writes
    a full JSON snapshot to brain_graph_snapshots for point-in-time rollback.
    """
    import json

    tables = _tables(conn)
    if "brain_graph_edges" not in tables:
        conn.commit()
        return

    # Baseline table: one row per edge with the weight at go-live time.
    if "brain_graph_edge_weight_baseline" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE brain_graph_edge_weight_baseline (
                    edge_id INTEGER PRIMARY KEY REFERENCES brain_graph_edges(id) ON DELETE CASCADE,
                    baseline_weight DOUBLE PRECISION NOT NULL,
                    baseline_min_source_confidence DOUBLE PRECISION NULL,
                    baseline_label VARCHAR(64) NOT NULL DEFAULT 'plasticity_go_live',
                    captured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )

    # Seed baseline from current weights. ON CONFLICT DO NOTHING keeps the first
    # captured weight as the baseline even if this migration were re-run.
    conn.execute(
        text(
            """
            INSERT INTO brain_graph_edge_weight_baseline (
                edge_id, baseline_weight, baseline_min_source_confidence, baseline_label
            )
            SELECT id, weight, min_source_confidence, 'plasticity_go_live'
            FROM brain_graph_edges
            WHERE enabled = TRUE
            ON CONFLICT (edge_id) DO NOTHING
            """
        )
    )

    # Optional full snapshot to brain_graph_snapshots for rollback / audit.
    if "brain_graph_snapshots" in tables:
        rows = conn.execute(
            text(
                """
                SELECT id, source_node_id, target_node_id, signal_type,
                       weight, polarity, edge_type, min_source_confidence
                FROM brain_graph_edges
                WHERE graph_version = 1 AND enabled = TRUE
                """
            )
        ).fetchall()
        snap = {
            "label": "plasticity_go_live",
            "migration": "157",
            "edge_count": len(rows),
            "edges": [
                {
                    "id": int(r[0]),
                    "source": r[1],
                    "target": r[2],
                    "signal_type": r[3],
                    "weight": float(r[4]),
                    "polarity": r[5],
                    "edge_type": r[6],
                    "min_source_confidence": float(r[7] or 0.0),
                }
                for r in rows
            ],
        }
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_snapshots (graph_version, domain, snapshot_json, created_at)
                VALUES (1, 'trading', CAST(:s AS jsonb), CURRENT_TIMESTAMP)
                """
            ),
            {"s": json.dumps(snap)},
        )

    conn.commit()


def _migration_163_cpcv_promotion_gate_evidence(conn) -> None:
    """CPCV / DSR / PBO evidence columns on scan_patterns (Q1.T1 promotion gate)."""
    tables = _tables(conn)
    if "scan_patterns" not in tables:
        conn.commit()
        return
    cols = _columns(conn, "scan_patterns")
    additions = {
        "cpcv_n_paths": "INTEGER",
        "cpcv_median_sharpe": "DOUBLE PRECISION",
        "cpcv_median_sharpe_by_regime": "JSONB",
        "deflated_sharpe": "DOUBLE PRECISION",
        "pbo": "DOUBLE PRECISION",
        "n_effective_trials": "INTEGER",
        "promotion_gate_passed": "BOOLEAN DEFAULT FALSE",
        "promotion_gate_reasons": "JSONB",
    }
    for col_name, col_type in additions.items():
        if col_name not in cols:
            conn.execute(text(f"ALTER TABLE scan_patterns ADD COLUMN {col_name} {col_type}"))
    conn.commit()
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_scan_patterns_promotion_gate_updated "
            "ON scan_patterns (promotion_gate_passed, updated_at)"
        )
    )
    conn.commit()


def _migration_164_cpcv_shadow_eval_log(conn) -> None:
    """Append-only log for CPCV shadow funnel (7d operator view on /brain)."""
    tables = _tables(conn)
    if "scan_patterns" not in tables:
        conn.commit()
        return
    if "cpcv_shadow_eval_log" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE cpcv_shadow_eval_log (
                    id BIGSERIAL PRIMARY KEY,
                    scan_pattern_id INTEGER NOT NULL REFERENCES scan_patterns(id) ON DELETE CASCADE,
                    scanner TEXT NOT NULL,
                    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    would_pass_cpcv BOOLEAN NOT NULL,
                    passed_prior_gates BOOLEAN NOT NULL DEFAULT TRUE,
                    deflated_sharpe DOUBLE PRECISION,
                    pbo DOUBLE PRECISION,
                    cpcv_n_paths INTEGER,
                    pattern_name TEXT,
                    skipped BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_cpcv_shadow_eval_log_evaluated "
                "ON cpcv_shadow_eval_log (evaluated_at DESC)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_cpcv_shadow_eval_log_scanner_time "
                "ON cpcv_shadow_eval_log (scanner, evaluated_at DESC)"
            )
        )
        conn.commit()

    conn.execute(text("DROP VIEW IF EXISTS cpcv_shadow_funnel_v"))
    conn.execute(
        text(
            """
            CREATE VIEW cpcv_shadow_funnel_v AS
            SELECT
                scanner,
                COUNT(*)::bigint AS n_evaluated,
                COUNT(*) FILTER (WHERE would_pass_cpcv)::bigint AS n_would_pass_cpcv,
                COUNT(*) FILTER (WHERE passed_prior_gates)::bigint AS n_actually_passed_existing_gate,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY deflated_sharpe) AS median_dsr,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY pbo) AS median_pbo,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY cpcv_n_paths::double precision)
                    AS median_cpcv_paths
            FROM cpcv_shadow_eval_log
            WHERE evaluated_at >= (CURRENT_TIMESTAMP - INTERVAL '7 days')
              AND NOT skipped
            GROUP BY scanner
            """
        )
    )
    conn.commit()


def _migration_165_regime_snapshot_and_tagging(conn) -> None:
    """Q1.T2: Gaussian HMM regime labels + posteriors; tag trading_snapshots."""
    tables = _tables(conn)
    if "trading_snapshots" not in tables:
        conn.commit()
        return

    if "regime_snapshot" not in tables:
        conn.execute(
            text(
                """
                CREATE TABLE regime_snapshot (
                    as_of TIMESTAMPTZ PRIMARY KEY,
                    regime TEXT NOT NULL CHECK (regime IN ('bull','chop','bear')),
                    posterior JSONB NOT NULL,
                    features JSONB NOT NULL,
                    model_version TEXT NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_regime_snapshot_model_version "
                "ON regime_snapshot (model_version, as_of)"
            )
        )
        conn.commit()

    cols = _columns(conn, "trading_snapshots")
    if "regime" not in cols:
        conn.execute(text("ALTER TABLE trading_snapshots ADD COLUMN regime TEXT"))
    if "regime_posterior" not in cols:
        conn.execute(
            text("ALTER TABLE trading_snapshots ADD COLUMN regime_posterior JSONB")
        )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_trading_snapshot_regime "
            "ON trading_snapshots (regime, bar_start_at) WHERE regime IS NOT NULL"
        )
    )
    conn.commit()


def _migration_166_scan_patterns_promotion_gate_null_default(conn) -> None:
    """CPCV: promotion_gate_passed default NULL; unevaluated rows not FALSE; gate implies CPCV.

    Migration 163 added ``promotion_gate_passed BOOLEAN DEFAULT FALSE``, so rows without CPCV
    evidence looked like gate failures. This clears that ambiguity.

    Rollback (manual):

        ALTER TABLE scan_patterns DROP CONSTRAINT IF EXISTS chk_scan_patterns_promotion_gate_requires_cpcv;
        ALTER TABLE scan_patterns ALTER COLUMN promotion_gate_passed SET DEFAULT FALSE;
    """
    tables = _tables(conn)
    if "scan_patterns" not in tables:
        conn.commit()
        return
    cols = _columns(conn, "scan_patterns")
    if "promotion_gate_passed" not in cols:
        conn.commit()
        return

    conn.execute(
        text(
            """
            UPDATE scan_patterns
            SET promotion_gate_passed = NULL,
                promotion_gate_reasons = NULL
            WHERE cpcv_n_paths IS NULL
              AND (
                promotion_gate_passed IS NOT NULL
                OR promotion_gate_reasons IS NOT NULL
              )
            """
        )
    )
    conn.commit()

    conn.execute(
        text(
            "ALTER TABLE scan_patterns ALTER COLUMN promotion_gate_passed DROP DEFAULT"
        )
    )
    conn.commit()

    row = conn.execute(
        text(
            """
            SELECT 1 FROM pg_constraint c
            JOIN pg_class t ON c.conrelid = t.oid
            WHERE t.relname = 'scan_patterns'
              AND c.conname = 'chk_scan_patterns_promotion_gate_requires_cpcv'
            """
        )
    ).fetchone()
    if not row:
        conn.execute(
            text(
                """
                ALTER TABLE scan_patterns ADD CONSTRAINT chk_scan_patterns_promotion_gate_requires_cpcv
                CHECK (promotion_gate_passed IS NULL OR cpcv_n_paths IS NOT NULL) NOT VALID
                """
            )
        )
        conn.commit()
        conn.execute(
            text(
                "ALTER TABLE scan_patterns VALIDATE CONSTRAINT "
                "chk_scan_patterns_promotion_gate_requires_cpcv"
            )
        )
    conn.commit()


def _migration_167_unified_signals_table(conn) -> None:
    """Q1.T3 phase 1: unified per-scan signal rows (additive; consumers in later PRs).

    Rollback (manual)::

        DROP TABLE IF EXISTS unified_signals;
    """
    if "unified_signals" in _tables(conn):
        conn.commit()
        return
    conn.execute(
        text(
            """
            CREATE TABLE unified_signals (
                signal_id TEXT PRIMARY KEY,
                scanner TEXT NOT NULL,
                strategy_family TEXT NOT NULL,
                pattern_id TEXT NULL,
                symbol TEXT NOT NULL,
                venue TEXT NOT NULL,
                side TEXT NOT NULL,
                horizon TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                entry_price NUMERIC(28, 10) NOT NULL,
                stop_price NUMERIC(28, 10) NOT NULL,
                take_profit_price NUMERIC(28, 10) NULL,
                atr NUMERIC(28, 10) NOT NULL,
                expected_return NUMERIC(28, 10) NOT NULL,
                expected_vol NUMERIC(28, 10) NOT NULL,
                confidence DOUBLE PRECISION NOT NULL,
                deflated_sharpe DOUBLE PRECISION NULL,
                pbo DOUBLE PRECISION NULL,
                regime TEXT NULL,
                regime_posterior JSONB NULL,
                llm_rationale TEXT NULL,
                features JSONB NOT NULL DEFAULT '{}'::jsonb,
                rule_fires JSONB NOT NULL DEFAULT '[]'::jsonb,
                gate_status TEXT NOT NULL,
                gate_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
                CONSTRAINT chk_unified_signals_gate_status CHECK (
                    gate_status IN ('proposed', 'gated_ok', 'gated_reject')
                ),
                CONSTRAINT chk_unified_signals_side CHECK (
                    side IN ('long', 'short', 'flat')
                )
            )
            """
        )
    )
    conn.commit()
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_unified_signals_symbol_created_at "
            "ON unified_signals (symbol, created_at)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_unified_signals_scanner_horizon_gate "
            "ON unified_signals (scanner, horizon, gate_status)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_unified_signals_strategy_family_created_at "
            "ON unified_signals (strategy_family, created_at)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_unified_signals_pattern_id_created_at "
            "ON unified_signals (pattern_id, created_at)"
        )
    )
    conn.commit()


def _migration_168_restore_pattern_1047_cpcv_miscalibration(conn) -> None:
    """Restore pattern 1047 after incorrect CPCV demotion (classifier vs realized PnL).

    Pre–Q1.T1.6 CPCV used a LightGBM classifier on triple-barrier labels, which for
    rule-based patterns can disagree sharply with realized ``trading_pattern_trades``
    outcomes (e.g. 158 trades, positive mean return vs negative median path Sharpe).
    This migration resets lifecycle to **promoted** and clears CPCV gate columns so
    T1.6 realized-PnL CPCV can repopulate evidence.

    Rollback (manual): set ``lifecycle_stage`` back to ``challenged`` and/or rerun
    historical backfill if operators choose to re-apply the old metrics.

    See: Q1.T1.6 ``evaluate_pattern_cpcv_realized_pnl`` and runbook evaluator routing.
    """
    if "scan_patterns" not in _tables(conn):
        conn.commit()
        return
    row = conn.execute(text("SELECT 1 FROM scan_patterns WHERE id = 1047")).fetchone()
    if not row:
        conn.commit()
        return
    conn.execute(
        text(
            """
            UPDATE scan_patterns
            SET lifecycle_stage = 'promoted',
                promotion_gate_passed = NULL,
                promotion_gate_reasons = NULL,
                cpcv_n_paths = NULL,
                cpcv_median_sharpe = NULL,
                cpcv_median_sharpe_by_regime = NULL,
                deflated_sharpe = NULL,
                pbo = NULL,
                n_effective_trials = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1047
            """
        )
    )
    conn.commit()


def _migration_169_scan_patterns_pattern_evidence_kind(conn) -> None:
    """Q1.T1.6: route CPCV — ``realized_pnl`` (trade-return sequence) vs ``ml_signal`` (LightGBM).

    Default ``realized_pnl`` for rule-based patterns; ``ml_signal`` reserved for future
    ML-signal patterns using the legacy triple-barrier + classifier evaluator.

    Rollback (manual)::

        ALTER TABLE scan_patterns DROP CONSTRAINT IF EXISTS chk_scan_patterns_pattern_evidence_kind;
        ALTER TABLE scan_patterns DROP COLUMN IF EXISTS pattern_evidence_kind;
    """
    if "scan_patterns" not in _tables(conn):
        conn.commit()
        return
    cols = _columns(conn, "scan_patterns")
    if "pattern_evidence_kind" not in cols:
        conn.execute(
            text(
                """
                ALTER TABLE scan_patterns ADD COLUMN pattern_evidence_kind TEXT NOT NULL DEFAULT 'realized_pnl'
                """
            )
        )
        conn.commit()
    row = conn.execute(
        text(
            """
            SELECT 1 FROM pg_constraint c
            JOIN pg_class t ON c.conrelid = t.oid
            WHERE t.relname = 'scan_patterns'
              AND c.conname = 'chk_scan_patterns_pattern_evidence_kind'
            """
        )
    ).fetchone()
    if not row:
        conn.execute(
            text(
                """
                ALTER TABLE scan_patterns ADD CONSTRAINT chk_scan_patterns_pattern_evidence_kind
                CHECK (pattern_evidence_kind IN ('realized_pnl', 'ml_signal')) NOT VALID
                """
            )
        )
        conn.commit()
        conn.execute(
            text(
                "ALTER TABLE scan_patterns VALIDATE CONSTRAINT "
                "chk_scan_patterns_pattern_evidence_kind"
            )
        )
    conn.commit()
    conn.execute(
        text(
            """
            UPDATE scan_patterns
            SET pattern_evidence_kind = 'realized_pnl'
            WHERE lifecycle_stage IN ('promoted', 'live')
            """
        )
    )
    conn.commit()


def _migration_170_restore_pattern_1047_n_paths_threshold_second(conn) -> None:
    """Second restoration of pattern 1047 (CPCV gate calibration).

    **First restoration:** migration **168** after ML-signal (classifier) CPCV
    miscalibration vs realized ``trading_pattern_trades``.

    **This migration:** T1.6 backfill demoted 1047 on ``cpcv_n_paths_lt_50`` alone
    (institutional 50-path bar unreachable at ~158 trades). Q1.T1.7 adds graded
    ``n_paths`` thresholds (provisional 20–49 vs full 50+). This reset clears gate
    columns and sets **promoted** so a post-merge backfill can persist provisional
    evidence under the new policy.

    SQL mirrors **168**; rollback is the same manual note as 168.
    """
    if "scan_patterns" not in _tables(conn):
        conn.commit()
        return
    row = conn.execute(text("SELECT 1 FROM scan_patterns WHERE id = 1047")).fetchone()
    if not row:
        conn.commit()
        return
    conn.execute(
        text(
            """
            UPDATE scan_patterns
            SET lifecycle_stage = 'promoted',
                promotion_gate_passed = NULL,
                promotion_gate_reasons = NULL,
                cpcv_n_paths = NULL,
                cpcv_median_sharpe = NULL,
                cpcv_median_sharpe_by_regime = NULL,
                deflated_sharpe = NULL,
                pbo = NULL,
                n_effective_trials = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1047
            """
        )
    )
    conn.commit()


def _migration_171_chili_dispatch_tables(conn) -> None:
    """CHILI Dispatch: autonomous coding loop tables (Phase D.0+).

    See ``app/migrations_proposed/171_chili_dispatch_tables.py`` for design notes.
    Adds ``llm_call_log`` (distillation training set), ``code_agent_runs``
    (cycle audit), ``code_kill_switch_state`` (singleton), ``distillation_runs``
    (fine-tune attempts), ``frozen_scope_paths`` (hard-rule guard). Idempotent
    via ``CREATE TABLE IF NOT EXISTS`` + ``ON CONFLICT DO NOTHING``.
    """
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS llm_call_log (
            id            BIGSERIAL PRIMARY KEY,
            trace_id      TEXT NOT NULL,
            cycle_id      BIGINT,
            provider      TEXT NOT NULL,
            model         TEXT NOT NULL,
            tier          INTEGER NOT NULL,
            purpose       TEXT NOT NULL,
            system_prompt TEXT,
            user_prompt   TEXT NOT NULL,
            completion    TEXT,
            tokens_in     INTEGER,
            tokens_out    INTEGER,
            latency_ms    INTEGER,
            cost_usd      NUMERIC(10, 6),
            success       BOOLEAN,
            weak_response BOOLEAN DEFAULT FALSE,
            failure_kind  TEXT,
            validation_status TEXT,
            distillable   BOOLEAN DEFAULT FALSE,
            created_at    TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS llm_call_log_distillable_idx "
        "ON llm_call_log (distillable, validation_status, created_at)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS llm_call_log_trace_idx "
        "ON llm_call_log (trace_id)"
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS code_agent_runs (
            id                BIGSERIAL PRIMARY KEY,
            started_at        TIMESTAMP NOT NULL DEFAULT NOW(),
            finished_at       TIMESTAMP,
            task_id           BIGINT,
            repo_id           BIGINT,
            cycle_step        TEXT NOT NULL,
            decision          TEXT,
            rule_snapshot     JSONB,
            llm_snapshot      JSONB,
            diff_summary      JSONB,
            validation_run_id BIGINT,
            branch_name       TEXT,
            commit_sha        TEXT,
            merged_to         TEXT,
            escalation_reason TEXT,
            notify_user       BOOLEAN DEFAULT FALSE,
            notified_at       TIMESTAMP
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS code_agent_runs_started_idx "
        "ON code_agent_runs (started_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS code_agent_runs_task_idx "
        "ON code_agent_runs (task_id)"
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS code_kill_switch_state (
            id            INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            active        BOOLEAN NOT NULL DEFAULT FALSE,
            reason        TEXT,
            activated_at  TIMESTAMP,
            activated_by  TEXT,
            consecutive_failures INTEGER DEFAULT 0,
            last_run_id   BIGINT
        )
        """
    ))
    conn.execute(text(
        "INSERT INTO code_kill_switch_state (id, active) "
        "VALUES (1, false) ON CONFLICT (id) DO NOTHING"
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS distillation_runs (
            id                   BIGSERIAL PRIMARY KEY,
            started_at           TIMESTAMP NOT NULL DEFAULT NOW(),
            finished_at          TIMESTAMP,
            base_model           TEXT NOT NULL,
            candidate_tag        TEXT,
            train_rows           INTEGER,
            eval_rows            INTEGER,
            incumbent_pass       NUMERIC(5, 4),
            candidate_pass       NUMERIC(5, 4),
            candidate_latency_ms INTEGER,
            decision             TEXT,
            decision_reason      TEXT,
            artifact_path        TEXT
        )
        """
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS frozen_scope_paths (
            id          SERIAL PRIMARY KEY,
            glob        TEXT NOT NULL UNIQUE,
            severity    TEXT NOT NULL,
            reason      TEXT NOT NULL,
            added_at    TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    seed_rows = [
        ('app/services/trading/**',           'block',           'CLAUDE.md Hard Rules 1-2 (kill switch, drawdown breaker)'),
        ('app/trading_brain/**',              'block',           'CLAUDE.md Hard Rule 5 (prediction mirror authority frozen)'),
        ('app/migrations.py',                 'review_required', 'CLAUDE.md Hard Rule 6 (sequential idempotent migrations)'),
        ('app/services/trading/governance.py','block',           'kill switch and frozen-scope guard logic itself'),
        ('docs/KILL_SWITCH_RUNBOOK.md',       'review_required', 'incident playbook'),
        ('docs/PHASE_ROLLBACK_RUNBOOK.md',    'review_required', 'rollback playbook'),
        ('docs/DRAWDOWN_BREAKER_RUNBOOK.md',  'review_required', 'incident playbook'),
        ('certs/**',                          'block',           'TLS certs'),
        ('.env',                              'block',           'secrets'),
        ('docker-compose.yml',                'review_required', 'production topology'),
    ]
    for glob, severity, reason in seed_rows:
        conn.execute(
            text(
                "INSERT INTO frozen_scope_paths (glob, severity, reason) "
                "VALUES (:glob, :severity, :reason) "
                "ON CONFLICT (glob) DO NOTHING"
            ),
            {"glob": glob, "severity": severity, "reason": reason},
        )
    conn.execute(text(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'coding_tasks') THEN
            IF NOT EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name = 'coding_tasks' AND column_name = 'force_tier'
            ) THEN
              ALTER TABLE coding_tasks ADD COLUMN force_tier INTEGER;
            END IF;
            IF NOT EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_name = 'coding_tasks' AND column_name = 'intended_files'
            ) THEN
              ALTER TABLE coding_tasks ADD COLUMN intended_files JSONB;
            END IF;
          END IF;
        END$$;
        """
    ))
    conn.commit()


def _migration_172_code_brain_neural_tables(conn) -> None:
    """Code Brain neural architecture (mirror of trading brain).

    Replaces the dumb 60s dispatch timer with a reactive, deterministic-first
    routing system. Adds:

      * ``code_patterns`` — mined diff archetypes / templates discovered from
        successful llm_call_log + coding_agent_suggestion rows. The decision
        router uses these to apply tasks WITHOUT calling an LLM when a
        confident pattern matches.
      * ``code_brain_events`` — durable DB-backed event queue. Replaces the
        timer-driven cycle. Watchers enqueue events (new task ready, source
        changed, validation failed, etc.); a single processor claims and
        routes them.
      * ``code_decision_router_log`` — audit row per routing decision
        (which gate path was taken, which template matched, what novelty
        score, what the eventual outcome was, what cost was spent).
      * ``code_brain_runtime_state`` — singleton mode + budget + thresholds.
        Mode in {'reactive', 'legacy_60s', 'paused'}. Daily premium-USD cap
        enforced HERE (not in rule_gate.py which had the cost_usd-NULL bug).

    Idempotent. Safe to re-run.
    """
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS code_patterns (
            id                BIGSERIAL PRIMARY KEY,
            name              TEXT NOT NULL UNIQUE,
            description       TEXT,
            -- Matching signature
            brief_keywords    JSONB,
            file_glob_pattern TEXT,
            diff_archetype    TEXT,
            -- Template body (parameterized unified diff or Jinja-ish)
            template_body     TEXT,
            template_params   JSONB,
            -- Confidence + scoring
            confidence        NUMERIC(5, 4) DEFAULT 0.5,
            success_count     INTEGER DEFAULT 0,
            failure_count     INTEGER DEFAULT 0,
            -- Provenance
            mined_from_llm_call_ids JSONB,
            last_used_at      TIMESTAMP,
            created_at        TIMESTAMP DEFAULT NOW(),
            updated_at        TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS code_patterns_archetype_idx "
        "ON code_patterns (diff_archetype, confidence DESC)"
    ))

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS code_brain_events (
            id            BIGSERIAL PRIMARY KEY,
            event_type    TEXT NOT NULL,
            subject_kind  TEXT,
            subject_id    BIGINT,
            payload       JSONB,
            priority      SMALLINT DEFAULT 5,
            enqueued_at   TIMESTAMP DEFAULT NOW(),
            claimed_at    TIMESTAMP,
            claimed_by    TEXT,
            processed_at  TIMESTAMP,
            outcome       TEXT,
            error_message TEXT
        )
        """
    ))
    # Partial index for fast claim_next() — only unclaimed rows.
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS code_brain_events_unclaimed_idx "
        "ON code_brain_events (priority, enqueued_at) "
        "WHERE claimed_at IS NULL"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS code_brain_events_subject_idx "
        "ON code_brain_events (subject_kind, subject_id)"
    ))

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS code_decision_router_log (
            id                  BIGSERIAL PRIMARY KEY,
            event_id            BIGINT,
            task_id             BIGINT,
            decided_at          TIMESTAMP DEFAULT NOW(),
            decision            TEXT NOT NULL,
            matched_pattern_id  BIGINT,
            pattern_confidence  NUMERIC(5, 4),
            novelty_score       NUMERIC(5, 4),
            rule_snapshot       JSONB,
            -- Filled in when the routed action completes
            completed_at        TIMESTAMP,
            outcome             TEXT,
            cost_usd            NUMERIC(10, 6) DEFAULT 0,
            llm_tokens_used     INTEGER DEFAULT 0
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS code_decision_router_log_task_idx "
        "ON code_decision_router_log (task_id, decided_at DESC)"
    ))

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS code_brain_runtime_state (
            id                          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            mode                        TEXT NOT NULL DEFAULT 'reactive',
            -- Daily budget cap on PREMIUM (paid) LLM calls; resets per day
            daily_premium_usd_cap       NUMERIC(10, 2) DEFAULT 5.00,
            spent_today_usd             NUMERIC(10, 6) DEFAULT 0,
            spend_reset_date            DATE DEFAULT CURRENT_DATE,
            -- Routing thresholds
            template_min_confidence     NUMERIC(5, 4) DEFAULT 0.7,
            novelty_premium_threshold   NUMERIC(5, 4) DEFAULT 0.6,
            -- Distillation state
            local_model_promoted        BOOLEAN DEFAULT FALSE,
            local_model_tag             TEXT,
            last_pattern_mining_at      TIMESTAMP,
            updated_at                  TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    conn.execute(text(
        "INSERT INTO code_brain_runtime_state (id) "
        "VALUES (1) ON CONFLICT (id) DO NOTHING"
    ))
    conn.commit()


def _migration_173_context_brain_tables(conn) -> None:
    """Context Brain (Phase F) — TurboQuant-style retrieve→rank→compress→compose.

    The Context Brain is the third domain brain (after trading and code).
    It owns the LLM context-assembly pipeline for chat: classifies intent,
    pulls candidates from each registered retriever, scores them with
    learned weights, enforces a token budget, optionally distills overflow
    via a cheap model, and composes a final structured prompt.

    Tables:
      * ``context_assembly_log`` — one row per chat assembly. Tracks intent,
        sources used, total tokens, strategy version, and links back to
        the chat_message_id and the resulting llm_call_log row.
      * ``context_candidate_log`` — one row per retriever result inside an
        assembly. Records raw score, final relevance, whether selected,
        token count, and content hash (for distillation cache lookup).
      * ``context_outcome_log`` — feedback layer. Linked to assembly +
        llm_call_log + the user's downstream reaction (followed-up,
        regenerated, dismissed, action succeeded). The learning loop
        consumes this to update weights.
      * ``learned_context_weights`` — global per (intent, source_id)
        weight, learned from outcomes. UNIQUE because we chose global
        learning over per-user (cheaper to converge, easier to validate).
      * ``context_distillation_cache`` — content_hash → distilled_text so
        we never pay the distiller LLM twice for identical inputs.
      * ``context_brain_runtime_state`` — singleton (mode, token budget,
        distillation USD cap, current strategy version).

    Idempotent. Safe to re-run.
    """
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS context_brain_runtime_state (
            id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            mode TEXT NOT NULL DEFAULT 'reactive',
            -- Per-request token budget for the assembled context
            -- (NOT including the response tokens). 8K leaves headroom in
            -- a 32K-context model and is plenty for most chat turns.
            token_budget_per_request INTEGER NOT NULL DEFAULT 8000,
            -- If selected candidates exceed this many tokens, distill
            -- the overflow via the cheap model.
            distillation_threshold_tokens INTEGER NOT NULL DEFAULT 12000,
            -- Daily cap on what the distiller can spend on cheap-LLM
            -- summarization. 0.50 by default to stay miles below the
            -- premium-LLM budget.
            daily_distillation_usd_cap NUMERIC(10, 2) NOT NULL DEFAULT 0.50,
            spent_today_distillation_usd NUMERIC(10, 6) NOT NULL DEFAULT 0,
            spend_reset_date DATE NOT NULL DEFAULT CURRENT_DATE,
            -- Toggles
            learning_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            distillation_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            -- Bumped every time the learning cycle promotes a new weight
            -- set. Lets us A/B-compare assembly strategies offline.
            learned_strategy_version INTEGER NOT NULL DEFAULT 1,
            last_learning_cycle_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    conn.execute(text(
        "INSERT INTO context_brain_runtime_state (id) "
        "VALUES (1) ON CONFLICT (id) DO NOTHING"
    ))

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS context_assembly_log (
            id BIGSERIAL PRIMARY KEY,
            -- The chat_message that triggered this assembly (if known)
            chat_message_id BIGINT,
            user_id INTEGER,
            -- Intent classification result
            intent TEXT,
            intent_confidence NUMERIC(5, 4),
            -- Hash of the raw user query (debug + dedupe)
            query_hash TEXT,
            -- {"rag": 3, "memory": 2, "code_brain": 1, ...}
            sources_used JSONB,
            total_tokens_input INTEGER,
            -- Budget cap that was in effect when this assembled
            budget_token_cap INTEGER,
            budget_used_pct NUMERIC(5, 2),
            -- "v1_naive" | "v1_weighted" | future versions...
            strategy_version INTEGER,
            distilled BOOLEAN DEFAULT FALSE,
            distillation_tokens_saved INTEGER,
            -- Latency of the assembly path itself (NOT the LLM call)
            elapsed_ms INTEGER,
            -- Filled in once the LLM responds
            llm_call_log_id BIGINT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS context_assembly_log_user_idx "
        "ON context_assembly_log (user_id, created_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS context_assembly_log_intent_idx "
        "ON context_assembly_log (intent, created_at DESC)"
    ))

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS context_candidate_log (
            id BIGSERIAL PRIMARY KEY,
            assembly_id BIGINT NOT NULL REFERENCES context_assembly_log(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL,    -- 'rag', 'memory', 'code_brain', etc.
            -- The retriever's own confidence (cosine, distance, freshness, ...)
            raw_score NUMERIC(8, 5),
            -- After applying learned_weight + recency + intent_match
            relevance_score NUMERIC(8, 5),
            final_weight NUMERIC(8, 5),
            -- Did the budget allow this one through?
            selected BOOLEAN DEFAULT FALSE,
            tokens_estimated INTEGER,
            -- For distillation-cache lookups
            content_hash TEXT,
            distilled BOOLEAN DEFAULT FALSE,
            -- A small preview so operators can debug from the UI
            preview TEXT
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS context_candidate_log_assembly_idx "
        "ON context_candidate_log (assembly_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS context_candidate_log_source_idx "
        "ON context_candidate_log (source_id, selected)"
    ))

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS context_outcome_log (
            id BIGSERIAL PRIMARY KEY,
            assembly_id BIGINT REFERENCES context_assembly_log(id) ON DELETE CASCADE,
            llm_call_log_id BIGINT,
            chat_message_id BIGINT,
            action_type TEXT,
            -- All optional / fill in async over time as user reacts
            user_followed_up BOOLEAN,
            user_regenerated BOOLEAN,
            user_edited BOOLEAN,
            user_dismissed BOOLEAN,
            -- Composite signal in [0, 1]; the learner reads this
            quality_signal NUMERIC(5, 4),
            measured_at TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS context_outcome_log_assembly_idx "
        "ON context_outcome_log (assembly_id)"
    ))

    # Global learned weights (per intent × source). Per-user comes later.
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS learned_context_weights (
            id BIGSERIAL PRIMARY KEY,
            intent TEXT NOT NULL,
            source_id TEXT NOT NULL,
            weight NUMERIC(8, 5) NOT NULL DEFAULT 1.0,
            sample_count INTEGER NOT NULL DEFAULT 0,
            last_outcome_quality NUMERIC(5, 4),
            last_updated TIMESTAMP DEFAULT NOW(),
            UNIQUE (intent, source_id)
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS learned_context_weights_intent_idx "
        "ON learned_context_weights (intent)"
    ))

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS context_distillation_cache (
            id BIGSERIAL PRIMARY KEY,
            content_hash TEXT NOT NULL UNIQUE,
            original_tokens INTEGER,
            distilled_tokens INTEGER,
            distilled_text TEXT NOT NULL,
            distiller_model TEXT,
            distiller_cost_usd NUMERIC(10, 6),
            hit_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    conn.commit()


def _migration_174_llm_gateway_decomposition_tables(conn) -> None:
    """Phase F.10 — Universal LLM Gateway + Tree-of-Context decomposition.

    Architecture (validated 2026-04-26 with operator):

        gateway_chat(query, purpose=...) is the SOLE entry point for every
        LLM call in CHILI. The gateway looks up the purpose's policy and
        routes one of three ways:

          * ``passthrough``  — straight to openai_client.chat() (legacy
                               behavior; for trading-brain JSON callers
                               where wrapping would break formatting)
          * ``augmented``    — assemble Context Brain prompt then call
                               (no decomposition, single LLM call)
          * ``tree``         — full pipeline:
                                  decompose query → N parallel Ollama calls
                                  for each chunk → optional cross-examination
                                  → compile chunks → synthesize via gpt-5.5

    The "tree" path keeps 95%+ of LLM cost on local (free) Ollama. Only
    the final synthesis call goes to gpt-5.5. As the learning cycle mines
    successful patterns, we mechanically graduate them from "tree" to
    "passthrough+template" — at which point even the synthesis call goes
    away.

    Tables:

      * ``llm_gateway_log``        — every gateway call (one row per turn).
                                      Source of truth for cost, latency,
                                      routing strategy, success rate.
      * ``decomposition_tree``     — for tree-routed calls: the parent
                                      tree record linking back to the gateway
                                      log row.
      * ``decomposition_chunk``    — per-chunk record inside a tree. Holds
                                      primary + secondary model responses,
                                      similarity score, selected response.
      * ``llm_purpose_policy``     — per-purpose routing config. Operators
                                      flip strategies here without code.
                                      Seeded with sensible defaults.
      * ``context_brain_outcome``  — links a gateway call to user reaction
                                      (followed-up, regenerated, edited)
                                      so the learning cycle can score it.
    """
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS llm_gateway_log (
            id BIGSERIAL PRIMARY KEY,
            purpose TEXT NOT NULL,
            user_id INTEGER,
            chat_message_id BIGINT,
            -- The chosen routing for this call
            routing_strategy TEXT NOT NULL,   -- passthrough | augmented | tree
            -- Tree-path stats (NULL when passthrough/augmented)
            decomposed BOOLEAN DEFAULT FALSE,
            chunk_count INTEGER DEFAULT 0,
            cross_examined BOOLEAN DEFAULT FALSE,
            -- Models used in the various tiers
            primary_local_model TEXT,
            secondary_local_model TEXT,
            synthesizer_model TEXT,
            -- Cost / latency breakdown
            ollama_calls_count INTEGER DEFAULT 0,
            premium_calls_count INTEGER DEFAULT 0,
            ollama_total_tokens INTEGER DEFAULT 0,
            premium_total_tokens INTEGER DEFAULT 0,
            premium_cost_usd NUMERIC(10, 6) DEFAULT 0,
            total_latency_ms INTEGER,
            decompose_latency_ms INTEGER,
            chunk_latency_ms INTEGER,
            compile_latency_ms INTEGER,
            synthesize_latency_ms INTEGER,
            -- Outcome
            success BOOLEAN,
            error_kind TEXT,
            error_message TEXT,
            -- Timestamps
            started_at TIMESTAMP NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMP
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS llm_gateway_log_purpose_idx "
        "ON llm_gateway_log (purpose, started_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS llm_gateway_log_user_idx "
        "ON llm_gateway_log (user_id, started_at DESC)"
    ))

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS decomposition_tree (
            id BIGSERIAL PRIMARY KEY,
            gateway_log_id BIGINT REFERENCES llm_gateway_log(id) ON DELETE CASCADE,
            parent_query TEXT NOT NULL,
            chunk_count INTEGER DEFAULT 0,
            chunks_resolved INTEGER DEFAULT 0,
            chunks_failed INTEGER DEFAULT 0,
            decomposition_strategy TEXT,    -- 'heuristic_passthrough' | 'llm_decompose' | 'cached'
            decomposer_model TEXT,
            compiled_context_tokens INTEGER,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS decomposition_tree_gateway_idx "
        "ON decomposition_tree (gateway_log_id)"
    ))

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS decomposition_chunk (
            id BIGSERIAL PRIMARY KEY,
            tree_id BIGINT NOT NULL REFERENCES decomposition_tree(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            chunk_query TEXT NOT NULL,
            chunk_kind TEXT,                  -- 'fact' | 'reasoning' | 'code' | 'general'
            -- Primary local model response
            primary_model TEXT,
            primary_response TEXT,
            primary_tokens_out INTEGER,
            primary_latency_ms INTEGER,
            -- Optional secondary local model (cross-exam)
            secondary_model TEXT,
            secondary_response TEXT,
            secondary_tokens_out INTEGER,
            secondary_latency_ms INTEGER,
            similarity_score NUMERIC(5, 4),  -- character-level similarity 0..1
            -- After disagreement resolution: which response went forward
            selected_response TEXT,
            selection_reason TEXT,            -- 'agreement' | 'primary_only' | 'referee'
            is_high_stakes BOOLEAN DEFAULT FALSE,
            success BOOLEAN,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS decomposition_chunk_tree_idx "
        "ON decomposition_chunk (tree_id, chunk_index)"
    ))

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS llm_purpose_policy (
            id SERIAL PRIMARY KEY,
            purpose TEXT NOT NULL UNIQUE,
            description TEXT,
            -- Routing decision
            routing_strategy TEXT NOT NULL DEFAULT 'augmented',
            -- Tree-path knobs
            decompose BOOLEAN DEFAULT FALSE,
            cross_examine BOOLEAN DEFAULT FALSE,
            use_premium_synthesis BOOLEAN DEFAULT TRUE,
            high_stakes BOOLEAN DEFAULT FALSE,
            -- Model overrides (NULL = use runtime_state defaults)
            primary_local_model TEXT,
            secondary_local_model TEXT,
            synthesizer_model TEXT,
            -- Tuning
            max_chunks INTEGER DEFAULT 8,
            chunk_timeout_sec INTEGER DEFAULT 30,
            -- Toggle without code change
            enabled BOOLEAN DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """
    ))

    # Seed sensible default policies. Each call site in CHILI should map
    # its purpose to one of these. ON CONFLICT DO NOTHING so re-running is
    # safe and operator edits aren't clobbered.
    seeds = [
        # User-facing chat. Full tree pipeline. High-quality synthesis.
        ("chat_user", "User chat in the chat UI", "tree",
         True, True, True, False),
        # Code dispatch — high stakes (will commit + push). Decomposition
        # gives sharper plans; cross-exam catches hallucinated diffs.
        ("code_dispatch_plan", "Dispatch loop plan step", "tree",
         True, True, True, True),
        ("code_dispatch_edit", "Dispatch loop per-file edit step", "augmented",
         False, True, True, True),
        ("code_dispatch_create", "Dispatch loop create-new-file step", "augmented",
         False, True, True, True),
        # Trading brain — strict JSON output, latency-sensitive. Wrapping
        # would break things. Stays passthrough.
        ("trading_pattern_adjust", "Trading brain pattern adjuster (JSON)", "passthrough",
         False, False, False, True),
        ("trading_reasoning", "Trading brain reasoning calls", "passthrough",
         False, False, False, False),
        # Vision passthrough — image content not amenable to chunked text retrieval
        ("vision_describe", "Vision/image description", "passthrough",
         False, False, False, False),
        # Catch-all
        ("llm_default", "Catch-all for unspecified call sites", "augmented",
         False, False, True, False),
    ]
    for purpose, desc, strategy, decompose, cross, prem_synth, hs in seeds:
        conn.execute(text(
            "INSERT INTO llm_purpose_policy "
            "(purpose, description, routing_strategy, decompose, "
            " cross_examine, use_premium_synthesis, high_stakes) "
            "VALUES (:p, :d, :s, :de, :ce, :ps, :hs) "
            "ON CONFLICT (purpose) DO NOTHING"
        ), {
            "p": purpose, "d": desc, "s": strategy,
            "de": decompose, "ce": cross, "ps": prem_synth, "hs": hs,
        })

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS context_brain_outcome (
            id BIGSERIAL PRIMARY KEY,
            gateway_log_id BIGINT REFERENCES llm_gateway_log(id) ON DELETE CASCADE,
            tree_id BIGINT REFERENCES decomposition_tree(id) ON DELETE SET NULL,
            chat_message_id BIGINT,
            user_followed_up BOOLEAN,
            user_regenerated BOOLEAN,
            user_edited BOOLEAN,
            user_dismissed BOOLEAN,
            quality_signal NUMERIC(5, 4),    -- composite 0..1
            measured_at TIMESTAMP DEFAULT NOW()
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS context_brain_outcome_gateway_idx "
        "ON context_brain_outcome (gateway_log_id)"
    ))
    conn.commit()


def _migration_175_gateway_learning_loop(conn) -> None:
    """Phase F.4-F.6: distiller patterns, policy proposals, outcome enrichment.

    Adds the schema needed for the gateway learning loop:

      * Extends ``context_brain_outcome`` with explicit signals (thumbs,
        outcome_source, raw_signal_json) so we can track multiple kinds of
        feedback (chat followup heuristic, explicit thumbs, code dispatch
        success, trade close PnL).
      * Adds ``gateway_pattern`` — distilled correlations between gateway
        input/strategy and downstream outcomes (the F.4 distiller's output).
      * Adds ``policy_change_proposal`` — proposed adjustments to
        ``llm_purpose_policy`` from the F.6 evolver. Low-stakes changes
        (max_chunks ±1, chunk_timeout ±5s) auto-apply; routing strategy or
        high_stakes flips wait for explicit human approval.
      * Adds ``gateway_learning_run`` — one row per distiller/evolver pass so
        operators can see when the loop last ran and what it touched.
    """
    from sqlalchemy import text

    # 1. Enrich context_brain_outcome.
    conn.execute(text("""
        ALTER TABLE context_brain_outcome
            ADD COLUMN IF NOT EXISTS thumbs_vote SMALLINT,
            ADD COLUMN IF NOT EXISTS outcome_source TEXT,
            ADD COLUMN IF NOT EXISTS raw_signal_json TEXT,
            ADD COLUMN IF NOT EXISTS purpose TEXT,
            ADD COLUMN IF NOT EXISTS user_id INTEGER
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS context_brain_outcome_purpose_idx "
        "ON context_brain_outcome (purpose, measured_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS context_brain_outcome_source_idx "
        "ON context_brain_outcome (outcome_source, measured_at DESC)"
    ))

    # 2. gateway_pattern — distilled correlations.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS gateway_pattern (
            id BIGSERIAL PRIMARY KEY,
            purpose TEXT NOT NULL,
            pattern_kind TEXT NOT NULL,            -- 'strategy_vs_outcome', 'chunks_vs_outcome', etc.
            pattern_key TEXT NOT NULL,             -- e.g. 'tree|chunks=4'
            sample_count INTEGER NOT NULL DEFAULT 0,
            avg_quality NUMERIC(5, 4),             -- 0..1
            success_rate NUMERIC(5, 4),            -- 0..1
            avg_latency_ms NUMERIC(10, 2),
            confidence NUMERIC(5, 4) NOT NULL DEFAULT 0,  -- f(samples, variance)
            description TEXT,
            first_seen_at TIMESTAMP DEFAULT NOW(),
            last_seen_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (purpose, pattern_kind, pattern_key)
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS gateway_pattern_purpose_idx "
        "ON gateway_pattern (purpose, confidence DESC)"
    ))

    # 3. policy_change_proposal — what the evolver wants to tweak.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS policy_change_proposal (
            id BIGSERIAL PRIMARY KEY,
            purpose TEXT NOT NULL,
            field_name TEXT NOT NULL,           -- 'routing_strategy', 'max_chunks', etc.
            current_value TEXT,
            proposed_value TEXT NOT NULL,
            justification TEXT,                 -- distiller pattern reference
            pattern_id BIGINT REFERENCES gateway_pattern(id) ON DELETE SET NULL,
            severity TEXT NOT NULL DEFAULT 'low',  -- 'low' (auto-apply) | 'high' (gate)
            status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | auto_applied
            decided_by TEXT,
            decided_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS policy_change_proposal_status_idx "
        "ON policy_change_proposal (status, created_at DESC)"
    ))

    # 4. gateway_learning_run — operator visibility into the loop itself.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS gateway_learning_run (
            id BIGSERIAL PRIMARY KEY,
            phase TEXT NOT NULL,                -- 'distiller' | 'evolver'
            started_at TIMESTAMP DEFAULT NOW(),
            ended_at TIMESTAMP,
            success BOOLEAN,
            patterns_touched INTEGER DEFAULT 0,
            proposals_created INTEGER DEFAULT 0,
            proposals_auto_applied INTEGER DEFAULT 0,
            error_message TEXT
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS gateway_learning_run_phase_idx "
        "ON gateway_learning_run (phase, started_at DESC)"
    ))

    conn.commit()


def _migration_190_seed_kraken_futures_perp_contracts(conn) -> None:
    """Q2 follow-up — seed perp_contracts with Kraken Futures perpetuals.

    Kraken Futures is the first centralized US-regulated venue in the
    perps lane (Hyperliquid + dYdX v4 are both DEXs). Useful for
    cross-venue funding-rate divergence — when DEX funding diverges
    from a regulated venue, that's signal beyond what either DEX alone
    can show.

    Funding cadence: hourly (matches Hyperliquid + dYdX), so
    funding_interval_hours=1.

    Symbol convention: Kraken uses 'PF_<base>USD' format, with 'XBT'
    instead of 'BTC' for Bitcoin per Kraken's historical convention.
    The seed rows use the literal Kraken symbol so the adapter doesn't
    need to renormalize.

    Idempotent on the existing UNIQUE (symbol, venue) constraint.
    """
    from sqlalchemy import text as _text

    seeds = [
        ("PF_XBTUSD",   "BTC",   "USD"),
        ("PF_ETHUSD",   "ETH",   "USD"),
        ("PF_SOLUSD",   "SOL",   "USD"),
        ("PF_LINKUSD",  "LINK",  "USD"),
        ("PF_AVAXUSD",  "AVAX",  "USD"),
        ("PF_MATICUSD", "MATIC", "USD"),
        ("PF_DOGEUSD",  "DOGE",  "USD"),
        ("PF_LTCUSD",   "LTC",   "USD"),
        ("PF_ATOMUSD",  "ATOM",  "USD"),
        ("PF_ADAUSD",   "ADA",   "USD"),
        ("PF_ARBUSD",   "ARB",   "USD"),
        ("PF_OPUSD",    "OP",    "USD"),
    ]
    for sym, base, quote in seeds:
        conn.execute(_text(
            """
            INSERT INTO perp_contracts
                (symbol, venue, base_ccy, quote_ccy,
                 contract_multiplier, funding_interval_hours,
                 max_leverage, tradable)
            VALUES (:s, 'kraken_futures', :b, :q,
                    1.0, 1, 10.0, TRUE)
            ON CONFLICT (symbol, venue) DO NOTHING
            """
        ), {"s": sym, "b": base, "q": quote})


def _migration_189_seed_dydx_v4_perp_contracts(conn) -> None:
    """Q2 follow-up — seed perp_contracts with dYdX v4 perpetuals.

    Bybit is geo-blocked from US (CloudFront 403, same pattern as
    Binance fapi). dYdX v4 is decentralized + US-accessible via the
    public indexer (``indexer.dydx.trade/v4``), so it's the natural
    third venue alongside Hyperliquid for cross-venue funding/OI
    signals.

    Like Hyperliquid, dYdX v4 is hourly-funding so funding_interval_hours=1.
    Symbol convention: ``BTC-USD`` (matches the spot-pair format), not
    bare ``BTC`` like Hyperliquid. The ingestion adapter normalizes
    output rows so the basis / quote / OI consumers don't care.

    Tier-1 majors only — dYdX v4 has 295 active markets but most are
    long-tail. Cross-venue signal is most valuable on the high-volume
    pairs where we already have Hyperliquid + Binance entries.

    Idempotent on the existing UNIQUE (symbol, venue) constraint.
    """
    from sqlalchemy import text as _text

    seeds = [
        ("BTC-USD",   "BTC",   "USD"),
        ("ETH-USD",   "ETH",   "USD"),
        ("SOL-USD",   "SOL",   "USD"),
        ("LINK-USD",  "LINK",  "USD"),
        ("AVAX-USD",  "AVAX",  "USD"),
        ("MATIC-USD", "MATIC", "USD"),
        ("DOGE-USD",  "DOGE",  "USD"),
        ("LTC-USD",   "LTC",   "USD"),
        ("ATOM-USD",  "ATOM",  "USD"),
        ("ADA-USD",   "ADA",   "USD"),
        ("ARB-USD",   "ARB",   "USD"),
        ("OP-USD",    "OP",    "USD"),
    ]
    for sym, base, quote in seeds:
        conn.execute(_text(
            """
            INSERT INTO perp_contracts
                (symbol, venue, base_ccy, quote_ccy,
                 contract_multiplier, funding_interval_hours,
                 max_leverage, tradable)
            VALUES (:s, 'dydx_v4', :b, :q,
                    1.0, 1, 10.0, TRUE)
            ON CONFLICT (symbol, venue) DO NOTHING
            """
        ), {"s": sym, "b": base, "q": quote})


def _migration_188_pattern_survival_promote_review_queue(conn) -> None:
    """K Phase 3 Step S.7 — promote-gate review queue table.

    Phase 3.C (the promote-gate consumer) holds candidates that passed
    CPCV but have a low predicted survival probability. Per the
    resolved Q1 from the Phase 3 design doc (Task T), the held
    candidates stay at ``lifecycle_stage='candidate'`` and are tracked
    via this dedicated queue rather than introducing a new
    ``lifecycle='review'`` state. Avoids surgery on the
    ``chk_sp_lifecycle`` CHECK constraint that's referenced across the
    trading brain.

    Lifecycle of a row:
      INSERT when CPCV-passes a candidate AND predicted survival
        is below promote_gate_threshold (queued_at = NOW(),
        review_decision = NULL).
      UPDATE when the operator clears the queue: review_decision in
        ('approve' | 'reject'), review_decided_at = NOW(),
        decided_by = '<operator>'.

    The CPCV-promotion path (S.8 wiring) checks this table BEFORE
    promoting: if a row exists with review_decision IS NULL or 'reject',
    the candidate stays at lifecycle='candidate'. Only when a row has
    review_decision='approve' (or no row exists at all) does promotion
    proceed.

    Idempotent — IF NOT EXISTS guard.
    """
    from sqlalchemy import text as _text

    conn.execute(_text(
        """
        CREATE TABLE IF NOT EXISTS pattern_survival_promote_review_queue (
            id BIGSERIAL PRIMARY KEY,
            scan_pattern_id INTEGER NOT NULL,
            queued_at TIMESTAMP NOT NULL DEFAULT NOW(),
            predicted_p DOUBLE PRECISION,
            cpcv_passed_at TIMESTAMP,
            review_decision TEXT,
            review_decided_at TIMESTAMP,
            decided_by TEXT,
            notes TEXT
        )
        """
    ))
    conn.execute(_text(
        "CREATE INDEX IF NOT EXISTS pattern_survival_promote_review_pattern_idx "
        "ON pattern_survival_promote_review_queue (scan_pattern_id, queued_at DESC)"
    ))
    # Pending-only partial index keeps the operator's "what's waiting?"
    # query cheap regardless of historical queue size.
    conn.execute(_text(
        "CREATE INDEX IF NOT EXISTS pattern_survival_promote_review_pending_idx "
        "ON pattern_survival_promote_review_queue (queued_at DESC) "
        "WHERE review_decision IS NULL"
    ))


def _migration_187_pattern_survival_decision_log(conn) -> None:
    """K Phase 3 Step S.1 — decision log table + at-risk streak column.

    The data plane for Phase 3. No consumer reads or writes either yet
    (the consumer wiring lands in S.4 / S.5 / S.8). Shipping the schema
    first so the migration is reviewed independently of the policy
    code.

    Two changes:

      1. ``pattern_survival_decision_log``: one row per decision the
         classifier-driven gate makes (sizing multiplier, demote, or
         promote_gate). Captures predicted_survival, threshold,
         decision (apply / no_op / manual_override), model_version,
         and a per-consumer JSONB blob with the consumer-specific
         details. Indexed by (scan_pattern_id, decided_at) and
         (consumer, decided_at) so the operator can pull "what did
         the gate do for pattern X today" or "what's the gate done
         all week" cheaply.

      2. ``scan_patterns.survival_at_risk_streak_days``: integer
         column counting consecutive days where the pattern's latest
         prediction was below the demote threshold. Updated in S.5 by
         the daily demote pass. NULL-default would force the consumer
         to handle two cases on every read; default 0 keeps the read
         path simple.

    Idempotent — IF NOT EXISTS guards on both the table and the column.
    """
    from sqlalchemy import text as _text

    conn.execute(_text(
        """
        CREATE TABLE IF NOT EXISTS pattern_survival_decision_log (
            id BIGSERIAL PRIMARY KEY,
            scan_pattern_id INTEGER NOT NULL,
            consumer TEXT NOT NULL,
            predicted_survival DOUBLE PRECISION,
            threshold_used DOUBLE PRECISION,
            decision TEXT NOT NULL,
            details JSONB,
            model_version TEXT,
            decided_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """
    ))
    conn.execute(_text(
        "CREATE INDEX IF NOT EXISTS pattern_survival_decision_log_pattern_idx "
        "ON pattern_survival_decision_log (scan_pattern_id, decided_at DESC)"
    ))
    conn.execute(_text(
        "CREATE INDEX IF NOT EXISTS pattern_survival_decision_log_consumer_idx "
        "ON pattern_survival_decision_log (consumer, decided_at DESC)"
    ))

    # Add the streak column on scan_patterns. ALTER ADD COLUMN IF NOT
    # EXISTS is supported on PG 9.6+ which the chili stack uses.
    conn.execute(_text(
        "ALTER TABLE scan_patterns "
        "ADD COLUMN IF NOT EXISTS survival_at_risk_streak_days "
        "INTEGER NOT NULL DEFAULT 0"
    ))


def _migration_186_demote_lifecycle_active_drift(conn) -> None:
    """Q2 Task Q — repair lifecycle vs active drift on never-validated patterns.

    Diagnosis: 17 of 18 patterns with lifecycle_stage='live' had
    active=False, plus last_backtest_at IS NULL, plus trade_count=0,
    plus confidence <= 0.2. They were promoted via the seed path
    (origin='web_discovered') but never validated through the backtest
    pipeline. The scanner only evaluates active=True patterns, so they
    sat silent — taking up a live slot in operator visibility while
    contributing nothing.

    The underlying code-path bug (promotion writes lifecycle_stage but
    not active=True consistently, OR a later demotion path writes
    active=False without dropping lifecycle_stage) is tracked
    separately as tech debt. This migration repairs the existing
    drifted rows so KPI counts are honest going forward.

    Conservative criteria: only repair patterns that meet ALL of:
      * lifecycle_stage='live' AND active=False  (the contradiction)
      * trade_count = 0                          (never produced a trade)
      * last_backtest_at IS NULL                 (never validated)
    Demoted to lifecycle_stage='candidate' so the existing backtest
    queue picks them up. If they pass backtest, the normal promotion
    path will move them back to live with active=True.

    Idempotent. Patterns that have ANY trade history or backtest
    record are left untouched — repairing those requires per-pattern
    operator review, not a bulk migration.
    """
    from sqlalchemy import text as _text

    conn.execute(_text(
        """
        UPDATE scan_patterns
        SET lifecycle_stage = 'candidate',
            lifecycle_changed_at = NOW()
        WHERE lifecycle_stage = 'live'
          AND active = FALSE
          AND COALESCE(trade_count, 0) = 0
          AND last_backtest_at IS NULL
        """
    ))


def _migration_185_backfill_pattern_families(conn) -> None:
    """Q2 Task P — backfill scan_patterns.hypothesis_family by name keyword.

    Diagnosis: KPI's diversity rollup showed pnl_herfindahl=0.92 with
    'unknown' family contributing -$1721 of -$1641 total realized PnL.
    Splitting that bucket showed two distinct sources of "unknown":

      1. 4 active NULL-family patterns whose names clearly indicated
         the strategy family (Bear flag breakdown, Descending triangle
         breakdown, rsi_bullish_divergence variants) but were never
         tagged. Driven by web_discovered seed patterns + variant
         creation paths that didn't propagate family from parent when
         the parent itself was NULL.

      2. 75 broker-sync trades (tags='robinhood-sync') with no
         scan_pattern_id at all. These are operator manual-trades
         mirrored from Robinhood; not a CHILI signal source. The
         brain.py KPI endpoint splits these into a separate
         external_unattributed bucket so they don't pollute the
         signal-diversity Herfindahl.

    This migration handles bucket 1 — name-keyword backfill across all
    NULL/unknown hypothesis_family rows. Idempotent on subsequent runs
    (only rewrites NULL/unknown values; preserves explicit tags).

    The priority order matches app/services/trading/pattern_family_backfill.py:
    specific oscillator / continuation signals before generic breakout/
    reversal so RSI divergence reversals don't get tagged as breakouts.
    """
    from sqlalchemy import text as _text

    conn.execute(_text(
        """
        UPDATE scan_patterns
        SET hypothesis_family = (
            CASE
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       LIKE '%divergence%'
                THEN 'mean_reversion'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       ~ 'rsi_oversold|rsi_overbought|vwap_revert|vwap_reclaim|oversold|overbought'
                THEN 'mean_reversion'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       ~ 'squeeze|compression|triangle|flag|pennant|wedge'
                THEN 'compression_expansion'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       ~ 'liquidity_sweep|breakdown'
                THEN 'liquidity_sweep'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       ~ 'opening_range|orb_'
                THEN 'opening_range'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       ~ 'gap_fill|gap_and_go|gap_continuation|ema_stack|ema_cross|breakout'
                THEN 'momentum_continuation'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       LIKE '%reversal%'
                THEN 'mean_reversion'
                ELSE NULL
            END
        )
        WHERE (hypothesis_family IS NULL OR hypothesis_family = 'unknown')
          AND (CASE
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       LIKE '%divergence%'
                THEN 'mean_reversion'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       ~ 'rsi_oversold|rsi_overbought|vwap_revert|vwap_reclaim|oversold|overbought'
                THEN 'mean_reversion'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       ~ 'squeeze|compression|triangle|flag|pennant|wedge'
                THEN 'compression_expansion'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       ~ 'liquidity_sweep|breakdown'
                THEN 'liquidity_sweep'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       ~ 'opening_range|orb_'
                THEN 'opening_range'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       ~ 'gap_fill|gap_and_go|gap_continuation|ema_stack|ema_cross|breakout'
                THEN 'momentum_continuation'
                WHEN LOWER(name || ' ' || COALESCE(description, ''))
                       LIKE '%reversal%'
                THEN 'mean_reversion'
                ELSE NULL
              END) IS NOT NULL
        """
    ))


def _migration_184_seed_hyperliquid_perp_contracts(conn) -> None:
    """Q2 Task M — seed perp_contracts with Hyperliquid rows.

    Hyperliquid is geo-unrestricted (Binance fapi 451's from US, see
    project memory ``project_binance_geoblock``). Adding it as a
    second venue lets the perps lane actually accumulate funding /
    OI / basis data without operator manual proxy work.

    Hyperliquid has 230+ tradable coins; we seed the major liquid
    set so the ingestion pass doesn't hammer with no payoff. New rows
    use ``funding_interval_hours = 1`` because Hyperliquid funds
    hourly, not 8-hourly like Binance — the features.py annualizer
    multiplies by ``intervals_per_day = 24 / funding_interval_hours``
    so the right APY falls out automatically.

    Idempotent — INSERT ... ON CONFLICT DO NOTHING on the existing
    UNIQUE (symbol, venue) constraint.
    """
    from sqlalchemy import text as _text

    # Tier-1 majors first; can expand later via a follow-up migration.
    seeds = [
        ("BTC",   "BTC",   "USD"),
        ("ETH",   "ETH",   "USD"),
        ("SOL",   "SOL",   "USD"),
        ("BNB",   "BNB",   "USD"),
        ("XRP",   "XRP",   "USD"),
        ("AVAX",  "AVAX",  "USD"),
        ("LINK",  "LINK",  "USD"),
        ("DOGE",  "DOGE",  "USD"),
        ("MATIC", "MATIC", "USD"),
        ("ARB",   "ARB",   "USD"),
        ("OP",    "OP",    "USD"),
        ("LTC",   "LTC",   "USD"),
        ("ATOM",  "ATOM",  "USD"),
        ("APT",   "APT",   "USD"),
        ("INJ",   "INJ",   "USD"),
    ]
    for sym, base, quote in seeds:
        conn.execute(_text(
            """
            INSERT INTO perp_contracts
                (symbol, venue, base_ccy, quote_ccy,
                 contract_multiplier, funding_interval_hours,
                 max_leverage, tradable)
            VALUES (:s, 'hyperliquid', :b, :q,
                    1.0, 1, 10.0, TRUE)
            ON CONFLICT (symbol, venue) DO NOTHING
            """
        ), {"s": sym, "b": base, "q": quote})


def _migration_183_pattern_survival_meta_classifier(conn) -> None:
    """Q2 Task K — meta-classifier scaffold for pattern survival prediction.

    This migration scaffolds the data plane for an offline supervised model
    that predicts whether a candidate / promoted ScanPattern will survive
    the next 30 days of live exposure (continue trading + maintain positive
    expectancy + not get demoted by drift). Today the lifecycle treats every
    pattern symmetrically — promoted → live → maybe demoted later. The
    meta-classifier gives us a *forward-looking* probability so the brain
    can de-risk pre-emptively instead of waiting for the realized loss.

    Two tables:

      * ``pattern_survival_features`` — point-in-time feature snapshots,
        keyed (scan_pattern_id, snapshot_date). Features include the
        pattern's recent hit_rate, expectancy, age_days, regime affinity,
        CPCV promotion confidence, recent live PnL slope, and family
        diversity context. One row per (pattern, day) — small table.

      * ``pattern_survival_predictions`` — per-snapshot probability output
        from the trained classifier, plus the ground-truth label backfilled
        30 days later. ``label_resolved_at`` IS NULL until the 30d window
        closes; the trainer joins live to features to update labels.

    Both tables are flag-gated by chili_pattern_survival_classifier_enabled
    (default OFF). Reads always work; writes only happen when the flag is
    on. Phase 1 ships as feature collection only — no model training, no
    decisions wired anywhere. Phase 2 (separate task) trains LightGBM,
    Phase 3 wires the score into the demotion / sizing decision.
    """
    from sqlalchemy import text as _text

    conn.execute(_text(
        """
        CREATE TABLE IF NOT EXISTS pattern_survival_features (
            id BIGSERIAL PRIMARY KEY,
            scan_pattern_id INTEGER NOT NULL,
            snapshot_date DATE NOT NULL,
            -- Pattern lifecycle context
            pattern_lifecycle TEXT,            -- candidate | live | challenged | demoted
            age_days INTEGER,
            promoted_at TIMESTAMP,
            -- Recent realized performance (rolling 30d)
            trades_30d INTEGER,
            hit_rate_30d DOUBLE PRECISION,
            expectancy_30d_pct DOUBLE PRECISION,
            sharpe_30d DOUBLE PRECISION,
            max_drawdown_30d_pct DOUBLE PRECISION,
            pnl_slope_14d DOUBLE PRECISION,    -- linear regression slope of cum PnL over last 14d
            -- CPCV evidence
            cpcv_dsr DOUBLE PRECISION,
            cpcv_pbo DOUBLE PRECISION,
            cpcv_n_paths INTEGER,
            cpcv_promotion_confident BOOLEAN,
            -- Regime / diversity
            regime_at_snapshot TEXT,
            family_concentration_herfindahl DOUBLE PRECISION,
            family_active_count INTEGER,
            -- Drift signals
            feature_psi_max DOUBLE PRECISION,  -- max PSI across input features
            recert_overdue BOOLEAN,
            -- Free-form additional features
            features_json JSONB,
            CONSTRAINT pattern_survival_features_unique
                UNIQUE (scan_pattern_id, snapshot_date)
        );
        CREATE INDEX IF NOT EXISTS pattern_survival_features_pattern_idx
            ON pattern_survival_features (scan_pattern_id, snapshot_date DESC);
        CREATE INDEX IF NOT EXISTS pattern_survival_features_date_idx
            ON pattern_survival_features (snapshot_date DESC);
        """
    ))

    conn.execute(_text(
        """
        CREATE TABLE IF NOT EXISTS pattern_survival_predictions (
            id BIGSERIAL PRIMARY KEY,
            feature_id BIGINT NOT NULL REFERENCES pattern_survival_features(id)
                ON DELETE CASCADE,
            scan_pattern_id INTEGER NOT NULL,
            snapshot_date DATE NOT NULL,
            -- Model identity
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            trained_at TIMESTAMP,
            -- Prediction (0..1: probability the pattern survives the next 30d)
            survival_probability DOUBLE PRECISION NOT NULL,
            decision_threshold DOUBLE PRECISION,
            predicted_label BOOLEAN,
            -- Backfilled outcome (NULL until horizon closes)
            label_horizon_days INTEGER NOT NULL DEFAULT 30,
            label_resolved_at TIMESTAMP,
            actual_survived BOOLEAN,
            actual_demote_reason TEXT,
            -- Diagnostics
            shap_top_features JSONB,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS pattern_survival_predictions_pattern_idx
            ON pattern_survival_predictions (scan_pattern_id, snapshot_date DESC);
        CREATE INDEX IF NOT EXISTS pattern_survival_predictions_unresolved_idx
            ON pattern_survival_predictions (label_resolved_at)
            WHERE label_resolved_at IS NULL;
        CREATE INDEX IF NOT EXISTS pattern_survival_predictions_model_idx
            ON pattern_survival_predictions (model_name, model_version);
        """
    ))


def _migration_182_perps_lane_scaffold(conn) -> None:
    """Q2.T3 — crypto perps + funding lane scaffold.

    Foundation tables for perpetual futures trading on crypto exchanges:

      * ``perp_contracts`` — registry of tradable perp symbols across
        venues (Binance, Bybit). Tracks tick size, contract multiplier,
        funding interval, mark-vs-index spread cap.
      * ``perp_quotes`` — bid/ask snapshots per contract.
      * ``perp_funding`` — 8-hour funding rate snapshots (positive =
        longs pay shorts, negative = shorts pay longs).
      * ``perp_oi`` — open interest snapshots (USD notional).
      * ``perp_basis`` — perp price minus underlying spot, in bps.
        Spot drives via existing trading_snapshots; this records the
        spread.
      * ``perp_position`` — open positions, similar shape to fx_position
        but with funding accruals.

    Flag: ``CHILI_PERPS_LANE_ENABLED=False`` (default).
    Live: ``CHILI_PERPS_LANE_LIVE=False`` (default).
    """
    from sqlalchemy import text

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS perp_contracts (
            id BIGSERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,              -- e.g. 'BTCUSDT', 'ETHUSDT'
            venue TEXT NOT NULL,               -- 'binance' | 'bybit'
            base_ccy TEXT NOT NULL,
            quote_ccy TEXT NOT NULL,
            contract_multiplier NUMERIC(14, 6) NOT NULL DEFAULT 1.0,
            tick_size NUMERIC(14, 8),
            funding_interval_hours INTEGER NOT NULL DEFAULT 8,
            max_leverage NUMERIC(8, 2) NOT NULL DEFAULT 10.0,
            tradable BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (symbol, venue)
        )
    """))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS perp_quotes (
            id BIGSERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            venue TEXT NOT NULL,
            ts TIMESTAMP NOT NULL DEFAULT NOW(),
            mark_price NUMERIC(20, 8),
            index_price NUMERIC(20, 8),
            bid NUMERIC(20, 8),
            ask NUMERIC(20, 8),
            spread_bps NUMERIC(10, 4)
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_perp_quotes_sym_ts "
        "ON perp_quotes (symbol, venue, ts DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS perp_funding (
            id BIGSERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            venue TEXT NOT NULL,
            funding_time TIMESTAMP NOT NULL,
            funding_rate NUMERIC(14, 8) NOT NULL,  -- e.g. 0.0001 = 0.01% per 8h
            mark_at_funding NUMERIC(20, 8),
            ingested_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (symbol, venue, funding_time)
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_perp_funding_time "
        "ON perp_funding (symbol, venue, funding_time DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS perp_oi (
            id BIGSERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            venue TEXT NOT NULL,
            ts TIMESTAMP NOT NULL DEFAULT NOW(),
            open_interest NUMERIC(24, 4),       -- in contracts
            open_interest_usd NUMERIC(24, 2),
            UNIQUE (symbol, venue, ts)
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_perp_oi_sym_ts "
        "ON perp_oi (symbol, venue, ts DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS perp_basis (
            id BIGSERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            venue TEXT NOT NULL,
            ts TIMESTAMP NOT NULL DEFAULT NOW(),
            perp_price NUMERIC(20, 8),
            spot_price NUMERIC(20, 8),
            basis_bps NUMERIC(10, 4),           -- (perp - spot) / spot * 10000
            UNIQUE (symbol, venue, ts)
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_perp_basis_sym_ts "
        "ON perp_basis (symbol, venue, ts DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS perp_position (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER,
            symbol TEXT NOT NULL,
            venue TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('long', 'short')),
            contracts NUMERIC(20, 8) NOT NULL,
            entry_price NUMERIC(20, 8) NOT NULL,
            mark_at_entry NUMERIC(20, 8),
            stop_price NUMERIC(20, 8),
            take_profit_price NUMERIC(20, 8),
            opened_at TIMESTAMP DEFAULT NOW(),
            closed_at TIMESTAMP,
            close_price NUMERIC(20, 8),
            close_reason TEXT,
            realized_pnl_usd NUMERIC(20, 2),
            funding_accrued_usd NUMERIC(20, 4) DEFAULT 0,
            strategy_family TEXT,
            is_paper BOOLEAN NOT NULL DEFAULT TRUE
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_perp_position_user_open "
        "ON perp_position (user_id, closed_at) "
        "WHERE closed_at IS NULL"
    ))

    # Seed the major perpetual contracts on Binance.
    conn.execute(text("""
        INSERT INTO perp_contracts (symbol, venue, base_ccy, quote_ccy)
        VALUES
            ('BTCUSDT',   'binance', 'BTC',   'USDT'),
            ('ETHUSDT',   'binance', 'ETH',   'USDT'),
            ('SOLUSDT',   'binance', 'SOL',   'USDT'),
            ('BNBUSDT',   'binance', 'BNB',   'USDT'),
            ('XRPUSDT',   'binance', 'XRP',   'USDT'),
            ('AVAXUSDT',  'binance', 'AVAX',  'USDT'),
            ('LINKUSDT',  'binance', 'LINK',  'USDT'),
            ('MATICUSDT', 'binance', 'MATIC', 'USDT'),
            ('DOGEUSDT',  'binance', 'DOGE',  'USDT')
        ON CONFLICT (symbol, venue) DO NOTHING
    """))

    conn.commit()


def _migration_181_forex_lane_scaffold(conn) -> None:
    """Q2.T2 — forex lane scaffold (OANDA-first).

    Foundation tables for FX trading:

      * ``fx_pairs`` — registry of tradable currency pairs with pip
        size, swap rates (long/short), session-active flag, leverage
        cap.
      * ``fx_quotes`` — tick-level bid/ask plus session tag at the
        time of quote (Sydney / Tokyo / London / NY / overlap).
      * ``fx_economic_calendar`` — upcoming + recent macro events
        ingested from Trading Economics or ForexFactory; used for
        news-blackout windows and news-fade entries.
      * ``fx_cot`` — weekly CFTC Commitments of Traders for
        non-commercial positioning z-scores.
      * ``fx_position`` — open FX positions (managed separately from
        ``trading_trades`` because of pip math + swap accruals).

    Default flag: ``CHILI_FOREX_LANE_ENABLED=False``. When ON, paper-
    only by default (``CHILI_FOREX_LANE_LIVE=False``). Hard 10:1
    effective-leverage cap regardless of broker allowance.
    """
    from sqlalchemy import text

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS fx_pairs (
            id BIGSERIAL PRIMARY KEY,
            pair TEXT NOT NULL UNIQUE,         -- e.g. 'EUR_USD', 'USD_JPY'
            base_ccy TEXT NOT NULL,
            quote_ccy TEXT NOT NULL,
            pip_size NUMERIC(12, 8) NOT NULL DEFAULT 0.0001,
            pip_decimals SMALLINT NOT NULL DEFAULT 4,
            min_trade_units INTEGER NOT NULL DEFAULT 1,
            max_leverage NUMERIC(8, 2) NOT NULL DEFAULT 10.0,
            swap_long_pips NUMERIC(10, 4),     -- triple on Wednesday
            swap_short_pips NUMERIC(10, 4),
            sessions_active TEXT[] DEFAULT
              ARRAY['sydney','tokyo','london','ny']::TEXT[],
            tradable BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_fx_pairs_tradable "
        "ON fx_pairs (tradable, pair)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS fx_quotes (
            id BIGSERIAL PRIMARY KEY,
            pair TEXT NOT NULL,
            ts TIMESTAMP NOT NULL DEFAULT NOW(),
            session_tag TEXT,                  -- sydney|tokyo|london|ny|tokyo_london|london_ny
            bid NUMERIC(14, 6) NOT NULL,
            ask NUMERIC(14, 6) NOT NULL,
            spread_pips NUMERIC(8, 2),
            venue TEXT NOT NULL DEFAULT 'oanda'
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_fx_quotes_pair_ts "
        "ON fx_quotes (pair, ts DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS fx_economic_calendar (
            id BIGSERIAL PRIMARY KEY,
            event_id TEXT,                     -- vendor-specific id
            ccy TEXT NOT NULL,                 -- e.g. 'USD', 'EUR'
            event_name TEXT NOT NULL,
            scheduled_at TIMESTAMP NOT NULL,
            importance SMALLINT,               -- 1-3 (3 = high impact)
            actual NUMERIC(20, 6),
            forecast NUMERIC(20, 6),
            previous NUMERIC(20, 6),
            source TEXT,                       -- 'trading_economics' | 'forex_factory'
            ingested_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (event_id, source)
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_fx_economic_calendar_scheduled "
        "ON fx_economic_calendar (scheduled_at, importance DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS fx_cot (
            id BIGSERIAL PRIMARY KEY,
            ccy TEXT NOT NULL,                 -- 'EUR', 'JPY', 'GBP', etc.
            report_date DATE NOT NULL,
            non_commercial_long INTEGER,
            non_commercial_short INTEGER,
            non_commercial_net INTEGER,
            non_commercial_z_score NUMERIC(10, 4),  -- vs 1y window
            commercial_long INTEGER,
            commercial_short INTEGER,
            ingested_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (ccy, report_date)
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_fx_cot_ccy_date "
        "ON fx_cot (ccy, report_date DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS fx_position (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER,
            pair TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('long', 'short')),
            units INTEGER NOT NULL,
            entry_price NUMERIC(14, 6) NOT NULL,
            entry_session TEXT,
            stop_price NUMERIC(14, 6),
            take_profit_price NUMERIC(14, 6),
            opened_at TIMESTAMP DEFAULT NOW(),
            closed_at TIMESTAMP,
            close_price NUMERIC(14, 6),
            close_reason TEXT,                 -- stop|target|manual|news_blackout|swap_purge
            realized_pnl_usd NUMERIC(14, 2),
            swap_accrued_usd NUMERIC(14, 4),
            strategy_family TEXT,
            is_paper BOOLEAN NOT NULL DEFAULT TRUE,
            venue TEXT NOT NULL DEFAULT 'oanda'
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_fx_position_user_open "
        "ON fx_position (user_id, closed_at) "
        "WHERE closed_at IS NULL"
    ))

    # Seed the major + minor FX pair list so OANDA adapter has somewhere to point.
    conn.execute(text("""
        INSERT INTO fx_pairs (pair, base_ccy, quote_ccy, pip_size, pip_decimals)
        VALUES
            ('EUR_USD', 'EUR', 'USD', 0.0001, 4),
            ('GBP_USD', 'GBP', 'USD', 0.0001, 4),
            ('USD_JPY', 'USD', 'JPY', 0.01,   2),
            ('USD_CHF', 'USD', 'CHF', 0.0001, 4),
            ('AUD_USD', 'AUD', 'USD', 0.0001, 4),
            ('USD_CAD', 'USD', 'CAD', 0.0001, 4),
            ('NZD_USD', 'NZD', 'USD', 0.0001, 4),
            ('EUR_GBP', 'EUR', 'GBP', 0.0001, 4),
            ('EUR_JPY', 'EUR', 'JPY', 0.01,   2),
            ('GBP_JPY', 'GBP', 'JPY', 0.01,   2)
        ON CONFLICT (pair) DO NOTHING
    """))

    conn.commit()


def _migration_180_options_lane_scaffold(conn) -> None:
    """Q2.T1 — options lane scaffold.

    Foundation tables for options trading:

      * ``options_chains`` — full option chain snapshots per (underlying, as_of).
        Contains every contract present in the chain at the snapshot time.
      * ``options_quotes`` — quote-level data per contract (bid/ask/last/IV/greeks).
        Rows are append-only; one per snapshot.
      * ``options_flow`` — unusual-options-activity feed (volume vs OI, sweeps,
        block trades) typically sourced from a third-party API.
      * ``options_strategy_proposal`` — recommended multi-leg constructs the
        brain considers (covered_call / cash_secured_put / vertical_spread /
        iron_condor).
      * ``options_position`` — open option positions (managed separately from
        ``trading_trades``; closed when assigned, expired, or closed manually).
      * ``options_greeks_budget`` — portfolio-level greeks limits per user
        (max_abs_delta, max_abs_gamma, max_vega_per_tenor). Hard rules
        consulted before any new option trade.

    Default flag: ``CHILI_OPTIONS_LANE_ENABLED=False``. When OFF, no options
    code paths execute and these tables sit empty. When ON, paper-only by
    default (``CHILI_OPTIONS_LANE_LIVE=False``).
    """
    from sqlalchemy import text

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS options_chains (
            id BIGSERIAL PRIMARY KEY,
            underlying TEXT NOT NULL,
            as_of TIMESTAMP NOT NULL DEFAULT NOW(),
            venue TEXT NOT NULL DEFAULT 'tradier',
            expirations_json JSONB,             -- list of expiration dates
            n_contracts INTEGER,
            spot_price NUMERIC(14, 4),
            UNIQUE (underlying, as_of, venue)
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_options_chains_underlying_asof "
        "ON options_chains (underlying, as_of DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS options_quotes (
            id BIGSERIAL PRIMARY KEY,
            chain_id BIGINT REFERENCES options_chains(id) ON DELETE CASCADE,
            occ_symbol TEXT NOT NULL,           -- standardized OCC symbol
            underlying TEXT NOT NULL,
            expiration DATE NOT NULL,
            strike NUMERIC(14, 4) NOT NULL,
            opt_type TEXT NOT NULL CHECK (opt_type IN ('call', 'put')),
            bid NUMERIC(14, 4),
            ask NUMERIC(14, 4),
            last NUMERIC(14, 4),
            volume INTEGER,
            open_interest INTEGER,
            implied_vol NUMERIC(8, 6),
            delta NUMERIC(8, 6),
            gamma NUMERIC(10, 8),
            theta NUMERIC(10, 6),
            vega NUMERIC(10, 6),
            rho NUMERIC(10, 6),
            recorded_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_options_quotes_chain "
        "ON options_quotes (chain_id, expiration, strike, opt_type)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_options_quotes_occ "
        "ON options_quotes (occ_symbol, recorded_at DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS options_flow (
            id BIGSERIAL PRIMARY KEY,
            underlying TEXT NOT NULL,
            occ_symbol TEXT,
            flow_kind TEXT,                     -- 'sweep' | 'block' | 'volume_spike' | etc.
            premium_usd NUMERIC(14, 2),
            sentiment TEXT,                     -- 'bullish' | 'bearish' | 'neutral'
            details JSONB,
            source TEXT,                        -- 'unusual_whales' | 'cboe' | etc.
            recorded_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_options_flow_underlying "
        "ON options_flow (underlying, recorded_at DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS options_strategy_proposal (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER,
            underlying TEXT NOT NULL,
            strategy_family TEXT NOT NULL,      -- 'covered_call' | 'cash_secured_put'
                                                -- | 'vertical_spread' | 'iron_condor'
            legs_json JSONB NOT NULL,           -- list of {occ_symbol, qty, side}
            net_debit NUMERIC(14, 4),
            net_credit NUMERIC(14, 4),
            max_loss NUMERIC(14, 4),
            max_profit NUMERIC(14, 4),
            breakevens NUMERIC(14, 4)[],
            net_delta NUMERIC(8, 6),
            net_gamma NUMERIC(10, 8),
            net_theta NUMERIC(10, 6),
            net_vega NUMERIC(10, 6),
            confidence NUMERIC(5, 4),
            rationale TEXT,
            status TEXT NOT NULL DEFAULT 'proposed',  -- proposed|accepted|rejected|placed
            decided_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_options_strategy_proposal_user "
        "ON options_strategy_proposal (user_id, created_at DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS options_position (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER,
            underlying TEXT NOT NULL,
            strategy_family TEXT,
            proposal_id BIGINT REFERENCES options_strategy_proposal(id) ON DELETE SET NULL,
            legs_json JSONB NOT NULL,
            net_debit NUMERIC(14, 4),
            net_credit NUMERIC(14, 4),
            opened_at TIMESTAMP DEFAULT NOW(),
            closed_at TIMESTAMP,
            close_reason TEXT,                  -- 'expired'|'assigned'|'manual'|'stop'
            realized_pnl_usd NUMERIC(14, 2),
            is_paper BOOLEAN NOT NULL DEFAULT TRUE,
            venue TEXT NOT NULL DEFAULT 'tradier'
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_options_position_user_open "
        "ON options_position (user_id, closed_at) "
        "WHERE closed_at IS NULL"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS options_greeks_budget (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE,
            max_abs_delta NUMERIC(10, 4) NOT NULL DEFAULT 0.50,
            max_abs_gamma NUMERIC(10, 6) NOT NULL DEFAULT 0.05,
            max_vega_per_tenor JSONB,           -- {'30d': 100, '60d': 80, '90d': 60}
            max_total_vega NUMERIC(10, 4) NOT NULL DEFAULT 200.0,
            max_theta_burn_per_day NUMERIC(10, 4),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))

    conn.commit()


def _migration_179_portfolio_sizing_log(conn) -> None:
    """Q1.T5 — HRP portfolio sizing log.

    Adds ``portfolio_sizing_log`` for tracking every sizing decision the
    HRP allocator makes. Each row records the symbol, the per-trade-2%
    naive sizing result, the HRP-allocated sizing result, the active
    position covariance summary, and the chosen sizing (which may be
    naive when flag is off and HRP when on).

    Operator can compare the two columns over time to assess whether
    HRP is producing meaningfully different sizing than naive — and
    whether that difference correlates with realized PnL improvement.
    """
    from sqlalchemy import text

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS portfolio_sizing_log (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER,
            symbol TEXT NOT NULL,
            decision_at TIMESTAMP DEFAULT NOW(),
            account_equity_usd NUMERIC(14, 2),
            naive_size_usd NUMERIC(14, 2),
            hrp_size_usd NUMERIC(14, 2),
            hrp_weight NUMERIC(8, 6),
            chosen_sizing TEXT NOT NULL,        -- 'naive' | 'hrp'
            n_active_positions INTEGER,
            cov_condition_number NUMERIC(14, 4),
            hrp_cluster_label TEXT,
            meta JSONB
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_portfolio_sizing_log_symbol "
        "ON portfolio_sizing_log (symbol, decision_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_portfolio_sizing_log_chosen "
        "ON portfolio_sizing_log (chosen_sizing, decision_at DESC)"
    ))
    conn.commit()


def _migration_178_strategy_parameter_adaptive(conn) -> None:
    """Q1.T4 — adaptive strategy parameter learning.

    Adds:

      * ``strategy_parameter`` — registry of every parameter the brain
        adapts. One row per (strategy_family, parameter_key, scope).
        ``current_value`` is what live code reads. ``min_value`` and
        ``max_value`` define the search space. ``learning_state`` is a
        JSON blob holding the Bayesian posterior (mean, variance, n,
        last_updated). ``locked`` blocks any update — used to freeze a
        parameter that's been mis-trained.

      * ``strategy_parameter_outcome`` — every observation fed into the
        learner. ``parameter_id`` references the parameter; ``trade_id``
        and ``pattern_id`` are optional links back to the originating
        decision; ``outcome_score`` is normalized to [0, 1] (1 =
        successful, 0 = failed); ``meta`` is JSON for any other context.

      * ``strategy_parameter_proposal`` — the learner's proposed updates
        to ``current_value``. Mirrors the ``policy_change_proposal``
        pattern: low-confidence proposals are auto-applied, high-stakes
        ones wait for operator approval.

    Flag-gated: ``CHILI_STRATEGY_PARAMETER_LEARNING_ENABLED`` (default
    OFF). When OFF, code may still READ from the table for parameter
    values, but the learner does not write proposals or update values.
    """
    from sqlalchemy import text

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS strategy_parameter (
            id BIGSERIAL PRIMARY KEY,
            strategy_family TEXT NOT NULL,
            parameter_key TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',  -- 'global' | 'per_symbol' | 'per_regime'
            scope_value TEXT,                      -- e.g. ticker symbol or regime label
            current_value DOUBLE PRECISION NOT NULL,
            initial_value DOUBLE PRECISION NOT NULL,
            min_value DOUBLE PRECISION,
            max_value DOUBLE PRECISION,
            param_type TEXT NOT NULL DEFAULT 'float',  -- 'float' | 'int' | 'bool'
            learning_state JSONB,
            locked BOOLEAN NOT NULL DEFAULT FALSE,
            description TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (strategy_family, parameter_key, scope, scope_value)
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_strategy_parameter_family_key "
        "ON strategy_parameter (strategy_family, parameter_key)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS strategy_parameter_outcome (
            id BIGSERIAL PRIMARY KEY,
            parameter_id BIGINT NOT NULL REFERENCES strategy_parameter(id) ON DELETE CASCADE,
            value_used DOUBLE PRECISION NOT NULL,
            outcome_score NUMERIC(5, 4) NOT NULL,  -- 0..1
            trade_id BIGINT,
            pattern_id BIGINT,
            meta JSONB,
            recorded_at TIMESTAMP DEFAULT NOW()
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_strategy_parameter_outcome_param "
        "ON strategy_parameter_outcome (parameter_id, recorded_at DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS strategy_parameter_proposal (
            id BIGSERIAL PRIMARY KEY,
            parameter_id BIGINT NOT NULL REFERENCES strategy_parameter(id) ON DELETE CASCADE,
            current_value DOUBLE PRECISION NOT NULL,
            proposed_value DOUBLE PRECISION NOT NULL,
            confidence NUMERIC(5, 4) NOT NULL,
            sample_count INTEGER NOT NULL,
            justification TEXT,
            severity TEXT NOT NULL DEFAULT 'low',  -- 'low' (auto) | 'high' (gated)
            status TEXT NOT NULL DEFAULT 'pending',
            decided_by TEXT,
            decided_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_strategy_parameter_proposal_status "
        "ON strategy_parameter_proposal (status, created_at DESC)"
    ))

    conn.commit()


def _migration_177_unified_signal_consumer_parity_log(conn) -> None:
    """Q1.T3 phase 2 — shadow consumer parity log.

    Adds ``unified_signal_consumer_parity_log`` for tracking discrepancies
    between bespoke ``BreakoutAlert``-driven decisions and what the
    autotrader would have decided from the matching ``unified_signals``
    row. Operator reads this table to assess consumer-readiness before
    flipping ``CHILI_UNIFIED_SIGNAL_CONSUMER_AUTHORITATIVE`` (phase 3, future).
    """
    from sqlalchemy import text

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS unified_signal_consumer_parity_log (
            id BIGSERIAL PRIMARY KEY,
            alert_id BIGINT NOT NULL,
            unified_signal_id BIGINT,
            decision TEXT,
            decision_reason TEXT,
            discrepancies JSONB,
            recorded_at TIMESTAMP DEFAULT NOW()
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_unified_signal_consumer_parity_log_alert "
        "ON unified_signal_consumer_parity_log (alert_id, recorded_at DESC)"
    ))
    conn.commit()


def _migration_176_macro_yield_curve_real(conn) -> None:
    """A4 — replace yield_curve_slope_proxy with real DGS10-DGS2 from FRED.

    Adds two columns to ``trading_macro_regime_snapshots``:

      * ``dgs10_real`` (REAL) — 10-year Treasury constant-maturity yield from FRED.
      * ``dgs2_real`` (REAL) — 2-year Treasury constant-maturity yield from FRED.

    The slope = ``dgs10_real - dgs2_real`` is computed at read time and used
    by the regime classifier feature pipeline in preference to
    ``yield_curve_slope_proxy`` when both real values are present.

    Plus a new ``macro_fred_fetch_log`` table for ingestion observability:
    operator can see when the last successful FRED pull happened, which
    series, and any error.

    Both are nullable — if FRED ingestion is unavailable (network, missing
    free key, etc.) the existing proxy continues to be used.
    """
    from sqlalchemy import text

    conn.execute(text("""
        ALTER TABLE trading_macro_regime_snapshots
            ADD COLUMN IF NOT EXISTS dgs10_real REAL,
            ADD COLUMN IF NOT EXISTS dgs2_real REAL,
            ADD COLUMN IF NOT EXISTS yield_slope_source TEXT
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_trading_macro_regime_snapshots_yield_slope_source "
        "ON trading_macro_regime_snapshots (yield_slope_source, as_of_date DESC)"
    ))

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS macro_fred_fetch_log (
            id BIGSERIAL PRIMARY KEY,
            series_id TEXT NOT NULL,            -- e.g. 'DGS10', 'DGS2'
            as_of_date DATE NOT NULL,
            value REAL,                         -- NULL on fetch failure
            success BOOLEAN NOT NULL,
            error_message TEXT,
            fetched_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (series_id, as_of_date)
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_macro_fred_fetch_log_series "
        "ON macro_fred_fetch_log (series_id, as_of_date DESC)"
    ))

    conn.commit()


# (version_id, callable that receives conn and runs migration)

def _migration_191_brain_batch_jobs_lifecycle(conn) -> None:
    """Add lifecycle columns + 'orphaned' status for periodic reconciler.

    Background: ``brain_batch_jobs`` only had a 4h startup-time sweep; long
    uptimes accumulated stale 'running' rows (40 of them on 2026-04-27, all
    from morning, never transitioned). The new periodic reconciler in
    ``app/services/trading/brain_batch_reconciler.py`` needs:

      - ``heartbeat_at`` so workers can prove liveness during long jobs
      - ``worker_instance_id`` to correlate orphans with the dead worker
      - ``final_state_reason`` to distinguish stale_heartbeat vs no_heartbeat
        vs explicit timeout vs error
      - ``orphaned_at`` separate from ``ended_at`` to keep audit clarity
      - ``'orphaned'`` value in chk_bbj_status (drop & recreate the constraint)

    Idempotent: only adds columns/constraints if missing.
    """
    if "brain_batch_jobs" not in _tables(conn):
        return
    cols = _columns(conn, "brain_batch_jobs")
    if "heartbeat_at" not in cols:
        conn.execute(text("ALTER TABLE brain_batch_jobs ADD COLUMN heartbeat_at TIMESTAMP"))
    if "worker_instance_id" not in cols:
        conn.execute(text("ALTER TABLE brain_batch_jobs ADD COLUMN worker_instance_id VARCHAR(80)"))
    if "final_state_reason" not in cols:
        conn.execute(text("ALTER TABLE brain_batch_jobs ADD COLUMN final_state_reason VARCHAR(120)"))
    if "orphaned_at" not in cols:
        conn.execute(text("ALTER TABLE brain_batch_jobs ADD COLUMN orphaned_at TIMESTAMP"))
    # Index for the reconciler query (status + heartbeat_at + started_at).
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_bbj_running_heartbeat "
        "ON brain_batch_jobs (status, heartbeat_at, started_at) "
        "WHERE status = 'running'"
    ))
    # Update the status check constraint to allow 'orphaned'.
    conn.execute(text("ALTER TABLE brain_batch_jobs DROP CONSTRAINT IF EXISTS chk_bbj_status"))
    conn.execute(text(
        "ALTER TABLE brain_batch_jobs ADD CONSTRAINT chk_bbj_status "
        "CHECK (status IN ('queued','running','ok','error','timeout','orphaned'))"
    ))
    conn.commit()




def _migration_192_trade_asset_kind(conn) -> None:
    """Add explicit asset_kind to trading_trades + backfill from existing signals.

    Background: trade 392 wrote ticker=SPY entry_price=4.01 — an option premium
    being managed as if it's the SPY underlying. The existing ``is_option_trade``
    helper detects options from ``indicator_snapshot.option_meta`` etc., but it's
    only consulted at READ time. With no explicit column, query-side filters
    (e.g., "show me all option trades") have to scan JSONB.

    Backfill rules (most-specific first):
      * indicator_snapshot.option_meta or breakout_alert.asset_type='options' -> 'option'
      * ticker LIKE '%-USD' (crypto convention) -> 'crypto'
      * default -> 'equity'

    CHECK constraint allows the known set; future asset classes (perp/forex)
    can be added in a follow-up migration.

    Idempotent: only ALTERs if column missing.
    """
    if "trading_trades" not in _tables(conn):
        return
    cols = _columns(conn, "trading_trades")
    if "asset_kind" not in cols:
        conn.execute(text("ALTER TABLE trading_trades ADD COLUMN asset_kind VARCHAR(20)"))
        # Backfill: option detection from indicator_snapshot, crypto from ticker, default equity.
        conn.execute(text(
            """
            UPDATE trading_trades SET asset_kind = 'option'
            WHERE asset_kind IS NULL
              AND (
                (indicator_snapshot::jsonb) ? 'option_meta'
                OR (indicator_snapshot::jsonb -> 'breakout_alert' ->> 'asset_type') = 'options'
                OR (indicator_snapshot::jsonb -> 'breakout_alert') ? 'option_meta'
              )
            """
        ))
        conn.execute(text(
            "UPDATE trading_trades SET asset_kind = 'crypto' "
            "WHERE asset_kind IS NULL AND ticker LIKE '%-USD'"
        ))
        conn.execute(text(
            "UPDATE trading_trades SET asset_kind = 'equity' WHERE asset_kind IS NULL"
        ))
        conn.execute(text(
            "ALTER TABLE trading_trades ADD CONSTRAINT chk_trade_asset_kind "
            "CHECK (asset_kind IN ('equity','option','crypto','forex','perp'))"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_trades_asset_kind ON trading_trades(asset_kind)"
        ))
    conn.commit()


def _migration_193_cleanup_corrupt_win_rates(conn) -> None:
    """Clean corrupt scan_patterns.win_rate values + add CHECK guard.

    Audit on 2026-04-28 found two distinct corruptions, all on n=0 patterns:

    * 11 rows with ``win_rate = 'NaN'::float`` (divide-by-zero on the
      ``wins/total`` formula in :mod:`learning.py` when ``total = 0``).
    * 11 rows with ``win_rate > 1`` (max ``60.0``) where an LLM-spawn
      write site stored a percent value (``60.0`` = "60%") into a
      column the rest of the system reads as a fraction (``0.60``).

    Cleanup rules (idempotent):

    * NaN     -> NULL    (no real evidence to keep)
    * > 1     -> /100    (interpret stored value as percent, scale back)
    * NULL/in-range -> untouched.

    Also adds a CHECK constraint so future writes that violate the
    fraction contract fail at the DB level rather than silently
    corrupting the table again. The constraint is added with
    ``NOT VALID`` first so existing data isn't re-scanned, then we
    explicitly ``VALIDATE`` after cleanup.

    Rollback (manual)::

        ALTER TABLE scan_patterns DROP CONSTRAINT IF EXISTS chk_scan_patterns_win_rate_range;
    """
    if "scan_patterns" not in _tables(conn):
        conn.commit()
        return

    # Step 1: NaN -> NULL (use ::text comparison since NaN<>NaN in float8 but we
    # want to be explicit and avoid surprises with SET clauses on float).
    conn.execute(text(
        """
        UPDATE scan_patterns
        SET win_rate = NULL
        WHERE win_rate IS NOT NULL AND win_rate::text = 'NaN'
        """
    ))
    conn.commit()

    # Step 2: > 1 -> /100 (percent stored where fraction expected).
    conn.execute(text(
        """
        UPDATE scan_patterns
        SET win_rate = win_rate / 100.0
        WHERE win_rate IS NOT NULL
          AND win_rate::text <> 'NaN'
          AND win_rate > 1.0
        """
    ))
    conn.commit()

    # Step 3: avg_return_pct NaN -> NULL (defense in depth — same divide-by-zero
    # source could land here). Range left unconstrained since avg_return_pct is
    # in PCT and can legitimately be e.g. -50.0.
    conn.execute(text(
        """
        UPDATE scan_patterns
        SET avg_return_pct = NULL
        WHERE avg_return_pct IS NOT NULL AND avg_return_pct::text = 'NaN'
        """
    ))
    conn.commit()

    # Step 4: add CHECK constraint (idempotent — only adds if missing).
    row = conn.execute(text(
        """
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        WHERE t.relname = 'scan_patterns'
          AND c.conname = 'chk_scan_patterns_win_rate_range'
        """
    )).fetchone()
    if not row:
        conn.execute(text(
            """
            ALTER TABLE scan_patterns
            ADD CONSTRAINT chk_scan_patterns_win_rate_range
            CHECK (
                win_rate IS NULL
                OR (win_rate >= 0.0 AND win_rate <= 1.0)
            ) NOT VALID
            """
        ))
        conn.commit()
        # Now safe to validate — corruptions cleaned above.
        conn.execute(text(
            "ALTER TABLE scan_patterns VALIDATE CONSTRAINT "
            "chk_scan_patterns_win_rate_range"
        ))
    conn.commit()


def _migration_194_realized_ev_gate_retroactive_demote(conn) -> None:
    """Retroactive realized-EV gate sweep + explicit pattern 1047 retirement.

    The new EV gate (see :mod:`app.services.trading.realized_ev_gate`)
    only blocks **future** promotions. This migration applies the same
    rule retroactively to currently-promoted patterns that were grandfathered
    in before the gate existed (notably pattern 1047, twice rescued by
    migrations 168 and 170 on CPCV evidence despite negative realized
    avg_return).

    Rules (must hold for a pattern to STAY promoted)::

        avg_return_pct > 0    AND    win_rate > 0    AND    trade_count >= 5

    Patterns that fail are demoted to ``challenged`` with
    ``promotion_status = ev_gate_retroactive_demote``. The reason field
    captures which sub-rule failed for each row.

    **Pattern 1047 is handled explicitly** in step 2: it has stored
    ``avg_return_pct = -3.97`` and the prior rescue migrations are
    overridden by being a NEWER, higher-numbered migration that
    explicitly sets lifecycle_stage. The CLAUDE.md hard rule that says
    "do not erode authority via side edits" applies to the *prediction
    mirror* contract; this is a lifecycle-stage decision and is exactly
    what new migrations are for.

    Rollback (manual)::

        UPDATE scan_patterns SET lifecycle_stage = 'promoted',
                                 promotion_status = 'promoted'
        WHERE id = 1047;  -- or per the audit log
    """
    if "scan_patterns" not in _tables(conn):
        conn.commit()
        return

    # Step 0: ensure audit columns exist. Safe to add idempotently — these
    # are nullable so backfill is implicit.
    cols = _columns(conn, "scan_patterns")
    if "demoted_at" not in cols:
        conn.execute(text(
            "ALTER TABLE scan_patterns ADD COLUMN demoted_at TIMESTAMP"
        ))
        conn.commit()
    if "promotion_demote_reason" not in cols:
        conn.execute(text(
            "ALTER TABLE scan_patterns ADD COLUMN promotion_demote_reason TEXT"
        ))
        conn.commit()

    # Step 1: log a snapshot of who would be demoted (for audit), then demote.
    # We do NOT touch CPCV columns — only lifecycle_stage / promotion_status /
    # demoted_at / promotion_demote_reason. CPCV evidence stays intact so a
    # future re-evaluation can re-promote if the pattern's realized stats
    # turn around.
    snapshot = conn.execute(text("""
        SELECT id, name, win_rate, avg_return_pct, trade_count
        FROM scan_patterns
        WHERE lifecycle_stage IN ('promoted', 'live')
          AND (
            avg_return_pct IS NULL
            OR avg_return_pct <= 0
            OR win_rate IS NULL
            OR win_rate <= 0
            OR trade_count IS NULL
            OR trade_count < 5
          )
    """)).fetchall()

    demoted = 0
    for r in snapshot:
        conn.execute(text("""
            UPDATE scan_patterns
            SET lifecycle_stage = 'challenged',
                promotion_status = 'ev_gate_retroactive_demote',
                demoted_at = COALESCE(demoted_at, CURRENT_TIMESTAMP),
                promotion_demote_reason =
                  'realized_ev_gate: avg_return_pct=' || coalesce(avg_return_pct::text, 'NULL')
                  || ' win_rate=' || coalesce(win_rate::text, 'NULL')
                  || ' trade_count=' || coalesce(trade_count::text, 'NULL'),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :pid
        """), {"pid": r.id})
        demoted += 1

    # Step 2: explicit pattern 1047 retirement. Even if it slipped through
    # step 1 (e.g. trade_count was bumped above 5 with mixed-sign returns),
    # the project memory ``project_pattern_1047_history`` captured the case
    # for explicit retirement — we want it OFF the promoted list until
    # somebody manually flips it back with new evidence under the EV gate.
    row_1047 = conn.execute(text("""
        SELECT id, lifecycle_stage, avg_return_pct, win_rate, trade_count
        FROM scan_patterns WHERE id = 1047
    """)).fetchone()
    if row_1047 is not None:
        conn.execute(text("""
            UPDATE scan_patterns
            SET lifecycle_stage = 'challenged',
                promotion_status = 'ev_demote_1047_supersede',
                demoted_at = COALESCE(demoted_at, CURRENT_TIMESTAMP),
                promotion_demote_reason =
                  'pattern 1047 explicit retire: supersedes migrations 168 + 170. '
                  || 'Realized avg_return_pct=' || coalesce(avg_return_pct::text, 'NULL')
                  || ' contradicted backtest CPCV evidence. New EV gate makes the rescue '
                  || 'no longer admissible.',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1047
        """))

    conn.commit()


def _migration_195_pattern_condition_signature(conn) -> None:
    """Add ``scan_patterns.condition_signature`` + backfill from ``rules_json``.

    Closes the variant-treadmill loophole identified 2026-04-28: many of
    the 614 patterns are functionally identical near-duplicates spawned
    by different paths. The signature column lets future write paths
    short-circuit on duplicate conditions (look up by hash, increment
    evidence on the existing row instead of spawning new).

    Steps:
      1. ALTER TABLE ADD COLUMN IF NOT EXISTS condition_signature CHAR(40).
      2. Compute and write the signature for every existing row using the
         pure :mod:`pattern_signature` helpers (no app.db / settings
         needed — plain JSON parse + hash).
      3. CREATE INDEX (non-unique — we WANT to find duplicates) on the
         column for fast lookup.

    Idempotent. Safe to re-run.

    Rollback (manual)::

        ALTER TABLE scan_patterns DROP COLUMN IF EXISTS condition_signature;
    """
    if "scan_patterns" not in _tables(conn):
        conn.commit()
        return

    cols = _columns(conn, "scan_patterns")
    if "condition_signature" not in cols:
        conn.execute(text(
            "ALTER TABLE scan_patterns ADD COLUMN condition_signature CHAR(40)"
        ))
        conn.commit()

    # Backfill: pull rules_json for every row, compute signature, write back.
    # We do it in Python because SQL string normalization for JSON is fragile;
    # the canonical helpers in :mod:`pattern_signature` are the source of truth.
    rows = conn.execute(text(
        "SELECT id, rules_json FROM scan_patterns "
        "WHERE condition_signature IS NULL OR condition_signature = ''"
    )).fetchall()
    if rows:
        # Local import — the migration shouldn't hard-fail if the helper
        # module can't load (e.g. mid-deploy mismatch). Fall back to a
        # do-nothing stamp so we don't spin re-running this migration.
        try:
            from app.services.trading.pattern_signature import (
                signature_for_rules_json,
                EMPTY_SIGNATURE,
            )
        except Exception:
            signature_for_rules_json = lambda _x: "ERR_NO_HELPER"
            EMPTY_SIGNATURE = "EMPTY"

        updated = 0
        for r in rows:
            sig = signature_for_rules_json(r.rules_json)
            conn.execute(text(
                "UPDATE scan_patterns SET condition_signature = :sig WHERE id = :pid"
            ), {"sig": sig, "pid": r.id})
            updated += 1
        conn.commit()

    # Non-unique index. We WANT duplicates to surface, not constrain them.
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_scan_patterns_condition_signature "
        "ON scan_patterns (condition_signature)"
    ))
    conn.commit()


def _migration_196_pattern_signature_with_timeframe(conn) -> None:
    """Re-backfill ``condition_signature`` to include the pattern's timeframe.

    The signature added by migration 195 hashed only ``rules_json.conditions``,
    which collapsed patterns with identical entry rules but different bar
    intervals onto the same hash. The 2026-04-28 audit found 31 rows
    sharing one signature where the actual ``timeframe`` column ranged
    over {1m, 5m, 15m, 1h, 4h, 1d}.

    Operator correction: those suffixes ARE timeframe variants. They are
    NOT duplicates — a strategy on 1-minute bars and the same rule on
    daily bars are fundamentally different signal streams. This migration
    recomputes the signature using the helper's new timeframe-aware form.

    Idempotent: ``signature_for_pattern`` is pure, recomputing always
    produces the same value for the same row.

    Rollback (manual)::

        UPDATE scan_patterns SET condition_signature = NULL;
        # then re-run migrations 195 + 196 in order.
    """
    if "scan_patterns" not in _tables(conn):
        conn.commit()
        return

    # Pull every row's rules_json + timeframe and recompute. The helper
    # falls back to a stable EMPTY marker if either is unparseable.
    try:
        from app.services.trading.pattern_signature import signature_for_rules_json
    except Exception:
        # If the helper module isn't importable yet (mid-deploy), no-op
        # this migration so it doesn't fail boot. It will run on a future
        # restart once the module lands.
        conn.commit()
        return

    rows = conn.execute(text(
        "SELECT id, rules_json, timeframe FROM scan_patterns"
    )).fetchall()
    updated = 0
    for r in rows:
        sig = signature_for_rules_json(r.rules_json, timeframe=r.timeframe)
        conn.execute(text(
            "UPDATE scan_patterns SET condition_signature = :sig WHERE id = :pid"
        ), {"sig": sig, "pid": r.id})
        updated += 1
    conn.commit()


def _migration_197_promote_top_backtest_evidence(conn) -> None:
    """Re-promote patterns with strong backtest-evidence in the new ledger.

    Migration 194 retroactively demoted every promoted pattern. The brain
    is now in a state where 0 patterns can fire (lifecycle gate blocks all).
    The 2026-04-28 backtest-history backfill of
    `trading_pattern_regime_performance_daily` surfaced 1,355 confident
    (n>=5) backtest cells. Use that evidence to re-promote the strongest
    universal winners.

    Selection criteria (strict):
      * mode = 'backtest' AND has_confidence
      * max(n_trades) >= 30
      * avg(mean_pnl_pct) > 0.5
      * avg(hit_rate) >= 0.45

    Patterns re-promoted here pass through the FULL gate stack at
    runtime: ticker autotune, EV gate, regime gate, lifecycle gate,
    drawdown breaker.

    Rollback (manual)::

        UPDATE scan_patterns SET lifecycle_stage = 'challenged',
                                 promotion_status = 'rolled_back_197'
        WHERE promotion_status = 'promoted_via_bt_ev_197';
    """
    if "scan_patterns" not in _tables(conn):
        conn.commit()
        return
    if "trading_pattern_regime_performance_daily" not in _tables(conn):
        conn.commit()
        return

    rows = conn.execute(text("""
        SELECT pattern_id,
               max(n_trades) AS n,
               avg(hit_rate) AS hr,
               avg(mean_pnl_pct) AS mp
        FROM trading_pattern_regime_performance_daily
        WHERE mode = 'backtest'
          AND has_confidence
          AND n_trades >= 30
        GROUP BY pattern_id
        HAVING avg(mean_pnl_pct) > 0.5
           AND avg(hit_rate) >= 0.45
        ORDER BY avg(mean_pnl_pct) DESC
        LIMIT 25
    """)).fetchall()

    for r in rows:
        pid = int(r.pattern_id)
        conn.execute(text("""
            UPDATE scan_patterns
            SET lifecycle_stage = 'promoted',
                promotion_status = 'promoted_via_bt_ev_197',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :pid
              AND lifecycle_stage NOT IN ('promoted', 'live')
        """), {"pid": pid})
    conn.commit()


def _migration_198_seed_short_swing_patterns(conn) -> None:
    """Seed 6 short-swing (1-2 day hold) patterns + boost their backtest queue.

    Operator request 2026-04-28: existing promoted patterns have multi-day to
    multi-week holds. The user wants faster capital turnover, ~1-2 day swing
    trades. The current ``_EXIT_PARAMS_BY_TIMEFRAME['1d']`` defaults set
    max_bars to 50/15/25 (way too long for short swings); these patterns
    pin ``exit_config.max_bars`` to 1-2 explicitly so the backtest engine
    and live exit_monitor honor the short hold.

    Each pattern has a distinct ``rules_json`` + ``timeframe='1d'`` +
    ``exit_config`` JSON. Inserted as ``lifecycle_stage='backtested'`` with
    ``backtest_priority=200`` so the brain-worker queue picks them up first.
    Once enough backtest history accumulates and they pass the EV gate,
    the existing promotion path graduates them to live trading.

    Rollback (manual)::

        DELETE FROM scan_patterns WHERE origin = 'short_swing_seed_198';
    """
    if "scan_patterns" not in _tables(conn):
        conn.commit()
        return

    seeds = [
        # (name, description, rules_json, exit_config_json, asset_class)
        (
            "Daily IBS<0.2 + Bull Engulf — 1-2d Mean Reversion",
            "Internal Bar Strength below 0.2 (close near low) + previous-day bull engulf signals "
            "exhaustion. Enter at close, exit within 1-2 days on mean-reversion bounce.",
            '{"conditions": [{"indicator": "ibs", "op": "<", "value": 0.2}, '
            '{"indicator": "bullish_engulfing", "op": "==", "value": true}]}',
            '{"max_bars": 2, "atr_mult": 1.0, "use_bos": false}',
            "stocks",
        ),
        (
            "Gap-Down Fade — 1d Close Fill",
            "Gap-down >2 percent with elevated volume + RSI<40. Enters at open, fades the "
            "gap as price reverts to prior close. Exit by next session.",
            '{"conditions": [{"indicator": "gap_pct", "op": "<", "value": -0.02}, '
            '{"indicator": "volume_ratio", "op": ">", "value": 1.2}, '
            '{"indicator": "rsi_14", "op": "<", "value": 40}]}',
            '{"max_bars": 1, "atr_mult": 1.5, "use_bos": false}',
            "stocks",
        ),
        (
            "Inside Day Breakout — 1-2d Continuation",
            "NR7 (narrow range 7) + volume confirmation + neutral RSI. Enters on breakout, "
            "rides 1-2 day continuation with BOS confirmation.",
            '{"conditions": [{"indicator": "nr7", "op": "==", "value": true}, '
            '{"indicator": "volume_ratio", "op": ">", "value": 1.0}, '
            '{"indicator": "rsi_14", "op": "between", "value": [40, 60]}]}',
            '{"max_bars": 2, "atr_mult": 1.3, "use_bos": true, "bos_buffer_pct": 0.001}',
            "stocks",
        ),
        (
            "Daily RSI<30 + Above SMA50 — 1-2d Bounce",
            "Oversold (RSI<30) while price still above the 50-day moving average (uptrend "
            "intact). Counter-trend bounce trade with 1-2 day hold.",
            '{"conditions": [{"indicator": "rsi_14", "op": "<", "value": 30}, '
            '{"indicator": "above_sma50", "op": "==", "value": true}, '
            '{"indicator": "volume_ratio", "op": ">", "value": 0.8}]}',
            '{"max_bars": 2, "atr_mult": 1.0, "use_bos": false}',
            "stocks",
        ),
        (
            "Bull Hammer + High Volume — 1d Confirmation",
            "Hammer reversal candle with elevated volume in oversold zone. Next-day "
            "continuation trade; exit by close of day after entry.",
            '{"conditions": [{"indicator": "hammer", "op": "==", "value": true}, '
            '{"indicator": "volume_ratio", "op": ">", "value": 1.5}, '
            '{"indicator": "rsi_14", "op": "<", "value": 50}]}',
            '{"max_bars": 1, "atr_mult": 1.5, "use_bos": false}',
            "stocks",
        ),
        (
            "EMA20 Pullback in Strong Trend — 1-2d Resumption",
            "Price pulled back to within 0.5 percent of 20-EMA in a confirmed trend "
            "(ADX>20, RSI>40). Trend continuation entry; exit on 1-2 day resumption.",
            '{"conditions": [{"indicator": "ema20_dist_pct", "op": "<", "value": 0.005}, '
            '{"indicator": "rsi_14", "op": ">", "value": 40}, '
            '{"indicator": "adx", "op": ">", "value": 20}]}',
            '{"max_bars": 2, "atr_mult": 1.2, "use_bos": false}',
            "stocks",
        ),
    ]

    inserted = 0
    for name, description, rules, exit_cfg, asset_class in seeds:
        # Idempotent: skip if a pattern with this exact name already exists.
        row = conn.execute(text(
            "SELECT id FROM scan_patterns WHERE name = :n LIMIT 1"
        ), {"n": name}).fetchone()
        if row:
            continue
        conn.execute(text("""
            INSERT INTO scan_patterns (
                name, description, rules_json, exit_config,
                origin, asset_class, timeframe,
                confidence, win_rate, avg_return_pct,
                evidence_count, trade_count,
                lifecycle_stage, promotion_status,
                ticker_scope, backtest_priority,
                active, created_at, updated_at
            ) VALUES (
                :name, :description, CAST(:rules AS jsonb), CAST(:exit_cfg AS jsonb),
                'short_swing_seed_198', :asset_class, '1d',
                0.55, NULL, NULL,
                0, 0,
                'backtested', 'pending_oos',
                'universal', 200,
                true, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
        """), {
            "name": name,
            "description": description,
            "rules": rules,
            "exit_cfg": exit_cfg,
            "asset_class": asset_class,
        })
        inserted += 1
    conn.commit()


MIGRATIONS = [
    ("001_add_email", _migration_001_add_email),
    ("002_add_image_path", _migration_002_add_image_path),
    ("003_conversations_project_id", _migration_003_conversations_project_id),
    ("004_chore_columns", _migration_004_chore_columns),
    ("005_plan_projects_key", _migration_005_plan_projects_key),
    ("006_plan_tasks_parent_reporter", _migration_006_plan_tasks_parent_reporter),
    ("007_backfill_project_members", _migration_007_backfill_project_members),
    ("008_trade_broker_columns", _migration_008_trade_broker_columns),
    ("009_snapshot_predicted_score", _migration_009_snapshot_predicted_score),
    ("010_snapshot_extra_columns", _migration_010_snapshot_extra_columns),
    ("011_trade_pattern_tags", _migration_011_trade_pattern_tags),
    ("012_snapshot_sentiment_fundamentals", _migration_012_snapshot_sentiment_fundamentals),
    ("013_trade_order_sync_columns", _migration_013_trade_order_sync_columns),
    ("014_code_brain_tables", _migration_014_code_brain_tables),
    ("015_reasoning_brain_tables", _migration_015_reasoning_brain_tables),
    ("016_reasoning_learning_structures", _migration_016_reasoning_learning_structures),
    ("017_code_brain_innovation", _migration_017_code_brain_innovation),
    ("018_breakout_alert_outcome_cols", _migration_018_breakout_alert_outcome_cols),
    ("019_project_brain_tables", _migration_019_project_brain_tables),
    ("020_user_google_oauth_cols", _migration_020_user_google_oauth_cols),
    ("021_broker_credentials_table", _migration_021_broker_credentials_table),
    ("022_alert_trade_type_cols", _migration_022_alert_trade_type_cols),
    ("023_qa_engineer_tables", _migration_023_qa_engineer_tables),
    ("024_po_question_options", _migration_024_po_question_options),
    ("025_insight_win_loss_counts", _migration_025_insight_win_loss_counts),
    ("026_reset_backfilled_win_loss", _migration_026_reset_backfilled_win_loss),
    ("027_backtest_insight_link", _migration_027_backtest_insight_link),
    ("028_seed_rsi_ema_breakout_pattern", _migration_028_seed_rsi_ema_breakout_pattern),
    ("029_seed_rsi_ema_insight", _migration_029_seed_rsi_ema_insight),
    ("030_pattern_exit_evolution", _migration_030_pattern_exit_evolution),
    ("031_seed_ross_cameron_patterns", _migration_031_seed_ross_cameron_patterns),
    ("032_seed_candlestick_patterns", _migration_032_seed_candlestick_patterns),
    ("033_insight_pattern_fk", _migration_033_insight_pattern_fk),
    ("034_pattern_backtest_queue", _migration_034_pattern_backtest_queue),
    ("035_backtest_scan_pattern_fk", _migration_035_backtest_scan_pattern_fk),
    ("036_pattern_trade_analytics", _migration_036_pattern_trade_analytics),
    ("037_learning_cycle_ai_reports", _migration_037_learning_cycle_ai_reports),
    ("038_brain_worker_control_stop_heartbeat", _migration_038_brain_worker_control_stop_heartbeat),
    ("039_scan_pattern_oos_promotion", _migration_039_scan_pattern_oos_promotion),
    ("040_scan_pattern_bench_walk_forward", _migration_040_scan_pattern_bench_walk_forward),
    ("041_trade_tca_columns", _migration_041_trade_tca_columns),
    ("042_trade_attribution_exit_tca", _migration_042_trade_attribution_exit_tca),
    ("043_insight_scan_pattern_required", _migration_043_insight_scan_pattern_required),
    ("044_trading_insight_scan_pattern_constraints", _migration_044_trading_insight_scan_pattern_constraints),
    ("045_trading_alert_scan_pattern_id", _migration_045_trading_alert_scan_pattern_id),
    ("046_hypothesis_family_columns", _migration_046_hypothesis_family_columns),
    ("047_scan_pattern_research_quant_columns", _migration_047_scan_pattern_research_quant_columns),
    ("048_brain_learning_cycle_tables", _migration_048_brain_learning_cycle_tables),
    ("049_brain_cycle_lease", _migration_049_brain_cycle_lease),
    ("050_brain_integration_event", _migration_050_brain_integration_event),
    ("051_brain_prediction_snapshot", _migration_051_brain_prediction_snapshot),
    ("052_planner_coding_task_layer", _migration_052_planner_coding_task_layer),
    ("053_coding_agent_suggestion", _migration_053_coding_agent_suggestion),
    ("054_coding_agent_suggestion_apply", _migration_054_coding_agent_suggestion_apply),
    ("055_brain_worker_ui_digest_json", _migration_055_brain_worker_ui_digest_json),
    ("056_snapshot_bar_key", _migration_056_snapshot_bar_key),
    ("057_trading_insight_evidence", _migration_057_trading_insight_evidence),
    ("058_trading_prescreen_artifacts", _migration_058_trading_prescreen_artifacts),
    ("059_prescreen_asset_universe", _migration_059_prescreen_asset_universe),
    ("060_brain_batch_jobs", _migration_060_brain_batch_jobs),
    ("061_brain_batch_jobs_payload_json", _migration_061_brain_batch_jobs_payload_json),
    ("062_breakout_alert_feedback_cols", _migration_062_breakout_alert_feedback_cols),
    ("063_composite_indexes_performance", _migration_063_composite_indexes_performance),
    ("064_backtest_oos_fields", _migration_064_backtest_oos_fields),
    ("065_scan_pattern_lifecycle", _migration_065_scan_pattern_lifecycle),
    ("066_foreign_keys", _migration_066_foreign_keys),
    ("067_data_retention_columns", _migration_067_data_retention_columns),
    ("068_paper_trades_table", _migration_068_paper_trades_table),
    ("069_supporting_tables", _migration_069_supporting_tables),
    ("070_normalize_win_rate", _migration_070_normalize_win_rate),
    ("071_reconcile_backtest_links", _migration_071_reconcile_backtest_links),
    ("072_recompute_pattern_stats", _migration_072_recompute_pattern_stats),
    ("073_scan_patterns_user_id", _migration_073_scan_patterns_user_id),
    ("074_backfill_pattern_trade_asset_class", _migration_074_backfill_pattern_trade_asset_class),
    ("075_text_to_jsonb", _migration_075_text_to_jsonb),
    ("076_check_constraints", _migration_076_check_constraints),
    ("077_composite_indexes", _migration_077_composite_indexes),
    ("078_foreign_keys_phase2", _migration_078_foreign_keys_phase2),
    ("079_unique_constraints", _migration_079_unique_constraints),
    ("080_orphan_cleanup", _migration_080_orphan_cleanup),
    ("081_graduate_startup_repairs", _migration_081_graduate_startup_repairs),
    ("082_breakout_alert_outcome_notes", _migration_082_breakout_alert_outcome_notes),
    ("083_backtest_win_rate_scale_cleanup", _migration_083_backtest_win_rate_scale_cleanup),
    ("084_align_backtest_scan_pattern_from_insight", _migration_084_align_backtest_scan_pattern_from_insight),
    ("085_brain_worker_learning_live_json", _migration_085_brain_worker_learning_live_json),
    ("086_trading_brain_neural_mesh", _migration_086_trading_brain_neural_mesh),
    ("087_neural_mesh_seed_expand_v15", _migration_087_neural_mesh_seed_expand_v15),
    ("088_backtest_param_sets", _migration_088_backtest_param_sets),
    ("089_momentum_neural_mesh", _migration_089_momentum_neural_mesh),
    ("090_momentum_neural_persistence", _migration_090_momentum_neural_persistence),
    ("091_momentum_automation_outcomes", _migration_091_momentum_automation_outcomes),
    ("092_speculative_momentum_neural_subgraph", _migration_092_speculative_momentum_neural_subgraph),
    ("093_automation_session_promotion_lineage", _migration_093_automation_session_promotion_lineage),
    ("094_trading_autopilot_runtime", _migration_094_trading_autopilot_runtime),
    ("095_task_workspace_binding", _migration_095_task_workspace_binding),
    ("096_momentum_variant_refinement", _migration_096_momentum_variant_refinement),
    ("097_scan_pattern_lifecycle_challenged", _migration_097_scan_pattern_lifecycle_challenged),
    ("098_brain_validation_slice_ledger", _migration_098_brain_validation_slice_ledger),
    ("099_execution_audit_and_allocator", _migration_099_execution_audit_and_allocator),
    ("100_momentum_viability_scope", _migration_100_momentum_viability_scope),
    ("101_coding_execution_iteration", _migration_101_coding_execution_iteration),
    ("102_learning_cycle_neural_nodes", _migration_102_learning_cycle_neural_nodes),
    ("103_unified_neural_mesh", _migration_103_unified_neural_mesh),
    ("104_split_c_meta_cluster", _migration_104_split_c_meta_cluster),
    ("105_execution_context_venue_nodes", _migration_105_execution_context_venue_nodes),
    ("106_split_c_secondary_cluster", _migration_106_split_c_secondary_cluster),
    ("107_scan_pattern_regime_affinity", _migration_107_scan_pattern_regime_affinity),
    ("108_neural_mesh_lc_causal_edges", _migration_108_neural_mesh_lc_causal_edges),
    ("109_brain_work_events", _migration_109_brain_work_events),
    ("110_brain_work_lease_scope", _migration_110_brain_work_lease_scope),
    ("111_trading_decision_stack", _migration_111_trading_decision_stack),
    ("112_trade_sector_and_governance_approvals", _migration_112_trade_sector_and_governance_approvals),
    ("113_trade_stop_columns", _migration_113_trade_stop_columns),
    ("114_stop_decisions_and_delivery", _migration_114_stop_decisions_and_delivery),
    ("115_schema_hardening_fks", _migration_115_schema_hardening_fks),
    ("116_trade_type_column", _migration_116_trade_type_column),
    ("117_pattern_position_monitor", _migration_117_pattern_position_monitor),
    ("118_dynamic_trade_plan_monitor", _migration_118_dynamic_trade_plan_monitor),
    ("119_broker_sessions_table", _migration_119_broker_sessions_table),
    ("120_monitor_learning_engine", _migration_120_monitor_learning_engine),
    ("121_autopilot_profitability_outcomes", _migration_121_autopilot_profitability_outcomes),
    ("122_position_plans_table", _migration_122_position_plans_table),
    ("123_setup_vitals_engine", _migration_123_setup_vitals_engine),
    ("124_alert_content_signature", _migration_124_alert_content_signature),
    ("125_mesh_reactive_sensors", _migration_125_mesh_reactive_sensors),
    ("126_mesh_dependency_edges", _migration_126_mesh_dependency_edges),
    ("127_net_edge_ranker", _migration_127_net_edge_ranker),
    ("128_exit_evaluator_parity", _migration_128_exit_evaluator_parity),
    ("129_economic_ledger", _migration_129_economic_ledger),
    ("130_pit_hygiene", _migration_130_pit_hygiene),
    ("131_triple_barrier", _migration_131_triple_barrier),
    ("132_execution_cost_model", _migration_132_execution_cost_model),
    ("133_live_brackets_reconciliation", _migration_133_live_brackets_reconciliation),
    ("134_position_sizer_log", _migration_134_position_sizer_log),
    ("135_risk_dial_capital_reweight", _migration_135_risk_dial_capital_reweight),
    ("136_drift_monitor_recert", _migration_136_drift_monitor_recert),
    ("137_divergence_panel", _migration_137_divergence_panel),
    ("138_macro_regime_snapshot", _migration_138_macro_regime_snapshot),
    ("139_breadth_relstr_snapshot", _migration_139_breadth_relstr_snapshot),
    ("140_cross_asset_snapshot", _migration_140_cross_asset_snapshot),
    ("141_ticker_regime_snapshot", _migration_141_ticker_regime_snapshot),
    ("142_vol_dispersion_snapshot", _migration_142_vol_dispersion_snapshot),
    ("143_intraday_session_snapshot", _migration_143_intraday_session_snapshot),
    ("144_pattern_regime_performance_ledger", _migration_144_pattern_regime_performance_ledger),
    ("145_pattern_regime_m2_consumers", _migration_145_pattern_regime_m2_consumers),
    ("146_m2_autopilot", _migration_146_m2_autopilot),
    ("147_autotrader_audit", _migration_147_autotrader_audit),
    ("148_trade_pending_exit_columns", _migration_148_trade_pending_exit_columns),
    ("149_schema_drift_repairs", _migration_149_schema_drift_repairs),
    ("150_venue_order_idempotency", _migration_150_venue_order_idempotency),
    ("151_trade_management_scope", _migration_151_trade_management_scope),
    ("152_operational_clusters", _migration_152_operational_clusters),
    ("153_portfolio_and_exit_clusters", _migration_153_portfolio_and_exit_clusters),
    ("154_spine_to_learning_feedback_edges", _migration_154_spine_to_learning_feedback_edges),
    ("155_plasticity_tables_and_nodes", _migration_155_plasticity_tables_and_nodes),
    ("156_observers_and_momentum_bridge", _migration_156_observers_and_momentum_bridge),
    ("157_trade_mesh_entry_correlation", _migration_157_trade_mesh_entry_correlation),
    ("158_plasticity_pre_live_snapshot", _migration_158_plasticity_pre_live_snapshot),
    ("159_order_state_log", _migration_159_order_state_log),
    ("160_exec_events_venue_ts_index", _migration_160_exec_events_venue_ts_index),
    ("161_trade_broker_order_id_unique", _migration_161_trade_broker_order_id_unique),
    ("162_project_domain_recovery", _migration_162_project_domain_recovery),
    ("163_cpcv_promotion_gate_evidence", _migration_163_cpcv_promotion_gate_evidence),
    ("164_cpcv_shadow_eval_log", _migration_164_cpcv_shadow_eval_log),
    ("165_regime_snapshot_and_tagging", _migration_165_regime_snapshot_and_tagging),
    (
        "166_scan_patterns_promotion_gate_null_default",
        _migration_166_scan_patterns_promotion_gate_null_default,
    ),
    ("167_unified_signals_table", _migration_167_unified_signals_table),
    (
        "168_restore_pattern_1047_cpcv_miscalibration",
        _migration_168_restore_pattern_1047_cpcv_miscalibration,
    ),
    ("169_scan_patterns_pattern_evidence_kind", _migration_169_scan_patterns_pattern_evidence_kind),
    (
        "170_restore_pattern_1047_n_paths_threshold_second",
        _migration_170_restore_pattern_1047_n_paths_threshold_second,
    ),
    ("171_chili_dispatch_tables", _migration_171_chili_dispatch_tables),
    ("172_code_brain_neural_tables", _migration_172_code_brain_neural_tables),
    ("173_context_brain_tables", _migration_173_context_brain_tables),
    ("174_llm_gateway_decomposition_tables", _migration_174_llm_gateway_decomposition_tables),
    ("175_gateway_learning_loop", _migration_175_gateway_learning_loop),
    ("176_macro_yield_curve_real", _migration_176_macro_yield_curve_real),
    ("177_unified_signal_consumer_parity_log", _migration_177_unified_signal_consumer_parity_log),
    ("178_strategy_parameter_adaptive", _migration_178_strategy_parameter_adaptive),
    ("179_portfolio_sizing_log", _migration_179_portfolio_sizing_log),
    ("180_options_lane_scaffold", _migration_180_options_lane_scaffold),
    ("181_forex_lane_scaffold", _migration_181_forex_lane_scaffold),
    ("182_perps_lane_scaffold", _migration_182_perps_lane_scaffold),
    ("183_pattern_survival_meta_classifier", _migration_183_pattern_survival_meta_classifier),
    ("184_seed_hyperliquid_perp_contracts", _migration_184_seed_hyperliquid_perp_contracts),
    ("185_backfill_pattern_families", _migration_185_backfill_pattern_families),
    ("186_demote_lifecycle_active_drift", _migration_186_demote_lifecycle_active_drift),
    ("187_pattern_survival_decision_log", _migration_187_pattern_survival_decision_log),
    ("188_pattern_survival_promote_review_queue", _migration_188_pattern_survival_promote_review_queue),
    ("189_seed_dydx_v4_perp_contracts", _migration_189_seed_dydx_v4_perp_contracts),
    ("190_seed_kraken_futures_perp_contracts", _migration_190_seed_kraken_futures_perp_contracts),
    ("191_brain_batch_jobs_lifecycle", _migration_191_brain_batch_jobs_lifecycle),
    ("192_trade_asset_kind", _migration_192_trade_asset_kind),
    ("193_cleanup_corrupt_win_rates", _migration_193_cleanup_corrupt_win_rates),
    ("194_realized_ev_gate_retroactive_demote", _migration_194_realized_ev_gate_retroactive_demote),
    ("195_pattern_condition_signature", _migration_195_pattern_condition_signature),
    ("196_pattern_signature_with_timeframe", _migration_196_pattern_signature_with_timeframe),
    ("197_promote_top_backtest_evidence", _migration_197_promote_top_backtest_evidence),
    ("198_seed_short_swing_patterns", _migration_198_seed_short_swing_patterns),
]


def run_migrations(engine: Engine) -> None:
    """Create schema_version table if missing, then run any migrations not yet applied.

    Fails fast (before any DB writes) if a migration ID is duplicated or
    collides with a retired ID — see ``_assert_migration_ids_unique``.
    """
    _assert_migration_ids_unique()
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version_id TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        ))
        conn.commit()

        applied = {row[0] for row in conn.execute(text("SELECT version_id FROM schema_version")).fetchall()}

        for version_id, migrate_fn in MIGRATIONS:
            if version_id in applied:
                continue
            try:
                migrate_fn(conn)
                conn.execute(
                    text(
                        "INSERT INTO schema_version (version_id) VALUES (:vid) "
                        "ON CONFLICT (version_id) DO NOTHING"
                    ),
                    {"vid": version_id},
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise RuntimeError(f"Migration {version_id} failed: {e}") from e
