import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "scripts" / "dispatch-phase5i-post-rename-soak-probe.ps1"
OUT = ROOT / "scripts" / "dispatch-phase5i-post-rename-soak-probe-out.txt"


def _powershell():
    if os.name != "nt":
        pytest.skip("Phase 5I dispatcher is a Windows PowerShell wrapper")

    return shutil.which("powershell") or shutil.which("pwsh")


def _write_cmd(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _run_dispatcher(
    tmp_path: Path,
    conda_body: str,
    docker_body: str | None,
    timeout: int = 5,
    isolate_path: bool = False,
):
    ps = _powershell()
    if ps is None:
        raise AssertionError("PowerShell is required for this dispatcher test")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_cmd(bin_dir / "conda.cmd", conda_body)
    if docker_body is not None:
        _write_cmd(bin_dir / "docker.cmd", docker_body)

    env = os.environ.copy()
    if isolate_path:
        system32 = Path(os.environ["SystemRoot"]) / "System32"
        env["PATH"] = str(bin_dir) + os.pathsep + str(system32)
    else:
        env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    if OUT.exists():
        OUT.unlink()

    result = subprocess.run(
        [
            ps,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(DISPATCHER),
            "-LogScanTimeoutSeconds",
            str(timeout),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    output = OUT.read_text(encoding="utf-8")
    return result, output


def test_phase5i_dispatcher_emits_success_log_scan_and_exit_code(tmp_path):
    result, output = _run_dispatcher(
        tmp_path,
        conda_body="@echo off\necho VERDICT_STATUS=IN_FLIGHT\nexit /b 0\n",
        docker_body="@echo off\necho ordinary service log line\nexit /b 0\n",
    )

    assert result.returncode == 0
    assert "VERDICT_STATUS=IN_FLIGHT" in output
    assert "LOG_SCHEMA_SCAN_STATUS=OK" in output
    assert "LOG_SCHEMA_ERRORS=0" in output
    assert "EXIT_CODE=0" in output


def test_phase5i_dispatcher_marks_log_scan_failure_without_masking_probe(tmp_path):
    result, output = _run_dispatcher(
        tmp_path,
        conda_body="@echo off\necho VERDICT_STATUS=BLOCKED_DRIFT\nexit /b 1\n",
        docker_body="@echo off\necho docker unavailable 1>&2\nexit /b 7\n",
    )

    assert result.returncode == 1
    assert "VERDICT_STATUS=BLOCKED_DRIFT" in output
    assert "LOG_SCHEMA_SCAN_STATUS=FAILED" in output
    assert "LOG_SCHEMA_ERRORS=UNKNOWN" in output
    assert "EXIT_CODE=1" in output


def test_phase5i_dispatcher_marks_log_scan_unavailable_without_masking_probe(tmp_path):
    result, output = _run_dispatcher(
        tmp_path,
        conda_body="@echo off\necho VERDICT_STATUS=IN_FLIGHT\nexit /b 0\n",
        docker_body=None,
        isolate_path=True,
    )

    assert result.returncode == 0
    assert "LOG_SCHEMA_SCAN_STATUS=UNAVAILABLE" in output
    assert "LOG_SCHEMA_ERRORS=UNKNOWN" in output
    assert "EXIT_CODE=0" in output


def test_phase5i_dispatcher_marks_log_scan_timeout_without_hanging(tmp_path):
    result, output = _run_dispatcher(
        tmp_path,
        conda_body="@echo off\necho VERDICT_STATUS=IN_FLIGHT\nexit /b 0\n",
        docker_body="@echo off\npowershell -NoProfile -Command Start-Sleep -Seconds 5\nexit /b 0\n",
        timeout=1,
    )

    assert result.returncode == 0
    assert "LOG_SCHEMA_SCAN_STATUS=TIMEOUT" in output
    assert "LOG_SCHEMA_ERRORS=UNKNOWN" in output
    assert "EXIT_CODE=0" in output
