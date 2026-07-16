from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

from scripts import alpaca_paper_operational_preflight as preflight


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "benchmark_replay_capture_runtime.py"
)


def test_benchmark_measures_the_same_canonical_bytes_used_by_admission(
    tmp_path: Path,
) -> None:
    """The resolved throughput budget must use CaptureEvent canonical bytes.

    Stored raw material is not equivalent: payload deduplication and payload
    references can change its size, while ingress admission always charges
    ``CaptureEvent.canonical_size_bytes``.
    """

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output-root",
            str(tmp_path),
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
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    canonical_bytes = int(report["enqueue"]["accepted_canonical_bytes"])
    writer_seconds = float(report["writer"]["wall_seconds"])

    assert report["enqueue"]["accepted"] == 1000
    assert report["benchmark_schema_version"] == (
        "chili.replay-capture-benchmark.v4"
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
    assert not tuple(tmp_path.iterdir())


def test_retained_report_is_content_addressed_and_stdout_identical(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output-root",
            str(tmp_path),
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
        ],
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


def test_rejected_acceptance_is_persisted_and_returns_nonzero(
    tmp_path: Path,
    monkeypatch,
    capfd,
) -> None:
    namespace = runpy.run_path(str(SCRIPT), run_name="capture_benchmark_rejected_test")
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "_run_benchmark",
        lambda _args, _directory: {
            "acceptance": {"accepted": False, "reasons": ["synthetic_rejection"]},
            "benchmark_schema_version": namespace["BENCHMARK_SCHEMA_VERSION"],
        },
    )

    result = namespace["main"](
        ["--output-root", str(tmp_path), "--events", "1000", "--keep"]
    )
    raw = capfd.readouterr().out.encode("utf-8").removesuffix(b"\n")
    report = json.loads(raw.decode("utf-8"))
    digest = hashlib.sha256(raw).hexdigest()
    persisted = Path(report["output"]["directory"]) / "reports" / f"{digest}.json"

    assert result == 2
    assert report["acceptance"]["accepted"] is False
    assert persisted.read_bytes() == raw


def test_benchmark_zstd_request_fails_explicitly_without_dependency(
    monkeypatch,
) -> None:
    namespace = runpy.run_path(str(SCRIPT), run_name="capture_benchmark_test")
    runtime = namespace["RUNTIME"]
    monkeypatch.setattr(runtime, "zstd", None)

    with pytest.raises(RuntimeError, match="no fallback codec was selected"):
        namespace["_require_codec_available"]("zstd")

    namespace["_require_codec_available"]("zlib")
