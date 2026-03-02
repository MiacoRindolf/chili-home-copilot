"""Tests for vision image upload, validation, and chat integration."""
import io
from unittest.mock import patch, MagicMock
from app.models import ChatMessage
from app.vision import save_upload, UPLOAD_DIR


class TestSaveUpload:
    """Validate image upload handling."""

    def test_valid_png(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.vision.UPLOAD_DIR", tmp_path)
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        result = save_upload(png_header, "screenshot.png", "image/png")
        assert result is not None
        assert result.endswith(".png")
        assert (tmp_path / result).exists()

    def test_valid_jpeg(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.vision.UPLOAD_DIR", tmp_path)
        jpeg_data = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        result = save_upload(jpeg_data, "photo.jpg", "image/jpeg")
        assert result is not None
        assert result.endswith(".jpg")

    def test_rejects_invalid_type(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.vision.UPLOAD_DIR", tmp_path)
        result = save_upload(b"not an image", "malware.exe", "application/octet-stream")
        assert result is None

    def test_rejects_oversized(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.vision.UPLOAD_DIR", tmp_path)
        monkeypatch.setattr("app.vision.MAX_SIZE_BYTES", 100)
        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        result = save_upload(big, "huge.png", "image/png")
        assert result is None

    def test_rejects_empty_bytes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.vision.UPLOAD_DIR", tmp_path)
        result = save_upload(b"", "empty.png", "image/png")
        assert result is None


class TestVisionRouting:
    """Vision model routing: local first, OpenAI fallback."""

    @patch("app.vision.call_ollama_vision")
    def test_local_success(self, mock_ollama):
        mock_ollama.return_value = "I see a cat on a table."
        from app.vision import describe_image
        reply, model = describe_image(["test.png"], "what is this?")
        assert reply == "I see a cat on a table."
        assert "llava" in model

    @patch("app.vision.call_openai_vision")
    @patch("app.vision.call_ollama_vision")
    def test_fallback_to_openai(self, mock_ollama, mock_openai):
        mock_ollama.return_value = None
        mock_openai.return_value = "This appears to be a receipt."
        from app.vision import describe_image
        reply, model = describe_image(["test.png"], "what is this?")
        assert reply == "This appears to be a receipt."

    @patch("app.vision.call_openai_vision")
    @patch("app.vision.call_ollama_vision")
    def test_both_offline(self, mock_ollama, mock_openai):
        mock_ollama.return_value = None
        mock_openai.return_value = None
        from app.vision import describe_image
        reply, model = describe_image(["test.png"])
        assert "couldn't analyze" in reply.lower()
        assert model == "none"


def _make_png_bytes():
    """Generate minimal valid-ish PNG bytes for upload tests."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


class TestImageChatAPI:
    """Integration: POST /api/chat with image(s)."""

    @patch("app.vision.describe_image")
    def test_image_upload_stores_path(self, mock_describe, paired_client, db):
        mock_describe.return_value = ("Nice screenshot!", "llava-llama3")
        client, user = paired_client

        png = _make_png_bytes()
        resp = client.post(
            "/api/chat",
            data={"message": "what is this?"},
            files=[("images", ("test.png", io.BytesIO(png), "image/png"))],
        )
        data = resp.json()

        assert resp.status_code == 200
        assert data["action_type"] == "vision"
        assert data["reply"] == "Nice screenshot!"

        user_msg = db.query(ChatMessage).filter(ChatMessage.role == "user").first()
        assert user_msg is not None
        assert user_msg.image_path is not None

    @patch("app.vision.describe_image")
    def test_multiple_images(self, mock_describe, paired_client, db):
        mock_describe.return_value = ("I see two images.", "llava-llama3")
        client, user = paired_client

        png1 = _make_png_bytes()
        png2 = _make_png_bytes()
        resp = client.post(
            "/api/chat",
            data={"message": "compare these"},
            files=[
                ("images", ("a.png", io.BytesIO(png1), "image/png")),
                ("images", ("b.png", io.BytesIO(png2), "image/png")),
            ],
        )
        data = resp.json()

        assert resp.status_code == 200
        assert data["action_type"] == "vision"

        import json
        user_msg = db.query(ChatMessage).filter(ChatMessage.role == "user").first()
        paths = json.loads(user_msg.image_path)
        assert len(paths) == 2

    @patch("app.vision.describe_image")
    def test_image_only_no_text(self, mock_describe, paired_client, db):
        mock_describe.return_value = ("I see an image.", "llava-llama3")
        client, user = paired_client

        png = _make_png_bytes()
        resp = client.post(
            "/api/chat",
            data={"message": ""},
            files=[("images", ("photo.png", io.BytesIO(png), "image/png"))],
        )
        data = resp.json()

        assert resp.status_code == 200
        assert data["action_type"] == "vision"

    def test_no_message_no_image_rejected(self, paired_client):
        client, _ = paired_client
        resp = client.post("/api/chat", data={"message": ""})
        assert resp.status_code == 400

    def test_invalid_image_type_ignored(self, paired_client, db):
        """Non-image files are silently ignored; message still processed."""
        client, _ = paired_client
        with patch("app.services.chat_service.plan_action", side_effect=Exception("offline")):
            resp = client.post(
                "/api/chat",
                data={"message": "hello"},
                files=[("images", ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf"))],
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["action_type"] != "vision"

    @patch("app.vision.describe_image")
    def test_history_includes_image_paths(self, mock_describe, paired_client, db):
        mock_describe.return_value = ("A sunset photo.", "gpt-4o-mini")
        client, user = paired_client

        png = _make_png_bytes()
        resp = client.post(
            "/api/chat",
            data={"message": "describe this"},
            files=[("images", ("sunset.png", io.BytesIO(png), "image/png"))],
        )
        convo_id = resp.json()["conversation_id"]

        hist = client.get(f"/api/chat/history?conversation_id={convo_id}")
        data = hist.json()

        user_msgs = [m for m in data["messages"] if m["role"] == "user"]
        assert len(user_msgs) > 0
        assert len(user_msgs[0]["image_paths"]) == 1
        assert user_msgs[0]["image_paths"][0].endswith(".png")
