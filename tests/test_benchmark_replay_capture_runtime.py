from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts import alpaca_paper_operational_preflight as preflight
from scripts import captured_paper_isolated_stage0 as stage0
from scripts import captured_paper_pressure_probe as pressure_probe


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "benchmark_replay_capture_runtime.py"
)
SCRIPT_SHA256 = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
REPO = SCRIPT.parents[1]
SOURCE_PATHS = {
    "benchmark": SCRIPT,
    "contract": REPO / "app/services/trading/momentum_neural/replay_capture_contract.py",
    "runtime": REPO / "app/services/trading/momentum_neural/replay_capture_runtime.py",
    "pressure_probe": REPO / "scripts/captured_paper_pressure_probe.py",
    "replay_errors": REPO / "app/services/trading/momentum_neural/replay_errors.py",
    "first_dip_tape_policy": (
        REPO / "app/services/trading/momentum_neural/first_dip_tape_policy.py"
    ),
    "stage0": REPO / "scripts/captured_paper_isolated_stage0.py",
}


def _dependency_identity(root: Path) -> str:
    executable = Path(sys.executable).resolve(strict=True)
    return stage0.dependency_root_identity_sha256(
        dependency_root=root,
        python_executable=executable,
        python_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )


def _empty_dependency_root(tmp_path: Path) -> Path:
    root = tmp_path / "dependencies" / "empty" / "site-packages"
    root.mkdir(parents=True)
    return root


def _zstandard_dependency_root(tmp_path: Path) -> Path:
    root = tmp_path / "dependencies" / "zstandard" / "site-packages"
    root.mkdir(parents=True)
    source_root = Path(sys.base_prefix) / "Lib" / "site-packages"
    for name in ("zstandard", "zstandard-0.25.0.dist-info"):
        shutil.copytree(source_root / name, root / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    return root


def _mirrored_sources(tmp_path: Path) -> dict[str, Path]:
    candidate = tmp_path / "candidate"
    result: dict[str, Path] = {}
    for role, source in SOURCE_PATHS.items():
        target = candidate / source.relative_to(REPO)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        result[role] = target
    for relative in (
        Path("scripts/build_captured_paper_benchmark_authority.py"),
        Path("scripts/run_captured_paper_benchmark_authority.py"),
    ):
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO / relative).read_bytes())
    return result


