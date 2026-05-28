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
