from unittest.mock import MagicMock
import inspect

from app.services.code_brain import reviewer
from app.services.code_brain import search as code_search
from app.services.code_brain import agent as code_agent


def test_code_reviewer_routes_llm_through_code_review_purpose(monkeypatch):
    llm = MagicMock(
        return_value=(
            "SUMMARY: Adds a safe cache.\n"
            'FINDINGS: [{"severity":"info","category":"test-coverage","message":"ok","file":"x.py"}]\n'
            "SCORE: 8"
        )
    )
    monkeypatch.setattr("app.services.llm_caller.call_llm", llm)

    result = reviewer._review_diff_with_llm(
        "diff --git a/x.py b/x.py\n+print('hi')\n",
        "Repo: demo",
        {"hash": "abcdef123456", "author": "dev", "message": "cache work"},
    )

    assert result["summary"] == "Adds a safe cache."
    assert result["score"] == 8.0
    assert result["findings"][0]["file"] == "x.py"
    assert llm.call_args.kwargs["purpose"] == "code_review"
    assert llm.call_args.kwargs["cacheable"] is True
    messages = llm.call_args.args[0]
    assert "Repository context:" not in messages[0]["content"]
    assert messages[1]["content"].startswith("Repository context:\nRepo: demo")


def test_code_search_routes_llm_through_cacheable_code_search_purpose(monkeypatch):
    llm = MagicMock(return_value="Use app/services/code_brain/search.py:153.")
    monkeypatch.setattr("app.services.llm_caller.call_llm", llm)
    monkeypatch.setattr(
        code_search,
        "search_code",
        lambda *_args, **_kwargs: [
            {
                "type": "function",
                "symbol": "search_code",
                "file": "app/services/code_brain/search.py",
                "line": 153,
                "signature": "def search_code(db, query, repo_id=None, repo_ids=None, limit=20)",
            }
        ],
    )

    class Query:
        def filter(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def all(self):
            return []

    class Db:
        def query(self, *_args, **_kwargs):
            return Query()

    result = code_search.search_with_llm(Db(), "where is search_code", repo_id=1)

    assert result["answer"] == "Use app/services/code_brain/search.py:153."
    assert result["results"][0]["symbol"] == "search_code"
    assert llm.call_args.kwargs["purpose"] == "code_search"
    assert llm.call_args.kwargs["cacheable"] is True


def test_code_agent_source_has_no_direct_openai_fallback():
    source = inspect.getsource(code_agent.run_code_agent)

    assert "openai_client.chat(" not in source
    assert "_legacy_chat" not in source
    assert "direct_openai_bypass_disabled" in source
