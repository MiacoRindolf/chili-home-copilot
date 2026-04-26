"""GET /api/brain/dispatch/status (operator dispatch health)."""
from __future__ import annotations

import uuid

from app.models import Device, User


def _paired_with_unique_name(db, client):
    """Same as ``conftest.paired_client`` but unique ``User.name`` (avoids users_name_key collisions)."""
    user = User(name=f"TestUser_{uuid.uuid4().hex[:12]}")
    db.add(user)
    db.flush()
    from app.pairing import DEVICE_COOKIE_NAME

    token = f"test-device-token-{uuid.uuid4().hex}"
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


def test_status_requires_paired_user(client) -> None:
    r = client.get("/api/brain/dispatch/status")
    assert r.status_code == 403
    assert r.json().get("error") == "unauthorized"


def test_status_returns_expected_shape(db, client) -> None:
    client, _user = _paired_with_unique_name(db, client)
    r = client.get("/api/brain/dispatch/status")
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("kill_switch", "counters_5min", "spend_24h", "recent_runs"):
        assert k in d
    assert isinstance(d["counters_5min"], dict)
    assert isinstance(d["spend_24h"], list)
    assert isinstance(d["recent_runs"], list)
    assert "active" in d["kill_switch"]
