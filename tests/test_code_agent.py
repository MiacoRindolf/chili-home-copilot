"""Tests for the Code Brain Agent: multi-step flow, relevance, diff validation."""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.services.code_brain.agent import (
    _gather_context,
    _parse_plan_json,
    _validate_diff,
    _build_plan_prompt,
    _build_edit_prompt,
)


class TestParsePlanJson:
    def test_extracts_from_json_block(self):
        reply = '''Here is my plan:
```json
{
  "analysis": "Refactor the auth module",
  "files": [{"path": "auth.py", "action": "modify", "description": "Add rate limiting"}],
  "notes": "Consider adding tests"
}
```'''
        result = _parse_plan_json(reply)
        assert result is not None
        assert result["analysis"] == "Refactor the auth module"
        assert len(result["files"]) == 1
        assert result["files"][0]["path"] == "auth.py"

    def test_extracts_raw_json(self):
        reply = '{"analysis": "Fix bug", "files": [{"path": "main.py", "action": "modify", "description": "Fix null check"}], "notes": ""}'
        result = _parse_plan_json(reply)
        assert result is not None
        assert result["files"][0]["path"] == "main.py"

    def test_returns_none_for_invalid(self):
        assert _parse_plan_json("No JSON here") is None
        assert _parse_plan_json("```json\nnot valid json\n```") is None


class TestValidateDiff:
    def test_valid_diff(self):
        file_content = "def hello():\n    print('hello')\n    return True\n"
        diff = "-    print('hello')\n+    print('world')\n"
        result = _validate_diff(diff, "test.py", file_content)
        assert result["valid"] is True
        assert len(result["warnings"]) == 0

    def test_invalid_diff_hallucinated_lines(self):
        file_content = "def hello():\n    return True\n"
        diff = (
            "-    print('this line does not exist')\n"
            "-    x = fake_variable\n"
            "-    y = another_fake\n"
            "+    return False\n"
        )
        result = _validate_diff(diff, "test.py", file_content)
        assert result["valid"] is False
        assert any("hallucinated" in w.lower() or "do not match" in w.lower() for w in result["warnings"])

    def test_missing_file_content(self):
        diff = "-old\n+new\n"
        result = _validate_diff(diff, "missing.py", None)
        assert len(result["warnings"]) > 0
        assert "not readable" in result["warnings"][0].lower() or "validate" in result["warnings"][0].lower()


class TestBuildPlanPrompt:
    def test_includes_repos(self):
        context = {
            "repos": [{"name": "myrepo", "path": "/code", "file_count": 10, "total_lines": 500, "languages": {"python": 10}, "frameworks": ["fastapi"]}],
            "insights": [],
            "hotspots": [],
            "relevant_files": [],
        }
        prompt = _build_plan_prompt(context)
        assert "myrepo" in prompt
        assert "fastapi" in prompt
        assert "JSON" in prompt

    def test_includes_relevant_files(self):
        context = {
            "repos": [],
            "insights": [],
            "hotspots": [],
            "relevant_files": [{"file": "auth.py", "symbol": "login"}],
        }
        prompt = _build_plan_prompt(context)
        assert "auth.py" in prompt
        assert "login" in prompt


class TestBuildEditPrompt:
    def test_includes_file_content(self):
        prompt = _build_edit_prompt(
            "utils.py",
            "def add(a, b):\n    return a + b\n",
            "Add input validation",
            ["Use type hints"],
        )
        assert "utils.py" in prompt
        assert "def add(a, b):" in prompt
        assert "Add input validation" in prompt
        assert "Use type hints" in prompt

    def test_strict_rules_present(self):
        prompt = _build_edit_prompt("f.py", "x = 1", "change x", [])
        assert "MUST be based ONLY" in prompt
        assert "placeholder" in prompt.lower() or "Do NOT" in prompt


class TestGatherContext:
    def test_uses_search_code(self, db):
        """Verify context gathering uses search_code for relevance."""
        from app.models.code_brain import CodeRepo
        repo = CodeRepo(name="test", path="/tmp/nonexistent", file_count=0, total_lines=0)
        db.add(repo)
        db.commit()

        context = _gather_context(db, repo.id, "find the login function")
        assert "repos" in context
        assert "insights" in context
        assert "relevant_files" in context
        assert len(context["repos"]) == 1
