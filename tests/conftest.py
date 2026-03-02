"""Shared fixtures for CHILI tests.

Creates an isolated in-memory SQLite DB per test so tests never touch
the real data/chili.db and can run in parallel without interference.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.db import Base
from app.main import app, get_db
from app.models import User, Device
from app.pairing import DEVICE_COOKIE_NAME


@pytest.fixture()
def db():
    """Yield a fresh DB session backed by an in-memory SQLite database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db):
    """FastAPI TestClient wired to the in-memory DB."""
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
    db.add(Device(
        token=token,
        user_id=user.id,
        label="Test Device",
        client_ip_last="127.0.0.1",
    ))
    db.commit()

    client.cookies.set(DEVICE_COOKIE_NAME, token)
    return client, user
