"""Tests for the incremental user memory system."""
import json
from unittest.mock import patch, MagicMock

import pytest

from app.memory import (
    extract_facts,
    get_memory_context,
    get_memories_paginated,
    get_interest_breakdown,
    delete_memory,
    _should_extract,
    _extract_mechanical_facts,
    _memory_duplicate_key,
    _is_duplicate,
    _parse_facts,
    VALID_CATEGORIES,
)
from app.models import UserMemory, User, ChatMessage


# ---------------------------------------------------------------------------
# _should_extract
# ---------------------------------------------------------------------------

class TestShouldExtract:
    def test_skips_tool_actions(self):
        assert _should_extract("list_chores", "show me my chores") is False
        assert _should_extract("add_chore", "add chore buy milk") is False
        assert _should_extract("mark_chore_done", "done with chore 3") is False
        assert _should_extract("crisis_support", "help me") is False

    def test_skips_short_messages(self):
        assert _should_extract("general_chat", "hi") is False
        assert _should_extract("general_chat", "ok") is False
        assert _should_extract(None, "yes") is False

    def test_skips_messages_without_personal_memory_signal(self):
        assert _should_extract("general_chat", "Analyze AAPL on the daily timeframe") is False
        assert _should_extract(None, "What is BTC doing today?") is False
        assert _should_extract("general_chat", "Show me the project status") is False
        assert _should_extract("general_chat", "What is the order id for AAPL?") is False

    def test_allows_general_chat(self):
        assert _should_extract("general_chat", "I really enjoy cooking Italian food") is True
        assert _should_extract(None, "I've been working as a nurse for five years") is True

    def test_allows_unknown(self):
        assert _should_extract("unknown", "My favorite hobby is painting landscapes") is True


# ---------------------------------------------------------------------------
# _parse_facts
# ---------------------------------------------------------------------------

class TestParseFacts:
    def test_valid_json_array(self):
        text = '[{"category": "interest", "content": "Likes hiking"}]'
        assert _parse_facts(text, "test") == [{"category": "interest", "content": "Likes hiking"}]

    def test_json_with_surrounding_text(self):
        text = 'Here are the facts:\n[{"category": "habit", "content": "Runs every morning"}]\nDone.'
        result = _parse_facts(text, "test")
        assert len(result) == 1
        assert result[0]["content"] == "Runs every morning"

    def test_empty_array(self):
        assert _parse_facts("[]", "test") == []

    def test_invalid_json(self):
        assert _parse_facts("not json at all", "test") == []

    def test_no_brackets(self):
        assert _parse_facts('{"key": "val"}', "test") == []


# ---------------------------------------------------------------------------
# _extract_mechanical_facts
# ---------------------------------------------------------------------------

class TestMechanicalFacts:
    def test_extracts_explicit_interest_and_diet_without_llm(self):
        facts, complete = _extract_mechanical_facts("I love hiking and I'm vegetarian")
        assert complete is True
        assert facts == [
            {"category": "interest", "content": "Likes hiking"},
            {"category": "dietary", "content": "Vegetarian"},
        ]

    def test_leaves_mixed_ambiguous_message_for_llm(self):
        facts, complete = _extract_mechanical_facts("I love hiking and my brother is Sam")
        assert complete is False
        assert facts == [{"category": "interest", "content": "Likes hiking"}]

    def test_duplicate_key_normalizes_interest_verbs(self):
        assert _memory_duplicate_key("Likes hiking") == _memory_duplicate_key("Enjoys hiking")
        assert _memory_duplicate_key("Loves hiking") == "hiking"


# ---------------------------------------------------------------------------
# _is_duplicate
# ---------------------------------------------------------------------------

class TestIsDuplicate:
    def test_detects_exact_duplicate(self, db):
        user = User(name="DupTest")
        db.add(user)
        db.commit()

        db.add(UserMemory(user_id=user.id, category="interest", content="Likes pizza"))
        db.commit()

        assert _is_duplicate(user.id, "Likes pizza", db) is True

    def test_case_insensitive(self, db):
        user = User(name="DupTest2")
        db.add(user)
        db.commit()

        db.add(UserMemory(user_id=user.id, category="interest", content="Likes Pizza"))
        db.commit()

        assert _is_duplicate(user.id, "likes pizza", db) is True

    def test_not_duplicate(self, db):
        user = User(name="DupTest3")
        db.add(user)
        db.commit()

        db.add(UserMemory(user_id=user.id, category="interest", content="Likes pizza"))
        db.commit()

        assert _is_duplicate(user.id, "Likes hiking", db) is False

    def test_superseded_not_counted(self, db):
        user = User(name="DupTest4")
        db.add(user)
        db.commit()

        db.add(UserMemory(user_id=user.id, category="interest", content="Likes pizza", superseded=True))
        db.commit()

        assert _is_duplicate(user.id, "Likes pizza", db) is False


