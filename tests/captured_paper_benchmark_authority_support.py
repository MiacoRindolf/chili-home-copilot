from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from scripts import captured_paper_activation_runner as activation_runner
from scripts import captured_paper_isolated_stage0 as stage0


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def file_sha(path: Path) -> str:
    return sha(path.read_bytes())


def publish(root: Path, kind: str, document: Mapping[str, Any]) -> tuple[Path, str]:
    raw = canonical(document)
    digest = sha(raw)
    path = root / "authority" / kind / digest[:2] / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path.resolve(strict=True), digest


@dataclass(frozen=True, slots=True)
class SyntheticBenchmarkAuthority:
    python_dependency_root_identity_sha256: str
    resource_benchmark_path: Path
    resource_benchmark_sha256: str
    benchmark_authority_manifest_path: Path
    benchmark_authority_manifest_sha256: str
    benchmark_runner_authority_path: Path
    benchmark_runner_authority_sha256: str
    benchmark_execution_receipt_path: Path
    benchmark_execution_receipt_sha256: str

    def references(self) -> dict[str, dict[str, str]]:
        return {
            "resource_benchmark": {
                "path": str(self.resource_benchmark_path),
                "sha256": self.resource_benchmark_sha256,
            },
            "benchmark_authority_manifest": {
                "path": str(self.benchmark_authority_manifest_path),
                "sha256": self.benchmark_authority_manifest_sha256,
            },
            "benchmark_runner_authority": {
                "path": str(self.benchmark_runner_authority_path),
                "sha256": self.benchmark_runner_authority_sha256,
            },
            "benchmark_execution_receipt": {
                "path": str(self.benchmark_execution_receipt_path),
                "sha256": self.benchmark_execution_receipt_sha256,
            },
        }


