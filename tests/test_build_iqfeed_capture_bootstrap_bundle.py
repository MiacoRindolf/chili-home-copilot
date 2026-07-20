from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from scripts import build_iqfeed_capture_bootstrap_bundle as builder
from scripts import iqfeed_capture_bootstrap_preflight as preflight


REPO = Path(__file__).resolve().parents[1]
ACCOUNT_ID = "11111111-2222-4333-8444-555555555555"
GENERATION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
UTC = timezone.utc
FIXED_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
HOST_FINGERPRINT = "a" * 64


def _publish(path: Path, value: dict[str, Any]) -> tuple[Path, str]:
    raw = builder._canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _request(tmp_path: Path) -> tuple[dict[str, Any], Path, str, Path]:
    now = datetime.now(UTC)
    write_root = tmp_path / "write"
    artifact_root = write_root / "artifacts"
    artifact_root.mkdir(parents=True)
    benchmark_document = {"fixture": "hash-bound-cli-input"}
    benchmark_raw = builder._canonical_json_bytes(benchmark_document)
    benchmark_sha = hashlib.sha256(benchmark_raw).hexdigest()
    benchmark_path = tmp_path / "inputs" / f"{benchmark_sha}.json"
    benchmark_path.parent.mkdir(parents=True)
    benchmark_path.write_bytes(benchmark_raw)
    source_sha256 = {
        role: hashlib.sha256(role.encode("utf-8")).hexdigest()
        for role in builder._SOURCE_RELATIVE_PATHS
    }
    request = {
        "schema_version": builder.BUILD_REQUEST_SCHEMA_VERSION,
        "repo_root": str(REPO),
        "artifact_root": str(artifact_root),
        "capture_store_root": str(write_root / "capture"),
        "resource_benchmark": {
            "path": str(benchmark_path),
            "sha256": benchmark_sha,
        },
        "source_sha256": source_sha256,
        "expected_account_id": ACCOUNT_ID,
        "account_risk_snapshot": {
            "equity": "100000.00",
            "buying_power": "400000.00",
        },
        "account_query": {
            "endpoint": "/v2/account",
            "environment": "paper",
            "account_id": ACCOUNT_ID,
        },
        "account_received_at": builder._iso(now - timedelta(seconds=3)),
        "account_available_at": builder._iso(now - timedelta(seconds=2)),
        "effective_config": {"capture_profile": "diagnostic_only_bootstrap"},
        "bridge_configuration": {
            "iqfeed_l1": {"schema_version": "l1-test"},
            "iqfeed_l2": {"schema_version": "l2-test"},
        },
        "activation_generation": GENERATION,
        "generated_at": builder._iso(now),
        "generation": 1,
    }
    request_path, request_sha = _publish(tmp_path / "request.json", request)
    return request, request_path, request_sha, write_root


def _built(request: dict[str, Any]) -> SimpleNamespace:
    artifacts = Path(request["artifact_root"])
    return SimpleNamespace(
        manifest_path=artifacts / "objects" / "aa" / f"{'a' * 64}.json",
        manifest_sha256="a" * 64,
        startup_evidence_path=(
            artifacts / "objects" / "bb" / f"{'b' * 64}.json"
        ),
        startup_evidence_sha256="b" * 64,
        resource_benchmark_path=Path(request["resource_benchmark"]["path"]),
        resource_benchmark_sha256=request["resource_benchmark"]["sha256"],
        commit_path=artifacts / "objects" / "cc" / f"{'c' * 64}.json",
        commit_sha256="c" * 64,
        capture_store_root=Path(request["capture_store_root"]),
        source_hashes=MappingProxyType(dict(request["source_sha256"])),
        builder_receipt=MappingProxyType(
            {
                "schema_version": builder.BUILDER_SCHEMA_VERSION,
                "paper_order_submission_authorized": False,
                "live_cash_authorized": False,
            }
        ),
    )


def _actual_source_hashes() -> dict[str, str]:
    return {
        role: hashlib.sha256((REPO / relative).read_bytes()).hexdigest()
        for role, relative in builder._SOURCE_RELATIVE_PATHS.items()
    }


