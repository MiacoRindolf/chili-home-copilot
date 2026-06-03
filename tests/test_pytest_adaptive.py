from __future__ import annotations

import os
import sys
from pathlib import Path

from scripts import pytest_adaptive


def _venv_python(root: Path, dirname: str = ".venv") -> Path:
    if os.name == "nt":
        return root / dirname / "Scripts" / "python.exe"
    return root / dirname / "bin" / "python"


def _write_pyvenv_cfg(root: Path, dirname: str = ".venv", *, include_system: bool) -> None:
    env_dir = root / dirname
    env_dir.mkdir(parents=True, exist_ok=True)
    value = "true" if include_system else "false"
    (env_dir / "pyvenv.cfg").write_text(
        f"include-system-site-packages = {value}\n",
        encoding="utf-8",
    )


def test_pytest_runtime_isolation_reads_isolated_pyvenv_cfg(tmp_path):
    local_python = _venv_python(tmp_path)
    local_python.parent.mkdir(parents=True)
    local_python.write_text("", encoding="utf-8")
    _write_pyvenv_cfg(tmp_path, include_system=False)

    isolation = pytest_adaptive.pytest_runtime_isolation(local_python, source=".venv")

    assert isolation == ("isolated", False)


def test_pytest_runtime_isolation_flags_shared_site_packages(tmp_path):
    local_python = _venv_python(tmp_path)
    local_python.parent.mkdir(parents=True)
    local_python.write_text("", encoding="utf-8")
    _write_pyvenv_cfg(tmp_path, include_system=True)

    isolation = pytest_adaptive.pytest_runtime_isolation(local_python, source=".venv")

    assert isolation == ("shared_site_packages", True)


def test_pytest_runtime_contract_rejects_unsupported_major(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pytest_adaptive.importlib.metadata,
        "version",
        lambda package: "9.0.2" if package == "pytest" else "0",
    )

    contract = pytest_adaptive.pytest_runtime_contract(tmp_path)

    assert contract.status == "warning"
    assert contract.required == "pytest>=8.2,<9"
    assert contract.actual == "9.0.2"
    assert contract.source == "current"
    assert contract.isolation_status == "shared_runtime"
    assert "repo-local Python environment" in contract.recovery


def test_pytest_runtime_contract_accepts_supported_pytest(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pytest_adaptive.importlib.metadata,
        "version",
        lambda package: "8.4.2" if package == "pytest" else "0",
    )

    contract = pytest_adaptive.pytest_runtime_contract(tmp_path)

    assert contract.status == "passed"
    assert contract.passed is True
    assert contract.source == "current"
    assert contract.isolation_status == "shared_runtime"


def test_pytest_runtime_contract_prefers_repo_local_supported_python(
    monkeypatch,
    tmp_path,
):
    local_python = _venv_python(tmp_path)
    local_python.parent.mkdir(parents=True)
    local_python.write_text("", encoding="utf-8")
    _write_pyvenv_cfg(tmp_path, include_system=False)

    def fake_version_for_python(path):
        return "8.4.2" if Path(path) == local_python.resolve() else "9.0.2"

    monkeypatch.setattr(pytest_adaptive, "pytest_version_for_python", fake_version_for_python)

    contract = pytest_adaptive.pytest_runtime_contract(tmp_path)

    assert contract.status == "passed"
    assert contract.source == ".venv"
    assert contract.python == str(local_python.resolve())
    assert contract.actual == "8.4.2"
    assert contract.isolation_status == "isolated"


def test_pytest_runtime_contract_honors_explicit_python_override(
    monkeypatch,
    tmp_path,
):
    override_python = tmp_path / "tools" / "python.exe"
    override_python.parent.mkdir(parents=True)
    override_python.write_text("", encoding="utf-8")
    local_python = _venv_python(tmp_path)
    local_python.parent.mkdir(parents=True)
    local_python.write_text("", encoding="utf-8")

    monkeypatch.setenv(
        pytest_adaptive.PYTEST_PYTHON_ENV_VAR,
        str(override_python),
    )
    monkeypatch.setattr(
        pytest_adaptive,
        "pytest_version_for_python",
        lambda _path: "8.4.2",
    )

    contract = pytest_adaptive.pytest_runtime_contract(tmp_path)

    assert contract.status == "passed"
    assert contract.source == pytest_adaptive.PYTEST_PYTHON_ENV_VAR
    assert contract.python == str(override_python.resolve())


