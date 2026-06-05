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
    @patch("app.web_search.search_providers.resilient_search")
    def test_returns_results(self, mock_resilient):
        # Provider cascade returns normalized {title,url,snippet}; search() must
        # map back to the historical {title,href,body} contract for callers.
        mock_resilient.return_value = [
            {"title": "Result 1", "url": "https://example.com/1", "snippet": "Description 1"},
            {"title": "Result 2", "url": "https://example.com/2", "snippet": "Description 2"},
        ]
        results = search("test query")
        assert len(results) == 2
        assert results[0]["title"] == "Result 1"
        assert results[0]["body"] == "Description 1"
        assert results[1]["href"] == "https://example.com/2"

    @patch("app.web_search.search_providers.resilient_search")
    def test_handles_error(self, mock_resilient):
        mock_resilient.side_effect = Exception("network error")
        results = search("test query")
        assert results == []

    @patch("app.web_search.search_providers.resilient_search")
    def test_empty_cascade_returns_empty(self, mock_resilient):
        mock_resilient.return_value = []
        assert search("test query") == []


class TestResearchSearch:
    """research_search() is search() unless settings.search_fetch_sources is on."""

    @staticmethod
    def _fresh_results(n=4):
        # Fresh dicts per call so research_search's in-place enrichment of the
        # mock's return value can't leak across tests.
        return [{"title": chr(65 + i), "href": f"https://{chr(97 + i)}.com",
                 "body": f"snippet {chr(97 + i)}"} for i in range(n)]

    @patch("app.web_search.fetch_sources")
    @patch("app.web_search.search")
    def test_flag_off_no_fetch(self, mock_search, mock_fetch_sources):
        from app import web_search as ws
        expected = self._fresh_results()
        mock_search.return_value = self._fresh_results()
        with patch.object(ws.settings, "search_fetch_sources", False, create=True):
            out = ws.research_search("topic")
        assert out == expected
        mock_fetch_sources.assert_not_called()

    @patch("app.web_search.fetch_sources")
    @patch("app.web_search.search")
    def test_flag_on_enriches_up_to_cap_concurrently(self, mock_search, mock_fetch_sources):
        from app import web_search as ws
        mock_search.return_value = self._fresh_results()
        # research_search now fetches the top results in ONE concurrent call.
        mock_fetch_sources.return_value = [
            {"url": "https://a.com", "success": True, "content": "FULL ARTICLE TEXT"},
            {"url": "https://b.com", "success": True, "content": "FULL ARTICLE TEXT"},
        ]
        with patch.object(ws.settings, "search_fetch_sources", True, create=True), \
             patch.object(ws.settings, "search_max_fetch", 2, create=True):
            out = ws.research_search("topic")
        # One concurrent fetch call, with exactly the capped URLs.
        mock_fetch_sources.assert_called_once()
        assert mock_fetch_sources.call_args[0][0] == ["https://a.com", "https://b.com"]
        assert out[0]["content"] == "FULL ARTICLE TEXT"
        assert out[1]["content"] == "FULL ARTICLE TEXT"
        assert "content" not in out[2]

    @patch("app.web_search.fetch_sources")
    @patch("app.web_search.search")
    def test_failed_fetch_leaves_result_unenriched(self, mock_search, mock_fetch_sources):
        from app import web_search as ws
        mock_search.return_value = self._fresh_results(1)
        mock_fetch_sources.return_value = [
            {"url": "https://a.com", "success": False, "content": "", "error": "blocked"},
        ]
        with patch.object(ws.settings, "search_fetch_sources", True, create=True), \
             patch.object(ws.settings, "search_max_fetch", 3, create=True):
            out = ws.research_search("topic")
        assert "content" not in out[0]

    @patch("app.web_search.fetch_sources")
    @patch("app.web_search.search")
    def test_override_true_forces_fetch_despite_flag_off(self, mock_search, mock_fetch_sources):
        # On-demand research passes fetch_sources=True to get full content even
        # when the background default (search_fetch_sources) is off.
        from app import web_search as ws
        mock_search.return_value = self._fresh_results()
        mock_fetch_sources.return_value = [
            {"url": "https://a.com", "success": True, "content": "FULL ARTICLE TEXT"},
        ]
        with patch.object(ws.settings, "search_fetch_sources", False, create=True), \
             patch.object(ws.settings, "search_max_fetch", 1, create=True):
            out = ws.research_search("topic", fetch_content=True)
        mock_fetch_sources.assert_called_once()
        assert out[0]["content"] == "FULL ARTICLE TEXT"

    @patch("app.web_search.fetch_sources")
    @patch("app.web_search.search")
    def test_override_false_forces_snippets_despite_flag_on(self, mock_search, mock_fetch_sources):
        from app import web_search as ws
        mock_search.return_value = self._fresh_results()
        with patch.object(ws.settings, "search_fetch_sources", True, create=True):
            out = ws.research_search("topic", fetch_content=False)
        mock_fetch_sources.assert_not_called()
        assert all("content" not in r for r in out)


class TestFetchSources:
    """fetch_sources() is a thin wrapper over search_providers.fetch_many."""

    @patch("app.web_search.search_providers.fetch_many")
    def test_delegates_and_returns_result(self, mock_fetch_many):
        from app import web_search as ws
        expected = [
            {"url": "https://a.com", "success": True, "content": "x"},
            {"url": "https://b.com", "success": False, "content": ""},
        ]
        mock_fetch_many.return_value = expected
        out = ws.fetch_sources(["https://a.com", "https://b.com"])
        assert out == expected
        mock_fetch_many.assert_called_once()
        # URLs forwarded to fetch_many.
        args, kwargs = mock_fetch_many.call_args
        assert args[0] == ["https://a.com", "https://b.com"]

    @patch("app.web_search.search_providers.fetch_many")
    def test_logs_without_raising(self, mock_fetch_many):
        from app import web_search as ws
        mock_fetch_many.return_value = [{"url": "https://a.com", "success": True}]
        # Should not raise even with a custom trace id.
        out = ws.fetch_sources(["https://a.com"], max_chars=1000, trace_id="t1")
        assert len(out) == 1

    @patch("app.web_search.search_providers.fetch_many")
    def test_empty_input(self, mock_fetch_many):
        from app import web_search as ws
        mock_fetch_many.return_value = []
        assert ws.fetch_sources([]) == []


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
        from app.schemas import validate_plan
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
        from app.schemas import validate_plan
        plan = {
            "type": "web_search",
            "data": {"query": ""},
            "reply": "Searching!",
        }
        result = validate_plan(plan)
        assert result is None
