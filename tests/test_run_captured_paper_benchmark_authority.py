from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time
from typing import Any, Mapping

import pytest
import psutil

from scripts import build_captured_paper_benchmark_authority as builder
from scripts import run_captured_paper_benchmark_authority as runner


REPO = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _git(candidate: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git.exe" if sys.platform == "win32" else "git")
    assert git
    return subprocess.run(
        [git, *arguments],
        cwd=candidate,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        shell=False,
        check=True,
    )


def _dependency_root(tmp_path: Path, python: Path, python_sha: str) -> tuple[Path, str]:
    capsule = tmp_path / "dependencies"
    pending = capsule / "pending" / "site-packages"
    for name, version in (("psutil", "5.9.8"), ("zstandard", "0.23.0")):
        (pending / name).mkdir(parents=True)
        (pending / name / "__init__.py").write_text(
            f"__version__ = {version!r}\n", encoding="utf-8"
        )
        metadata = pending / f"{name}-{version}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            encoding="utf-8",
        )
    tree = builder._inventory_dependency_tree(pending.resolve(strict=True))
    sealed = capsule / str(tree["tree_sha256"])
    (capsule / "pending").rename(sealed)
    dependency = (sealed / "site-packages").resolve(strict=True)
    identity, _ = builder._dependency_identity(
        root=dependency,
        python_executable=python,
        python_executable_sha256=python_sha,
    )
    return dependency, _sha(builder._canonical_json_bytes(dict(identity)))