# ---------------------------------------------------------------------------
# extract_facts
# ---------------------------------------------------------------------------

class TestExtractFacts:
    @patch("app.memory.openai_client")
    def test_stores_extracted_facts(self, mock_client, db):
        mock_client.is_configured.return_value = True
        mock_client.chat.return_value = {
            "reply": '[{"category": "interest", "content": "Enjoys hiking"}, {"category": "dietary", "content": "Vegetarian"}]',
            "model": "test",
            "tokens_used": 0,
        }

        user = User(name="MemUser")
        db.add(user)
        db.commit()

        result = extract_facts("I love hiking and I'm vegetarian", "That's great!", user.id, db, trace_id="test")
        assert len(result) == 2
        assert result[0]["category"] == "interest"
        assert result[1]["category"] == "dietary"
        mock_client.chat.assert_not_called()

        stored = db.query(UserMemory).filter(UserMemory.user_id == user.id).all()
        assert len(stored) == 2

    @patch("app.memory.openai_client")
    def test_skips_when_not_configured(self, mock_client, db):
        mock_client.is_configured.return_value = False

        result = extract_facts("Something personal", "Cool!", 1, db, trace_id="test")
        assert result == []

    @patch("app.memory.openai_client")
    def test_mechanical_facts_do_not_require_llm_configuration(self, mock_client, db):
        mock_client.is_configured.return_value = False

        user = User(name="NoLlmMemUser")
        db.add(user)
        db.commit()

        result = extract_facts("I'm allergic to peanuts", "Noted.", user.id, db, trace_id="test")

        assert result == [{"category": "health", "content": "Allergic to peanuts"}]
        mock_client.chat.assert_not_called()

    @patch("app.memory.openai_client")
    def test_skips_tool_actions(self, mock_client, db):
        mock_client.is_configured.return_value = True

        result = extract_facts("list chores", "Here are your chores", 1, db, action_type="list_chores", trace_id="test")
        assert result == []
        mock_client.chat.assert_not_called()

    @patch("app.memory.openai_client")
    def test_deduplication(self, mock_client, db):
        mock_client.is_configured.return_value = True
        mock_client.chat.return_value = {
            "reply": '[{"category": "interest", "content": "Likes hiking"}]',
            "model": "test",
            "tokens_used": 0,
        }

        user = User(name="DedupUser")
        db.add(user)
        db.commit()

        db.add(UserMemory(user_id=user.id, category="interest", content="Likes hiking"))
        db.commit()

        result = extract_facts("I love hiking", "Awesome!", user.id, db, trace_id="test")
        assert len(result) == 0

    @patch("app.memory.openai_client")
    def test_invalid_category_skipped(self, mock_client, db):
        mock_client.is_configured.return_value = True
        mock_client.chat.return_value = {
            "reply": '[{"category": "invalid_cat", "content": "Something"}]',
            "model": "test",
            "tokens_used": 0,
        }

        user = User(name="CatUser")
        db.add(user)
        db.commit()

        result = extract_facts("Something personal", "Noted!", user.id, db, trace_id="test")
        assert len(result) == 0

    @patch("app.memory.openai_client")
    def test_handles_llm_error(self, mock_client, db):
        mock_client.is_configured.return_value = True
        mock_client.chat.side_effect = Exception("LLM down")

        result = extract_facts("Something personal", "Cool!", 1, db, trace_id="test")
        assert result == []

    @patch("app.memory.openai_client")
    def test_empty_reply(self, mock_client, db):
        mock_client.is_configured.return_value = True
        mock_client.chat.return_value = {"reply": "", "model": "test", "tokens_used": 0}

        result = extract_facts("Something personal", "Cool!", 1, db, trace_id="test")
        assert result == []


# ---------------------------------------------------------------------------
# get_memory_context
# ---------------------------------------------------------------------------

class TestGetMemoryContext:
    def test_returns_none_when_empty(self, db):
        assert get_memory_context(999, db) is None

    def test_returns_formatted_context(self, db):
        user = User(name="CtxUser")
        db.add(user)
        db.commit()

        db.add(UserMemory(user_id=user.id, category="interest", content="Enjoys cooking"))
        db.add(UserMemory(user_id=user.id, category="habit", content="Runs every morning"))
        db.commit()

        ctx = get_memory_context(user.id, db)
        assert ctx is not None
        assert "Enjoys cooking" in ctx
        assert "Runs every morning" in ctx
        assert "[interest]" in ctx
        assert "[habit]" in ctx

    def test_excludes_superseded(self, db):
        user = User(name="SupUser")
        db.add(user)
        db.commit()

        db.add(UserMemory(user_id=user.id, category="dietary", content="Eats meat", superseded=True))
        db.add(UserMemory(user_id=user.id, category="dietary", content="Vegetarian"))
        db.commit()

        ctx = get_memory_context(user.id, db)
        assert "Vegetarian" in ctx
        assert "Eats meat" not in ctx


