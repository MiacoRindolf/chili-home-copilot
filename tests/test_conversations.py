"""Tests for conversation management and streaming endpoints."""
import pytest
from unittest.mock import patch, MagicMock

from app.models import Conversation, ChatMessage


class TestConversationCRUD:
    """Conversation list, create, delete endpoints."""

    def test_guest_gets_empty_list(self, client):
        resp = client.get("/api/conversations")
        data = resp.json()
        assert data["is_guest"] is True
        assert data["conversations"] == []

    def test_paired_user_empty_list(self, paired_client):
        client, user = paired_client
        resp = client.get("/api/conversations")
        data = resp.json()
        assert data["is_guest"] is False
        assert data["conversations"] == []

    def test_create_conversation(self, paired_client):
        client, user = paired_client
        resp = client.post("/api/conversations")
        data = resp.json()
        assert "id" in data
        assert data["title"] == "New Chat"

    def test_create_then_list(self, paired_client):
        client, user = paired_client
        client.post("/api/conversations")
        client.post("/api/conversations")
        resp = client.get("/api/conversations")
        data = resp.json()
        assert len(data["conversations"]) == 2

    def test_delete_conversation(self, paired_client, db):
        client, user = paired_client
        resp = client.post("/api/conversations")
        convo_id = resp.json()["id"]

        del_resp = client.delete(f"/api/conversations/{convo_id}")
        assert del_resp.json()["ok"] is True

        list_resp = client.get("/api/conversations")
        assert len(list_resp.json()["conversations"]) == 0

    def test_delete_nonexistent(self, paired_client):
        client, user = paired_client
        resp = client.delete("/api/conversations/9999")
        assert resp.status_code == 404

    def test_guest_cannot_create(self, client):
        resp = client.post("/api/conversations")
        assert resp.status_code == 403


