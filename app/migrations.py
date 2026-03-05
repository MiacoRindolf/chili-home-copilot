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


# (version_id, callable that receives conn and runs migration)
MIGRATIONS = [
    ("001_add_email", _migration_001_add_email),
    ("002_add_image_path", _migration_002_add_image_path),
    ("003_conversations_project_id", _migration_003_conversations_project_id),
    ("004_chore_columns", _migration_004_chore_columns),
    ("005_plan_projects_key", _migration_005_plan_projects_key),
    ("006_plan_tasks_parent_reporter", _migration_006_plan_tasks_parent_reporter),
    ("007_backfill_project_members", _migration_007_backfill_project_members),
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