# ---------------------------------------------------------------------------
# get_memories_paginated
# ---------------------------------------------------------------------------

class TestGetMemoriesPaginated:
    def test_empty(self, db):
        result = get_memories_paginated(999, db)
        assert result["total"] == 0
        assert result["memories"] == []

    def test_pagination(self, db):
        user = User(name="PageUser")
        db.add(user)
        db.commit()

        for i in range(5):
            db.add(UserMemory(user_id=user.id, category="interest", content=f"Fact {i}"))
        db.commit()

        page1 = get_memories_paginated(user.id, db, page=1, per_page=3)
        assert len(page1["memories"]) == 3
        assert page1["total"] == 5
        assert page1["pages"] == 2

        page2 = get_memories_paginated(user.id, db, page=2, per_page=3)
        assert len(page2["memories"]) == 2


# ---------------------------------------------------------------------------
# get_interest_breakdown
# ---------------------------------------------------------------------------

class TestGetInterestBreakdown:
    def test_empty(self, db):
        assert get_interest_breakdown(999, db) == []

    def test_counts_categories(self, db):
        user = User(name="BreakUser")
        db.add(user)
        db.commit()

        db.add(UserMemory(user_id=user.id, category="interest", content="Hiking"))
        db.add(UserMemory(user_id=user.id, category="interest", content="Cooking"))
        db.add(UserMemory(user_id=user.id, category="habit", content="Runs"))
        db.commit()

        breakdown = get_interest_breakdown(user.id, db)
        cats = {b["category"]: b["count"] for b in breakdown}
        assert cats["interest"] == 2
        assert cats["habit"] == 1


# ---------------------------------------------------------------------------
# delete_memory
# ---------------------------------------------------------------------------

class TestDeleteMemory:
    def test_deletes_own_memory(self, db):
        user = User(name="DelUser")
        db.add(user)
        db.commit()

        db.add(UserMemory(user_id=user.id, category="interest", content="Test"))
        db.commit()
        mem = db.query(UserMemory).first()

        assert delete_memory(mem.id, user.id, db) is True
        assert db.query(UserMemory).count() == 0

    def test_cannot_delete_other_users_memory(self, db):
        user1 = User(name="User1")
        user2 = User(name="User2")
        db.add_all([user1, user2])
        db.commit()

        db.add(UserMemory(user_id=user1.id, category="interest", content="Test"))
        db.commit()
        mem = db.query(UserMemory).first()

        assert delete_memory(mem.id, user2.id, db) is False
        assert db.query(UserMemory).count() == 1

    def test_nonexistent(self, db):
        assert delete_memory(999, 1, db) is False


# ---------------------------------------------------------------------------
# Profile API endpoints
# ---------------------------------------------------------------------------

class TestProfileAPI:
    def test_guest_blocked(self, client):
        r = client.get("/api/profile")
        assert r.status_code == 403

    def test_paired_user_gets_profile(self, paired_client, db):
        client, user = paired_client
        r = client.get("/api/profile")
        assert r.status_code == 200
        data = r.json()
        assert data["user_name"] == "TestUser"
        assert "profile" in data
        assert "memory_count" in data

    def test_memories_endpoint(self, paired_client, db):
        client, user = paired_client
        r = client.get("/api/profile/memories")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0

    def test_memories_with_data(self, paired_client, db):
        client, user = paired_client
        db.add(UserMemory(user_id=user.id, category="interest", content="Hiking"))
        db.commit()

        r = client.get("/api/profile/memories")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["memories"][0]["content"] == "Hiking"

    def test_delete_memory_endpoint(self, paired_client, db):
        client, user = paired_client
        db.add(UserMemory(user_id=user.id, category="interest", content="Test"))
        db.commit()
        mem = db.query(UserMemory).first()

        r = client.delete(f"/api/profile/memories/{mem.id}")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert db.query(UserMemory).count() == 0

    def test_delete_nonexistent_memory(self, paired_client, db):
        client, user = paired_client
        r = client.delete("/api/profile/memories/999")
        assert r.status_code == 404

    def test_interests_endpoint(self, paired_client, db):
        client, user = paired_client
        db.add(UserMemory(user_id=user.id, category="interest", content="Hiking"))
        db.add(UserMemory(user_id=user.id, category="interest", content="Cooking"))
        db.commit()

        r = client.get("/api/profile/interests")
        assert r.status_code == 200
        data = r.json()
        assert len(data["breakdown"]) >= 1
        assert data["breakdown"][0]["count"] == 2


# ---------------------------------------------------------------------------
# Valid categories
# ---------------------------------------------------------------------------

class TestCategories:
    def test_expected_categories(self):
        expected = {"interest", "preference", "habit", "event", "person",
                    "dietary", "work", "health", "memory", "schedule", "goal"}
        assert VALID_CATEGORIES == expected
