"""Tests for the Code Brain Agent: multi-step flow, relevance, diff validation."""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.services.code_brain.agent import (
    _gather_context,
    _parse_plan_json,
    _snapshots_by_repo,
    _validate_diff,
    _build_plan_prompt,
    _build_edit_prompt,
    _extract_full_file_replacement,
    _is_mutating_plan_action,
    _semantic_replacement_warnings,
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

    def test_plan_action_classifier_blocks_advisory_entries(self):
        assert _is_mutating_plan_action("modify") is True
        assert _is_mutating_plan_action("create") is True
        assert _is_mutating_plan_action("review") is False
        assert _is_mutating_plan_action("no change") is False


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


class TestFullFileFallback:
    def test_accepts_one_similar_syntax_valid_replacement(self):
        original = "def value():\n    return 1\n"
        reply = "```python\ndef value():\n    return 2\n```"

        result = _extract_full_file_replacement(reply, "example.py", original)

        assert result["new_content"] == "def value():\n    return 2\n"
        assert "accepted guarded full-file fallback" in result["warnings"][0]

    def test_rejects_multiple_fences_and_invalid_python(self):
        original = "def value():\n    return 1\n"
        multiple = _extract_full_file_replacement(
            "```python\ndef value():\n    return 2\n```\n```text\nextra\n```",
            "example.py",
            original,
        )
        invalid = _extract_full_file_replacement(
            "```python\ndef value(:\n    return 2\n```",
            "example.py",
            original,
        )

        assert multiple["new_content"] is None
        assert invalid["new_content"] is None
        assert "syntax error" in invalid["warnings"][0]

    def test_rejects_diff_fence_instead_of_treating_it_as_non_python_source(self):
        original = "export const value = 1;\nexport const stable = true;\n"
        reply = (
            "```diff\n"
            "--- a/example.js\n"
            "+++ b/example.js\n"
            "@@ -1,2 +1,2 @@\n"
            "-export const value = 1;\n"
            "+export const value = 2;\n"
            " export const stable = true;\n"
            "```"
        )

        result = _extract_full_file_replacement(reply, "example.js", original)

        assert result["new_content"] is None
        assert "unified diff" in result["warnings"][0]


class TestSemanticReplacementGuard:
    def test_rejects_false_literals_in_true_value_set(self):
        warnings = _semantic_replacement_warnings(
            "feature_gate.py",
            '_TRUE_VALUES = {"1", "true", "0", "off"}\n',
        )

        assert warnings
        assert "_TRUE_VALUES" in warnings[0]
        assert "'0'" in warnings[0]

    def test_accepts_consistent_true_and_false_value_sets(self):
        warnings = _semantic_replacement_warnings(
            "feature_gate.py",
            (
                '_TRUE_VALUES = {"1", "true", "yes", "on"}\n'
                '_FALSE_VALUES = {"0", "false", "no", "off", ""}\n'
            ),
        )

        assert warnings == []


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

    def test_snapshot_fallback_batches_repo_lookup(self):
        from app.models.code_brain import CodeSnapshot

        class FakeQuery:
            def __init__(self, rows):
                self._rows = rows
                self.filter_calls = 0

            def filter(self, *_args, **_kwargs):
                self.filter_calls += 1
                return self

            def all(self):
                return self._rows

        class FakeSession:
            def __init__(self, rows):
                self._rows = rows
                self.last_query = None
                self.query_calls = 0

            def query(self, model):
                assert model is CodeSnapshot
                self.query_calls += 1
                self.last_query = FakeQuery(self._rows)
                return self.last_query

        rows = [
            CodeSnapshot(repo_id=1, file_path="app/auth.py"),
            CodeSnapshot(repo_id=2, file_path="app/chat.py"),
        ]
        db = FakeSession(rows)

        grouped = _snapshots_by_repo(db, [1, 2])

        assert [snap.file_path for snap in grouped[1]] == ["app/auth.py"]
        assert [snap.file_path for snap in grouped[2]] == ["app/chat.py"]
        assert db.query_calls == 1
        assert db.last_query.filter_calls == 1
