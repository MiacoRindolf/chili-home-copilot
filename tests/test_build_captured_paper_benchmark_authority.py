from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any

import pytest

from scripts import build_captured_paper_benchmark_authority as builder


REPO = Path(__file__).resolve().parents[1]


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


@dataclass(slots=True)
class BenchmarkAuthorityFixture:
    candidate: Path
    authority: Path
    output: Path
    dependency: Path
    dependency_identity_sha256: str
    python: Path
    python_sha256: str
    commit: str
    source_hashes: dict[str, str]
    git: Path
    git_sha256: str

    def kwargs(self, **overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "repo_root": self.candidate,
            "authority_root": self.authority,
            "expected_git_commit": self.commit,
            "git_executable": self.git,
            "git_executable_sha256": self.git_sha256,
            "expected_source_sha256": dict(self.source_hashes),
            "builder_source_path": self.candidate / builder.AUTHORITY_PROGRAM_PATHS["builder"],
            "builder_source_sha256": _sha(
                (self.candidate / builder.AUTHORITY_PROGRAM_PATHS["builder"]).read_bytes()
            ),
            "runner_source_path": self.candidate / builder.AUTHORITY_PROGRAM_PATHS["runner"],
            "runner_source_sha256": _sha(
                (self.candidate / builder.AUTHORITY_PROGRAM_PATHS["runner"]).read_bytes()
            ),
            "python_executable": self.python,
            "python_executable_sha256": self.python_sha256,
            "python_dependency_root": self.dependency,
            "python_dependency_root_identity_sha256": (
                self.dependency_identity_sha256
            ),
            "benchmark_output_root": self.output,
            "benchmark_arguments": (
                "--events",
                "1000",
                "--compression-codec",
                "zlib",
                "--keep",
            ),
        }
        values.update(overrides)
        return values


def _make_dependency_root(tmp_path: Path, python: Path, python_sha: str) -> tuple[Path, str]:
    capsule_root = tmp_path / "dependencies"
    draft = capsule_root / "pending" / "site-packages"
    (draft / "psutil").mkdir(parents=True)
    (draft / "psutil" / "__init__.py").write_text(
        "__version__ = '5.9.8'\n", encoding="utf-8"
    )
    (draft / "psutil-5.9.8.dist-info").mkdir()
    (draft / "psutil-5.9.8.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: psutil\nVersion: 5.9.8\n",
        encoding="utf-8",
    )
    (draft / "zstandard").mkdir()
    (draft / "zstandard" / "__init__.py").write_text(
        "__version__ = '0.23.0'\n", encoding="utf-8"
    )
    (draft / "zstandard-0.23.0.dist-info").mkdir()
    (draft / "zstandard-0.23.0.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: zstandard\nVersion: 0.23.0\n",
        encoding="utf-8",
    )
    tree = builder._inventory_dependency_tree(draft.resolve(strict=True))
    sealed_parent = capsule_root / str(tree["tree_sha256"])
    (capsule_root / "pending").rename(sealed_parent)
    dependency = (sealed_parent / "site-packages").resolve(strict=True)
    identity, _tree = builder._dependency_identity(
        root=dependency,
        python_executable=python,
        python_executable_sha256=python_sha,
    )
    return dependency, _sha(builder._canonical_json_bytes(dict(identity)))


