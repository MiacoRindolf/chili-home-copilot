"""Phase 1 coding validator: allowlist, timeout, read-only AST step."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.coding_task.envelope import subprocess_safe_env
from app.services.coding_task.validator_runner import (
    assert_allowlisted_step,
    run_ast_syntax,
    run_pytest_collect,
    run_pytest_targeted,
    _run_subprocess_allowlisted,
)


def test_disallowed_step_raises() -> None:
    with pytest.raises(ValueError, match="Disallowed"):
        assert_allowlisted_step("pip_install")


def test_subprocess_safe_env_strips_arbitrary_vars(monkeypatch) -> None:
    monkeypatch.setenv("MALICIOUS_INJECTION", "1")
    monkeypatch.setenv("HTTP_PROXY", "http://evil")
    env = subprocess_safe_env()
    assert "MALICIOUS_INJECTION" not in env
    assert "HTTP_PROXY" not in env


def test_ast_syntax_does_not_mutate_source_file(tmp_path: Path) -> None:
    p = tmp_path / "sample.py"
    p.write_text("a = 1\n", encoding="utf-8")
    before = p.stat().st_mtime_ns
    r = run_ast_syntax(tmp_path)
    assert p.stat().st_mtime_ns == before
    assert r.exit_code == 0
    assert r.step_key == "ast_syntax"


def test_ast_syntax_targets_changed_python_files_and_records_scope(tmp_path: Path) -> None:
    changed = tmp_path / "app/changed.py"
    untouched = tmp_path / "app/untouched.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("value = 1\n", encoding="utf-8")
    untouched.write_text("def broken(:\n", encoding="utf-8")

    result = run_ast_syntax(tmp_path, ["app/changed.py", "README.md"])

    assert result.exit_code == 0
    assert result.metadata == {
        "validation_scope": "changed_python_files",
        "changed_files": ["app/changed.py", "README.md"],
        "parsed_python_files": ["app/changed.py"],
        "ignored_files": ["README.md"],
    }


def test_ast_syntax_rejects_changed_path_outside_worktree(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("def broken(:\n", encoding="utf-8")

    result = run_ast_syntax(tmp_path, ["../outside.py"])

    assert result.exit_code == 0
    assert result.metadata is not None
    assert result.metadata["parsed_python_files"] == []
    assert result.metadata["ignored_files"] == ["../outside.py"]


def test_subprocess_timeout_kills_long_running_step(tmp_path: Path) -> None:
    with patch("app.services.coding_task.validator_runner._timeout", return_value=0.08):
        code, timed_out, out, err = _run_subprocess_allowlisted(
            [__import__("sys").executable, "-c", "import time; time.sleep(30)"],
            tmp_path,
        )
    assert timed_out is True
    assert code == -1


def test_pytest_collect_skips_when_safe_test_database_is_not_configured(tmp_path: Path) -> None:
    err = "RuntimeError: Tests require TEST_DATABASE_URL pointing at a dedicated test database"
    with patch(
        "app.services.coding_task.validator_runner._run_subprocess_allowlisted",
        return_value=(4, False, "", err),
    ):
        result = run_pytest_collect(tmp_path)

    assert result.step_key == "pytest_collect"
    assert result.exit_code == 0
    assert result.skipped is True
    assert result.skip_reason == "safe TEST_DATABASE_URL not configured"
    assert "TEST_DATABASE_URL" in result.stderr


def test_pytest_targeted_skips_when_safe_test_database_is_not_configured(tmp_path: Path) -> None:
    err = "RuntimeError: TEST_DATABASE_URL must be a PostgreSQL URL for pytest."
    with patch(
        "app.services.coding_task.validator_runner._run_subprocess_allowlisted",
        return_value=(4, False, "", err),
    ):
        result = run_pytest_targeted(tmp_path, ["app/example.py"])

    assert result.step_key == "pytest_targeted"
    assert result.exit_code == 0
    assert result.skipped is True
    assert result.skip_reason == "safe TEST_DATABASE_URL not configured"
    assert "TEST_DATABASE_URL" in result.stderr


def test_pytest_targeted_records_selected_behavior_evidence(tmp_path: Path) -> None:
    source = tmp_path / "app/example.py"
    test_file = tmp_path / "tests/test_example.py"
    source.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source.write_text("def value():\n    return 2\n", encoding="utf-8")
    test_file.write_text(
        "from app.example import value\n\n\ndef test_value():\n    assert value() == 2\n",
        encoding="utf-8",
    )

    result = run_pytest_targeted(tmp_path, ["app/example.py"])

    assert result.exit_code == 0
    assert result.metadata is not None
    assert result.metadata["targeted"] is True
    assert result.metadata["fallback_collect_only"] is False
    assert result.metadata["validation_scope"] == "targeted_tests"
    assert result.metadata["test_files"] == ["tests/test_example.py"]
    assert result.metadata["test_selection"][0]["test_file"] == "tests/test_example.py"


def test_pytest_targeted_runs_changed_test_file_directly(tmp_path: Path) -> None:
    test_file = tmp_path / "tests/test_contract.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_contract():\n    assert True\n", encoding="utf-8")

    result = run_pytest_targeted(tmp_path, ["tests/test_contract.py"])

    assert result.exit_code == 0
    assert result.metadata is not None
    assert result.metadata["test_files"] == ["tests/test_contract.py"]
    assert result.metadata["targeted"] is True
