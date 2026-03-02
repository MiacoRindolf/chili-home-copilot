"""Tests for the rule-based fallback parser (chili_nlu).

This parser is the safety net when Ollama is unavailable. It must
correctly map common command patterns to the right action types.
"""
from datetime import date
from app.chili_nlu import parse_message


class TestChoreCommands:
    def test_add_chore(self):
        action = parse_message("add chore take out trash")
        assert action.type == "add_chore"
        assert action.data["title"] == "take out trash"

    def test_add_chore_with_article(self):
        action = parse_message("add a chore: wash the dishes")
        assert action.type == "add_chore"
        assert action.data["title"] == "wash the dishes"

    def test_add_chore_case_insensitive(self):
        action = parse_message("ADD CHORE vacuum")
        assert action.type == "add_chore"

    def test_list_chores(self):
        action = parse_message("list chores")
        assert action.type == "list_chores"

    def test_show_chores(self):
        action = parse_message("show chores")
        assert action.type == "list_chores"

    def test_list_pending_chores(self):
        action = parse_message("list pending chores")
        assert action.type == "list_chores_pending"

    def test_list_unfinished_chores(self):
        action = parse_message("show unfinished chores")
        assert action.type == "list_chores_pending"

    def test_mark_done(self):
        action = parse_message("done 3")
        assert action.type == "mark_chore_done"
        assert action.data["id"] == 3

    def test_mark_done_verbose(self):
        action = parse_message("mark done 7")
        assert action.type == "mark_chore_done"
        assert action.data["id"] == 7


class TestBirthdayCommands:
    def test_add_birthday(self):
        action = parse_message("add birthday Mom 2026-05-12")
        assert action.type == "add_birthday"
        assert action.data["name"] == "Mom"
        assert action.data["date"] == date(2026, 5, 12)

    def test_list_birthdays(self):
        action = parse_message("list birthdays")
        assert action.type == "list_birthdays"

    def test_show_birthdays(self):
        action = parse_message("show birthdays")
        assert action.type == "list_birthdays"


class TestUnknownFallback:
    def test_gibberish(self):
        action = parse_message("asdfghjkl")
        assert action.type == "unknown"

    def test_empty_after_strip(self):
        action = parse_message("   hello world   ")
        assert action.type == "unknown"