def _make_fixture(tmp_path: Path) -> BenchmarkAuthorityFixture:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    source_hashes: dict[str, str] = {}
    for role, relative in builder.SOURCE_ROLE_PATHS.items():
        source = REPO / relative
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        source_hashes[role] = _sha(target.read_bytes())
    for relative in builder.AUTHORITY_PROGRAM_PATHS.values():
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)
    _git(candidate, "init")
    _git(candidate, "config", "core.autocrlf", "false")
    _git(candidate, "config", "user.name", "CHILI Test")
    _git(candidate, "config", "user.email", "chili-test@example.invalid")
    _git(candidate, "add", "--all")
    _git(candidate, "commit", "-m", "sealed benchmark candidate")
    commit = _git(candidate, "rev-parse", "HEAD").stdout.strip()
    git = Path(shutil.which("git.exe" if sys.platform == "win32" else "git") or "").resolve(
        strict=True
    )

    python = Path(sys.executable).resolve(strict=True)
    python_sha = _sha(python.read_bytes())
    dependency, dependency_identity = _make_dependency_root(
        tmp_path, python, python_sha
    )
    authority = tmp_path / "authority"
    authority.mkdir()
    output = tmp_path / "benchmark-output"
    output.mkdir()
    return BenchmarkAuthorityFixture(
        candidate=candidate.resolve(strict=True),
        authority=authority.resolve(strict=True),
        output=output.resolve(strict=True),
        dependency=dependency,
        dependency_identity_sha256=dependency_identity,
        python=python,
        python_sha256=python_sha,
        commit=commit,
        source_hashes=source_hashes,
        git=git,
        git_sha256=_sha(git.read_bytes()),
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path/fd mode synthesis")
@pytest.mark.parametrize("suffix", (".cmd", ".exe"))
def test_dependency_inventory_accepts_windows_path_fd_permission_mode_alias(
    tmp_path: Path, suffix: str
) -> None:
    dependency_root = tmp_path / "site-packages"
    dependency_root.mkdir()
    dependency = dependency_root / f"mode-probe{suffix}"
    dependency.write_bytes(b"sealed\n")
    path_metadata = os.lstat(dependency)
    descriptor = os.open(
        dependency, os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    )
    try:
        descriptor_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    assert path_metadata.st_mode != descriptor_metadata.st_mode
    assert stat.S_IFMT(path_metadata.st_mode) == stat.S_IFMT(
        descriptor_metadata.st_mode
    )

    inventory = builder._inventory_dependency_tree(dependency_root)
    assert inventory["files"][dependency.name]["sha256"] == _sha(
        dependency.read_bytes()
    )


@pytest.fixture
def authority_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> BenchmarkAuthorityFixture:
    fixture = _make_fixture(tmp_path)
    # The test host may not itself be CPython 3.11.  Version-probe behavior has
    # a focused negative test below; construction tests exercise all byte/path
    # pins while treating this exact executable as a successful 3.11 probe.
    monkeypatch.setattr(builder, "_probe_python311", lambda _path: None)
    return fixture


def _read_canonical(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    value = json.loads(raw)
    assert raw == builder._canonical_json_bytes(value)
    assert path.name == f"{_sha(raw)}.json"
    assert path.parent.name == _sha(raw)[:2]
    return value


def test_builds_exact_inert_content_addressed_authority(
    authority_fixture: BenchmarkAuthorityFixture,
) -> None:
    built = builder.build_captured_paper_benchmark_authority(
        **authority_fixture.kwargs()
    )

    manifest = _read_canonical(built.manifest_path)
    receipt = _read_canonical(built.receipt_path)
    runner_authority = _read_canonical(built.runner_authority_path)
    assert built.manifest_sha256 == _sha(built.manifest_path.read_bytes())
    assert built.receipt_sha256 == _sha(built.receipt_path.read_bytes())
    assert manifest["schema_version"] == builder.MANIFEST_SCHEMA_VERSION
    assert receipt["schema_version"] == builder.RECEIPT_SCHEMA_VERSION
    assert runner_authority["schema_version"] == builder.RUNNER_AUTHORITY_SCHEMA_VERSION
    assert built.runner_authority_sha256 == _sha(
        built.runner_authority_path.read_bytes()
    )
    assert manifest["expected_git_commit"] == authority_fixture.commit
    assert manifest["source_roster"] == sorted(
        manifest["source_roster"], key=lambda row: row["role"]
    )
    assert [row["role"] for row in manifest["source_roster"]] == sorted(
        builder.SOURCE_ROLE_PATHS
    )
    assert manifest["source_roster_sha256"] == _sha(
        builder._canonical_json_bytes(manifest["source_roster"])
    )
    assert receipt["manifest"] == {
        "path": str(built.manifest_path),
        "sha256": built.manifest_sha256,
    }
    assert runner_authority["manifest"] == receipt["manifest"]
    assert runner_authority["launch_receipt"] == {
        "path": str(built.receipt_path),
        "sha256": built.receipt_sha256,
    }
    assert runner_authority["runner_argv"] == list(built.runner_argv)
    assert built.runner_argv[:6] == (
        str(authority_fixture.python),
        "-I",
        "-S",
        "-B",
        "-c",
        built.runner_argv[5],
    )
    assert built.runner_argv[-5:] == (
        "--",
        "--receipt",
        str(built.receipt_path),
        "--receipt-sha256",
        built.receipt_sha256,
    )
    assert manifest["git"] == receipt["git"] == {
        "executable_path": str(authority_fixture.git),
        "executable_sha256": authority_fixture.git_sha256,
    }
    assert manifest["authority_programs"] == receipt["authority_programs"]
    assert receipt["held_loader_sha256"] == manifest["held_loader"]["sha256"]
    assert receipt["benchmark_argv"] == list(built.benchmark_argv)
    assert receipt["argv_is_shell_string"] is False
    assert receipt["invoked"] is False
    assert receipt["benchmark_completed"] is False
    assert receipt["benchmark_report"] is None
    assert receipt["posture"] == {
        "benchmark_output_written": False,
        "broker_contacted": False,
        "cutover_performed": False,
        "database_accessed": False,
        "host_activation_performed": False,
        "live_cash_authorized": False,
        "orders_submitted": False,
        "provider_contacted": False,
        "task_scheduler_mutated": False,
    }
    assert manifest["posture"] == {
        "benchmark_execution_authorized": True,
        "broker_contact_authorized": False,
        "database_access_authorized": False,
        "host_activation_authorized": False,
        "live_cash_authorized": False,
        "order_submission_authorized": False,
        "provider_contact_authorized": False,
    }
    assert manifest["python"]["version"] == [3, 11]
    assert manifest["python"]["isolation_flags"] == ["-I", "-S", "-B"]
    assert {
        row["name"]
        for row in manifest["python_dependency_root"]["required_distributions"]
    } == {"psutil", "zstandard"}
    assert manifest["output"]["storage_volume_identity_sha256"] == receipt[
        "output"
    ]["storage_volume_identity_sha256"]
    assert not tuple(authority_fixture.output.iterdir())

    argv = list(built.benchmark_argv)
    assert argv[:6] == [
        str(authority_fixture.python),
        "-I",
        "-S",
        "-B",
        "-c",
        argv[5],
    ]
    assert _sha(argv[5].encode("utf-8")) == receipt["held_loader_sha256"]
    boundary = argv.index("--")
    bootstrap = argv[6:boundary]
    assert len(bootstrap) == len(builder.LOADER_ROLE_ORDER) * 2 + 5
    for index, role in enumerate(builder.LOADER_ROLE_ORDER):
        assert bootstrap[index * 2] == str(
            authority_fixture.candidate / builder.SOURCE_ROLE_PATHS[role]
        )
        assert bootstrap[index * 2 + 1] == authority_fixture.source_hashes[role]
    assert bootstrap[-5:] == [
        str(authority_fixture.dependency),
        authority_fixture.dependency_identity_sha256,
        str(built.manifest_path),
        built.manifest_sha256,
        authority_fixture.python_sha256,
    ]
    assert argv[boundary + 1 :] == manifest["benchmark_arguments"]


@pytest.mark.parametrize("role", tuple(builder.LOADER_ROLE_ORDER))
def test_rejects_each_wrong_external_source_hash(
    authority_fixture: BenchmarkAuthorityFixture, role: str
) -> None:
    hashes = dict(authority_fixture.source_hashes)
    hashes[role] = "0" * 64 if hashes[role] != "0" * 64 else "1" * 64
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError,
        match="SOURCE_HASH_MISMATCH",
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs(expected_source_sha256=hashes)
        )
    assert not tuple(authority_fixture.authority.iterdir())


def test_rejects_missing_or_extra_source_roles(
    authority_fixture: BenchmarkAuthorityFixture,
) -> None:
    missing = dict(authority_fixture.source_hashes)
    missing.pop("runtime")
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="SOURCE_ROSTER_INVALID"
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs(expected_source_sha256=missing)
        )
    extra = dict(authority_fixture.source_hashes)
    extra["other"] = "a" * 64
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="SOURCE_ROSTER_INVALID"
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs(expected_source_sha256=extra)
        )


