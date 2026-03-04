"""Tests for voice assistant: service, command parsing, and API endpoints."""
import pytest
from unittest.mock import patch, MagicMock
from io import BytesIO

from app.services.voice_service import (
    parse_voice_command, transcribe_audio, get_voice_capabilities,
)


class TestParseVoiceCommand:
    def test_list_chores(self):
        result = parse_voice_command("what are my chores")
        assert result["matched"] is True
        assert result["action"] == "list_chores"

    def test_list_chores_variant(self):
        result = parse_voice_command("What's the todos")
        assert result["matched"] is True
        assert result["action"] == "list_chores"

    def test_list_tasks(self):
        result = parse_voice_command("what are the tasks")
        assert result["matched"] is True
        assert result["action"] == "list_chores"

    def test_add_chore(self):
        result = parse_voice_command("add a chore clean the kitchen")
        assert result["matched"] is True
        assert result["action"] == "add_chore"
        assert result["data"]["title"] == "clean the kitchen"

    def test_add_todo(self):
        result = parse_voice_command("add todo buy groceries")
        assert result["matched"] is True
        assert result["action"] == "add_chore"
        assert result["data"]["title"] == "buy groceries"

    def test_weather(self):
        result = parse_voice_command("what's the weather")
        assert result["matched"] is True
        assert result["action"] == "weather"

    def test_weather_simple(self):
        result = parse_voice_command("weather")
        assert result["matched"] is True
        assert result["action"] == "weather"

    def test_birthdays(self):
        result = parse_voice_command("who's birthday is coming up")
        assert result["matched"] is True
        assert result["action"] == "list_birthdays"

    def test_set_status(self):
        result = parse_voice_command("set my status to busy")
        assert result["matched"] is True
        assert result["action"] == "set_status"
        assert result["data"]["status"] == "busy"

    def test_toggle_dnd(self):
        result = parse_voice_command("turn on do not disturb")
        assert result["matched"] is True
        assert result["action"] == "toggle_dnd"

    def test_general_chat_fallback(self):
        result = parse_voice_command("tell me a joke")
        assert result["matched"] is False
        assert result["action"] == "general_chat"
        assert result["original_text"] == "tell me a joke"

    def test_empty_text(self):
        result = parse_voice_command("")
        assert result["matched"] is False
        assert result["action"] == "general_chat"

    def test_case_insensitive(self):
        result = parse_voice_command("WHAT ARE MY CHORES")
        assert result["matched"] is True
        assert result["action"] == "list_chores"


class TestTranscribeAudio:
    @patch("app.services.voice_service._try_openai_whisper", return_value=None)
    def test_no_backend_available(self, mock_whisper):
        result = transcribe_audio(b"fake audio", "audio/webm")
        assert result["ok"] is False
        assert "No transcription backend" in result["error"]

    @patch("app.services.voice_service._try_openai_whisper")
    def test_openai_whisper_success(self, mock_whisper):
        mock_whisper.return_value = {"ok": True, "text": "hello world", "backend": "openai-whisper"}
        result = transcribe_audio(b"audio data", "audio/webm")
        assert result["ok"] is True
        assert result["text"] == "hello world"
        assert result["backend"] == "openai-whisper"


class TestVoiceCapabilities:
    @patch("app.openai_client.is_configured", return_value=True)
    def test_with_openai(self, mock_oai):
        caps = get_voice_capabilities()
        assert caps["stt_backends"]["openai_whisper"] is True
        assert caps["stt_backends"]["browser_speech_api"] is True
        assert caps["tts_backends"]["qwen3_tts"] is True
        assert caps["tts_backends"]["edge_tts"] is True

    @patch("app.openai_client.is_configured", return_value=False)
    def test_without_openai(self, mock_oai):
        caps = get_voice_capabilities()
        assert caps["stt_backends"]["openai_whisper"] is False


class TestVoiceAPI:
    def test_capabilities_endpoint(self, client):
        resp = client.get("/api/voice/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert "stt_backends" in data
        assert "tts_backends" in data

    def test_command_parse_endpoint(self, client):
        resp = client.post("/api/voice/command", data={"text": "what are my chores"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched"] is True
        assert data["action"] == "list_chores"

    def test_command_general_chat(self, client):
        resp = client.post("/api/voice/command", data={"text": "hello there"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched"] is False
        assert data["action"] == "general_chat"

    def test_transcribe_empty_audio(self, client):
        resp = client.post(
            "/api/voice/transcribe",
            files={"audio": ("test.webm", b"", "audio/webm")},
            data={"mime_type": "audio/webm"},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False

    @patch("app.services.voice_service.transcribe_audio")
    def test_transcribe_endpoint(self, mock_transcribe, client):
        mock_transcribe.return_value = {"ok": True, "text": "hello", "backend": "test"}
        resp = client.post(
            "/api/voice/transcribe",
            files={"audio": ("test.webm", b"audio data here", "audio/webm")},
            data={"mime_type": "audio/webm"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["text"] == "hello"
