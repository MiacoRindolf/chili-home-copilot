"""Tests for planner schema validation (Pydantic guardrails).

These verify that the strict discriminated-union schemas correctly
accept valid LLM plans and reject malformed / dangerous ones.
"""
from app.planner_schema import validate_plan


class TestValidPlans:
    def test_add_chore(self):
        result = validate_plan({
            "type": "add_chore",
            "data": {"title": "Take out trash"},
            "reply": "Added!",
        })
        assert result is not None
        assert result["type"] == "add_chore"
        assert result["data"]["title"] == "Take out trash"

    def test_list_chores(self):
        result = validate_plan({
            "type": "list_chores",
            "data": {},
            "reply": "Here are your chores.",
        })
        assert result is not None
        assert result["type"] == "list_chores"

    def test_mark_chore_done(self):
        result = validate_plan({
            "type": "mark_chore_done",
            "data": {"id": 3},
            "reply": "Done!",
        })
        assert result is not None
        assert result["data"]["id"] == 3

    def test_add_birthday(self):
        result = validate_plan({
            "type": "add_birthday",
            "data": {"name": "Mom", "date": "2026-05-12"},
            "reply": "Birthday added.",
        })
        assert result is not None
        assert result["data"]["date"] == "2026-05-12"

    def test_list_birthdays(self):
        result = validate_plan({
            "type": "list_birthdays",
            "data": {},
            "reply": "Here are the birthdays.",
        })
        assert result is not None
        assert result["type"] == "list_birthdays"

    def test_unknown_with_reason(self):
        result = validate_plan({
            "type": "unknown",
            "data": {"reason": "ambiguous request"},
            "reply": "Could you clarify?",
        })
        assert result is not None
        assert result["type"] == "unknown"

    def test_answer_from_docs(self):
        result = validate_plan({
            "type": "answer_from_docs",
            "data": {"source": "house-info.txt"},
            "reply": "The WiFi password is spicypepper2026.",
        })
        assert result is not None
        assert result["type"] == "answer_from_docs"
        assert result["data"]["source"] == "house-info.txt"


class TestRejectedPlans:
    """These must all return None -- rejected by the schema."""

    def test_extra_field_in_data(self):
        result = validate_plan({
            "type": "add_chore",
            "data": {"title": "Dishes", "injected": "malicious"},
            "reply": "Added!",
        })
        assert result is None, "Extra fields in data must be rejected"

    def test_extra_field_at_top_level(self):
        result = validate_plan({
            "type": "add_chore",
            "data": {"title": "Dishes"},
            "reply": "Added!",
            "extra_key": "should fail",
        })
        assert result is None, "Extra top-level keys must be rejected"

    def test_invalid_action_type(self):
        result = validate_plan({
            "type": "delete_all_data",
            "data": {},
            "reply": "Deleting everything.",
        })
        assert result is None, "Unknown action types must be rejected"

    def test_chore_id_zero(self):
        result = validate_plan({
            "type": "mark_chore_done",
            "data": {"id": 0},
            "reply": "Done!",
        })
        assert result is None, "id=0 must be rejected (ge=1)"

    def test_chore_id_negative(self):
        result = validate_plan({
            "type": "mark_chore_done",
            "data": {"id": -1},
            "reply": "Done!",
        })
        assert result is None, "Negative id must be rejected"

    def test_empty_chore_title(self):
        result = validate_plan({
            "type": "add_chore",
            "data": {"title": ""},
            "reply": "Added!",
        })
        assert result is None, "Empty title must be rejected (min_length=1)"

    def test_invalid_birthday_date(self):
        result = validate_plan({
            "type": "add_birthday",
            "data": {"name": "Mom", "date": "not-a-date"},
            "reply": "Added!",
        })
        assert result is None, "Invalid date format must be rejected"

    def test_missing_reply(self):
        result = validate_plan({
            "type": "list_chores",
            "data": {},
        })
        assert result is None, "Missing reply field must be rejected"

    def test_empty_reply(self):
        result = validate_plan({
            "type": "list_chores",
            "data": {},
            "reply": "",
        })
        assert result is None, "Empty reply must be rejected (min_length=1)"

    def test_answer_from_docs_empty_source(self):
        result = validate_plan({
            "type": "answer_from_docs",
            "data": {"source": ""},
            "reply": "Some answer.",
        })
        assert result is None, "Empty source must be rejected (min_length=1)"