def test_rejects_wrong_commit_and_dirty_worktree(
    authority_fixture: BenchmarkAuthorityFixture,
) -> None:
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="GIT_HEAD_MISMATCH"
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs(expected_git_commit="f" * 40)
        )
    pressure_path = (
        authority_fixture.candidate / builder.SOURCE_ROLE_PATHS["pressure_probe"]
    )
    pressure_path.write_bytes(pressure_path.read_bytes() + b"\n# dirty\n")
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="SOURCE_HASH_MISMATCH"
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs()
        )


def test_rejects_dirty_unrelated_worktree_before_publication(
    authority_fixture: BenchmarkAuthorityFixture,
) -> None:
    (authority_fixture.candidate / "untracked.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="WORKTREE_DIRTY"
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs()
        )
    assert not tuple(authority_fixture.authority.iterdir())


def test_final_drift_leaves_only_inert_manifest_and_no_launch_receipt(
    authority_fixture: BenchmarkAuthorityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = builder._verify_git_worktree
    calls = 0

    def fail_final_recheck(**kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        original(**kwargs)
        if calls == 2:
            raise builder.CapturedPaperBenchmarkAuthorityError(
                "TEST_FINAL_DRIFT", "simulated final Git drift"
            )

    monkeypatch.setattr(builder, "_verify_git_worktree", fail_final_recheck)
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="TEST_FINAL_DRIFT"
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs()
        )

    assert calls == 2
    manifests = tuple(
        (authority_fixture.authority / "authority" / "manifest").rglob("*.json")
    )
    receipts_root = authority_fixture.authority / "authority" / "receipt"
    assert len(manifests) == 1
    assert not receipts_root.exists() or not tuple(receipts_root.rglob("*.json"))


def test_terminal_drift_never_publishes_runner_authority(
    authority_fixture: BenchmarkAuthorityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = builder._verify_git_worktree
    calls = 0

    def fail_terminal_recheck(**kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        original(**kwargs)
        if calls == 3:
            raise builder.CapturedPaperBenchmarkAuthorityError(
                "TEST_TERMINAL_DRIFT", "simulated terminal Git drift"
            )

    monkeypatch.setattr(builder, "_verify_git_worktree", fail_terminal_recheck)
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="TEST_TERMINAL_DRIFT"
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs()
        )

    root = authority_fixture.authority / "authority"
    assert len(tuple((root / "manifest").rglob("*.json"))) == 1
    assert len(tuple((root / "receipt").rglob("*.json"))) == 1
    assert not (root / "runner-authority").exists()


@pytest.mark.skipif(
    sys.version_info[:2] != (3, 11), reason="held authority loader requires CPython 3.11"
)
def test_held_runner_loader_authenticates_builder_runner_and_python(
    authority_fixture: BenchmarkAuthorityFixture,
) -> None:
    built = builder.build_captured_paper_benchmark_authority(
        **authority_fixture.kwargs()
    )
    boundary = built.runner_argv.index("--")
    completed = subprocess.run(
        [*built.runner_argv[: boundary + 1], "--help"],
        cwd=authority_fixture.candidate,
        env=builder._sanitized_execution_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        shell=False,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert b"--receipt-sha256" in completed.stdout


def test_extracts_loader_without_executing_pressure_probe_top_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _make_fixture(tmp_path)
    pressure_path = fixture.candidate / builder.SOURCE_ROLE_PATHS["pressure_probe"]
    marker = tmp_path / "candidate-code-executed"
    pressure_path.write_text(
        pressure_path.read_text(encoding="utf-8")
        + f"\nPath({str(marker)!r}).write_text('bad', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _git(fixture.candidate, "add", "--all")
    _git(fixture.candidate, "commit", "-m", "top-level trap")
    fixture.commit = _git(fixture.candidate, "rev-parse", "HEAD").stdout.strip()
    fixture.source_hashes["pressure_probe"] = _sha(pressure_path.read_bytes())
    monkeypatch.setattr(builder, "_probe_python311", lambda _path: None)

    builder.build_captured_paper_benchmark_authority(**fixture.kwargs())

    assert not marker.exists()


def test_rejects_dependency_and_python_hash_drift(
    authority_fixture: BenchmarkAuthorityFixture,
) -> None:
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError,
        match="DEPENDENCY_IDENTITY_MISMATCH",
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs(
                python_dependency_root_identity_sha256="b" * 64
            )
        )
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="PYTHON_HASH_MISMATCH"
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs(python_executable_sha256="c" * 64)
        )


def test_rejects_wrong_externally_supplied_git_hash_before_publication(
    authority_fixture: BenchmarkAuthorityFixture,
) -> None:
    with pytest.raises(builder.CapturedPaperBenchmarkAuthorityError, match="GIT_HASH_MISMATCH"):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs(git_executable_sha256="0" * 64)
        )
    assert not tuple(authority_fixture.authority.iterdir())


def test_path_shadow_git_is_never_selected(
    authority_fixture: BenchmarkAuthorityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shadow = authority_fixture.authority.parent / "shadow-bin"
    shadow.mkdir()
    marker = shadow / "invoked"
    if sys.platform == "win32":
        (shadow / "git.cmd").write_text(
            f"@echo off\r\ncopy nul \"{marker}\" >nul\r\nexit /b 97\r\n",
            encoding="utf-8",
        )
    else:
        fake = shadow / "git"
        fake.write_text(f"#!/bin/sh\n: > {marker!s}\nexit 97\n", encoding="utf-8")
        fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(shadow))
    built = builder.build_captured_paper_benchmark_authority(
        **authority_fixture.kwargs()
    )
    assert built.runner_authority_path.is_file()
    assert not marker.exists()


def test_bare_builder_cli_is_rejected_before_argument_processing() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO / builder.AUTHORITY_PROGRAM_PATHS["builder"]), "--help"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        shell=False,
        check=False,
    )
    assert result.returncode != 0
    assert "BUILDER_HELD_BOOTSTRAP_REQUIRED" in result.stderr


