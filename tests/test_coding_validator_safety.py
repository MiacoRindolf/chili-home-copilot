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


def test_dart_analyzer_warning_is_evidence_not_syntax_failure(tmp_path: Path) -> None:
    (tmp_path / "sample.dart").write_text("void main() {}\n", encoding="utf-8")
    warning = (
        "WARNING|STATIC_WARNING|UNUSED_LOCAL_VARIABLE|sample.dart|1|1|1|unused value"
    )
    with patch(
        "app.services.coding_task.validator_runner._dart_executable",
        return_value="dart",
    ), patch(
        "app.services.coding_task.validator_runner._run_subprocess_allowlisted",
        return_value=(2, False, warning, ""),
    ):
        result = run_ast_syntax(tmp_path, changed_files=["sample.dart"])

    assert result.exit_code == 0
    assert result.metadata["dart_analyzer_warnings"]
    assert "warning sample.dart" in result.stdout


def test_dart_analyzer_error_remains_fatal(tmp_path: Path) -> None:
    (tmp_path / "sample.dart").write_text("void main() {}\n", encoding="utf-8")
    error = "ERROR|COMPILE_TIME_ERROR|UNDEFINED_NAME|sample.dart|1|1|1|missing"
    with patch(
        "app.services.coding_task.validator_runner._dart_executable",
        return_value="dart",
    ), patch(
        "app.services.coding_task.validator_runner._run_subprocess_allowlisted",
        return_value=(3, False, error, ""),
    ):
        result = run_ast_syntax(tmp_path, changed_files=["sample.dart"])

    assert result.exit_code == 1
    assert "SyntaxError sample.dart" in result.stdout


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


def test_pytest_targeted_reports_full_contract_scope_without_first_failure(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "example.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run(argv, cwd, **kwargs):
        captured["argv"] = argv
        return 0, False, "1 passed", ""

    with patch(
        "app.services.coding_task.validator_runner._run_subprocess_allowlisted",
        side_effect=fake_run,
    ):
        result = run_pytest_targeted(tmp_path, ["app/example.py"])

    argv = captured["argv"]
    assert "-x" not in argv
    assert result.metadata["tests_executed"] is True
    assert result.metadata["test_files"] == ["tests/test_example.py"]
    assert result.metadata["targeted"] is True
    assert result.metadata["validation_scope"] == "targeted_tests"


def test_pytest_targeted_records_contract_identities_on_test_failure(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "example.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert False\n",
        encoding="utf-8",
    )
    output = "tests/test_example.py::test_example FAILED [100%]\n"
    with patch(
        "app.services.coding_task.validator_runner._run_subprocess_allowlisted",
        return_value=(1, False, output, ""),
    ):
        result = run_pytest_targeted(tmp_path, ["app/example.py"])

    assert result.exit_code == 1
    assert result.metadata["tests_executed"] is True
    assert result.metadata["test_contract_status"] == {
        "tests/test_example.py::test_example": "failed"
    }
    assert result.metadata["test_contracts_complete"] is True


def test_pytest_targeted_fails_closed_when_selected_files_collect_zero_tests(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "example.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_example.py").write_text("# no tests\n", encoding="utf-8")

    with patch(
        "app.services.coding_task.validator_runner._run_subprocess_allowlisted",
        return_value=(5, False, "no tests ran", ""),
    ):
        result = run_pytest_targeted(tmp_path, ["app/example.py"])

    assert result.exit_code == 5
    assert result.metadata["tests_executed"] is False
    assert result.metadata["zero_tests_collected"] is True


def test_pytest_targeted_pins_original_repair_contract(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "new_owner.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_new_owner.py").write_text(
        "def test_new():\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_original.py").write_text(
        "def test_original():\n    assert True\n",
        encoding="utf-8",
    )
    captured = {}

    def fake_run(argv, cwd, **kwargs):
        captured["argv"] = argv
        return 0, False, "1 passed", ""

    with patch(
        "app.services.coding_task.validator_runner._run_subprocess_allowlisted",
        side_effect=fake_run,
    ):
        result = run_pytest_targeted(
            tmp_path,
            ["app/new_owner.py"],
            selected_test_files=["tests/test_original.py"],
        )

    assert "tests/test_original.py" in captured["argv"]
    assert "tests/test_new_owner.py" not in captured["argv"]
    assert result.metadata["test_files"] == ["tests/test_original.py"]
