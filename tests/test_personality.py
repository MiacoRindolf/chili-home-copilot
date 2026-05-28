"""Tests for housemate personality profiling."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from datetime import datetime

from app.models import ChatMessage, HousemateProfile, User
from app.personality import (
    should_update,
    extract_profile,
    get_profile_context,
    EXTRACTION_THRESHOLD,
    _profile_from_memories,
)


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


class TestMechanicalProfile:
    def test_builds_profile_from_memory_facts_without_llm_shape(self):
        memories = [
            SimpleNamespace(category="interest", content="Enjoys hiking"),
            SimpleNamespace(category="interest", content="Likes cooking"),
            SimpleNamespace(category="dietary", content="Vegetarian"),
            SimpleNamespace(category="preference", content="Prefers brief direct answers"),
            SimpleNamespace(category="work", content="Works as a nurse"),
            SimpleNamespace(category="habit", content="Runs every morning"),
        ]

        profile = _profile_from_memories(memories)

        assert profile["interests"] == ["hiking", "cooking"]
        assert profile["dietary"] == "Vegetarian"
        assert profile["tone"] == "brief"
        assert "[work] nurse" in profile["notes"]
        assert "[habit] Runs every morning" in profile["notes"]


class TestExtractProfile:
    @patch("app.personality.openai_client")
    def test_uses_memory_facts_without_llm_when_available(self, mock_oc, db):
        user = User(name="TestUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        from app.models import UserMemory

        db.add(UserMemory(user_id=user.id, category="interest", content="Enjoys hiking"))
        db.add(UserMemory(user_id=user.id, category="dietary", content="Vegetarian"))
        db.commit()

        result = extract_profile(user.id, db)

        assert result == {
            "interests": ["hiking"],
            "dietary": "Vegetarian",
            "tone": "",
            "notes": "",
        }
        mock_oc.chat.assert_not_called()

        profile = db.query(HousemateProfile).filter(
            HousemateProfile.user_id == user.id
        ).first()
        assert profile is not None
        assert json.loads(profile.interests) == ["hiking"]
        assert profile.dietary == "Vegetarian"

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

        with patch("app.services.context_brain.llm_gateway.gateway_chat") as mock_gateway:
            mock_gateway.return_value = {
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
        assert mock_gateway.call_args.kwargs["purpose"] == "personality_apply"
        mock_oc.chat.assert_not_called()

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

        with patch("app.services.context_brain.llm_gateway.gateway_chat") as mock_gateway:
            mock_gateway.return_value = {
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
        mock_oc.chat.assert_not_called()

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

        with patch("app.services.context_brain.llm_gateway.gateway_chat") as mock_gateway:
            mock_gateway.return_value = {
                "reply": "",
                "tokens_used": 0,
                "model": "error",
            }

            result = extract_profile(user.id, db)
        assert result is None
        mock_oc.chat.assert_not_called()

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

        with patch("app.services.context_brain.llm_gateway.gateway_chat") as mock_gateway:
            mock_gateway.return_value = {
                "reply": "This is not JSON at all!",
                "tokens_used": 50,
                "model": "gpt-4o-mini",
            }

            result = extract_profile(user.id, db)
        assert result is None
        mock_oc.chat.assert_not_called()

    @patch("app.personality.openai_client")
    def test_gateway_exception_does_not_fall_back_to_direct_openai(self, mock_oc, db):
        user = User(name="TestUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        db.add(ChatMessage(
            convo_key=f"user:{user.id}", role="user", content="I have a few personal preferences"
        ))
        db.commit()

        mock_oc.chat = MagicMock(side_effect=AssertionError("personality_apply must not bypass gateway"))
        with patch("app.services.context_brain.llm_gateway.gateway_chat") as mock_gateway:
            mock_gateway.side_effect = RuntimeError("gateway unavailable")

            result = extract_profile(user.id, db)

        assert result is None
        mock_oc.chat.assert_not_called()

    def test_personality_source_has_no_direct_chat_fallback(self):
        import app.personality as personality

        source = Path(personality.__file__).read_text(encoding="utf-8")
        assert "openai_client.chat(" not in source


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
        assert "Edit Profile" in resp.text or "profile" in resp.text.lower()

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
