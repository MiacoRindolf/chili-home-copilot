from unittest.mock import patch

from app.models import ChatMessage, User, Device
from app.pairing import DEVICE_COOKIE_NAME


def test_mobile_chat_works_for_guest(client, db):
    resp = client.post("/api/mobile/chat", json={"message": "hello from mobile"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"] == "Guest"
    assert data["is_guest"] is True
    assert "reply" in data

    msgs = db.query(ChatMessage).all()
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].content == "hello from mobile"


def test_mobile_chat_respects_bearer_token(client, db):
    user = User(name="MobileUser")
    db.add(user)
    db.commit()
    db.refresh(user)

    token = "mobile-test-token"
    db.add(
        Device(
            token=token,
            user_id=user.id,
            label="Mobile Device",
            client_ip_last="127.0.0.1",
        )
    )
    db.commit()

    resp = client.post(
        "/api/mobile/chat",
        json={"message": "hi mobile"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_guest"] is False
    assert data["user"] == "MobileUser"


def test_mobile_pair_flow_issues_token(client, db):
    user = User(name="PairUser", email="pair@example.com")
    db.add(user)
    db.commit()

    with patch("app.email_service.is_configured", return_value=False):
        resp_req = client.post(
            "/api/pair/request",
            json={"email": "pair@example.com"},
        )
    assert resp_req.status_code == 200
    code = resp_req.json()["dev_code"]

    resp_verify = client.post(
        "/api/pair/verify",
        json={"code": code, "label": "My Phone"},
    )
    assert resp_verify.status_code == 200
    data = resp_verify.json()
    assert data["ok"] is True
    assert data["user_name"] == "PairUser"
    assert "token" in data