def test_pytest_runtime_contract_does_not_bypass_unsupported_explicit_python(
    monkeypatch,
    tmp_path,
):
    override_python = tmp_path / "tools" / "python.exe"
    override_python.parent.mkdir(parents=True)
    override_python.write_text("", encoding="utf-8")
    local_python = _venv_python(tmp_path)
    local_python.parent.mkdir(parents=True)
    local_python.write_text("", encoding="utf-8")

    monkeypatch.setenv(
        pytest_adaptive.PYTEST_PYTHON_ENV_VAR,
        str(override_python),
    )

    def fake_version_for_python(path):
        return "9.0.2" if Path(path) == override_python.resolve() else "8.4.2"

    monkeypatch.setattr(pytest_adaptive, "pytest_version_for_python", fake_version_for_python)

    contract = pytest_adaptive.pytest_runtime_contract(tmp_path)

    assert contract.status == "warning"
    assert contract.source == pytest_adaptive.PYTEST_PYTHON_ENV_VAR
    assert contract.actual == "9.0.2"


def test_run_pytest_stops_before_subprocess_when_runtime_unsupported(monkeypatch, capsys):
    monkeypatch.setattr(
        pytest_adaptive,
        "pytest_runtime_contract",
        lambda: pytest_adaptive.PytestRuntimeContract(
            status="warning",
            required=pytest_adaptive.SUPPORTED_PYTEST_SPEC,
            actual="9.0.2",
            python=sys.executable,
            source="current",
            candidate_count=1,
            isolation_status="shared_runtime",
            include_system_site_packages=None,
            isolation_recovery="create or select a repo-local runtime",
            recovery="use a repo-local runtime",
        ),
    )

    def fail_run(*_args, **_kwargs):
        raise AssertionError("pytest subprocess should not run")

    monkeypatch.setattr(pytest_adaptive.subprocess, "run", fail_run)

    exit_code = pytest_adaptive.run_pytest_with_runtime_contract(["tests/test_example.py"])

    captured = capsys.readouterr()
    assert exit_code == pytest_adaptive.PYTEST_UNSUPPORTED_EXIT_CODE
    assert "pytest runtime unsupported" in captured.err


def test_pytest_runtime_doctor_reports_repo_local_setup_action(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pytest_adaptive,
        "pytest_version_for_python",
        lambda _path: "9.0.2",
    )

    report = pytest_adaptive.pytest_runtime_doctor(tmp_path)

    assert report["status"] == "warning"
    assert report["schema"] == "chili.pytest-runtime.v1"
    assert report["env_dir"] == str(tmp_path / ".pytest_venv")
    assert "ensure-runtime --create" in str(report["next_action"])
    assert "venv" in str(report["create_command"])
    assert "requirements.txt" in str(report["install_command"])
    assert "pytest>=8.2,<9" in str(report["install_command"])
    assert "shared/global Python" in str(report["safety"])


def test_pytest_runtime_doctor_warns_when_supported_runtime_is_not_isolated(
    monkeypatch,
    tmp_path,
):
    local_python = _venv_python(tmp_path, dirname=".pytest_venv")
    local_python.parent.mkdir(parents=True)
    local_python.write_text("", encoding="utf-8")
    _write_pyvenv_cfg(tmp_path, dirname=".pytest_venv", include_system=True)
    monkeypatch.setattr(
        pytest_adaptive,
        "pytest_version_for_python",
        lambda _path: "8.4.2",
    )

    report = pytest_adaptive.pytest_runtime_doctor(tmp_path)

    assert report["status"] == "warning"
    assert report["runtime"]["status"] == "passed"
    assert report["runtime"]["isolation_status"] == "shared_site_packages"
    assert "without system site packages" in str(report["next_action"])


def test_ensure_runtime_without_create_only_reports_plan(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        pytest_adaptive,
        "pytest_version_for_python",
        lambda _path: "9.0.2",
    )

    def fail_run(*_args, **_kwargs):
        raise AssertionError("ensure-runtime without --create must not run commands")

    monkeypatch.setattr(pytest_adaptive.subprocess, "run", fail_run)

    exit_code = pytest_adaptive.ensure_pytest_runtime(root=tmp_path, create=False)

    captured = capsys.readouterr()
    assert exit_code == pytest_adaptive.PYTEST_UNSUPPORTED_EXIT_CODE
    assert '"schema": "chili.pytest-runtime.v1"' in captured.out
    assert "ensure-runtime --create" in captured.out


