"""Tests for housemate personality profiling."""
import json
from unittest.mock import patch, MagicMock
from datetime import datetime

from app.models import ChatMessage, HousemateProfile, User
from app.personality import should_update, extract_profile, get_profile_context, EXTRACTION_THRESHOLD


class TestShouldUpdate:
    def test_false_when_openai_not_configured(self, db):
        with patch("app.personality.openai_client") as mock_oc:
            mock_oc.is_configured.return_value = False
            assert should_update(1, db) is False

    def test_false_when_not_enough_messages(self, db):
        user = User(name="TestUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        for i in range(EXTRACTION_THRESHOLD - 1):
            db.add(ChatMessage(
                convo_key=f"user:{user.id}", role="user",
                content=f"message {i}"
            ))
        db.commit()

        with patch("app.personality.openai_client") as mock_oc:
            mock_oc.is_configured.return_value = True
            assert should_update(user.id, db) is False

    def test_true_when_threshold_reached_no_profile(self, db):
        user = User(name="TestUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        for i in range(EXTRACTION_THRESHOLD):
            db.add(ChatMessage(
                convo_key=f"user:{user.id}", role="user",
                content=f"message {i}"
            ))
        db.commit()

        with patch("app.personality.openai_client") as mock_oc:
            mock_oc.is_configured.return_value = True
            assert should_update(user.id, db) is True

    def test_true_when_enough_new_messages_since_extraction(self, db):
        user = User(name="TestUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        db.add(HousemateProfile(
            user_id=user.id,
            message_count_at_extraction=5,
        ))
        for i in range(5 + EXTRACTION_THRESHOLD):
            db.add(ChatMessage(
                convo_key=f"user:{user.id}", role="user",
                content=f"message {i}"
            ))
        db.commit()

        with patch("app.personality.openai_client") as mock_oc:
            mock_oc.is_configured.return_value = True
            assert should_update(user.id, db) is True


class TestExtractProfile:
    @patch("app.personality.openai_client")
    def test_creates_profile_on_first_extraction(self, mock_oc, db):
        user = User(name="TestUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        for i in range(5):
            db.add(ChatMessage(
                convo_key=f"user:{user.id}", role="user",
                content=f"I love cooking pasta and gardening"
            ))
        db.commit()

        mock_oc.chat.return_value = {
            "reply": json.dumps({
                "interests": ["cooking", "gardening"],
                "dietary": "none",
                "tone": "casual",
                "notes": "Enjoys Italian food",
            }),
            "tokens_used": 100,
            "model": "gpt-4o-mini",
        }

        result = extract_profile(user.id, db)
        assert result is not None
        assert "cooking" in result["interests"]

        profile = db.query(HousemateProfile).filter(
            HousemateProfile.user_id == user.id
        ).first()
        assert profile is not None
        assert "cooking" in profile.interests

    @patch("app.personality.openai_client")
    def test_updates_existing_profile(self, mock_oc, db):
        user = User(name="TestUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        db.add(HousemateProfile(
            user_id=user.id,
            interests=json.dumps(["old interest"]),
            tone="formal",
        ))
        db.add(ChatMessage(
            convo_key=f"user:{user.id}", role="user", content="test"
        ))
        db.commit()

        mock_oc.chat.return_value = {
            "reply": json.dumps({
                "interests": ["cooking", "gaming"],
                "dietary": "vegetarian",
                "tone": "casual",
                "notes": "Night owl",
            }),
            "tokens_used": 80,
            "model": "gpt-4o-mini",
        }

        result = extract_profile(user.id, db)
        assert result is not None

        profile = db.query(HousemateProfile).filter(
            HousemateProfile.user_id == user.id
        ).first()
        assert "cooking" in profile.interests
        assert profile.tone == "casual"
        assert profile.dietary == "vegetarian"

    @patch("app.personality.openai_client")
    def test_handles_openai_failure(self, mock_oc, db):
        user = User(name="TestUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        db.add(ChatMessage(
            convo_key=f"user:{user.id}", role="user", content="test"
        ))
        db.commit()

        mock_oc.chat.return_value = {
            "reply": "",
            "tokens_used": 0,
            "model": "error",
        }

        result = extract_profile(user.id, db)
        assert result is None

    @patch("app.personality.openai_client")
    def test_handles_invalid_json(self, mock_oc, db):
        user = User(name="TestUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        db.add(ChatMessage(
            convo_key=f"user:{user.id}", role="user", content="test"
        ))
        db.commit()

        mock_oc.chat.return_value = {
            "reply": "This is not JSON at all!",
            "tokens_used": 50,
            "model": "gpt-4o-mini",
        }

        result = extract_profile(user.id, db)
        assert result is None


class TestGetProfileContext:
    def test_returns_none_when_no_profile(self, db):
        result = get_profile_context(999, db)
        assert result is None

    def test_returns_formatted_string(self, db):
        user = User(name="TestUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        db.add(HousemateProfile(
            user_id=user.id,
            interests=json.dumps(["cooking", "gaming"]),
            dietary="vegetarian",
            tone="casual",
            notes="Night owl",
        ))
        db.commit()

        result = get_profile_context(user.id, db)
        assert result is not None
        assert "cooking" in result
        assert "vegetarian" in result
        assert "casual" in result
        assert "Night owl" in result

    def test_returns_none_when_profile_empty(self, db):
        user = User(name="TestUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        db.add(HousemateProfile(
            user_id=user.id,
            interests=json.dumps([]),
            dietary="",
            tone="",
            notes="",
        ))
        db.commit()

        result = get_profile_context(user.id, db)
        assert result is None


class TestProfilePage:
    def test_guest_sees_pairing_message(self, client):
        resp = client.get("/profile")
        assert resp.status_code == 200
        assert "pair" in resp.text.lower()

    def test_paired_user_sees_form(self, paired_client, db):
        client, user = paired_client
        resp = client.get("/profile")
        assert resp.status_code == 200
        assert user.name in resp.text
        assert "Save Profile" in resp.text

    def test_paired_user_can_save_profile(self, paired_client, db):
        client, user = paired_client
        resp = client.post("/profile", data={
            "interests": "cooking, gaming",
            "dietary": "vegetarian",
            "tone": "casual",
            "notes": "Night owl",
        }, follow_redirects=False)
        assert resp.status_code == 303

        profile = db.query(HousemateProfile).filter(
            HousemateProfile.user_id == user.id
        ).first()
        assert profile is not None
        assert "cooking" in profile.interests
        assert profile.dietary == "vegetarian"
