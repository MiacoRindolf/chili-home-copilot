"""Shared fixtures for CHILI tests.

Requires PostgreSQL: set ``TEST_DATABASE_URL`` or ``DATABASE_URL`` to a
*dedicated* database (e.g. ``chili_test``) before running pytest. See
``docs/DATABASE_POSTGRES.md``.

``DATABASE_URL`` is set from ``TEST_DATABASE_URL`` when present. Schema is
applied the first time a test uses the ``db`` fixture, or when ``app.main``
loads for ``client``. Pure unit tests with no DB fixture skip DB setup.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text
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
    _hydrate_test_database_url_from_dotenv()
    raw = (os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not raw:
        raise RuntimeError(
            "Tests require PostgreSQL. Set TEST_DATABASE_URL (preferred) or DATABASE_URL to a "
            "dedicated test database, e.g. postgresql://chili:chili@localhost:5433/chili_test — "
            "add TEST_DATABASE_URL to your .env or your shell; see docs/DATABASE_POSTGRES.md"
        )
    lowered = raw.lower()
    if not (
        lowered.startswith("postgresql://")
        or lowered.startswith("postgresql+psycopg2://")
        or lowered.startswith("postgresql+psycopg://")
    ):
        raise RuntimeError(
            "TEST_DATABASE_URL / DATABASE_URL must be a PostgreSQL URL for pytest."
        )
    os.environ["DATABASE_URL"] = raw
    return raw


_ensure_postgres_test_url()

# Engine + models only — no ``app.main`` at import. The full app loads when
# ``client`` / ``fastapi_app`` is used (routers, scheduler hooks, pattern seeds).
from app.db import Base, engine  # noqa: E402
from app.deps import get_db  # noqa: E402
from app.models import User, Device  # noqa: E402
from app.pairing import DEVICE_COOKIE_NAME  # noqa: E402


_schema_initialized = False


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
        "pytest: loading app.main (routers + pattern seeds; schema already applied)...\n"
    )
    sys.stderr.flush()
    from app.main import app as _app

    _schema_initialized = True
    return _app


def _truncate_app_tables() -> None:
    """Remove row data between tests; keep schema_version so migrations are not re-run."""
    # Static neural mesh topology is seeded by migration 086; keep nodes/edges so tests
    # do not need to re-seed the graph definition every time.
    _skip_truncate = frozenset({"schema_version", "brain_graph_nodes", "brain_graph_edges"})
    names = [
        f'"{t.name}"'
        for t in Base.metadata.sorted_tables
        if t.name not in _skip_truncate
    ]
    if not names:
        return
    stmt = text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE")
    with engine.begin() as conn:
        conn.execute(stmt)


@pytest.fixture()
def db():
    """Yield a DB session; tables are truncated before/after so each test is isolated."""
    _bootstrap_test_schema()
    _truncate_app_tables()
    SessionTesting = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionTesting()
    try:
        yield session
    finally:
        session.close()
        _truncate_app_tables()


@pytest.fixture()
def client(db, fastapi_app):
    """FastAPI TestClient wired to the same PostgreSQL database as ``db``."""

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    with TestClient(fastapi_app) as c:
        yield c
    fastapi_app.dependency_overrides.clear()


@pytest.fixture()
def paired_client(db, client):
    """TestClient with a cookie representing a paired (non-guest) user."""
    user = User(name="TestUser")
    db.add(user)
    db.commit()
    db.refresh(user)

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