def test_ensure_runtime_create_uses_repo_local_virtualenv(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(pytest_adaptive.PYTEST_PYTHON_ENV_VAR, raising=False)
    env_python = pytest_adaptive.runtime_env_python(root=tmp_path)
    calls: list[list[str]] = []

    def fake_version_for_python(path):
        resolved = Path(path)
        return "8.4.2" if resolved == env_python.resolve() and env_python.exists() else "9.0.2"

    def fake_run(command, **_kwargs):
        calls.append([str(part) for part in command])
        if command[:3] == [sys.executable, "-m", "venv"]:
            env_python.parent.mkdir(parents=True, exist_ok=True)
            env_python.write_text("", encoding="utf-8")
            _write_pyvenv_cfg(tmp_path, dirname=".pytest_venv", include_system=False)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(pytest_adaptive, "pytest_version_for_python", fake_version_for_python)
    monkeypatch.setattr(pytest_adaptive.subprocess, "run", fake_run)

    exit_code = pytest_adaptive.ensure_pytest_runtime(root=tmp_path, create=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls[0] == [sys.executable, "-m", "venv", str(tmp_path / ".pytest_venv")]
    assert calls[1] == [
        str(env_python),
        "-m",
        "pip",
        "install",
        "-r",
        str(tmp_path / "requirements.txt"),
        pytest_adaptive.SUPPORTED_PYTEST_SPEC,
    ]
    assert os.environ[pytest_adaptive.PYTEST_PYTHON_ENV_VAR] == str(env_python)
    assert '"status": "passed"' in captured.out


def test_ensure_runtime_create_repairs_shared_site_packages_runtime(
    monkeypatch,
    tmp_path,
    capsys,
):
    monkeypatch.delenv(pytest_adaptive.PYTEST_PYTHON_ENV_VAR, raising=False)
    env_python = pytest_adaptive.runtime_env_python(root=tmp_path)
    env_python.parent.mkdir(parents=True, exist_ok=True)
    env_python.write_text("", encoding="utf-8")
    _write_pyvenv_cfg(tmp_path, dirname=".pytest_venv", include_system=True)
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append([str(part) for part in command])
        if command[:3] == [sys.executable, "-m", "venv"]:
            env_python.parent.mkdir(parents=True, exist_ok=True)
            env_python.write_text("", encoding="utf-8")
            _write_pyvenv_cfg(tmp_path, dirname=".pytest_venv", include_system=False)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(
        pytest_adaptive,
        "pytest_version_for_python",
        lambda _path: "8.4.2",
    )
    monkeypatch.setattr(pytest_adaptive.subprocess, "run", fake_run)

    exit_code = pytest_adaptive.ensure_pytest_runtime(root=tmp_path, create=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls[0] == [
        sys.executable,
        "-m",
        "venv",
        "--clear",
        str(tmp_path / ".pytest_venv"),
    ]
    assert calls[1] == [
        str(env_python),
        "-m",
        "pip",
        "install",
        "-r",
        str(tmp_path / "requirements.txt"),
        pytest_adaptive.SUPPORTED_PYTEST_SPEC,
    ]
    assert '"isolation_status": "isolated"' in captured.out


def test_selected_test_file_count_uses_existing_pytest_paths(tmp_path):
    tests_dir = tmp_path / "tests"
    nested_dir = tests_dir / "nested"
    nested_dir.mkdir(parents=True)
    (tests_dir / "test_one.py").write_text("def test_one(): pass\n", encoding="utf-8")
    (tests_dir / "helper.py").write_text("", encoding="utf-8")
    (nested_dir / "test_two.py").write_text("def test_two(): pass\n", encoding="utf-8")

    assert pytest_adaptive.selected_test_file_count(["tests"], root=tmp_path) == 2
    assert pytest_adaptive.selected_test_file_count(["tests/test_one.py"], root=tmp_path) == 1
    assert (
        pytest_adaptive.selected_test_file_count(
            ["--ignore", "tests/test_one.py", "tests"],
            root=tmp_path,
        )
        == 2
    )


def test_build_profile_reuses_selected_test_count(monkeypatch):
    calls = 0

    def fake_selected_test_file_count(args, *, root=None):
        nonlocal calls
        calls += 1
        return 9

    monkeypatch.setattr(
        pytest_adaptive,
        "selected_test_file_count",
        fake_selected_test_file_count,
    )

    profile = pytest_adaptive.build_profile(["tests"])

    assert calls == 1
    assert profile.selected_test_file_count == 9


def test_environment_overrides_adaptive_db_slot_count(monkeypatch):
    monkeypatch.setenv("CHILI_PYTEST_DB_POOL_SIZE", "0")

    assert pytest_adaptive.resolve_db_slot_count(["-n", "auto"]) == 0


def test_advisory_lock_key_pair_is_stable_signed_int32():
    first = pytest_adaptive.advisory_lock_key_pair("repo", "chili_test")
    second = pytest_adaptive.advisory_lock_key_pair("repo", "chili_test")
    bits = (
        pytest_adaptive.POSTGRES_ADVISORY_LOCK_BYTES_PER_KEY
        * pytest_adaptive.BITS_PER_BYTE
    )
    lower_bound = -(1 << (bits - 1))
    upper_bound = (1 << (bits - 1)) - 1

    assert first == second
    assert all(lower_bound <= part <= upper_bound for part in first)
