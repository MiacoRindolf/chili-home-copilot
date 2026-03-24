"""Version-tracked schema migrations for PostgreSQL. Run at app startup."""
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
                conn.execute(text("INSERT INTO schema_version (version_id) VALUES (:vid)"), {"vid": version_id})
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise RuntimeError(f"Migration {version_id} failed: {e}") from e
