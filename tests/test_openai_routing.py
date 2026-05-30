"""Tests for OpenAI fallback routing.

When the local llama3 planner returns type=unknown and OpenAI is configured,
the message should be routed to OpenAI for a general chat response.
"""
from unittest.mock import patch
from app.models import ChatMessage


class TestOpenAIFallbackRouting:
    """When planner returns unknown, route to OpenAI if configured."""

    def test_routes_to_openai_when_configured(self, paired_client, db):
        client, _user = paired_client
        context_only = {
            "type": "unknown",
            "data": {"reason": "ambiguous"},
            "reply": "What would you like?",
        }

        with patch("app.routers.chat.openai_client") as mock_oc, patch(
            "app.routers.chat.gather_context_only"
        ) as mock_gather_context, patch(
            "app.services.context_brain.llm_gateway.gateway_chat"
        ) as mock_gateway:
            mock_oc.is_configured.return_value = True
            mock_oc.SYSTEM_PROMPT = "You are CHILI."
            mock_gather_context.return_value = {
                "planned": context_only,
                "rag_context": None,
                "rag_hits": [],
                "personality_context": None,
                "brain_prompt": None,
            }
            mock_gateway.return_value = {
                "reply": "Hey! I'm CHILI, your household assistant.",
                "tokens_used": 42,
                "model": "gpt-4o-mini",
                "gateway_log_id": 123,
            }

            resp = client.post("/api/chat", data={"message": "tell me a joke"})

        mock_gather_context.assert_called_once()
        mock_gateway.assert_called_once()
        gateway_kwargs = mock_gateway.call_args.kwargs
        assert gateway_kwargs["purpose"] == "chat_user"
        assert gateway_kwargs["user_message"] == "tell me a joke"
        data = resp.json()

        assert data["action_type"] == "general_chat"
        assert data["model_used"] == "gpt-4o-mini"
        assert "CHILI" in data["reply"]

    @patch("app.routers.chat.openai_client")
    @patch("app.services.chat_service.plan_action")
    def test_falls_back_to_help_when_no_key(self, mock_plan, mock_oc, client, db):
        mock_plan.return_value = {
            "type": "unknown",
            "data": {"reason": "ambiguous"},
            "reply": "",
        }
        mock_oc.is_configured.return_value = False

        resp = client.post("/api/chat", data={"message": "tell me a joke"})
        data = resp.json()

        assert data["action_type"] == "unknown"
        assert "add chore" in data["reply"].lower()
        mock_oc.chat.assert_not_called()

    @patch("app.routers.chat.openai_client")
    @patch("app.services.chat_service.plan_action")
    def test_openai_error_falls_back_gracefully(self, mock_plan, mock_oc, client, db):
        mock_plan.return_value = {
            "type": "unknown",
            "data": {"reason": "ambiguous"},
            "reply": "",
        }
        mock_oc.is_configured.return_value = True
        mock_oc.SYSTEM_PROMPT = "You are CHILI."
        mock_oc.chat.return_value = {
            "reply": "",
            "tokens_used": 0,
            "model": "error",
        }

        resp = client.post("/api/chat", data={"message": "tell me a joke"})
        data = resp.json()

        assert "add chore" in data["reply"].lower()

    @patch("app.routers.chat.openai_client")
    @patch("app.services.chat_service.plan_action")
    def test_tool_actions_stay_local(self, mock_plan, mock_oc, paired_client, db):
        """Tool actions should stay local even when OpenAI is configured."""
        client, user = paired_client
        mock_plan.return_value = {
            "type": "list_chores",
            "data": {},
            "reply": "Here are your chores.",
        }
        mock_oc.is_configured.return_value = True

        resp = client.post("/api/chat", data={"message": "list chores"})
        data = resp.json()

        assert data["action_type"] == "list_chores"
        assert data["model_used"] == "nlu-fallback"
        mock_oc.chat.assert_not_called()


class TestModelUsedTracking:
    """model_used should be recorded on every assistant ChatMessage."""

    @patch("app.services.chat_service.plan_action")
    def test_local_tool_model_recorded(self, mock_plan, paired_client, db):
        client, user = paired_client
        mock_plan.return_value = {
            "type": "list_chores",
            "data": {},
            "reply": "No chores.",
        }

        client.post("/api/chat", data={"message": "list chores"})

        assistant_msg = db.query(ChatMessage).filter(
            ChatMessage.role == "assistant"
        ).first()
        assert assistant_msg.model_used == "nlu-fallback"

    @patch("app.routers.chat.openai_client")
    @patch("app.services.chat_service.plan_action", side_effect=Exception("offline"))
    def test_offline_model_recorded(self, mock_plan, mock_openai, client, db):
        mock_openai.is_configured.return_value = False
        client.post("/api/chat", data={"message": "hello"})

        assistant_msg = db.query(ChatMessage).filter(
            ChatMessage.role == "assistant"
        ).first()
        assert assistant_msg.model_used == "offline"