def _valid_benchmark(
    *,
    now: datetime,
    source_hashes: dict[str, str],
) -> dict[str, Any]:
    from app.services.trading.momentum_neural import replay_capture_contract as contract
    from app.services.trading.momentum_neural import replay_capture_runtime as runtime

    measurement = runtime.CaptureResourceMeasurement(
        measured_at=now - timedelta(seconds=10),
        sample_seconds=30.0,
        total_memory_bytes=64 * 1024**3,
        available_memory_bytes=16 * 1024**3,
        disk_free_bytes=300 * 1024**3,
        average_cpu_percent=20.0,
        sustained_append_bytes_per_second=5_000_000.0,
        fsync_p95_milliseconds=10.0,
        logical_cpu_count=32,
        host_fingerprint_sha256=HOST_FINGERPRINT,
    )
    policy = runtime.CaptureBudgetPolicy(
        memory_reserve_bytes=4 * 1024**3,
        disk_reserve_bytes=20 * 1024**3,
        capture_fraction_of_memory_headroom=0.35,
        ring_fraction_of_capture_memory=0.30,
        queue_fraction_of_capture_memory=0.30,
        capture_fraction_of_disk_headroom=0.25,
        capture_fraction_of_measured_write_bandwidth=0.50,
        max_average_cpu_percent=90.0,
        capture_fraction_of_cpu_headroom=0.80,
        calibrated_hot_symbol_bytes=8 * 1024**2,
        max_queue_events=10_000,
        max_ring_events=10_000,
        max_gap_keys=1_000,
        raw_retention_days=3,
        derived_retention_days=90,
        pressure_cpu_enter_percent=85.0,
        pressure_cpu_exit_percent=70.0,
        pressure_memory_enter_margin_bytes=512 * 1024**2,
        pressure_memory_exit_margin_bytes=1024 * 1024**2,
        pressure_disk_enter_margin_bytes=2 * 1024**3,
        pressure_disk_exit_margin_bytes=4 * 1024**3,
        pressure_write_latency_enter_milliseconds=100.0,
        pressure_write_latency_exit_milliseconds=50.0,
        pressure_enter_samples=3,
        pressure_recovery_samples=3,
        pressure_sample_max_age_seconds=5.0,
        store_owner_lease_seconds=60.0,
        store_owner_heartbeat_seconds=10.0,
    )
    binding = runtime.CaptureResourceBinding.resolve(measurement, policy)
    record = json.loads(contract.canonical_json_bytes(binding.to_record()))
    resolved = {
        **record,
        "binding_sha256": binding.binding_sha256,
        "hashes": binding.hashes,
        "max_writer_threads": binding.budget.max_writer_threads,
    }
    return {
        "acceptance": {"accepted": True, "reasons": []},
        "artifact_freshness": {
            "age_seconds_at_emit": 1.0,
            "fresh_at_emit": True,
            "max_age_seconds": 3600.0,
        },
        "authority": {
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
        },
        "benchmark_schema_version": preflight.BENCHMARK_SCHEMA_VERSION,
        "capture_identity": {"run_id": GENERATION, "generation": 1},
        "capture_runtime_source": {
            "benchmark_script_sha256": source_hashes[
                "benchmark_replay_capture_runtime"
            ],
            "contract_sha256": source_hashes["replay_capture_contract"],
            "runtime_sha256": source_hashes["replay_capture_runtime"],
        },
        "enqueue": {},
        "environment": {
            "current_host_fingerprint_sha256": HOST_FINGERPRINT,
            "host_fingerprint_matches": True,
            "logical_cpu_count": 32,
            "measurement_host_fingerprint_sha256": HOST_FINGERPRINT,
            "platform": "test",
            "psutil_version": "test",
            "python": "test",
        },
        "generated_at": builder._iso(now - timedelta(seconds=5)),
        "measurement_window": {},
        "output": {},
        "parameters": {},
        "process": {},
        "resolved_resource_binding": resolved,
        "resource_measurement": {
            **resolved["measurement"],
            "durable_publication": {},
            "measurement_sha256": binding.measurement.measurement_sha256,
        },
        "shared_store_validation": {},
        "storage": {},
        "workload_base_utc": builder._iso(now - timedelta(minutes=1)),
        "writer": {},
    }