def test_rechecks_python_after_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _make_fixture(tmp_path)
    copied_python = tmp_path / "python.exe"
    shutil.copy2(fixture.python, copied_python)
    copied_python = copied_python.resolve(strict=True)
    copied_sha = _sha(copied_python.read_bytes())
    identity, _tree = builder._dependency_identity(
        root=fixture.dependency,
        python_executable=copied_python,
        python_executable_sha256=copied_sha,
    )
    dependency_sha = _sha(builder._canonical_json_bytes(dict(identity)))

    def mutate_python(path: Path) -> None:
        path.write_bytes(path.read_bytes() + b"drift")

    monkeypatch.setattr(builder, "_probe_python311", mutate_python)
    with pytest.raises(builder.CapturedPaperBenchmarkAuthorityError, match="FILE_DRIFT"):
        builder.build_captured_paper_benchmark_authority(
            **fixture.kwargs(
                python_executable=copied_python,
                python_executable_sha256=copied_sha,
                python_dependency_root_identity_sha256=dependency_sha,
            )
        )


def test_python_probe_requires_exact_cpython311_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="cpython:3:12:1:1:1:1\n", stderr=""
    )
    monkeypatch.setattr(builder.subprocess, "run", lambda *args, **kwargs: completed)
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="PYTHON_RUNTIME_INVALID"
    ):
        builder._probe_python311(Path(sys.executable).resolve(strict=True))