def build_test_benchmark_authority(
    *,
    root: Path,
    candidate_root: Path,
    expected_git_commit: str,
    git_executable: Path,
    python_executable: Path,
    python_dependency_root: Path,
) -> SyntheticBenchmarkAuthority:
    authority_root = (root / "benchmark-authority").resolve()
    output_root = (root / "benchmark-output").resolve()
    authority_root.mkdir(parents=True)
    output_root.mkdir(parents=True)
    candidate = candidate_root.resolve(strict=True)
    git = git_executable.resolve(strict=True)
    python = python_executable.resolve(strict=True)
    dependency = python_dependency_root.resolve(strict=True)
    python_sha = file_sha(python)
    git_sha = file_sha(git)
    dependency_identity = dict(stage0.dependency_root_identity(
        dependency_root=dependency,
        python_executable=python,
        python_executable_sha256=python_sha,
    ))
    dependency_sha = sha(canonical(dependency_identity))
    source_rows: list[dict[str, str]] = []
    source_hashes: dict[str, str] = {}
    for role, relative in sorted(activation_runner._BENCHMARK_SOURCE_PATHS.items()):
        path = (candidate / relative).resolve(strict=True)
        digest = file_sha(path)
        source_rows.append({"path": str(path), "role": role, "sha256": digest})
        source_hashes[role] = digest
    programs: dict[str, dict[str, str]] = {}
    for role, relative in activation_runner._BENCHMARK_AUTHORITY_PROGRAM_PATHS.items():
        path = (candidate / relative).resolve(strict=True)
        programs[role] = {"path": str(path), "sha256": file_sha(path)}
    pressure_source = Path(
        next(row["path"] for row in source_rows if row["role"] == "pressure_probe")
    ).read_bytes()
    held_loader = activation_runner._extract_literal_string(
        pressure_source,
        variable=activation_runner._BENCHMARK_HELD_LOADER_VARIABLE,
        field="test_pressure_probe",
    )
    output_status = output_root.stat()
    root_identity = {
        "st_dev": int(output_status.st_dev),
        "st_ino": int(output_status.st_ino),
        "st_mode": int(output_status.st_mode),
        "st_mtime_ns": int(output_status.st_mtime_ns),
    }
    volume = {
        "normalized_anchor": os.path.normcase(
            os.path.normpath(output_root.anchor or os.sep)
        ),
        "schema_version": "chili.capture-storage-volume-identity.v1",
        "st_dev": int(output_status.st_dev),
    }
    volume_sha = sha(canonical(volume))
    environment: dict[str, str] = {}
    execution_context = {
        "cwd": str(candidate),
        "environment": environment,
        "environment_sha256": sha(canonical(environment)),
        "shell": False,
        "stderr": "bounded_binary_pipe_1mib",
        "stdin": "devnull",
        "stdout": "bounded_binary_pipe_64mib",
        "timeout_seconds": 60,
    }
    benchmark_arguments = ["--output-root", str(output_root)]
    false_posture = dict(activation_runner._BENCHMARK_FALSE_POSTURE)
    manifest = {
        "account_scope": activation_runner.ACCOUNT_SCOPE,
        "authority_mode": activation_runner._BENCHMARK_AUTHORITY_MODE,
        "benchmark_arguments": benchmark_arguments,
        "candidate_root": str(candidate),
        "expected_benchmark_schema_version": (
            activation_runner.BENCHMARK_REPORT_SCHEMA_VERSION
        ),
        "expected_git_commit": expected_git_commit,
        "execution_context": execution_context,
        "git": {"executable_path": str(git), "executable_sha256": git_sha},
        "held_loader": {
            "sha256": sha(held_loader.encode("utf-8")),
            "source_role": "pressure_probe",
            "variable": activation_runner._BENCHMARK_HELD_LOADER_VARIABLE,
        },
        "authority_programs": programs,
        "output": {
            "root": str(output_root),
            "root_identity": root_identity,
            "storage_volume_identity": volume,
            "storage_volume_identity_sha256": volume_sha,
        },
        "posture": {
            "benchmark_execution_authorized": True,
            "broker_contact_authorized": False,
            "database_access_authorized": False,
            "host_activation_authorized": False,
            "live_cash_authorized": False,
            "order_submission_authorized": False,
            "provider_contact_authorized": False,
        },
        "python": {
            "executable_path": str(python),
            "executable_sha256": python_sha,
            "implementation": "cpython",
            "isolation_flags": ["-I", "-S", "-B"],
            "version": [3, 11],
        },
        "python_dependency_root": {
            "identity": dependency_identity,
            "identity_sha256": dependency_sha,
            "path": str(dependency),
            "required_distributions": [],
            "tree_sha256": dependency_identity["tree_sha256"],
        },
        "schema_version": activation_runner.BENCHMARK_MANIFEST_SCHEMA_VERSION,
        "source_roster": source_rows,
        "source_roster_sha256": sha(canonical(source_rows)),
    }
    manifest_path, manifest_sha = publish(authority_root, "manifest", manifest)
    manifest_ref = {"path": str(manifest_path), "sha256": manifest_sha}

    owner_token = "a" * 32
    owned = output_root / f"chili-replay-capture-benchmark-{owner_token}-test"
    owned.mkdir()
    marker = {
        "benchmark_schema_version": activation_runner.BENCHMARK_REPORT_SCHEMA_VERSION,
        "directory": str(owned),
        "output_root": str(output_root),
        "owner_token": owner_token,
    }
    (owned / ".chili-replay-capture-benchmark-owner.json").write_bytes(
        canonical(marker) + b"\n"
    )
    expected_report_sources = {
        "benchmark_script_sha256": source_hashes["benchmark"],
        "contract_sha256": source_hashes["contract"],
        "first_dip_tape_policy_sha256": source_hashes["first_dip_tape_policy"],
        "pressure_probe_sha256": source_hashes["pressure_probe"],
        "replay_errors_sha256": source_hashes["replay_errors"],
        "runtime_sha256": source_hashes["runtime"],
        "stage0_sha256": source_hashes["stage0"],
    }
    report = {
        "acceptance": {"accepted": True, "reasons": []},
        "benchmark_schema_version": activation_runner.BENCHMARK_REPORT_SCHEMA_VERSION,
        "capture_runtime_source": expected_report_sources,
        "environment": {
            "benchmark_authority_manifest_sha256": manifest_sha,
            "dependency_root_identity_sha256": dependency_sha,
            "python_executable_sha256": python_sha,
        },
        "output": {
            "directory": str(owned),
            "report_artifact_layout": "reports/<canonical-sha256>.json_when_retained",
            "retained": True,
            "safe_cleanup_verified": False,
        },
    }
    report_raw = canonical(report)
    report_sha = sha(report_raw)
    report_path = owned / "reports" / f"{report_sha}.json"
    report_path.parent.mkdir()
    report_path.write_bytes(report_raw)
    report_path = report_path.resolve(strict=True)
    report_ref = {"path": str(report_path), "sha256": report_sha}

    benchmark_bootstrap: list[str] = []
    for role in activation_runner._BENCHMARK_LOADER_ROLE_ORDER:
        row = next(row for row in source_rows if row["role"] == role)
        benchmark_bootstrap.extend((row["path"], row["sha256"]))
    benchmark_argv = [
        str(python), "-I", "-S", "-B", "-c", held_loader,
        *benchmark_bootstrap,
        str(dependency), dependency_sha, str(manifest_path), manifest_sha,
        python_sha, "--", *benchmark_arguments,
    ]
    launch = {
        "account_scope": activation_runner.ACCOUNT_SCOPE,
        "authority_programs": programs,
        "argv_is_shell_string": False,
        "authority_mode": activation_runner._BENCHMARK_AUTHORITY_MODE,
        "benchmark_argv": benchmark_argv,
        "benchmark_completed": False,
        "benchmark_report": None,
        "candidate_root": str(candidate),
        "execution_context": execution_context,
        "expected_git_commit": expected_git_commit,
        "git": manifest["git"],
        "held_loader_sha256": manifest["held_loader"]["sha256"],
        "invoked": False,
        "manifest": manifest_ref,
        "output": {
            "root": str(output_root),
            "root_identity": root_identity,
            "storage_volume_identity_sha256": volume_sha,
        },
        "posture": false_posture,
        "python": {
            "executable_path": str(python),
            "executable_sha256": python_sha,
        },
        "python_dependency_root": {
            "identity_sha256": dependency_sha,
            "path": str(dependency),
            "tree_sha256": dependency_identity["tree_sha256"],
        },
        "schema_version": activation_runner.BENCHMARK_LAUNCH_RECEIPT_SCHEMA_VERSION,
        "source_roster": source_rows,
        "source_roster_sha256": manifest["source_roster_sha256"],
    }
    launch_path, launch_sha = publish(authority_root, "receipt", launch)
    launch_ref = {"path": str(launch_path), "sha256": launch_sha}

    runner_source = Path(programs["runner"]["path"]).read_bytes()
    runner_loader = activation_runner._extract_literal_string(
        runner_source,
        variable=activation_runner._BENCHMARK_RUNNER_LOADER_VARIABLE,
        field="test_benchmark_runner",
    )
    runner_argv = [
        str(python), "-I", "-S", "-B", "-c", runner_loader,
        programs["builder"]["path"], programs["builder"]["sha256"],
        programs["runner"]["path"], programs["runner"]["sha256"],
        python_sha, "--", "--receipt", str(launch_path),
        "--receipt-sha256", launch_sha,
    ]
    runner_authority = {
        "account_scope": activation_runner.ACCOUNT_SCOPE,
        "authority_mode": activation_runner._BENCHMARK_AUTHORITY_MODE,
        "argv_is_shell_string": False,
        "execution_context": execution_context,
        "git": manifest["git"],
        "launch_receipt": launch_ref,
        "manifest": manifest_ref,
        "posture": false_posture,
        "python": launch["python"],
        "runner_argv": runner_argv,
        "runner_loader_sha256": sha(runner_loader.encode("utf-8")),
        "schema_version": activation_runner.BENCHMARK_RUNNER_AUTHORITY_SCHEMA_VERSION,
    }
    runner_path, runner_sha = publish(
        authority_root, "runner-authority", runner_authority
    )

    started = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    completed = datetime(2026, 8, 13, 12, 0, 1, tzinfo=UTC)
    started_text = started.isoformat().replace("+00:00", "Z")
    claim = {
        "launch_receipt": launch_ref,
        "manifest": manifest_ref,
        "schema_version": activation_runner.BENCHMARK_EXECUTION_CLAIM_SCHEMA_VERSION,
        "started_at_utc": started_text,
    }
    claim_raw = canonical(claim)
    claim_sha = sha(claim_raw)
    claim_path = authority_root / "authority" / "execution-claim" / f"{launch_sha}.json"
    claim_path.parent.mkdir(parents=True)
    claim_path.write_bytes(claim_raw)
    claim_path = claim_path.resolve(strict=True)
    terminal = {
        "account_scope": activation_runner.ACCOUNT_SCOPE,
        "authority_programs": programs,
        "argv_is_shell_string": False,
        "authority_mode": activation_runner._BENCHMARK_AUTHORITY_MODE,
        "benchmark": {
            "acceptance": {"accepted": True, "reasons": []},
            "exit_code": 0,
            "report": report_ref,
            "schema_version": activation_runner.BENCHMARK_REPORT_SCHEMA_VERSION,
            "stderr_bytes": 0,
            "stdout_sha256": sha(report_raw + b"\n"),
            "stdout_without_newline_sha256": report_sha,
        },
        "benchmark_completed": True,
        "completed_at_utc": completed.isoformat().replace("+00:00", "Z"),
        "duration_seconds": 1.0,
        "execution_context": execution_context,
        "execution_claim": {"path": str(claim_path), "sha256": claim_sha},
        "expected_git_commit": expected_git_commit,
        "git": manifest["git"],
        "invoked": True,
        "launch_receipt": launch_ref,
        "manifest": manifest_ref,
        "posture": dict(activation_runner._BENCHMARK_TERMINAL_POSTURE),
        "schema_version": activation_runner.BENCHMARK_EXECUTION_RECEIPT_SCHEMA_VERSION,
        "started_at_utc": started_text,
    }
    terminal_path, terminal_sha = publish(
        authority_root, "execution-receipt", terminal
    )
    return SyntheticBenchmarkAuthority(
        python_dependency_root_identity_sha256=dependency_sha,
        resource_benchmark_path=report_path,
        resource_benchmark_sha256=report_sha,
        benchmark_authority_manifest_path=manifest_path,
        benchmark_authority_manifest_sha256=manifest_sha,
        benchmark_runner_authority_path=runner_path,
        benchmark_runner_authority_sha256=runner_sha,
        benchmark_execution_receipt_path=terminal_path,
        benchmark_execution_receipt_sha256=terminal_sha,
    )