def _valid_request(
    tmp_path: Path,
    *,
    now: datetime = FIXED_NOW,
) -> tuple[dict[str, Any], Path, str, Path]:
    write_root = tmp_path / "write"
    artifact_root = write_root / "artifacts"
    artifact_root.mkdir(parents=True)
    source_hashes = _actual_source_hashes()
    benchmark = _valid_benchmark(now=now, source_hashes=source_hashes)
    benchmark_raw = builder._canonical_json_bytes(benchmark)
    benchmark_sha = hashlib.sha256(benchmark_raw).hexdigest()
    benchmark_path = tmp_path / "inputs" / f"{benchmark_sha}.json"
    benchmark_path.parent.mkdir(parents=True)
    benchmark_path.write_bytes(benchmark_raw)
    request = {
        "schema_version": builder.BUILD_REQUEST_SCHEMA_VERSION,
        "repo_root": str(REPO),
        "artifact_root": str(artifact_root),
        "capture_store_root": str(write_root / "capture"),
        "resource_benchmark": {
            "path": str(benchmark_path),
            "sha256": benchmark_sha,
        },
        "source_sha256": source_hashes,
        "expected_account_id": ACCOUNT_ID,
        "account_risk_snapshot": {
            "equity": "100000.00",
            "buying_power": "400000.00",
        },
        "account_query": {
            "endpoint": "/v2/account",
            "environment": "paper",
            "account_id": ACCOUNT_ID,
        },
        "account_received_at": builder._iso(now - timedelta(seconds=3)),
        "account_available_at": builder._iso(now - timedelta(seconds=2)),
        "effective_config": {"capture_profile": "diagnostic_only_bootstrap"},
        "bridge_configuration": {
            "iqfeed_l1": {
                "schema_version": "chili.iqfeed-l1-bridge-capture-config.v3",
                "protocol_version": "6.2",
                "port": 5009,
            },
            "iqfeed_l2": {
                "schema_version": "chili.iqfeed-depth-bridge.capture-config.v1",
                "protocol_version": "6.2",
                "port": 9200,
            },
        },
        "activation_generation": GENERATION,
        "generated_at": builder._iso(now - timedelta(seconds=1)),
        "generation": 1,
    }
    request_path, request_sha = _publish(tmp_path / "request.json", request)
    return request, request_path, request_sha, write_root


def _build_valid(
    tmp_path: Path,
    *,
    now: datetime = FIXED_NOW,
) -> builder.BuiltIqfeedCaptureBootstrapBundle:
    _request_value, request_path, request_sha, write_root = _valid_request(
        tmp_path,
        now=now,
    )
    return builder.build_iqfeed_capture_bootstrap_bundle_from_request(
        request_path=request_path,
        request_sha256=request_sha,
        allowed_read_roots=(REPO.parent, tmp_path),
        allowed_write_roots=(write_root,),
        wall_clock=lambda: now,
        host_fingerprint_provider=lambda: HOST_FINGERPRINT,
        local_drive_check=lambda _path: True,
    )