def _build_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> builder.BuiltCapturedPaperBenchmarkAuthority:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    hashes: dict[str, str] = {}
    for role, relative in builder.SOURCE_ROLE_PATHS.items():
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)
        hashes[role] = _sha(target.read_bytes())
    for relative in builder.AUTHORITY_PROGRAM_PATHS.values():
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)
    _git(candidate, "init")
    _git(candidate, "config", "core.autocrlf", "false")
    _git(candidate, "config", "user.name", "CHILI Test")
    _git(candidate, "config", "user.email", "chili-test@example.invalid")
    _git(candidate, "add", "--all")
    _git(candidate, "commit", "-m", "sealed benchmark runner candidate")
    commit = _git(candidate, "rev-parse", "HEAD").stdout.strip()
    git = Path(shutil.which("git.exe" if sys.platform == "win32" else "git") or "").resolve(
        strict=True
    )
    python = Path(sys.executable).resolve(strict=True)
    python_sha = _sha(python.read_bytes())
    dependency, dependency_sha = _dependency_root(tmp_path, python, python_sha)
    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(builder, "_probe_python311", lambda _path: None)
    return builder.build_captured_paper_benchmark_authority(
        repo_root=candidate.resolve(strict=True),
        authority_root=authority_root.resolve(strict=True),
        expected_git_commit=commit,
        git_executable=git,
        git_executable_sha256=_sha(git.read_bytes()),
        expected_source_sha256=hashes,
        builder_source_path=candidate / builder.AUTHORITY_PROGRAM_PATHS["builder"],
        builder_source_sha256=_sha(
            (candidate / builder.AUTHORITY_PROGRAM_PATHS["builder"]).read_bytes()
        ),
        runner_source_path=candidate / builder.AUTHORITY_PROGRAM_PATHS["runner"],
        runner_source_sha256=_sha(
            (candidate / builder.AUTHORITY_PROGRAM_PATHS["runner"]).read_bytes()
        ),
        python_executable=python,
        python_executable_sha256=python_sha,
        python_dependency_root=dependency,
        python_dependency_root_identity_sha256=dependency_sha,
        benchmark_output_root=output.resolve(strict=True),
        benchmark_arguments=(
            "--events",
            "1000",
            "--compression-codec",
            "zlib",
            "--keep",
        ),
    )


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fake_success_executor(
    *,
    manifest: Mapping[str, Any],
    observed: dict[str, Any],
) -> Any:
    def execute(
        *,
        argv: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> tuple[int, bytes, bytes]:
        observed.update(
            {
                "argv": tuple(argv),
                "cwd": cwd,
                "environment": dict(environment),
                "timeout_seconds": timeout_seconds,
            }
        )
        output_root = Path(str(manifest["output"]["root"]))
        owner_token = "a" * 32
        directory = output_root / (
            f"chili-replay-capture-benchmark-{owner_token}-fake"
        )
        directory.mkdir()
        marker = {
            "benchmark_schema_version": builder.EXPECTED_BENCHMARK_SCHEMA_VERSION,
            "directory": str(directory),
            "output_root": str(output_root),
            "owner_token": owner_token,
        }
        (directory / ".chili-replay-capture-benchmark-owner.json").write_bytes(
            _canonical(marker) + b"\n"
        )
        source_key = {
            "benchmark": "benchmark_script_sha256",
            "contract": "contract_sha256",
            "runtime": "runtime_sha256",
            "pressure_probe": "pressure_probe_sha256",
            "replay_errors": "replay_errors_sha256",
            "first_dip_tape_policy": "first_dip_tape_policy_sha256",
            "stage0": "stage0_sha256",
        }
        sources = {
            source_key[row["role"]]: row["sha256"]
            for row in manifest["source_roster"]
        }
        report = {
            "acceptance": {"accepted": True, "reasons": []},
            "benchmark_schema_version": builder.EXPECTED_BENCHMARK_SCHEMA_VERSION,
            "capture_runtime_source": sources,
            "environment": {
                "benchmark_authority_manifest_sha256": observed["manifest_sha256"],
                "dependency_root_identity_sha256": manifest[
                    "python_dependency_root"
                ]["identity_sha256"],
                "python_executable_sha256": manifest["python"][
                    "executable_sha256"
                ],
            },
            "output": {
                "directory": str(directory),
                "report_artifact_layout": (
                    "reports/<canonical-sha256>.json_when_retained"
                ),
                "retained": True,
                "safe_cleanup_verified": False,
            },
        }
        raw = _canonical(report)
        reports = directory / "reports"
        reports.mkdir()
        (reports / f"{_sha(raw)}.json").write_bytes(raw)
        return 0, raw + b"\n", b""

    return execute


def _load_authority(
    built: builder.BuiltCapturedPaperBenchmarkAuthority,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    return (
        json.loads(built.receipt_path.read_bytes()),
        json.loads(built.manifest_path.read_bytes()),
    )


def test_runner_publishes_terminal_receipt_for_exact_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_authority(tmp_path, monkeypatch)
    launch, manifest = _load_authority(built)
    monkeypatch.setattr(runner.authority, "_probe_python311", lambda _path: None)
    observed: dict[str, Any] = {"manifest_sha256": built.manifest_sha256}
    instants = iter(
        (
            datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 13, 12, 0, 3, tzinfo=UTC),
        )
    )

    receipt_path, receipt_sha = runner.run_captured_paper_benchmark_authority(
        receipt_path=built.receipt_path,
        receipt_sha256=built.receipt_sha256,
        executor=_fake_success_executor(manifest=manifest, observed=observed),
        clock=lambda: next(instants),
    )

    raw = receipt_path.read_bytes()
    terminal = json.loads(raw)
    assert receipt_sha == _sha(raw)
    assert raw == _canonical(terminal)
    assert receipt_path.name == f"{receipt_sha}.json"
    assert terminal["schema_version"] == runner.EXECUTION_RECEIPT_SCHEMA_VERSION
    assert terminal["invoked"] is True
    assert terminal["benchmark_completed"] is True
    assert terminal["benchmark"]["exit_code"] == 0
    assert terminal["benchmark"]["acceptance"] == {
        "accepted": True,
        "reasons": [],
    }
    report_ref = terminal["benchmark"]["report"]
    report_raw = Path(report_ref["path"]).read_bytes()
    assert report_ref["sha256"] == _sha(report_raw)
    assert terminal["benchmark"]["stdout_without_newline_sha256"] == _sha(
        report_raw
    )
    assert terminal["launch_receipt"] == {
        "path": str(built.receipt_path),
        "sha256": built.receipt_sha256,
    }
    assert terminal["manifest"] == {
        "path": str(built.manifest_path),
        "sha256": built.manifest_sha256,
    }
    assert terminal["git"] == manifest["git"]
    assert terminal["expected_git_commit"] == manifest["expected_git_commit"]
    assert terminal["authority_programs"] == manifest["authority_programs"]
    assert Path(terminal["execution_claim"]["path"]).is_file()
    assert terminal["posture"] == {
        "benchmark_output_written": True,
        "broker_contacted": False,
        "cutover_performed": False,
        "database_accessed": False,
        "host_activation_performed": False,
        "live_cash_authorized": False,
        "orders_submitted": False,
        "provider_contacted": False,
        "task_scheduler_mutated": False,
    }
    assert observed["argv"] == tuple(launch["benchmark_argv"])
    assert observed["cwd"] == Path(manifest["execution_context"]["cwd"])
    assert observed["environment"] == manifest["execution_context"]["environment"]
    assert observed["timeout_seconds"] == manifest["execution_context"][
        "timeout_seconds"
    ]


def test_runner_rejects_tampered_launch_receipt_before_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_authority(tmp_path, monkeypatch)
    called = False

    def executor(**_kwargs: Any) -> tuple[int, bytes, bytes]:
        nonlocal called
        called = True
        return 0, b"{}\n", b""

    with pytest.raises(
        runner.CapturedPaperBenchmarkExecutionError,
        match="CONTENT_ADDRESS_INVALID",
    ):
        runner.run_captured_paper_benchmark_authority(
            receipt_path=built.receipt_path,
            receipt_sha256="0" * 64,
            executor=executor,
        )
    assert called is False


@pytest.mark.parametrize(
    "exit_code,stderr,expected",
    ((2, b"", "BENCHMARK_REJECTED"), (0, b"error", "BENCHMARK_REJECTED")),
)
def test_failed_process_is_claimed_but_never_terminally_attested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    stderr: bytes,
    expected: str,
) -> None:
    built = _build_authority(tmp_path, monkeypatch)
    monkeypatch.setattr(runner.authority, "_probe_python311", lambda _path: None)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    with pytest.raises(runner.CapturedPaperBenchmarkExecutionError, match=expected):
        runner.run_captured_paper_benchmark_authority(
            receipt_path=built.receipt_path,
            receipt_sha256=built.receipt_sha256,
            executor=lambda **_kwargs: (exit_code, b"{}\n", stderr),
            clock=lambda: now,
        )
    assert tuple(built.receipt_path.parents[3].glob("authority/execution-claim/*.json"))
    assert not tuple(
        built.receipt_path.parents[3].glob("authority/execution-receipt/**/*.json")
    )


