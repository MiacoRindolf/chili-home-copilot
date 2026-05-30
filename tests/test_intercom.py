"""Tests for the intercom feature: consent, status, voice messages, WebSocket auth."""
from types import SimpleNamespace

import pytest
from app.models import User, Device, UserStatus, IntercomConsent, IntercomMessage
from app.pairing import DEVICE_COOKIE_NAME
from app.services import intercom_service as svc


class _SequenceQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows


class _SequenceSession:
    def __init__(self, rows_by_query):
        self._rows_by_query = list(rows_by_query)
        self.query_calls = 0

    def query(self, *_args, **_kwargs):
        self.query_calls += 1
        return _SequenceQuery(self._rows_by_query.pop(0))


class TestIntercomConsent:
    """Consent REST API tests."""

    def test_guest_blocked(self, client):
        res = client.get("/api/intercom/consent")
        assert res.status_code == 403

    def test_check_consent_initially_false(self, paired_client):
        client, user = paired_client
        res = client.get("/api/intercom/consent")
        assert res.status_code == 200
        assert res.json()["consented"] is False

    def test_grant_consent(self, paired_client):
        client, user = paired_client
        res = client.post("/api/intercom/consent")
        assert res.status_code == 200
        assert res.json()["ok"] is True

        res = client.get("/api/intercom/consent")
        assert res.json()["consented"] is True

    def test_revoke_consent(self, paired_client):
        client, user = paired_client
        client.post("/api/intercom/consent")
        res = client.delete("/api/intercom/consent")
        assert res.status_code == 200

        res = client.get("/api/intercom/consent")
        assert res.json()["consented"] is False


class TestIntercomStatus:
    """Status REST API tests."""

    def test_guest_blocked(self, client):
        res = client.get("/api/intercom/status")
        assert res.status_code == 403

    def test_get_status_defaults_to_available(self, paired_client):
        client, user = paired_client
        res = client.get("/api/intercom/status")
        data = res.json()
        assert data["my_status"]["status"] == "available"
        assert data["user_id"] == user.id

    def test_set_dnd(self, paired_client):
        client, user = paired_client
        res = client.post("/api/intercom/status", data={"status": "dnd", "dnd_minutes": "60"})
        assert res.status_code == 200
        assert res.json()["status"] == "dnd"

    def test_set_available(self, paired_client):
        client, user = paired_client
        client.post("/api/intercom/status", data={"status": "dnd"})
        res = client.post("/api/intercom/status", data={"status": "available"})
        assert res.json()["status"] == "available"

    def test_invalid_status_rejected(self, paired_client):
        client, user = paired_client
        res = client.post("/api/intercom/status", data={"status": "sleeping"})
        assert res.status_code == 400


class TestIntercomMessages:
    """Voice message REST API tests."""

    def test_guest_blocked(self, client):
        res = client.get("/api/intercom/messages")
        assert res.status_code == 403

    def test_empty_messages(self, paired_client):
        client, user = paired_client
        res = client.get("/api/intercom/messages")
        assert res.status_code == 200
        assert res.json()["messages"] == []

    def test_mark_read(self, paired_client, db):
        client, user = paired_client
        msg = svc.save_voice_message(
            from_user_id=None, to_user_id=user.id,
            audio_bytes=b"\x00" * 100, duration_ms=1000,
            is_broadcast=False, db=db,
        )
        res = client.post(f"/api/intercom/messages/{msg.id}/read")
        assert res.status_code == 200
        assert res.json()["ok"] is True

    def test_delete_message_as_recipient(self, paired_client, db):
        client, user = paired_client
        msg = svc.save_voice_message(
            from_user_id=None, to_user_id=user.id,
            audio_bytes=b"\x00" * 100, duration_ms=1000,
            is_broadcast=False, db=db,
        )
        res = client.delete(f"/api/intercom/messages/{msg.id}")
        assert res.status_code == 200
        assert res.json()["ok"] is True
        assert db.query(IntercomMessage).filter(IntercomMessage.id == msg.id).first() is None

    def test_delete_message_404_for_invalid_id(self, paired_client):
        client, _ = paired_client
        res = client.delete("/api/intercom/messages/999999")
        assert res.status_code == 404


