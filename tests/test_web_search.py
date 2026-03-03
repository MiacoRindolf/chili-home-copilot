"""Tests for the web search module."""
import pytest
from unittest.mock import patch, MagicMock

from app.web_search import (
    detect_search_intent,
    extract_search_query,
    search,
    format_results,
)
from app.chili_nlu import parse_message


class TestSearchIntentDetection:
    def test_search_for(self):
        assert detect_search_intent("search for python tutorials") is True

    def test_google(self):
        assert detect_search_intent("google best pizza near me") is True

    def test_look_up(self):
        assert detect_search_intent("look up the weather today") is True

    def test_browse_the_web(self):
        assert detect_search_intent("browse the web for job openings") is True

    def test_find_online(self):
        assert detect_search_intent("find me online some recipes") is True

    def test_latest_news(self):
        assert detect_search_intent("what's the latest news on AI") is True

    def test_current_price(self):
        assert detect_search_intent("current price of bitcoin") is True

    def test_find_a_job(self):
        assert detect_search_intent("find a job opening for software engineer") is True

    def test_give_me_a_link(self):
        assert detect_search_intent("give me a link to the docs") is True

    def test_show_me_results(self):
        assert detect_search_intent("show me results for fastapi tutorial") is True

    def test_normal_chore(self):
        assert detect_search_intent("add chore clean kitchen") is False

    def test_normal_greeting(self):
        assert detect_search_intent("hello how are you") is False

    def test_empty(self):
        assert detect_search_intent("") is False


class TestExtractSearchQuery:
    def test_search_for_prefix(self):
        assert extract_search_query("search for python tutorials") == "python tutorials"

    def test_google_prefix(self):
        assert extract_search_query("google best pizza near me") == "best pizza near me"

    def test_look_up_prefix(self):
        assert extract_search_query("look up weather today") == "weather today"

    def test_no_prefix(self):
        result = extract_search_query("latest bitcoin price")
        assert "bitcoin" in result.lower()

    def test_web_search_for(self):
        assert extract_search_query("web search for fastapi docs") == "fastapi docs"


class TestNLUWebSearch:
    def test_search_for_detected(self):
        action = parse_message("search for software engineering jobs")
        assert action.type == "web_search"
        assert action.data["query"] == "software engineering jobs"

    def test_google_detected(self):
        action = parse_message("google python tutorial")
        assert action.type == "web_search"
        assert action.data["query"] == "python tutorial"

    def test_look_up_detected(self):
        action = parse_message("look up best restaurants nearby")
        assert action.type == "web_search"
        assert action.data["query"] == "best restaurants nearby"


class TestSearch:
    @patch("app.web_search.DDGS")
    def test_returns_results(self, mock_ddgs_cls):
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.return_value = [
            {"title": "Result 1", "href": "https://example.com/1", "body": "Description 1"},
            {"title": "Result 2", "href": "https://example.com/2", "body": "Description 2"},
        ]
        mock_ddgs_cls.return_value = mock_ddgs

        results = search("test query")
        assert len(results) == 2
        assert results[0]["title"] == "Result 1"
        assert results[1]["href"] == "https://example.com/2"

    @patch("app.web_search.DDGS")
    def test_handles_error(self, mock_ddgs_cls):
        mock_ddgs_cls.side_effect = Exception("network error")

        results = search("test query")
        assert results == []


class TestFormatResults:
    def test_formats_results(self):
        results = [
            {"title": "Test", "href": "https://example.com", "body": "A test result"},
        ]
        formatted = format_results(results)
        assert "**Test**" in formatted
        assert "https://example.com" in formatted
        assert "A test result" in formatted

    def test_empty_results(self):
        assert format_results([]) == "No web search results found."


class TestPlannerSchema:
    def test_web_search_plan_valid(self):
        from app.planner_schema import validate_plan
        plan = {
            "type": "web_search",
            "data": {"query": "python tutorials"},
            "reply": "Searching for python tutorials!",
        }
        result = validate_plan(plan)
        assert result is not None
        assert result["type"] == "web_search"
        assert result["data"]["query"] == "python tutorials"

    def test_web_search_plan_empty_query_rejected(self):
        from app.planner_schema import validate_plan
        plan = {
            "type": "web_search",
            "data": {"query": ""},
            "reply": "Searching!",
        }
        result = validate_plan(plan)
        assert result is None