def test_runner_rejects_stdout_that_differs_from_retained_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_authority(tmp_path, monkeypatch)
    _launch, manifest = _load_authority(built)
    monkeypatch.setattr(runner.authority, "_probe_python311", lambda _path: None)
    observed: dict[str, Any] = {"manifest_sha256": built.manifest_sha256}
    success = _fake_success_executor(manifest=manifest, observed=observed)

    def mismatch(**kwargs: Any) -> tuple[int, bytes, bytes]:
        exit_code, stdout, stderr = success(**kwargs)
        return exit_code, stdout.replace(b'"accepted":true', b'"accepted":false'), stderr

    start = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    instants = iter((start, start + timedelta(seconds=1)))
    with pytest.raises(
        runner.CapturedPaperBenchmarkExecutionError,
        match="BENCHMARK_REPORT_MISMATCH|BENCHMARK_REPORT_PATH_INVALID",
    ):
        runner.run_captured_paper_benchmark_authority(
            receipt_path=built.receipt_path,
            receipt_sha256=built.receipt_sha256,
            executor=mismatch,
            clock=lambda: next(instants),
        )


def test_runner_is_one_shot_even_after_failed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_authority(tmp_path, monkeypatch)
    monkeypatch.setattr(runner.authority, "_probe_python311", lambda _path: None)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    with pytest.raises(runner.CapturedPaperBenchmarkExecutionError, match="BENCHMARK_REJECTED"):
        runner.run_captured_paper_benchmark_authority(
            receipt_path=built.receipt_path,
            receipt_sha256=built.receipt_sha256,
            executor=lambda **_kwargs: (2, b"{}\n", b""),
            clock=lambda: now,
        )
    with pytest.raises(
        runner.CapturedPaperBenchmarkExecutionError,
        match="LAUNCH_RECEIPT_ALREADY_INVOKED",
    ):
        runner.run_captured_paper_benchmark_authority(
            receipt_path=built.receipt_path,
            receipt_sha256=built.receipt_sha256,
            executor=lambda **_kwargs: (0, b"{}\n", b""),
            clock=lambda: now,
        )


def test_runner_rejects_source_drift_before_claim_or_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_authority(tmp_path, monkeypatch)
    launch, _manifest = _load_authority(built)
    source = Path(launch["source_roster"][0]["path"])
    source.write_bytes(source.read_bytes() + b"drift")
    called = False

    def executor(**_kwargs: Any) -> tuple[int, bytes, bytes]:
        nonlocal called
        called = True
        return 0, b"{}\n", b""

    with pytest.raises(runner.CapturedPaperBenchmarkExecutionError, match="SOURCE_DRIFT"):
        runner.run_captured_paper_benchmark_authority(
            receipt_path=built.receipt_path,
            receipt_sha256=built.receipt_sha256,
            executor=executor,
        )
    assert called is False
    assert not tuple(built.receipt_path.parents[3].glob("authority/execution-claim/*.json"))