class TestIntercomService:
    """Unit tests for the intercom service layer."""

    def test_consent_flow(self, paired_client, db):
        _, user = paired_client
        assert svc.has_consent(user.id, db) is False
        svc.grant_consent(user.id, db)
        assert svc.has_consent(user.id, db) is True
        svc.revoke_consent(user.id, db)
        assert svc.has_consent(user.id, db) is False

    def test_status_defaults(self, paired_client, db):
        _, user = paired_client
        st = svc.get_user_status(user.id, db)
        assert st["status"] == "available"

    def test_dnd_toggle(self, paired_client, db):
        _, user = paired_client
        svc.set_user_status(user.id, "dnd", 30, db)
        assert svc.is_dnd(user.id, db) is True
        svc.set_user_status(user.id, "available", None, db)
        assert svc.is_dnd(user.id, db) is False

    def test_save_and_retrieve_voice_message(self, paired_client, db):
        _, user = paired_client
        audio = b"\x00\x01\x02" * 100
        msg = svc.save_voice_message(None, user.id, audio, 2000, False, db)
        assert msg.id is not None
        assert msg.audio_path.endswith(".webm")

        msgs = svc.get_all_messages(user.id, db)
        assert len(msgs) >= 1
        assert msgs[0]["id"] == msg.id

    def test_unread_messages(self, paired_client, db):
        _, user = paired_client
        msg = svc.save_voice_message(None, user.id, b"\x00" * 50, 500, False, db)
        unreads = svc.get_unread_messages(user.id, db)
        assert any(m["id"] == msg.id for m in unreads)

        svc.mark_read(msg.id, user.id, db)
        unreads = svc.get_unread_messages(user.id, db)
        assert not any(m["id"] == msg.id for m in unreads)

    def test_message_listing_batches_user_name_lookup(self):
        messages = [
            SimpleNamespace(
                id=1,
                from_user_id=2,
                to_user_id=1,
                is_broadcast=False,
                audio_path="one.webm",
                duration_ms=100,
                delivered=True,
                read=False,
                created_at="now",
            ),
            SimpleNamespace(
                id=2,
                from_user_id=3,
                to_user_id=1,
                is_broadcast=False,
                audio_path="two.webm",
                duration_ms=200,
                delivered=True,
                read=False,
                created_at="now",
            ),
        ]
        users = [
            SimpleNamespace(id=1, name="Recipient"),
            SimpleNamespace(id=2, name="Sender A"),
            SimpleNamespace(id=3, name="Sender B"),
        ]
        db = _SequenceSession([messages, users])

        result = svc.get_all_messages(user_id=1, db=db)

        assert [m["from_name"] for m in result] == ["Sender A", "Sender B"]
        assert [m["to_name"] for m in result] == ["Recipient", "Recipient"]
        assert db.query_calls == 2

    def test_all_statuses(self, paired_client, db):
        _, user = paired_client
        statuses = svc.get_all_statuses(db)
        assert len(statuses) >= 1
        assert any(s["user_id"] == user.id for s in statuses)


class TestWebSocketAuth:
    """WebSocket connection authentication tests."""

    def test_unauthenticated_ws_rejected(self, client):
        with client.websocket_connect("/ws/intercom") as ws:
            data = ws.receive_json()
            assert data["type"] == "ERROR"

    def test_no_consent_ws_gets_need_consent(self, paired_client):
        client, user = paired_client
        with client.websocket_connect("/ws/intercom") as ws:
            data = ws.receive_json()
            assert data["type"] == "NEED_CONSENT"

    def test_consented_ws_connects(self, paired_client, db):
        client, user = paired_client
        svc.grant_consent(user.id, db)
        with client.websocket_connect("/ws/intercom") as ws:
            data = ws.receive_json()
            assert data["type"] == "PRESENCE"


class TestNLUBroadcast:
    """Test NLU fallback recognizes broadcast commands."""

    def test_announce_command(self):
        from app.chili_nlu import parse_message
        result = parse_message("announce dinner is ready")
        assert result.type == "intercom_broadcast"
        assert result.data["text"] == "dinner is ready"

    def test_broadcast_command(self):
        from app.chili_nlu import parse_message
        result = parse_message("broadcast lights out in 10 minutes")
        assert result.type == "intercom_broadcast"
        assert result.data["text"] == "lights out in 10 minutes"
