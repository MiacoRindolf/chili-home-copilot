"""Version-tracked schema migrations for PostgreSQL. Run at app startup."""
from __future__ import annotations

import logging

from sqlalchemy import inspect as sa_inspect, text

logger = logging.getLogger(__name__)
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
                " promotion_status, created_at, updated_at) "
                "VALUES (:name, :desc, :rules, :origin, :ac, '1d', 0.0, 0, 0, 1.5, 4.0, "
                " TRUE, 0, 'universal', 0, 0, "
                " 'legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
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
            "(user_id, pattern_description, confidence, evidence_count, "
            " last_seen, created_at, active, win_count, loss_count) "
            f"VALUES (:uid, :desc, 0.5, 1, {now_sql}, {now_sql}, "
            f" {active_val}, 0, 0)"
        ), {"uid": uid, "desc": pat_desc})
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
            " promotion_status, created_at, updated_at) "
            "VALUES (:name, :desc, :rules, 'user_seeded', 'stocks', '1d', 0.5, "
            " 0, 0, 1.5, 4.0, TRUE, 0, 'universal', 0, 0, "
            " 'legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
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
                "(user_id, pattern_description, confidence, evidence_count, "
                " last_seen, created_at, active, win_count, loss_count) "
                f"VALUES (:uid, :desc, 0.5, 0, {now_sql}, {now_sql}, {active_val}, 0, 0)"
            ), {"uid": uid, "desc": pat_desc})
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
            " promotion_status, created_at, updated_at) "
            "VALUES (:name, :desc, :rules, 'user_seeded', 'all', '1d', 0.5, "
            " 0, 0, 1.5, 4.0, TRUE, 0, 'universal', 0, 0, "
            " 'legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
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
                "(user_id, pattern_description, confidence, evidence_count, "
                " last_seen, created_at, active, win_count, loss_count) "
                f"VALUES (:uid, :desc, 0.5, 0, {now_sql}, {now_sql}, {active_val}, 0, 0)"
            ), {"uid": uid, "desc": pat_desc})
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
                "created_at, updated_at) "
                "VALUES (:name, :desc, '{}', 'legacy_unlinked', 'all', '1d', 0.0, 0, 0, 0.0, 0.0, "
                "FALSE, 0, 'universal', 0, 0, 'legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
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
                "created_at, updated_at) "
                "VALUES (:name, :desc, '{}', 'legacy_unlinked', 'all', '1d', 0.0, 0, 0, 0.0, 0.0, "
                "FALSE, 0, 'universal', 0, 0, 'legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
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
        if gcfg is None:
            conn.execute(
                text(
                    """
                    INSERT INTO brain_graph_edges (
                        source_node_id, target_node_id, signal_type, weight, polarity,
                        delay_ms, min_confidence, enabled, graph_version, gate_config,
                        created_at, updated_at
                    ) VALUES (
                        :src, :tgt, :sig, :w, :pol,
                        0, 0.0, TRUE, :gv, NULL,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "gv": gv},
            )
        else:
            conn.execute(
                text(
                    """
                    INSERT INTO brain_graph_edges (
                        source_node_id, target_node_id, signal_type, weight, polarity,
                        delay_ms, min_confidence, enabled, graph_version, gate_config,
                        created_at, updated_at
                    ) VALUES (
                        :src, :tgt, :sig, :w, :pol,
                        0, 0.0, TRUE, :gv, CAST(:gcfg AS jsonb),
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "gv": gv, "gcfg": gcfg},
            )

    for nid, _, _, _, _, _, _ in nodes_seed:
        conn.execute(
            text(
                """
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state, staleness_at, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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

    for nid, _, _, _, _, _, _ in nodes:
        conn.execute(
            text(
                """
                INSERT INTO brain_node_states (
                    node_id, activation_score, confidence, local_state, staleness_at, updated_at
                )
                VALUES (:nid, 0.0, 0.5, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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
        conn.execute(
            text(
                """
                INSERT INTO brain_graph_edges (
                    source_node_id, target_node_id, signal_type, weight, polarity,
                    delay_ms, min_confidence, enabled, graph_version, gate_config,
                    created_at, updated_at
                )
                SELECT :src, :tgt, :sig, :w, :pol,
                    0, 0.0, TRUE, :gv, NULL,
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
            {"src": src, "tgt": tgt, "sig": sig, "w": w, "pol": pol, "gv": gv},
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
]


def run_migrations(engine: Engine) -> None:
    """Create schema_version table if missing, then run any migrations not yet applied."""
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
