"""Shared fixtures for CHILI tests.

Requires PostgreSQL: set ``TEST_DATABASE_URL`` or ``DATABASE_URL`` to a
*dedicated* database (e.g. ``chili_test``) before running pytest. See
``docs/DATABASE_POSTGRES.md``.

``DATABASE_URL`` is set from ``TEST_DATABASE_URL`` when present so imports of
``app.main`` use the test database (schema is created at import time).
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient


def _ensure_postgres_test_url() -> str:
    raw = (os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not raw:
        raise RuntimeError(
            "Tests require PostgreSQL. Set TEST_DATABASE_URL (preferred) or DATABASE_URL to a "
            "dedicated test database, e.g. postgresql://user:pass@localhost:5432/chili_test — "
            "see docs/DATABASE_POSTGRES.md"
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

# Import app only after DATABASE_URL is set (config validates at import).
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.deps import get_db  # noqa: E402
from app.models import User, Device  # noqa: E402
from app.pairing import DEVICE_COOKIE_NAME  # noqa: E402


def _truncate_app_tables() -> None:
    """Remove row data between tests; keep schema_version so migrations are not re-run."""
    names = [
        f'"{t.name}"'
        for t in Base.metadata.sorted_tables
        if t.name != "schema_version"
    ]
    if not names:
        return
    stmt = text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE")
    with engine.begin() as conn:
        conn.execute(stmt)


@pytest.fixture()
def db():
    """Yield a DB session; tables are truncated before/after so each test is isolated."""
    _truncate_app_tables()
    SessionTesting = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionTesting()
    try:
        yield session
    finally:
        session.close()
        _truncate_app_tables()


@pytest.fixture()
def client(db):
    """FastAPI TestClient wired to the same PostgreSQL database as ``db``."""

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


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