def test_runner_rejects_authority_program_drift_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build_authority(tmp_path, monkeypatch)
    _launch, manifest = _load_authority(built)
    runner_source = Path(manifest["authority_programs"]["runner"]["path"])
    runner_source.write_bytes(runner_source.read_bytes() + b"\n# drift\n")
    with pytest.raises(
        runner.CapturedPaperBenchmarkExecutionError, match="AUTHORITY_PROGRAM_DRIFT"
    ):
        runner.run_captured_paper_benchmark_authority(
            receipt_path=built.receipt_path,
            receipt_sha256=built.receipt_sha256,
            executor=lambda **_kwargs: (0, b"{}\n", b""),
        )
    assert not tuple(built.receipt_path.parents[3].glob("authority/execution-claim/*.json"))


def test_bare_runner_cli_is_rejected_before_argument_processing() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO / builder.AUTHORITY_PROGRAM_PATHS["runner"]), "--help"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        shell=False,
        check=False,
    )
    assert result.returncode != 0
    assert "RUNNER_HELD_BOOTSTRAP_REQUIRED" in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic Job Object contract")
def test_atomic_job_runner_captures_exact_stdout_and_stderr() -> None:
    code = "import sys;sys.stdout.write('ok');sys.stderr.write('note')"
    exit_code, stdout, stderr = runner._bounded_run(
        argv=(sys.executable, "-c", code),
        cwd=REPO,
        environment=builder._sanitized_execution_environment(),
        timeout_seconds=10,
    )
    assert (exit_code, stdout, stderr) == (0, b"ok", b"note")


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic Job Object contract")
def test_atomic_job_runner_caps_output_and_kills_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_MAX_STDOUT_BYTES", 32)
    with pytest.raises(
        runner.CapturedPaperBenchmarkExecutionError,
        match="BENCHMARK_OUTPUT_OVERSIZED",
    ):
        runner._bounded_run(
            argv=(sys.executable, "-c", "import sys;sys.stdout.write('x'*65536)"),
            cwd=REPO,
            environment=builder._sanitized_execution_environment(),
            timeout_seconds=10,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows kill-on-parent-close Job contract")
def test_abnormal_parent_exit_kills_benchmark_and_descendant(tmp_path: Path) -> None:
    pids_path = tmp_path / "job-pids.txt"
    child_code = textwrap.dedent(
        """
        import os, pathlib, subprocess, sys, time
        grandchild = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(300)'])
        pathlib.Path(sys.argv[1]).write_text(
            str(os.getpid()) + '\\n' + str(grandchild.pid), encoding='ascii')
        time.sleep(300)
        """
    )
    parent_code = textwrap.dedent(
        """
        import os, pathlib, sys
        sys.path.insert(0, sys.argv[1])
        from scripts import build_captured_paper_benchmark_authority as builder
        from scripts import run_captured_paper_benchmark_authority as runner
        runner._bounded_run(
            argv=(sys.executable, '-c', sys.argv[3], sys.argv[2]),
            cwd=pathlib.Path(sys.argv[1]),
            environment=builder._sanitized_execution_environment(),
            timeout_seconds=300)
        """
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(REPO), str(pids_path), child_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        close_fds=True,
    )
    pids: list[int] = []
    gone = False
    try:
        for _attempt in range(200):
            if pids_path.is_file():
                pids = [int(value) for value in pids_path.read_text(encoding="ascii").splitlines()]
                if len(pids) == 2:
                    break
            if parent.poll() is not None:
                break
            time.sleep(0.05)
        assert len(pids) == 2
        assert all(psutil.pid_exists(pid) for pid in pids)
        parent.kill()
        parent.wait(timeout=5)
        for _attempt in range(200):
            if not any(psutil.pid_exists(pid) for pid in pids):
                gone = True
                break
            time.sleep(0.05)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        for pid in pids:
            if psutil.pid_exists(pid):
                try:
                    process = psutil.Process(pid)
                    process.kill()
                    process.wait(timeout=5)
                except psutil.NoSuchProcess:
                    pass
    assert gone is True
