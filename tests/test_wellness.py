"""Tests for the wellness detection module."""
import pytest
from unittest.mock import patch, MagicMock

from app.wellness import (
    detect_crisis,
    detect_wellness_topic,
    CRISIS_RESPONSE,
    wellness_chat,
)


class TestCrisisDetection:
    def test_suicide_keyword(self):
        assert detect_crisis("I want to commit suicide") is True

    def test_suicidal(self):
        assert detect_crisis("I've been feeling suicidal lately") is True

    def test_kill_myself(self):
        assert detect_crisis("I want to kill myself") is True

    def test_want_to_die(self):
        assert detect_crisis("I just want to die") is True

    def test_self_harm(self):
        assert detect_crisis("I've been self-harming") is True

    def test_overdose(self):
        assert detect_crisis("thinking about overdose") is True

    def test_better_off_dead(self):
        assert detect_crisis("everyone would be better off dead") is True

    def test_no_reason_to_live(self):
        assert detect_crisis("I have no reason to live") is True

    def test_normal_message(self):
        assert detect_crisis("I need help with my chores") is False

    def test_empty(self):
        assert detect_crisis("") is False

    def test_unrelated_death_mention(self):
        assert detect_crisis("the battery is dead") is False


class TestWellnessDetection:
    def test_feeling_sad(self):
        assert detect_wellness_topic("I'm feeling sad today") is True

    def test_depressed(self):
        assert detect_wellness_topic("I think I'm depressed") is True

    def test_anxiety(self):
        assert detect_wellness_topic("my anxiety is really bad") is True

    def test_panic_attack(self):
        assert detect_wellness_topic("I had a panic attack") is True

    def test_cant_sleep(self):
        assert detect_wellness_topic("I can't sleep at all") is True

    def test_mental_health(self):
        assert detect_wellness_topic("I want to talk about mental health") is True

    def test_feeling_lonely(self):
        assert detect_wellness_topic("I'm feeling so lonely") is True

    def test_burnout(self):
        assert detect_wellness_topic("I'm really burned out") is True

    def test_stressed(self):
        assert detect_wellness_topic("I'm so stressed about everything") is True

    def test_therapy(self):
        assert detect_wellness_topic("should I try therapy") is True

    def test_normal_message(self):
        assert detect_wellness_topic("what's for dinner tonight") is False

    def test_chore_message(self):
        assert detect_wellness_topic("add clean kitchen to chores") is False

    def test_empty(self):
        assert detect_wellness_topic("") is False


class TestCrisisIntegration:
    """Test that crisis detection takes priority over wellness detection."""
    def test_crisis_trumps_wellness(self):
        msg = "I'm so depressed I want to kill myself"
        assert detect_crisis(msg) is True
        assert detect_wellness_topic(msg) is True


class TestWellnessChat:
    @patch("app.wellness.requests.post")
    def test_ollama_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": {"content": "I hear you."}}
        mock_post.return_value = mock_resp

        result = wellness_chat(
            messages=[{"role": "user", "content": "I'm feeling sad"}],
            user_name="Alice",
        )
        assert result["reply"] == "I hear you."
        assert "wellness" in result["model"]

    @patch("app.wellness.requests.post", side_effect=Exception("connection refused"))
    @patch("app.openai_client.is_configured", return_value=True)
    @patch("app.openai_client.chat", return_value={"reply": "I'm here for you.", "model": "llama-3.3"})
    def test_fallback_to_groq(self, mock_chat, mock_configured, mock_post):
        result = wellness_chat(
            messages=[{"role": "user", "content": "I'm feeling anxious"}],
            user_name="Bob",
        )
        assert result["reply"] == "I'm here for you."
        assert "wellness" in result["model"]

    @patch("app.wellness.requests.post", side_effect=Exception("connection refused"))
    @patch("app.openai_client.is_configured", return_value=False)
    def test_both_offline(self, mock_configured, mock_post):
        result = wellness_chat(
            messages=[{"role": "user", "content": "I feel lost"}],
            user_name="Carol",
        )
        assert "try again" in result["reply"].lower()
        assert result["model"] == "offline-wellness"
