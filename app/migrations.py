"""Lightweight version-tracked migrations for SQLite. Run at app startup."""
from __future__ import annotations

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.engine import Engine


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
            "INSERT INTO project_members (project_id, user_id, role, joined_at) VALUES (:pid, :uid, 'owner', datetime('now'))"
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
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
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
                last_seen TEXT NOT NULL DEFAULT (datetime('now')),
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
                snapshot_date TEXT NOT NULL DEFAULT (datetime('now'))
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
                snapshot_date TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
    if "code_learning_events" not in tables:
        conn.execute(text("""
            CREATE TABLE code_learning_events (
                id INTEGER PRIMARY KEY,
                user_id INTEGER, repo_id INTEGER, event_type TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
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
                last_seen TEXT NOT NULL DEFAULT (datetime('now')),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
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
                searched_at TEXT NOT NULL DEFAULT (datetime('now')),
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
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
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
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
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
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
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
                snapshot_date TEXT NOT NULL DEFAULT (datetime('now'))
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
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER NOT NULL,
                source_file VARCHAR(500) NOT NULL,
                target_file VARCHAR(500) NOT NULL,
                import_name VARCHAR(300),
                is_circular BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("CREATE INDEX ix_code_dep_repo ON code_dependencies(repo_id)"))
    if "code_quality_snapshots" not in tables:
        conn.execute(text("""
            CREATE TABLE code_quality_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER NOT NULL,
                total_files INTEGER DEFAULT 0,
                total_lines INTEGER DEFAULT 0,
                avg_complexity REAL DEFAULT 0.0,
                max_complexity REAL DEFAULT 0.0,
                test_file_count INTEGER DEFAULT 0,
                test_ratio REAL DEFAULT 0.0,
                hotspot_count INTEGER DEFAULT 0,
                insight_count INTEGER DEFAULT 0,
                recorded_at DATETIME DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("CREATE INDEX ix_code_qs_repo ON code_quality_snapshots(repo_id)"))
    if "code_reviews" not in tables:
        conn.execute(text("""
            CREATE TABLE code_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER NOT NULL,
                user_id INTEGER,
                commit_hash VARCHAR(50) NOT NULL,
                author VARCHAR(200),
                summary TEXT,
                findings_json TEXT,
                overall_score REAL DEFAULT 5.0,
                reviewed_at DATETIME DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("CREATE INDEX ix_code_rev_repo ON code_reviews(repo_id)"))
        conn.execute(text("CREATE INDEX ix_code_rev_hash ON code_reviews(commit_hash)"))
    if "code_dep_alerts" not in tables:
        conn.execute(text("""
            CREATE TABLE code_dep_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER NOT NULL,
                package_name VARCHAR(200) NOT NULL,
                current_version VARCHAR(50),
                latest_version VARCHAR(50),
                severity VARCHAR(20) NOT NULL DEFAULT 'info',
                alert_type VARCHAR(30) NOT NULL DEFAULT 'outdated',
                ecosystem VARCHAR(10) NOT NULL DEFAULT 'pip',
                resolved BOOLEAN NOT NULL DEFAULT 0,
                detected_at DATETIME DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("CREATE INDEX ix_code_depalert_repo ON code_dep_alerts(repo_id)"))
    if "code_search_index" not in tables:
        conn.execute(text("""
            CREATE TABLE code_search_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER NOT NULL,
                file_path VARCHAR(500) NOT NULL,
                symbol_name VARCHAR(300) NOT NULL,
                symbol_type VARCHAR(20) NOT NULL,
                signature TEXT,
                docstring TEXT,
                line_number INTEGER DEFAULT 0,
                indexed_at DATETIME DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("CREATE INDEX ix_code_search_repo ON code_search_index(repo_id)"))
        conn.execute(text("CREATE INDEX ix_code_search_sym ON code_search_index(symbol_name)"))
    conn.commit()


def _migration_018_breakout_alert_outcome_cols(conn) -> None:
    """Add exit-optimization and context columns to trading_breakout_alerts."""
    tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    if "trading_breakout_alerts" not in tables:
        return
    existing = {r[1] for r in conn.execute(text("PRAGMA table_info(trading_breakout_alerts)")).fetchall()}
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
    tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}

    if "project_agent_states" not in tables:
        conn.execute(text("""
            CREATE TABLE project_agent_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name VARCHAR(50) NOT NULL,
                user_id INTEGER,
                state_json TEXT,
                confidence REAL DEFAULT 0.0,
                last_cycle_at DATETIME,
                created_at DATETIME DEFAULT (datetime('now')),
                updated_at DATETIME DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("CREATE INDEX ix_pas_agent ON project_agent_states(agent_name)"))

    if "agent_findings" not in tables:
        conn.execute(text("""
            CREATE TABLE agent_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name VARCHAR(50) NOT NULL,
                user_id INTEGER,
                category VARCHAR(50) NOT NULL,
                title VARCHAR(300) NOT NULL,
                description TEXT NOT NULL,
                severity VARCHAR(20) NOT NULL DEFAULT 'info',
                evidence_json TEXT,
                status VARCHAR(20) NOT NULL DEFAULT 'new',
                created_at DATETIME DEFAULT (datetime('now')),
                updated_at DATETIME DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("CREATE INDEX ix_af_agent ON agent_findings(agent_name)"))

    if "agent_research" not in tables:
        conn.execute(text("""
            CREATE TABLE agent_research (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name VARCHAR(50) NOT NULL,
                user_id INTEGER,
                topic VARCHAR(300) NOT NULL,
                query VARCHAR(500) NOT NULL,
                summary TEXT NOT NULL,
                sources_json TEXT,
                relevance_score REAL DEFAULT 0.0,
                searched_at DATETIME DEFAULT (datetime('now')),
                stale BOOLEAN DEFAULT 0
            )
        """))
        conn.execute(text("CREATE INDEX ix_ar_agent ON agent_research(agent_name)"))

    if "agent_goals" not in tables:
        conn.execute(text("""
            CREATE TABLE agent_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name VARCHAR(50) NOT NULL,
                user_id INTEGER,
                description TEXT NOT NULL,
                goal_type VARCHAR(30) NOT NULL DEFAULT 'learn',
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                progress REAL DEFAULT 0.0,
                evidence_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now')),
                completed_at DATETIME
            )
        """))
        conn.execute(text("CREATE INDEX ix_ag_agent ON agent_goals(agent_name)"))

    if "agent_evolutions" not in tables:
        conn.execute(text("""
            CREATE TABLE agent_evolutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name VARCHAR(50) NOT NULL,
                user_id INTEGER,
                dimension VARCHAR(100) NOT NULL,
                description TEXT NOT NULL,
                confidence_before REAL DEFAULT 0.0,
                confidence_after REAL DEFAULT 0.0,
                trigger VARCHAR(200) NOT NULL DEFAULT 'cycle',
                created_at DATETIME DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("CREATE INDEX ix_ae_agent ON agent_evolutions(agent_name)"))

    if "agent_messages" not in tables:
        conn.execute(text("""
            CREATE TABLE agent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_agent VARCHAR(50) NOT NULL,
                to_agent VARCHAR(50) NOT NULL,
                user_id INTEGER,
                message_type VARCHAR(50) NOT NULL,
                content_json TEXT NOT NULL,
                acknowledged BOOLEAN DEFAULT 0,
                created_at DATETIME DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("CREATE INDEX ix_am_to ON agent_messages(to_agent)"))

    if "po_questions" not in tables:
        conn.execute(text("""
            CREATE TABLE po_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                question TEXT NOT NULL,
                context TEXT,
                category VARCHAR(50) NOT NULL DEFAULT 'general',
                priority INTEGER DEFAULT 5,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                answer TEXT,
                asked_at DATETIME DEFAULT (datetime('now')),
                answered_at DATETIME
            )
        """))

    if "po_requirements" not in tables:
        conn.execute(text("""
            CREATE TABLE po_requirements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title VARCHAR(300) NOT NULL,
                description TEXT NOT NULL,
                priority VARCHAR(20) NOT NULL DEFAULT 'medium',
                status VARCHAR(20) NOT NULL DEFAULT 'draft',
                acceptance_criteria TEXT,
                source_questions_json TEXT,
                created_at DATETIME DEFAULT (datetime('now')),
                updated_at DATETIME DEFAULT (datetime('now'))
            )
        """))

    conn.commit()


# (version_id, callable that receives conn and runs migration)
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
]


def run_migrations(engine: Engine) -> None:
    """Create schema_version table if missing, then run any migrations not yet applied."""
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_version (version_id TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
        ))
        conn.commit()

        applied = {row[0] for row in conn.execute(text("SELECT version_id FROM schema_version")).fetchall()}

        for version_id, migrate_fn in MIGRATIONS:
            if version_id in applied:
                continue
            try:
                migrate_fn(conn)
                conn.execute(text("INSERT INTO schema_version (version_id) VALUES (:vid)"), {"vid": version_id})
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise RuntimeError(f"Migration {version_id} failed: {e}") from e