def test_cli_accepts_only_hash_bound_allowlisted_local_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request, request_path, request_sha, write_root = _request(tmp_path)
    observed: dict[str, Any] = {}

    def fake_build(**kwargs: Any) -> SimpleNamespace:
        observed.update(kwargs)
        return _built(request)

    monkeypatch.setattr(builder, "build_iqfeed_capture_bootstrap_bundle", fake_build)

    result = builder.main(
        [
            "--request",
            str(request_path),
            "--request-sha256",
            request_sha,
            "--allow-read-root",
            str(REPO.parent),
            "--allow-read-root",
            str(tmp_path),
            "--allow-write-root",
            str(write_root),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["verdict"] == "IQFEED_CAPTURE_BOOTSTRAP_BUNDLE_PUBLISHED"
    assert report["request_sha256"] == request_sha
    assert report["paper_order_submission_authorized"] is False
    assert report["live_cash_authorized"] is False
    assert report["provider_sockets_started"] is False
    assert report["database_accessed"] is False
    assert report["broker_accessed"] is False
    assert report["tasks_or_processes_changed"] is False
    assert observed["repo_root"] == REPO
    assert observed["artifact_root"] == Path(request["artifact_root"])
    assert observed["capture_store_root"] == Path(request["capture_store_root"])
    assert dict(observed["expected_source_hashes"]) == request["source_sha256"]
    assert observed["request_generated_at"].isoformat() == (
        request["generated_at"].replace("Z", "+00:00")
    )


def test_cli_rejects_changed_request_before_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _request_value, request_path, request_sha, write_root = _request(tmp_path)
    request_path.write_bytes(request_path.read_bytes() + b"\n")
    called = False

    def forbidden_build(**_kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        builder,
        "build_iqfeed_capture_bootstrap_bundle",
        forbidden_build,
    )

    result = builder.main(
        [
            "--request",
            str(request_path),
            "--request-sha256",
            request_sha,
            "--allow-read-root",
            str(REPO.parent),
            "--allow-read-root",
            str(tmp_path),
            "--allow-write-root",
            str(write_root),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 2
    assert report["error_code"] == "HASH_MISMATCH"
    assert report["bootstrap_artifact_published"] is False
    assert called is False


def test_cli_rejects_output_outside_write_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request, request_path, _request_sha, write_root = _request(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    request["artifact_root"] = str(outside)
    request_path, request_sha = _publish(request_path, request)
    monkeypatch.setattr(
        builder,
        "build_iqfeed_capture_bootstrap_bundle",
        lambda **_kwargs: pytest.fail("builder must not run"),
    )

    result = builder.main(
        [
            "--request",
            str(request_path),
            "--request-sha256",
            request_sha,
            "--allow-read-root",
            str(REPO.parent),
            "--allow-read-root",
            str(tmp_path),
            "--allow-write-root",
            str(write_root),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 2
    assert report["error_code"] == "BOOTSTRAP_BUNDLE_REJECTED"
    assert report["bootstrap_artifact_published"] is False


def test_cli_rejects_credential_like_embedded_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request, request_path, _request_sha, write_root = _request(tmp_path)
    request["effective_config"]["api_secret"] = "must-not-be-published"
    request_path, request_sha = _publish(request_path, request)
    monkeypatch.setattr(
        builder,
        "build_iqfeed_capture_bootstrap_bundle",
        lambda **_kwargs: pytest.fail("builder must not run"),
    )

    result = builder.main(
        [
            "--request",
            str(request_path),
            "--request-sha256",
            request_sha,
            "--allow-read-root",
            str(REPO.parent),
            "--allow-read-root",
            str(tmp_path),
            "--allow-write-root",
            str(write_root),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 2
    assert report["bootstrap_artifact_published"] is False
    assert report["broker_accessed"] is False


def test_source_roster_requires_exact_external_pins(tmp_path: Path) -> None:
    repo = tmp_path / "candidate"
    pins: dict[str, str] = {}
    for role, relative in builder._SOURCE_RELATIVE_PATHS.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = f"{role}\n".encode("utf-8")
        path.write_bytes(raw)
        pins[role] = hashlib.sha256(raw).hexdigest()
    pins["iqfeed_trade_bridge"] = "f" * 64

    with pytest.raises(builder.IqfeedCaptureBootstrapBundleError, match="external pin"):
        builder._source_roster(repo, expected_source_hashes=pins)


def test_publication_is_content_addressed_idempotent_and_leaves_no_pending(
    tmp_path: Path,
) -> None:
    document = {"schema_version": "test.bootstrap.object.v1", "inert": True}

    first_path, first_sha = builder._publish_object(
        tmp_path,
        document,
        allowed_write_roots=(tmp_path.parent,),
        local_drive_check=lambda _path: True,
    )
    second_path, second_sha = builder._publish_object(
        tmp_path,
        document,
        allowed_write_roots=(tmp_path.parent,),
        local_drive_check=lambda _path: True,
    )

    assert first_path == second_path
    assert first_sha == second_sha
    assert first_path == tmp_path / first_sha[:2] / f"{first_sha}.json"
    assert first_path.read_bytes() == builder._canonical_json_bytes(document)
    assert list(tmp_path.rglob("*.pending")) == []


def test_builder_cli_has_no_external_io_or_application_imports() -> None:
    tree = ast.parse(Path(builder.__file__).read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert not any(name == "app" or name.startswith("app.") for name in imports)
    assert {
        "psycopg",
        "requests",
        "socket",
        "sqlalchemy",
        "subprocess",
    }.isdisjoint(imports)


def test_real_end_to_end_build_publishes_commit_last_and_reloads_preflight(
    tmp_path: Path,
) -> None:
    built = _build_valid(tmp_path)

    commit = json.loads(built.commit_path.read_text(encoding="utf-8"))
    assert commit["schema_version"] == builder.BUNDLE_COMMIT_SCHEMA_VERSION
    assert commit["accepted"] is True
    assert commit["manifest"] == {
        "path": str(built.manifest_path),
        "sha256": built.manifest_sha256,
    }
    assert commit["startup_evidence"]["sha256"] == built.startup_evidence_sha256
    assert commit["paper_order_submission_authorized"] is False
    assert commit["live_cash_authorized"] is False
    assert hashlib.sha256(built.commit_path.read_bytes()).hexdigest() == (
        built.commit_sha256
    )
    assert built.builder_receipt["commit_published"] is True
    assert built.preflight.manifest_sha256 == built.manifest_sha256
    assert not list((tmp_path / "write" / "artifacts" / ".staging").glob("*"))


def test_startup_identity_freshness_matches_the_bounded_operator_chain(
    tmp_path: Path,
) -> None:
    built = _build_valid(tmp_path)
    manifest = json.loads(built.manifest_path.read_text(encoding="utf-8"))

    assert manifest["freshness_policy"]["startup_evidence_max_age_seconds"] == (
        30 * 60.0
    )
    accepted = preflight.load_iqfeed_capture_bootstrap_preflight(
        built.manifest_path,
        expected_manifest_sha256=built.manifest_sha256,
        allowed_read_roots=(REPO.parent, tmp_path),
        allowed_write_roots=(tmp_path / "write",),
        wall_clock=lambda: FIXED_NOW + timedelta(seconds=30 * 60 - 1),
        host_fingerprint_provider=lambda: HOST_FINGERPRINT,
        local_drive_check=lambda _path: True,
    )
    assert accepted.manifest_sha256 == built.manifest_sha256

    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        preflight.load_iqfeed_capture_bootstrap_preflight(
            built.manifest_path,
            expected_manifest_sha256=built.manifest_sha256,
            allowed_read_roots=(REPO.parent, tmp_path),
            allowed_write_roots=(tmp_path / "write",),
            wall_clock=lambda: FIXED_NOW + timedelta(seconds=30 * 60 + 1),
            host_fingerprint_provider=lambda: HOST_FINGERPRINT,
            local_drive_check=lambda _path: True,
        )
    assert caught.value.code == "STALE_EVIDENCE"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda row: row.__setitem__(
                "generated_at",
                builder._iso(FIXED_NOW - timedelta(seconds=61)),
            ),
            "build request is stale",
        ),
        (
            lambda row: row.__setitem__(
                "generated_at",
                builder._iso(FIXED_NOW + timedelta(seconds=6)),
            ),
            "future skew",
        ),
        (
            lambda row: (
                row.__setitem__(
                    "account_received_at",
                    builder._iso(FIXED_NOW - timedelta(seconds=32)),
                ),
                row.__setitem__(
                    "account_available_at",
                    builder._iso(FIXED_NOW - timedelta(seconds=31)),
                ),
            ),
            "account_received_at is stale",
        ),
        (
            lambda row: (
                row.__setitem__(
                    "account_received_at",
                    builder._iso(FIXED_NOW + timedelta(seconds=1)),
                ),
                row.__setitem__(
                    "account_available_at",
                    builder._iso(FIXED_NOW + timedelta(seconds=2)),
                ),
            ),
            "causally inconsistent",
        ),
    ],
)
def test_independent_wall_clock_rejects_stale_or_future_request_and_account(
    tmp_path: Path,
    mutate: Any,
    match: str,
) -> None:
    request, request_path, _request_sha, write_root = _request(tmp_path)
    request["generated_at"] = builder._iso(FIXED_NOW - timedelta(seconds=1))
    request["account_received_at"] = builder._iso(
        FIXED_NOW - timedelta(seconds=3)
    )
    request["account_available_at"] = builder._iso(
        FIXED_NOW - timedelta(seconds=2)
    )
    mutate(request)
    request_path, request_sha = _publish(request_path, request)

    with pytest.raises(builder.IqfeedCaptureBootstrapBundleError, match=match):
        builder.build_iqfeed_capture_bootstrap_bundle_from_request(
            request_path=request_path,
            request_sha256=request_sha,
            allowed_read_roots=(REPO.parent, tmp_path),
            allowed_write_roots=(write_root,),
            wall_clock=lambda: FIXED_NOW,
            local_drive_check=lambda _path: True,
        )

    assert not (write_root / "artifacts" / "objects").exists()


@pytest.mark.parametrize(
    "secret_material",
    [
        {"headers": {"X-Api-Key": "SENTINEL_SECRET"}},
        {"access_key": "SENTINEL_SECRET"},
        {"safe_url": "https://name:SENTINEL_SECRET@example.test/v2/account"},
        {"safe_url": "https://example.test/v2/account?access-token=SENTINEL"},
        {"nested": [{"requestHeaders": {"safe": "SENTINEL_SECRET"}}]},
    ],
)
def test_request_boundary_rejects_normalized_key_and_value_secret_shapes(
    tmp_path: Path,
    secret_material: dict[str, Any],
) -> None:
    request, request_path, _request_sha, write_root = _request(tmp_path)
    request["effective_config"].update(secret_material)
    request_path, request_sha = _publish(request_path, request)

    with pytest.raises(builder.IqfeedCaptureBootstrapBundleError):
        builder.build_iqfeed_capture_bootstrap_bundle_from_request(
            request_path=request_path,
            request_sha256=request_sha,
            allowed_read_roots=(REPO.parent, tmp_path),
            allowed_write_roots=(write_root,),
            local_drive_check=lambda _path: True,
        )

    assert not (write_root / "artifacts" / "objects").exists()


def test_direct_public_builder_boundary_cannot_bypass_typed_secret_projection(
    tmp_path: Path,
) -> None:
    request, _request_path, request_sha, write_root = _valid_request(tmp_path)
    request["account_query"] = {
        "endpoint": "/v2/account",
        "environment": "paper",
        "account_id": ACCOUNT_ID,
        "headers": {"X-Api-Key": "SENTINEL_SECRET"},
    }

    with pytest.raises(builder.IqfeedCaptureBootstrapBundleError):
        builder.build_iqfeed_capture_bootstrap_bundle(
            repo_root=request["repo_root"],
            artifact_root=request["artifact_root"],
            capture_store_root=request["capture_store_root"],
            resource_benchmark_path=request["resource_benchmark"]["path"],
            resource_benchmark_sha256=request["resource_benchmark"]["sha256"],
            expected_source_hashes=request["source_sha256"],
            expected_account_id=request["expected_account_id"],
            account_risk_snapshot=request["account_risk_snapshot"],
            account_query=request["account_query"],
            account_received_at=FIXED_NOW - timedelta(seconds=3),
            account_available_at=FIXED_NOW - timedelta(seconds=2),
            effective_config=request["effective_config"],
            bridge_configuration=request["bridge_configuration"],
            activation_generation=request["activation_generation"],
            request_generated_at=FIXED_NOW - timedelta(seconds=1),
            build_request_sha256=request_sha,
            allowed_read_roots=(REPO.parent, tmp_path),
            allowed_write_roots=(write_root,),
            wall_clock=lambda: FIXED_NOW,
            host_fingerprint_provider=lambda: HOST_FINGERPRINT,
            local_drive_check=lambda _path: True,
        )

    assert not (write_root / "artifacts" / "objects").exists()


@pytest.mark.parametrize(
    ("risk_patch", "query_patch"),
    [
        ({"equity": "0"}, {}),
        ({"buying_power": "-1"}, {}),
        ({}, {"endpoint": "/v2/orders"}),
        ({}, {"operation": "submit_order"}),
    ],
)
def test_account_projection_rejects_nonpositive_risk_or_unapproved_operation(
    risk_patch: dict[str, Any],
    query_patch: dict[str, Any],
) -> None:
    risk = {"equity": "100000.00", "buying_power": "400000.00"}
    risk.update(risk_patch)
    query = {
        "endpoint": "/v2/account",
        "environment": "paper",
        "account_id": ACCOUNT_ID,
    }
    query.update(query_patch)

    with pytest.raises(builder.IqfeedCaptureBootstrapBundleError):
        if risk_patch:
            builder._safe_account_risk_snapshot(risk)
        else:
            builder._safe_account_query(
                query, expected_account_id=ACCOUNT_ID
            )


def test_publication_rejects_objects_directory_reparse_before_outside_write(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    artifact_root = allowed / "artifacts"
    outside = tmp_path / "outside"
    artifact_root.mkdir(parents=True)
    outside.mkdir()
    try:
        os.symlink(outside, artifact_root / "objects", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable on this host: {exc}")

    with pytest.raises(builder.IqfeedCaptureBootstrapBundleError, match="reparse"):
        builder._publish_object(
            artifact_root / "objects",
            {"schema_version": "test.bootstrap.escape.v1"},
            allowed_write_roots=(allowed,),
            local_drive_check=lambda _path: True,
        )

    assert list(outside.iterdir()) == []


def test_final_preflight_failure_leaves_truthful_uncommitted_objects_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _request_value, request_path, request_sha, write_root = _valid_request(tmp_path)
    real_load = preflight.load_iqfeed_capture_bootstrap_preflight
    calls = 0

    def fail_second_load(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise preflight.BootstrapPreflightError(
                "TEST_FINAL_REJECTED",
                "forced final preflight rejection",
            )
        return real_load(*args, **kwargs)

    monkeypatch.setattr(
        builder.preflight_module,
        "load_iqfeed_capture_bootstrap_preflight",
        fail_second_load,
    )

    with pytest.raises(builder.IqfeedCaptureBootstrapBundleError) as captured:
        builder.build_iqfeed_capture_bootstrap_bundle_from_request(
            request_path=request_path,
            request_sha256=request_sha,
            allowed_read_roots=(REPO.parent, tmp_path),
            allowed_write_roots=(write_root,),
            wall_clock=lambda: FIXED_NOW,
            host_fingerprint_provider=lambda: HOST_FINGERPRINT,
            local_drive_check=lambda _path: True,
        )

    assert calls == 2
    assert captured.value.commit_published is False
    assert len(captured.value.visible_objects) == 2
    documents = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (write_root / "artifacts" / "objects").glob("*/*.json")
    ]
    assert len(documents) == 2
    assert all(
        row.get("schema_version") != builder.BUNDLE_COMMIT_SCHEMA_VERSION
        for row in documents
    )
    assert not list((write_root / "artifacts" / ".staging").glob("*"))


def test_cli_failure_report_discloses_visible_objects_and_no_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _request_value, request_path, request_sha, write_root = _request(tmp_path)
    visible = ({"path": str(tmp_path / "visible.json"), "sha256": "d" * 64},)

    def reject(**_kwargs: Any) -> None:
        raise builder.IqfeedCaptureBootstrapBundleError(
            "forced rejection",
            visible_objects=visible,
            commit_published=False,
        )

    monkeypatch.setattr(
        builder,
        "build_iqfeed_capture_bootstrap_bundle_from_request",
        reject,
    )
    result = builder.main(
        [
            "--request",
            str(request_path),
            "--request-sha256",
            request_sha,
            "--allow-read-root",
            str(tmp_path),
            "--allow-write-root",
            str(write_root),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 2
    assert report["bootstrap_artifact_published"] is False
    assert report["commit_published"] is False
    assert report["visible_objects"] == list(visible)