def _held_benchmark_command(
    *arguments: str,
    dependency_root: Path,
    script: Path = SCRIPT,
    expected_sha256: str = SCRIPT_SHA256,
    expected_python_executable_sha256: str | None = None,
    source_paths: dict[str, Path] | None = None,
) -> list[str]:
    paths = dict(SOURCE_PATHS if source_paths is None else source_paths)
    paths["benchmark"] = script
    pairs: list[str] = []
    for role in (
        "benchmark",
        "contract",
        "runtime",
        "pressure_probe",
        "replay_errors",
        "first_dip_tape_policy",
        "stage0",
    ):
        path = paths[role].resolve(strict=True)
        digest = (
            expected_sha256
            if role == "benchmark"
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        pairs.extend((str(path), digest))
    output_index = arguments.index("--output-root")
    output_root = Path(arguments[output_index + 1]).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    output_stat = output_root.stat()
    output_identity = {
        "st_dev": int(output_stat.st_dev),
        "st_ino": int(output_stat.st_ino),
        "st_mode": int(output_stat.st_mode),
        "st_mtime_ns": int(output_stat.st_mtime_ns),
    }
    volume_identity = {
        "normalized_anchor": os.path.normcase(
            os.path.normpath(output_root.anchor or os.sep)
        ),
        "schema_version": "chili.capture-storage-volume-identity.v1",
        "st_dev": int(output_stat.st_dev),
    }
    source_roster = [
        {
            "path": str(paths[role].resolve(strict=True)),
            "role": role,
            "sha256": (
                expected_sha256
                if role == "benchmark"
                else hashlib.sha256(paths[role].read_bytes()).hexdigest()
            ),
        }
        for role in sorted(paths)
    ]
    dependency_identity = _dependency_identity(dependency_root)
    executable = Path(sys.executable).resolve(strict=True)
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    candidate_root = paths["benchmark"].resolve(strict=True).parent.parent
    git_raw = shutil.which("git")
    assert git_raw is not None
    git_executable = Path(git_raw).resolve(strict=True)
    builder_source = candidate_root / "scripts/build_captured_paper_benchmark_authority.py"
    runner_source = candidate_root / "scripts/run_captured_paper_benchmark_authority.py"
    builder_source.parent.mkdir(parents=True, exist_ok=True)
    if not builder_source.is_file():
        builder_source.write_bytes(
            (REPO / "scripts/build_captured_paper_benchmark_authority.py").read_bytes()
        )
    if not runner_source.is_file():
        runner_source.write_bytes(
            (REPO / "scripts/run_captured_paper_benchmark_authority.py").read_bytes()
        )
    tree = stage0._dependency_tree_inventory(
        dependency_root.resolve(strict=True), retain_mutation_guards=False
    )
    identity_body = stage0._dependency_root_identity_from_inventory(
        root=dependency_root.resolve(strict=True),
        executable=executable,
        python_executable_sha256=executable_sha256,
        tree=tree,
    )
    execution_environment = dict(os.environ)
    manifest = {
        "account_scope": "alpaca:paper",
        "authority_programs": {
            "builder": {
                "path": str(builder_source.resolve(strict=True)),
                "sha256": hashlib.sha256(builder_source.read_bytes()).hexdigest(),
            },
            "runner": {
                "path": str(runner_source.resolve(strict=True)),
                "sha256": hashlib.sha256(runner_source.read_bytes()).hexdigest(),
            },
        },
        "authority_mode": "diagnostic_capture_benchmark_only",
        "benchmark_arguments": list(arguments),
        "candidate_root": str(paths["benchmark"].resolve(strict=True).parent.parent),
        "expected_benchmark_schema_version": "chili.replay-capture-benchmark.v7",
        "expected_git_commit": "1" * 40,
        "execution_context": {
            "cwd": str(Path.cwd().resolve(strict=True)),
            "environment": execution_environment,
            "environment_sha256": hashlib.sha256(
                json.dumps(
                    execution_environment,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "shell": False,
            "stderr": "bounded_binary_pipe_1mib",
            "stdin": "devnull",
            "stdout": "bounded_binary_pipe_64mib",
            "timeout_seconds": 3600,
        },
        "git": {
            "executable_path": str(git_executable),
            "executable_sha256": hashlib.sha256(git_executable.read_bytes()).hexdigest(),
        },
        "held_loader": {
            "sha256": hashlib.sha256(
                pressure_probe._HELD_BENCHMARK_SOURCE_LOADER.encode("utf-8")
            ).hexdigest(),
            "source_role": "pressure_probe",
            "variable": "_HELD_BENCHMARK_SOURCE_LOADER",
        },
        "output": {
            "root": str(output_root),
            "root_identity": output_identity,
            "storage_volume_identity": volume_identity,
            "storage_volume_identity_sha256": hashlib.sha256(
                json.dumps(
                    volume_identity, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
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
            "executable_path": str(executable),
            "executable_sha256": executable_sha256,
            "implementation": "cpython",
            "isolation_flags": ["-I", "-S", "-B"],
            "version": [3, 11],
        },
        "python_dependency_root": {
            "identity": dict(identity_body),
            "identity_sha256": dependency_identity,
            "path": str(dependency_root.resolve(strict=True)),
            "required_distributions": [],
            "tree_sha256": tree["tree_sha256"],
        },
        "schema_version": "chili.captured-paper-benchmark-authority-manifest.v2",
        "source_roster": source_roster,
        "source_roster_sha256": hashlib.sha256(
            json.dumps(source_roster, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest(),
    }
    manifest_raw = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    manifest_path = (
        dependency_root.parent
        / "benchmark-test-authority"
        / "authority"
        / "manifest"
        / manifest_sha256[:2]
        / f"{manifest_sha256}.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_raw)
    return [
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-c",
        pressure_probe._HELD_BENCHMARK_SOURCE_LOADER,
        *pairs,
        str(dependency_root.resolve(strict=True)),
        dependency_identity,
        str(manifest_path.resolve(strict=True)),
        manifest_sha256,
        (
            expected_python_executable_sha256
            or hashlib.sha256(
                Path(sys.executable).resolve(strict=True).read_bytes()
            ).hexdigest()
        ),
        "--",
        *arguments,
    ]


def test_benchmark_measures_the_same_canonical_bytes_used_by_admission(
    tmp_path: Path,
) -> None:
    """The resolved throughput budget must use CaptureEvent canonical bytes.

    Stored raw material is not equivalent: payload deduplication and payload
    references can change its size, while ingress admission always charges
    ``CaptureEvent.canonical_size_bytes``.
    """

    completed = subprocess.run(
        _held_benchmark_command(
            "--output-root",
            str(tmp_path / "output"),
            "--events",
            "1000",
            "--symbols",
            "8",
            "--queue-events",
            "5000",
            "--queue-mib",
            "32",
            "--batch-events",
            "500",
            "--batch-mib",
            "4",
            "--compression-codec",
            "zlib",
            "--compression-level",
            "3",
            dependency_root=_empty_dependency_root(tmp_path / "dependency"),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    canonical_bytes = int(report["enqueue"]["accepted_canonical_bytes"])
    writer_seconds = float(report["writer"]["wall_seconds"])

    assert report["enqueue"]["accepted"] == 1000
    assert report["benchmark_schema_version"] == (
        "chili.replay-capture-benchmark.v7"
    )
    assert report["acceptance"] == {"accepted": True, "reasons": []}
    assert report["authority"] == {
        "capacity_authority": "diagnostic_only",
        "empirical_calibration_receipt_sha256": None,
        "hot_symbol_limit_authorized": False,
        "reasons": [
            "empirical_hot_symbol_calibration_receipt_unavailable",
            "full_runner_watcher_resource_calibration_unavailable",
            "writer_scaling_calibration_unavailable",
        ],
        "watcher_limit_authorized": False,
        "writer_limit_authorized": False,
    }
    assert report["measurement_window"]["event_count"] == 1000
    assert report["measurement_window"]["sample_seconds"] > 0
    assert report["artifact_freshness"]["fresh_at_emit"] is True
    assert report["environment"]["host_fingerprint_matches"] is True
    assert len(report["capture_runtime_source"]["pressure_probe_sha256"]) == 64
    assert report["writer"]["health"]["events_written"] == 1000
    assert canonical_bytes > 0
    assert report["resource_measurement"][
        "sustained_append_bytes_per_second"
    ] == pytest.approx(canonical_bytes / writer_seconds, rel=1e-6)
    assert report["resource_measurement"]["logical_cpu_count"] == report[
        "environment"
    ]["logical_cpu_count"]
    assert int(report["storage"]["compression"]["raw_bytes"]) != canonical_bytes
    assert report["storage"]["policy"]["payload_layout"] == (
        "content_addressed_pack_v1"
    )
    assert len(report["storage"]["policy"]["policy_sha256"]) == 64
    assert report["storage"]["resource_enforcement"] == {
        "calibration_mode": True,
        "enforced": False,
        "fail_closed": False,
        "failure_reasons": [],
        "resource_hashes": None,
    }
    assert report["storage"]["files"]["payload_packs"] > 0
    assert report["storage"]["files"]["standalone_payload_blobs"] == 0
    assert report["storage"]["files"]["logical_payload_records"] > (
        report["storage"]["files"]["payload_packs"]
    )
    assert report["storage"]["object_amplification"][
        "physical_capture_objects_per_1000_events"
    ] < 25
    assert report["writer"]["health"]["resource"]["sync"]["failures"] == 0
    assert report["writer"]["health"]["resource"]["sync"]["dirty_objects"] == 0
    durable = report["resource_measurement"]["durable_publication"]
    assert durable["sample_count"] >= 2
    assert durable["verified_count"] == durable["sample_count"]
    assert durable["all_verified"] is True
    assert durable["live_pressure_probe"]["count"] == durable["sample_count"]
    assert durable["live_pressure_probe"]["all_verified"] is True
    assert durable["live_pressure_probe"]["bytes_per_sample"] == 4096
    assert durable["live_pressure_probe"]["write_latency_profile"] == (
        "chili.capture-pressure.durable-write-fsync-helper-process.v1"
    )
    assert durable["live_pressure_probe"]["helper_sha256"] == report[
        "capture_runtime_source"
    ]["pressure_probe_sha256"]
    assert durable["file_fsync"]["count"] == durable["sample_count"]
    assert durable["parent_publication"]["count"] == durable["sample_count"]
    assert durable["parent_publication"]["methods"]
    binding = report["resolved_resource_binding"]
    assert binding["binding_sha256"] == binding["hashes"]["binding_sha256"]
    assert binding["max_writer_threads"] >= 2
    shared = report["shared_store_validation"]
    assert shared["executed"] is True
    assert shared["identity_count"] >= 2
    assert len(set(shared["identity_sha256s"])) == shared["identity_count"]
    assert shared["writers_stopped_cleanly"] is True
    assert shared["survivor_store_access_after_first_release"] is True
    assert shared["manager_before_release"]["lease_count"] >= 2
    assert shared["manager_after_first_release"]["lease_count"] == (
        shared["manager_before_release"]["lease_count"] - 1
    )
    assert shared["manager_before_close"]["lease_count"] == 0
    assert shared["manager_before_close"]["claimed_writer_ingresses"] == 0
    aggregate = shared["aggregate_admission"]
    assert aggregate["completed"] == shared["accepted_events"]
    assert aggregate["outstanding_events"] == 0
    assert aggregate["outstanding_bytes"] == 0
    assert aggregate["rejections"] == {}
    assert report["output"]["retained"] is False
    assert not tuple((tmp_path / "output").iterdir())


def test_retained_report_is_content_addressed_and_stdout_identical(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        _held_benchmark_command(
            "--output-root",
            str(tmp_path / "output"),
            "--events",
            "1000",
            "--queue-events",
            "5000",
            "--queue-mib",
            "32",
            "--batch-events",
            "500",
            "--batch-mib",
            "4",
            "--compression-codec",
            "zlib",
            "--keep",
            dependency_root=_empty_dependency_root(tmp_path / "dependency"),
        ),
        check=True,
        capture_output=True,
        text=False,
    )
    stdout_raw = completed.stdout.removesuffix(b"\n")
    report = json.loads(stdout_raw.decode("utf-8"))
    owned = Path(report["output"]["directory"])
    digest = hashlib.sha256(stdout_raw).hexdigest()
    persisted = owned / "reports" / f"{digest}.json"

    assert report["output"]["retained"] is True
    assert persisted.read_bytes() == stdout_raw
    assert [path.name for path in (owned / "reports").iterdir()] == [
        f"{digest}.json"
    ]
    summary, measurement = preflight.validate_capture_benchmark(
        persisted,
        repo_root=SCRIPT.parents[1],
        expected_artifact_sha256=digest,
        evaluated_at=datetime.now(timezone.utc),
    )
    assert summary["artifact_sha256"] == digest
    assert summary["capacity_authority"] == "diagnostic_only"
    assert summary["capacity_limits_authorized"] is False
    assert summary["ready"] is False
    assert measurement["logical_cpu_count"] == report["resource_measurement"][
        "logical_cpu_count"
    ]


def test_default_zstd_benchmark_uses_sealed_dependency_capsule(
    tmp_path: Path,
) -> None:
    dependency_root = _zstandard_dependency_root(tmp_path)
    completed = subprocess.run(
        _held_benchmark_command(
            "--output-root",
            str(tmp_path / "output"),
            "--events",
            "1000",
            "--queue-events",
            "5000",
            "--queue-mib",
            "32",
            "--batch-events",
            "500",
            "--batch-mib",
            "4",
            dependency_root=dependency_root,
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["parameters"]["compression_codec"] == "zstd"
    assert report["environment"]["dependency_root_identity_sha256"] == (
        _dependency_identity(dependency_root)
    )


def test_default_zstd_benchmark_rejects_empty_dependency_capsule(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        _held_benchmark_command(
            "--output-root",
            str(tmp_path / "output"),
            "--events",
            "1000",
            dependency_root=_empty_dependency_root(tmp_path / "dependency"),
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "zstandard dependency is unavailable" in completed.stderr


def test_direct_benchmark_script_execution_is_rejected_before_output(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output-root",
            str(tmp_path / "output"),
            "--events",
            "1000",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "approved held-source bundle loader" in completed.stderr
    assert not (tmp_path / "output").exists()


def test_held_benchmark_rejects_missing_runtime_isolation_flags(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [value for value in _held_benchmark_command(
            "--output-root",
            str(tmp_path / "output"),
            "--events",
            "1000",
            dependency_root=_empty_dependency_root(tmp_path / "dependency"),
        ) if value != "-I"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.returncode == 90
    assert (tmp_path / "output").is_dir()
    assert not tuple((tmp_path / "output").iterdir())


def test_held_benchmark_rejects_wrong_python_executable_authority(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        _held_benchmark_command(
            "--output-root",
            str(tmp_path / "output"),
            "--events",
            "1000",
            dependency_root=_empty_dependency_root(tmp_path / "dependency"),
            expected_python_executable_sha256="0" * 64,
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 111
    assert completed.stdout == ""
    assert (tmp_path / "output").is_dir()
    assert not tuple((tmp_path / "output").iterdir())


def test_held_loader_rejects_drifted_benchmark_before_restore_code_executes(
    tmp_path: Path,
) -> None:
    original = SCRIPT.read_bytes()
    backup = tmp_path / "sealed-benchmark.py"
    backup.write_bytes(original)
    drifted = tmp_path / "benchmark.py"
    marker = tmp_path / "untrusted-code-executed"
    prelude = (
        "from pathlib import Path\n"
        f"Path({str(drifted)!r}).write_bytes(Path({str(backup)!r}).read_bytes())\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
    ).encode("utf-8")
    drifted.write_bytes(prelude + original)

    completed = subprocess.run(
        _held_benchmark_command(
            "--output-root",
            str(tmp_path / "output"),
            "--events",
            "1000",
            script=drifted,
            expected_sha256=hashlib.sha256(original).hexdigest(),
            dependency_root=_empty_dependency_root(tmp_path / "dependency"),
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 98
    assert marker.exists() is False
    assert drifted.read_bytes() == prelude + original
    assert (tmp_path / "output").is_dir()
    assert not tuple((tmp_path / "output").iterdir())


def test_benchmark_rechecks_held_sources_immediately_before_emit(
    tmp_path: Path,
) -> None:
    paths = _mirrored_sources(tmp_path)
    mutated = paths["benchmark"]
    source = mutated.read_text(encoding="utf-8")
    needle = "        emitted_source_hashes = _verified_executed_source_hashes()\n"
    marker = tmp_path / "post-run-source-drift"
    replacement = (
        f"        Path({str(mutated)!r}).write_bytes(b'post-run-drift')\n"
        f"        Path({str(marker)!r}).write_text('drifted')\n"
        + needle
    )
    mutated.write_text(source.replace(needle, replacement, 1), encoding="utf-8")
    completed = subprocess.run(
        _held_benchmark_command(
            "--output-root",
            str(tmp_path / "output"),
            "--events",
            "1000",
            "--compression-codec",
            "zlib",
            script=mutated,
            expected_sha256=hashlib.sha256(mutated.read_bytes()).hexdigest(),
            dependency_root=_empty_dependency_root(tmp_path / "dependency"),
            source_paths=paths,
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert marker.is_file()
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "source bytes drifted" in completed.stderr