class TestConversationChat:
    """Chat with conversation_id support."""

    @patch("app.services.chat_service.plan_action")
    def test_auto_creates_conversation_for_paired_user(self, mock_plan, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {
            "type": "list_chores",
            "data": {},
            "reply": "No chores yet.",
        }
        resp = client.post("/api/chat", data={"message": "list chores"})
        data = resp.json()
        assert data["conversation_id"] is not None

        convos = client.get("/api/conversations").json()["conversations"]
        assert len(convos) == 1

    @patch("app.services.chat_service.plan_action")
    def test_auto_title_from_first_message(self, mock_plan, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {
            "type": "list_chores",
            "data": {},
            "reply": "No chores.",
        }
        client.post("/api/chat", data={"message": "what are my chores?"})

        convos = client.get("/api/conversations").json()["conversations"]
        assert convos[0]["title"] == "what are my chores?"

    @patch("app.services.chat_service.plan_action")
    def test_history_by_conversation_id(self, mock_plan, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {
            "type": "list_chores",
            "data": {},
            "reply": "Hello from convo 1",
        }
        r1 = client.post("/api/chat", data={"message": "hi"})
        convo_1 = r1.json()["conversation_id"]

        mock_plan.return_value = {
            "type": "list_chores",
            "data": {},
            "reply": "Hello from convo 2",
        }
        r2 = client.post("/api/chat", data={"message": "hey"})
        convo_2 = r2.json()["conversation_id"]

        hist1 = client.get(f"/api/chat/history?conversation_id={convo_1}").json()
        hist2 = client.get(f"/api/chat/history?conversation_id={convo_2}").json()

        assert any("Hello from convo 1" in m["content"] for m in hist1["messages"])
        assert any("Hello from convo 2" in m["content"] for m in hist2["messages"])

    @patch("app.services.chat_service.plan_action")
    def test_guest_no_conversation_id(self, mock_plan, client, db):
        mock_plan.return_value = {
            "type": "list_chores",
            "data": {},
            "reply": "No chores.",
        }
        resp = client.post("/api/chat", data={"message": "list chores"})
        data = resp.json()
        assert data["conversation_id"] is None


class TestStreamingEndpoint:
    """SSE streaming endpoint tests."""

    @patch("app.services.chat_service.plan_action")
    def test_stream_tool_action(self, mock_plan, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {
            "type": "list_chores",
            "data": {},
            "reply": "No chores yet.",
        }
        resp = client.post("/api/chat/stream", data={"message": "list chores"})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

        lines = resp.text.strip().split("\n")
        events = [l for l in lines if l.startswith("data: ")]
        assert len(events) >= 2

        import json
        last_event = json.loads(events[-1].replace("data: ", ""))
        assert last_event["done"] is True
        assert last_event["action_type"] == "list_chores"
        assert last_event["model_used"] == "llama3"

    @patch("app.routers.chat.openai_client")
    @patch("app.services.chat_service.plan_action")
    def test_stream_offline(self, mock_plan, mock_openai, paired_client, db):
        client, user = paired_client
        mock_plan.side_effect = Exception("Ollama is down")
        mock_openai.is_configured.return_value = False

        resp = client.post("/api/chat/stream", data={"message": "hello"})
        assert resp.status_code == 200

        import json
        lines = resp.text.strip().split("\n")
        events = [l for l in lines if l.startswith("data: ")]

        last_event = json.loads(events[-1].replace("data: ", ""))
        assert last_event["done"] is True
        assert last_event["action_type"] == "llm_offline"

    @patch("app.routers.chat.openai_client")
    @patch("app.services.chat_service.plan_action")
    def test_stream_openai_fallback(self, mock_plan, mock_openai, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {
            "type": "unknown",
            "data": {"reason": "general"},
            "reply": "",
        }
        mock_openai.is_configured.return_value = True
        mock_openai.SYSTEM_PROMPT = "Test system"
        mock_openai.OPENAI_MODEL = "gpt-test"
        mock_openai.chat_stream.return_value = iter(["Hello", " world", "!"])

        resp = client.post("/api/chat/stream", data={"message": "tell me a joke"})
        assert resp.status_code == 200

        import json
        lines = resp.text.strip().split("\n")
        events = [l for l in lines if l.startswith("data: ")]

        tokens = []
        done_event = None
        for evt_str in events:
            evt = json.loads(evt_str.replace("data: ", ""))
            if evt["done"]:
                done_event = evt
            elif evt.get("token"):
                tokens.append(evt["token"])

        assert "".join(tokens) == "Hello world!"
        assert done_event["action_type"] == "general_chat"
        assert done_event["model_used"] == "gpt-test"


class TestExecuteTool:
    """Tests for the _execute_tool helper."""

    def test_guest_blocked(self, db):
        from app.services.chat_service import execute_tool as _execute_tool
        reply, executed, atype = _execute_tool(db, "add_chore", {"title": "x"}, "", True)
        assert atype == "guest_blocked"
        assert executed is False
        assert "read-only" in reply

    def test_list_chores_empty(self, db):
        from app.services.chat_service import execute_tool as _execute_tool
        reply, executed, atype = _execute_tool(db, "list_chores", {}, "", False)
        assert executed is True
        assert "No chores" in reply


class TestNLUFallback:
    """Tests for the rule-based NLU fallback when Ollama is offline."""

    def test_nlu_fallback_known_action(self):
        from app.services.chat_service import nlu_fallback
        result = nlu_fallback("list chores")
        assert result is not None
        assert result["type"] == "list_chores"

    def test_nlu_fallback_add_chore(self):
        from app.services.chat_service import nlu_fallback
        result = nlu_fallback("add chore buy milk")
        assert result is not None
        assert result["type"] == "add_chore"
        assert result["data"]["title"] == "buy milk"

    def test_nlu_fallback_unknown(self):
        from app.services.chat_service import nlu_fallback
        result = nlu_fallback("tell me a funny joke")
        assert result is None

    @patch("app.routers.chat.openai_client")
    @patch("app.services.chat_service.plan_action", side_effect=Exception("offline"))
    def test_nlu_fallback_stream(self, mock_plan, mock_openai, paired_client, db):
        """Streaming endpoint uses NLU fallback when Ollama is offline."""
        client, user = paired_client
        mock_openai.is_configured.return_value = False

        resp = client.post("/api/chat/stream", data={"message": "list chores"})
        assert resp.status_code == 200

        import json
        lines = resp.text.strip().split("\n")
        events = [l for l in lines if l.startswith("data: ")]
        last_event = json.loads(events[-1].replace("data: ", ""))
        assert last_event["done"] is True
        assert last_event["model_used"] == "nlu-fallback"


class TestConversationExport:
    """Tests for conversation export endpoint."""

    @patch("app.services.chat_service.plan_action")
    def test_export_json(self, mock_plan, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {"type": "list_chores", "data": {}, "reply": "No chores."}
        r = client.post("/api/chat", data={"message": "hello"})
        convo_id = r.json()["conversation_id"]

        resp = client.get(f"/api/conversations/{convo_id}/export?fmt=json")
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("content-type", "")

        import json
        data = json.loads(resp.text)
        assert "conversation" in data
        assert "messages" in data
        assert len(data["messages"]) >= 2

    @patch("app.services.chat_service.plan_action")
    def test_export_markdown(self, mock_plan, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {"type": "list_chores", "data": {}, "reply": "No chores."}
        r = client.post("/api/chat", data={"message": "hello"})
        convo_id = r.json()["conversation_id"]

        resp = client.get(f"/api/conversations/{convo_id}/export?fmt=md")
        assert resp.status_code == 200
        assert "text/markdown" in resp.headers.get("content-type", "")
        assert "**CHILI**" in resp.text

    def test_export_nonexistent(self, paired_client):
        client, user = paired_client
        resp = client.get("/api/conversations/9999/export")
        assert resp.status_code == 404


class TestGuestChatVisibility:
    """Tests for housemate viewing guest conversations."""

    def _seed_guest_messages(self, db):
        """Insert guest chat messages directly into the DB."""
        db.add(ChatMessage(convo_key="guest:test-ip", role="user", content="hello from guest", trace_id="t1"))
        db.add(ChatMessage(convo_key="guest:test-ip", role="assistant", content="Hi guest!", trace_id="t1", model_used="llama3"))
        db.commit()

    def test_guest_cannot_list_guest_chats(self, client):
        resp = client.get("/api/conversations/guests")
        assert resp.status_code == 403

    def test_housemate_sees_guest_chats(self, paired_client, db):
        self._seed_guest_messages(db)
        pc, user = paired_client
        resp = pc.get("/api/conversations/guests")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["guest_conversations"]) >= 1
        assert data["guest_conversations"][0]["convo_key"] == "guest:test-ip"

    def test_housemate_view_guest_history(self, paired_client, db):
        self._seed_guest_messages(db)
        pc, user = paired_client

        resp = pc.get("/api/chat/guest-history?guest_convo_key=guest:test-ip")
        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert any("hello from guest" in m["content"] for m in msgs)

    def test_housemate_reply_to_guest(self, paired_client, db):
        self._seed_guest_messages(db)
        pc, user = paired_client

        resp = pc.post("/api/chat/guest-reply", data={"guest_convo_key": "guest:test-ip", "message": "I can help!"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        history = pc.get("/api/chat/guest-history?guest_convo_key=guest:test-ip").json()
        assert any("[TestUser]" in m["content"] for m in history["messages"])

    def test_guest_cannot_reply(self, client, db):
        self._seed_guest_messages(db)
        resp = client.post("/api/chat/guest-reply", data={"guest_convo_key": "guest:test-ip", "message": "sneaky"})
        assert resp.status_code == 403

    def test_invalid_convo_key_rejected(self, paired_client, db):
        pc, user = paired_client
        resp = pc.get("/api/chat/guest-history?guest_convo_key=user:1")
        assert resp.status_code == 400
