"""Integration tests for /api/chat and /api/chat/history.

These test the full request flow through FastAPI: identity resolution,
guest enforcement, chat memory storage/retrieval, and LLM offline handling.
"""
from unittest.mock import patch
from app.models import ChatMessage, Chore


class TestGuestReadOnly:
    """Guests must never trigger write actions, even if the LLM plans one."""

    def _post_chat(self, client, message):
        return client.post("/api/chat", data={"message": message})

    @patch("app.services.chat_service.plan_action")
    def test_guest_add_chore_blocked(self, mock_plan, client, db):
        mock_plan.return_value = {
            "type": "add_chore",
            "data": {"title": "Hack the system"},
            "reply": "Added!",
        }
        resp = self._post_chat(client, "add chore hack")
        data = resp.json()

        assert data["is_guest"] is True
        assert "Guest mode is read-only" in data["reply"]
        assert db.query(Chore).count() == 0, "No chore should be created for guests"

    @patch("app.services.chat_service.plan_action")
    def test_guest_mark_done_blocked(self, mock_plan, client, db):
        db.add(Chore(title="Existing chore", done=False))
        db.commit()

        mock_plan.return_value = {
            "type": "mark_chore_done",
            "data": {"id": 1},
            "reply": "Marked done!",
        }
        resp = self._post_chat(client, "done 1")
        data = resp.json()

        assert data["is_guest"] is True
        chore = db.query(Chore).first()
        assert chore.done is False, "Guest must not be able to mark chores done"

    @patch("app.services.chat_service.plan_action")
    def test_paired_user_can_write(self, mock_plan, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {
            "type": "add_chore",
            "data": {"title": "Buy milk"},
            "reply": "Added chore: Buy milk",
        }
        resp = client.post("/api/chat", data={"message": "add chore buy milk"})
        data = resp.json()

        assert data["is_guest"] is False
        assert data["executed"] is True
        assert db.query(Chore).count() == 1


class TestLLMOffline:
    """When Ollama is down, the NLU fallback handles known patterns; otherwise returns offline."""

    @patch("app.services.chat_service.plan_action", side_effect=Exception("Connection refused"))
    def test_nlu_fallback_handles_list_chores(self, mock_plan, client):
        """NLU parser picks up 'list chores' when Ollama is offline."""
        resp = client.post("/api/chat", data={"message": "list chores"})
        data = resp.json()

        assert resp.status_code == 200
        assert data["action_type"] == "list_chores"
        assert data["model_used"] == "nlu-fallback"

    @patch("app.services.chat_service.plan_action", side_effect=Exception("Connection refused"))
    def test_nlu_fallback_guest_blocked_write(self, mock_plan, client, db):
        """NLU parser matches 'add chore' but guest is still blocked from writing."""
        resp = client.post("/api/chat", data={"message": "add chore sneak attack"})
        data = resp.json()

        assert data["action_type"] == "guest_blocked"
        assert db.query(Chore).count() == 0, "No chore created for guest even via NLU fallback"

    @patch("app.routers.chat.openai_client")
    @patch("app.services.chat_service.plan_action", side_effect=Exception("Connection refused"))
    def test_offline_unknown_message(self, mock_plan, mock_openai, client):
        """Unrecognized messages with no OpenAI configured return offline message."""
        mock_openai.is_configured.return_value = False
        resp = client.post("/api/chat", data={"message": "tell me a joke about cats"})
        data = resp.json()

        assert resp.status_code == 200
        assert data["action_type"] == "llm_offline"
        assert "offline" in data["reply"].lower()


class TestChatMemory:
    """Messages must be stored in chat_messages and retrievable via /api/chat/history."""

    @patch("app.services.chat_service.plan_action")
    def test_messages_stored(self, mock_plan, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {
            "type": "list_chores",
            "data": {},
            "reply": "No chores yet.",
        }
        client.post("/api/chat", data={"message": "list chores"})

        msgs = db.query(ChatMessage).all()
        assert len(msgs) == 2  # user message + assistant reply
        assert msgs[0].role == "user"
        assert msgs[0].content == "list chores"
        assert msgs[1].role == "assistant"
        assert msgs[1].content == "No chores yet."

    @patch("app.services.chat_service.plan_action")
    def test_convo_key_paired_user(self, mock_plan, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {
            "type": "unknown",
            "data": {"reason": "test"},
            "reply": "What?",
        }
        client.post("/api/chat", data={"message": "hello"})

        msg = db.query(ChatMessage).first()
        assert msg.convo_key == f"user:{user.id}"

    def test_convo_key_guest(self, client, db):
        """Guest convo_key should start with 'guest:'."""
        with patch("app.services.chat_service.plan_action", side_effect=Exception("offline")):
            client.post("/api/chat", data={"message": "hi"})

        msg = db.query(ChatMessage).first()
        assert msg.convo_key.startswith("guest:")

    @patch("app.services.chat_service.plan_action")
    def test_history_returns_messages(self, mock_plan, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {
            "type": "unknown",
            "data": {"reason": "test"},
            "reply": "Hello!",
        }
        client.post("/api/chat", data={"message": "hi"})

        resp = client.get("/api/chat/history")
        data = resp.json()

        assert data["convo_key"] == f"user:{user.id}"
        assert data["is_guest"] is False
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][1]["role"] == "assistant"

    @patch("app.services.chat_service.plan_action")
    def test_history_filters_empty_content(self, mock_plan, paired_client, db):
        client, user = paired_client
        db.add(ChatMessage(
            convo_key=f"user:{user.id}", role="assistant", content=""
        ))
        db.commit()

        resp = client.get("/api/chat/history")
        data = resp.json()
        assert len(data["messages"]) == 0, "Empty messages must be filtered out"