@pytest.mark.parametrize(
    "arguments,code",
    (
        (("--keep", "--output-root", "X:\\escape"), "OUTPUT_ARGUMENT_FORBIDDEN"),
        (("--keep", "--unknown", "1"), "BENCHMARK_ARGUMENT_UNKNOWN"),
        (("--events", "1000"), "BENCHMARK_RETENTION_REQUIRED"),
        (("--keep", "--keep"), "BENCHMARK_ARGUMENT_DUPLICATE"),
        (("--events", "999", "--keep"), "BENCHMARK_ARGUMENT_VALUE_INVALID"),
        (("--poll-ms", "nan", "--keep"), "BENCHMARK_ARGUMENT_VALUE_INVALID"),
        (("--compression-codec", "other", "--keep"), "BENCHMARK_ARGUMENT_VALUE_INVALID"),
        (("--help", "--keep"), "BENCHMARK_ARGUMENT_UNKNOWN"),
    ),
)
def test_rejects_unsafe_or_ambiguous_benchmark_argv(
    authority_fixture: BenchmarkAuthorityFixture,
    arguments: tuple[str, ...],
    code: str,
) -> None:
    with pytest.raises(builder.CapturedPaperBenchmarkAuthorityError, match=code):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs(benchmark_arguments=arguments)
        )
    assert not tuple(authority_fixture.authority.iterdir())


def test_rejects_nonempty_authority_output_and_broad_root(
    authority_fixture: BenchmarkAuthorityFixture,
) -> None:
    (authority_fixture.authority / "prior").write_text("x", encoding="utf-8")
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="AUTHORITY_ROOT_NOT_EMPTY"
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs()
        )
    (authority_fixture.authority / "prior").unlink()
    (authority_fixture.output / "prior").write_text("x", encoding="utf-8")
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="OUTPUT_ROOT_NOT_EMPTY"
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs()
        )
    with pytest.raises(
        builder.CapturedPaperBenchmarkAuthorityError, match="BROAD_ROOT_FORBIDDEN"
    ):
        builder.build_captured_paper_benchmark_authority(
            **authority_fixture.kwargs(benchmark_output_root=Path.cwd().anchor)
        )


def test_builder_runs_only_sanitized_git_and_no_benchmark_or_provider_process(
    authority_fixture: BenchmarkAuthorityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = builder.subprocess.run
    observed: list[tuple[str, ...]] = []

    def recording_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        argv = tuple(map(str, args[0]))
        observed.append(argv)
        return original(*args, **kwargs)

    monkeypatch.setattr(builder.subprocess, "run", recording_run)
    builder.build_captured_paper_benchmark_authority(
        **authority_fixture.kwargs()
    )

    assert observed
    assert all(Path(argv[0]).name.casefold() in {"git", "git.exe"} for argv in observed)
    assert all("benchmark_replay_capture_runtime.py" not in argv for argv in observed)
    assert all("alpaca" not in " ".join(argv).casefold() for argv in observed)
    assert all("iqfeed" not in " ".join(argv).casefold() for argv in observed)
