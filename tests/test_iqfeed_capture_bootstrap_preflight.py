from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable
import uuid

import pytest

from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureContractError,
    CaptureProducerSpec,
    CaptureRunIdentity,
    CaptureStream,
    sha256_json,
)
from app.services.trading.momentum_neural.live_replay_capture import (
    CaptureIdentityEvidence,
    LiveCaptureRunInputs,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    CapturePressureSample,
    SharedCaptureStoreRuntime,
)
from scripts import iqfeed_capture_bootstrap as bootstrap
from scripts import iqfeed_capture_bootstrap_preflight as preflight
from scripts import iqfeed_capture_host as capture_host


UTC = timezone.utc
NOW = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[1]
CAPTURE_DIR = REPO / "app" / "services" / "trading" / "momentum_neural"
HOST_FINGERPRINT = "a" * 64
SETTINGS_PROJECTION_SHA256 = "9" * 64


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _publish(directory: Path, document: dict[str, Any]) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    raw = preflight._canonical_json_bytes(document)
    digest = hashlib.sha256(raw).hexdigest()
    path = directory / f"{digest}.json"
    path.write_bytes(raw)
    return path, digest


def _source_rows() -> tuple[list[dict[str, str]], dict[str, Path], dict[str, str]]:
    paths = {
        "app_migrations": REPO / "app" / "migrations.py",
        "benchmark_replay_capture_runtime": (
            REPO / "scripts" / "benchmark_replay_capture_runtime.py"
        ),
        "iqfeed_capture_bootstrap": REPO / "scripts" / "iqfeed_capture_bootstrap.py",
        "iqfeed_capture_bootstrap_preflight": (
            REPO / "scripts" / "iqfeed_capture_bootstrap_preflight.py"
        ),
        "iqfeed_capture_host": REPO / "scripts" / "iqfeed_capture_host.py",
        "iqfeed_capture_host_launcher": (
            REPO / "scripts" / "start-iqfeed-capture-host.ps1"
        ),
        "iqfeed_l1_capture": CAPTURE_DIR / "iqfeed_l1_capture.py",
        "iqfeed_l2_capture": CAPTURE_DIR / "iqfeed_l2_capture.py",
        "iqfeed_depth_bridge": REPO / "scripts" / "iqfeed_depth_bridge.py",
        "iqfeed_trade_bridge": REPO / "scripts" / "iqfeed_trade_bridge.py",
        "live_replay_capture": CAPTURE_DIR / "live_replay_capture.py",
        "replay_capture_contract": CAPTURE_DIR / "replay_capture_contract.py",
        "replay_capture_runtime": CAPTURE_DIR / "replay_capture_runtime.py",
    }
    hashes = {role: _sha(path) for role, path in paths.items()}
    rows = [
        {"role": role, "path": str(path), "sha256": hashes[role]}
        for role, path in sorted(paths.items())
    ]
    return rows, paths, hashes


def _binding(paths: dict[str, Path], hashes: dict[str, str]):
    contract, runtime = preflight._load_verified_capture_modules(
        paths["replay_capture_contract"],
        paths["replay_capture_runtime"],
        contract_sha256=hashes["replay_capture_contract"],
        runtime_sha256=hashes["replay_capture_runtime"],
    )
    measurement = runtime.CaptureResourceMeasurement(
        measured_at=NOW - timedelta(seconds=10),
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
    return binding, resolved


@dataclass
class _Bundle:
    manifest_path: Path
    manifest_sha256: str
    read_root: Path
    write_root: Path
    manifest: dict[str, Any]
    startup: dict[str, Any]
    resource: dict[str, Any]
    binding: Any


def _make_bundle(
    tmp_path: Path,
    *,
    mutate_startup: Callable[[dict[str, Any]], None] | None = None,
    mutate_resource: Callable[[dict[str, Any]], None] | None = None,
    mutate_manifest: Callable[[dict[str, Any]], None] | None = None,
) -> _Bundle:
    read_root = tmp_path / "read"
    write_root = tmp_path / "write"
    read_root.mkdir(parents=True)
    write_root.mkdir(parents=True)
    source_rows, source_paths, source_hashes = _source_rows()
    binding, resolved_binding = _binding(source_paths, source_hashes)
    resource: dict[str, Any] = {
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
        "capture_identity": {"run_id": str(uuid.uuid4()), "generation": 1},
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
        "generated_at": _iso(NOW - timedelta(seconds=5)),
        "measurement_window": {},
        "output": {},
        "parameters": {},
        "process": {},
        "resolved_resource_binding": resolved_binding,
        "resource_measurement": {
            **resolved_binding["measurement"],
            "durable_publication": {},
            "measurement_sha256": binding.measurement.measurement_sha256,
        },
        "shared_store_validation": {},
        "storage": {},
        "workload_base_utc": _iso(NOW - timedelta(minutes=1)),
        "writer": {},
    }
    if mutate_resource is not None:
        mutate_resource(resource)
    resource_path, resource_sha = _publish(read_root / "resources", resource)

    bridge_configuration = {
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
    }
    startup: dict[str, Any] = {
        "schema_version": preflight.STARTUP_EVIDENCE_SCHEMA_VERSION,
        "captured_at": _iso(NOW - timedelta(seconds=1)),
        "generation": 1,
        "process_instance_id": str(uuid.uuid4()),
        "broker": "alpaca",
        "broker_environment": "paper",
        "code_build": {
            "schema_version": preflight.CODE_BUILD_SCHEMA_VERSION,
            "artifacts": source_rows,
        },
        "effective_config": {"capture_profile": "diagnostic_only"},
        "feature_flags": {
            "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": False,
            "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED": False,
            "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED": False,
        },
        "account_identity": {"account_id": "hash-bound-test-account"},
        "account_risk_snapshot": {
            "equity": "100000.00",
            "buying_power": "400000.00",
        },
        "account_query": {"endpoint": "/v2/account", "environment": "paper"},
        "account_provider": "alpaca",
        "account_snapshot_clocks": {
            "provider_event_at": None,
            "received_at": _iso(NOW - timedelta(seconds=3)),
            "available_at": _iso(NOW - timedelta(seconds=2)),
        },
        "bridge_configuration": bridge_configuration,
        "bridge_configuration_sha256": preflight._sha256_json(
            bridge_configuration
        ),
        "iqfeed_l1_clock_contract": {
            "schema_version": preflight.IQFEED_L1_CLOCK_CONTRACT_SCHEMA_VERSION,
            "exact_print": {
                "message_type": "Q",
                "selected_field_ack_required": True,
                "provider_event_at_available": True,
                "event_clock_basis": "most_recent_trade_date_plus_timems",
                "tick_identity_field": "TickID",
                "certifying_exact_event_clock": True,
            },
            "nbbo_quote": {
                "message_type": "Q",
                "provider_event_at_available": False,
                "market_reference_basis": "most_recent_trade_date_plus_timems",
                "certifying_exact_event_clock": False,
            },
        },
        "iqfeed_l2_clock_contract": {
            "schema_version": preflight.IQFEED_L2_CLOCK_CONTRACT_SCHEMA_VERSION,
            "delta": {
                "message_type": "6",
                "provider_event_at_available": True,
                "event_clock_basis": "type6_provider_date_plus_time",
                "certifying_exact_event_clock": True,
            },
            "checkpoint": {
                "provider_event_at_available": False,
                "per_level_exact_clocks_required": True,
                "initial_snapshot_complete": False,
                "certifying_snapshot_completion": False,
            },
        },
    }
    if mutate_startup is not None:
        mutate_startup(startup)
    startup_path, startup_sha = _publish(read_root / "startup", startup)

    manifest: dict[str, Any] = {
        "schema_version": preflight.BOOTSTRAP_MANIFEST_SCHEMA_VERSION,
        "capture_mode": "diagnostic_only",
        "execution_boundary": {
            "alpaca_paper_order_submission_enabled": False,
            "live_cash_order_submission_enabled": False,
            "provider_socket_start_enabled": False,
            "database_write_start_enabled": False,
            "network_fallback_allowed": False,
            "current_database_fallback_allowed": False,
        },
        "freshness_policy": {
            "max_future_skew_seconds": 5.0,
            "resource_benchmark_max_age_seconds": 60.0,
            "startup_evidence_max_age_seconds": 30.0,
        },
        "resource_benchmark": {
            "path": str(resource_path),
            "sha256": resource_sha,
            "binding_sha256": binding.binding_sha256,
        },
        "startup_evidence": {"path": str(startup_path), "sha256": startup_sha},
        "capture_store_root": str(write_root / "capture-v4"),
        "run_configuration": {
            "schema_version": preflight.RUN_CONFIGURATION_SCHEMA_VERSION,
            "heartbeat_timeout_seconds": 30.0,
            "pretrigger_horizon_seconds": 300.0,
            "per_symbol_pretrigger_events": 1_000,
            "writer_batch_events": 256,
            "writer_batch_bytes": 1024 * 1024,
            "writer_poll_seconds": 0.05,
            "writer_flush_interval_seconds": 0.5,
            "max_change_keys": 2_000,
            "max_read_sources": 1_000,
        },
        "handoff_configuration": {
            "schema_version": preflight.IQFEED_HANDOFF_BUDGET_SCHEMA_VERSION,
            "l1": {
                "max_pending_events": 1_000,
                "max_pending_bytes": 16 * 1024 * 1024,
                "max_gap_keys": 200,
            },
            "l2": {
                "max_pending_events": 1_000,
                "max_pending_bytes": 32 * 1024 * 1024,
                "max_gap_keys": 200,
            },
        },
    }
    if mutate_manifest is not None:
        mutate_manifest(manifest)
    manifest_path, manifest_sha = _publish(read_root / "manifests", manifest)
    return _Bundle(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        read_root=read_root,
        write_root=write_root,
        manifest=manifest,
        startup=startup,
        resource=resource,
        binding=binding,
    )


def _load(bundle: _Bundle):
    return preflight.load_iqfeed_capture_bootstrap_preflight(
        bundle.manifest_path,
        expected_manifest_sha256=bundle.manifest_sha256,
        allowed_read_roots=(bundle.read_root, REPO),
        allowed_write_roots=(bundle.write_root,),
        wall_clock=lambda: NOW,
        host_fingerprint_provider=lambda: HOST_FINGERPRINT,
        local_drive_check=lambda _path: True,
    )


def _pressure_sample(
    bundle: _Bundle,
    *,
    observed_at: datetime = NOW,
    cpu_percent: float = 20.0,
) -> CapturePressureSample:
    return CapturePressureSample(
        observed_at=observed_at,
        resource_binding_sha256=bundle.binding.binding_sha256,
        cpu_percent=cpu_percent,
        available_memory_bytes=16 * 1024**3,
        disk_free_bytes=300 * 1024**3,
        write_latency_milliseconds=10.0,
    )


def _base_startup_provider(composition, *, calls: list[str] | None = None):
    def provide(
        symbol: str,
        *,
        resource_binding,
        run_configuration,
        capture_store_root,
    ) -> LiveCaptureRunInputs:
        if calls is not None:
            calls.append(symbol)
        assert resource_binding == composition.binding
        assert run_configuration == composition.run_configuration
        code = {"git_commit": "bootstrap-fixture", "dirty": True}
        config = bootstrap.captured_paper_base_capture_config(
            settings_projection_sha256=SETTINGS_PROJECTION_SHA256,
            certification_symbol=symbol,
            resource_binding=composition.binding,
            run_configuration=run_configuration,
            capture_store_root=capture_store_root,
            additional_config={"paper_execution": False},
        )
        features = {"replay_capture": True, "paper_execution": False}
        account_identity = {
            "broker": "alpaca",
            "environment": "paper",
            "account_id": "bootstrap-test-paper-account",
        }
        identity = CaptureRunIdentity(
            run_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"bootstrap-run:{symbol}")),
            generation=1,
            code_build_sha256=sha256_json(code),
            config_sha256=sha256_json(config),
            feature_flags_sha256=sha256_json(features),
            account_identity_sha256=sha256_json(account_identity),
            broker="alpaca",
            broker_environment="paper",
        )
        evidence = CaptureIdentityEvidence(
            code_build=code,
            config=config,
            feature_flags=features,
            account_identity=account_identity,
            account_risk_snapshot={
                "equity": "71876.85",
                "buying_power": "287507.40",
                "portfolio_heat_r": "0",
            },
            account_query={"operation": "get_account", "environment": "paper"},
            account_provider="alpaca",
        )
        producer = CaptureProducerSpec(
            producer_id="live_fsm",
            instance_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"bootstrap-fsm:{symbol}")),
            generation=identity.generation,
            streams=(
                CaptureStream.CODE_BUILD,
                CaptureStream.CONFIG_SNAPSHOT,
                CaptureStream.FEATURE_FLAG_SNAPSHOT,
                CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                CaptureStream.PROVIDER_OHLCV,
                CaptureStream.SCANNER_SNAPSHOT,
            ),
            code_build_sha256=identity.code_build_sha256,
            config_sha256=identity.config_sha256,
            feature_flags_sha256=identity.feature_flags_sha256,
            resource_binding_sha256=composition.binding.binding_sha256,
        )
        return LiveCaptureRunInputs(
            identity=identity,
            evidence=evidence,
            producers=(producer,),
        )

    return provide


def _open_external_generations(composition):
    l1 = composition.l1_handoff.record_connection_boundary(
        at=NOW,
        bridge_run_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "bootstrap-iqfeed-l1")),
        connection_generation=7,
        active=True,
    )
    composition.l2_handoff.record_connection_boundary(
        at=NOW,
        bridge_run_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "bootstrap-iqfeed-l2")),
        connection_generation=11,
        active=True,
    )
    l2 = composition.l2_handoff.active_producer_generation()
    assert l1 is not None and l2 is not None
    return l1, l2


def _shared_store_runtime(composition) -> SharedCaptureStoreRuntime:
    return SharedCaptureStoreRuntime.create(
        composition.preflight.capture_store_root,
        resource_binding=composition.binding,
        shared_admission_budget=composition.shared_admission_budget,
        compression_codec="zlib",
        compression_level=3,
    )


def test_valid_preflight_has_no_provider_db_store_or_activation_side_effect(tmp_path):
    bundle = _make_bundle(tmp_path)
    before_modules = set(sys.modules)
    assert not (bundle.write_root / "capture-v4").exists()

    result = _load(bundle)

    assert not (bundle.write_root / "capture-v4").exists()
    assert not {
        name
        for name in set(sys.modules) - before_modules
        if name.endswith("iqfeed_trade_bridge")
    }
    report = result.report
    assert report["verdict"] == "BOOTSTRAP_PREFLIGHT_VALID"
    assert report["activation_authorized"] is False
    assert report["certification_eligible"] is False
    assert report["provider_or_database_started"] is False
    assert report["resource_benchmark"]["binding_sha256"] == (
        bundle.binding.binding_sha256
    )
    assert set(report["source_hashes"]) == preflight._REQUIRED_SOURCE_ROLES
    assert report["startup_evidence"]["process_instance_id"] == (
        result.startup_process_instance_id
    )
    assert len(
        report["startup_evidence"]["iqfeed_l1_clock_contract_sha256"]
    ) == 64
    assert len(
        report["startup_evidence"]["iqfeed_l2_clock_contract_sha256"]
    ) == 64
    assert "iqfeed_l1_exact_quote_event_clock_unavailable" in report[
        "blocking_reasons"
    ]
    assert "iqfeed_unified_capture_host_not_installed_or_launched" in report[
        "blocking_reasons"
    ]
    handoff = report["handoff_configuration"]
    assert handoff["aggregate"]["max_pending_events"] == 2_000
    assert handoff["downstream_admission"]["max_pending_events"] == (
        bundle.binding.budget.max_queue_events - 2_000
    )
    assert handoff["downstream_admission"]["max_pending_bytes"] == (
        bundle.binding.budget.async_queue_bytes - 48 * 1024 * 1024
    )
    assert len(report["preflight_report_sha256"]) == 64


def test_inert_composition_rehydrates_one_shared_budget_without_store_or_activation(
    tmp_path,
):
    bundle = _make_bundle(tmp_path)
    verified = _load(bundle)
    capture_root = Path(bundle.manifest["capture_store_root"])
    assert not capture_root.exists()

    composition = bootstrap.prepare_iqfeed_capture_ingress(
        verified,
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )

    health = composition.health()
    assert health["state"] == "prepared"
    assert health["activation_authorized"] is False
    assert health["provider_socket_started"] is False
    assert health["database_or_broker_started"] is False
    assert health["hot_admission_available"] is False
    assert "iqfeed_generation_bound_hot_run_factory_uninstalled" in health[
        "blocking_reasons"
    ]
    assert composition.shared_admission_budget.max_events == verified.handoff_configuration[
        "downstream_admission"
    ]["max_pending_events"]
    assert composition.shared_admission_budget.max_bytes == verified.handoff_configuration[
        "downstream_admission"
    ]["max_pending_bytes"]
    assert composition.l1_handoff.max_pending_bytes == verified.handoff_configuration[
        "l1"
    ]["max_pending_bytes"]
    assert composition.l2_handoff.max_pending_bytes == verified.handoff_configuration[
        "l2"
    ]["max_pending_bytes"]
    assert not capture_root.exists()
    assert composition.close()["state"] == "closed"


def test_composition_audit_mappings_cannot_be_mutated_through_callers(tmp_path):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    original = composition.health()["handoff_budget"]

    caller_copy = composition.handoff_budget
    caller_copy["l1"]["max_pending_events"] = 1
    assert composition.health()["handoff_budget"] == original
    with pytest.raises(TypeError):
        composition.provenance.source_hashes["iqfeed_trade_bridge"] = "f" * 64

    composition.close()


def test_inert_composition_starts_and_drains_only_local_handoff_threads(tmp_path):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )

    composition.start_ingress()
    running = composition.health()
    assert running["state"] == "ingress_running"
    assert running["l1_handoff"]["started"] is True
    assert running["l2_handoff"]["started"] is True
    assert running["service"]["running_symbols"] == ()
    assert composition.close()["state"] == "closed"


def test_unified_host_binds_both_bridges_without_provider_db_store_or_task_side_effect(
    tmp_path, monkeypatch
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    capture_root = Path(bundle.manifest["capture_store_root"])
    provider_calls: list[str] = []

    def reject_provider(*_args, **_kwargs):
        provider_calls.append("provider")
        raise AssertionError("inert host binding opened a provider socket")

    monkeypatch.setattr(capture_host.iqfeed_trade_bridge.socket, "create_connection", reject_provider)
    assert capture_host.iqfeed_trade_bridge._capture_handoff is None
    assert capture_host.iqfeed_depth_bridge._capture_handoff is None
    host = capture_host.IqfeedCaptureHost(composition, wall_clock=lambda: NOW)

    receipt = host.bind()

    assert provider_calls == []
    assert not capture_root.exists()
    assert capture_host.iqfeed_trade_bridge._capture_handoff is composition.l1_handoff
    assert capture_host.iqfeed_depth_bridge._capture_handoff is composition.l2_handoff
    assert receipt.trade_bridge["sha256"] == composition.preflight.source_hashes[
        "iqfeed_trade_bridge"
    ]
    assert receipt.depth_bridge["sha256"] == composition.preflight.source_hashes[
        "iqfeed_depth_bridge"
    ]
    with pytest.raises(TypeError):
        receipt.trade_bridge["sha256"] = "f" * 64
    health = host.health()
    assert health["state"] == "bound"
    assert health["provider_sockets_started"] is False
    assert health["database_or_broker_started"] is False
    assert health["paper_live_execution_enabled"] is False
    assert health["task_or_service_mutated"] is False
    assert len(health["binding_receipt_sha256"]) == 64

    assert host.close()["state"] == "closed"
    assert capture_host.iqfeed_trade_bridge._capture_handoff is None
    assert capture_host.iqfeed_depth_bridge._capture_handoff is None


def test_unified_host_rejects_loaded_bridge_hash_mismatch_before_threads(
    tmp_path, monkeypatch
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    monkeypatch.setattr(
        capture_host.iqfeed_trade_bridge,
        "BRIDGE_SOURCE_SHA256",
        "f" * 64,
    )
    with pytest.raises(CaptureContractError, match="source hash escaped preflight"):
        capture_host.IqfeedCaptureHost(composition, wall_clock=lambda: NOW)
    assert composition.state is bootstrap.IqfeedIngressCompositionState.PREPARED
    assert composition.l1_handoff.health()["started"] is False
    assert composition.l2_handoff.health()["started"] is False
    composition.close()


def test_unified_host_launch_validation_binds_exact_launcher_and_python(tmp_path):
    bundle = _make_bundle(tmp_path)
    launcher = REPO / "scripts" / "start-iqfeed-capture-host.ps1"
    report = capture_host.validate_iqfeed_capture_host_launch(
        launcher_path=launcher,
        launcher_sha256=_sha(launcher),
        python_executable=sys.executable,
        manifest_path=bundle.manifest_path,
        manifest_sha256=bundle.manifest_sha256,
        allowed_read_roots=(bundle.read_root, REPO),
        allowed_write_roots=(bundle.write_root,),
        wall_clock=lambda: NOW,
        host_fingerprint_provider=lambda: HOST_FINGERPRINT,
        local_drive_check=lambda _path: True,
    )
    assert report["verdict"] == "IQFEED_CAPTURE_HOST_LAUNCH_VALIDATED_INERT"
    assert report["launcher"]["path"] == str(launcher.resolve())
    assert report["python_executable"] == str(Path(sys.executable).resolve())
    assert report["activation_authorized"] is False
    assert report["provider_sockets_started"] is False
    assert report["database_or_broker_started"] is False
    assert report["paper_live_execution_enabled"] is False
    assert report["task_or_service_mutated"] is False
    assert len(report["launch_validation_sha256"]) == 64


def test_unified_host_launch_validation_rejects_launcher_hash_forgery(tmp_path):
    bundle = _make_bundle(tmp_path)
    launcher = REPO / "scripts" / "start-iqfeed-capture-host.ps1"
    with pytest.raises(CaptureContractError, match="launcher source hash"):
        capture_host.validate_iqfeed_capture_host_launch(
            launcher_path=launcher,
            launcher_sha256="f" * 64,
            python_executable=sys.executable,
            manifest_path=bundle.manifest_path,
            manifest_sha256=bundle.manifest_sha256,
            allowed_read_roots=(bundle.read_root, REPO),
            allowed_write_roots=(bundle.write_root,),
            wall_clock=lambda: NOW,
            host_fingerprint_provider=lambda: HOST_FINGERPRINT,
            local_drive_check=lambda _path: True,
        )


def test_candidate_iqfeed_launchers_have_no_legacy_path_or_basename_guard():
    unified = (REPO / "scripts" / "start-iqfeed-capture-host.ps1").read_text(
        encoding="utf-8"
    )
    compatibility = (REPO / "scripts" / "start-iqfeed-trade-bridge.ps1").read_text(
        encoding="utf-8"
    )
    for source in (unified, compatibility):
        assert "SilentlyContinue" not in source
        assert "D:\\dev\\chili-home-copilot\\" not in source
        assert "Get-CimInstance Win32_Process" not in source
        assert "Start-Process" not in source
        assert "iqconnect.exe" not in source.lower()
    assert "ValidateOnly is required" in unified
    assert "start-iqfeed-capture-host.ps1" in compatibility


def test_unified_host_rolls_back_trade_binding_if_depth_binding_fails(
    tmp_path, monkeypatch
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    host = capture_host.IqfeedCaptureHost(composition, wall_clock=lambda: NOW)

    def fail_depth(_handoff):
        raise RuntimeError("fixture depth bind failure")

    monkeypatch.setattr(
        capture_host.iqfeed_depth_bridge,
        "bind_capture_handoff",
        fail_depth,
    )
    with pytest.raises(CaptureContractError, match="failed atomically"):
        host.bind()
    assert host.state is capture_host.IqfeedCaptureHostState.FAILED
    assert composition.state is bootstrap.IqfeedIngressCompositionState.CLOSED
    assert capture_host.iqfeed_trade_bridge._capture_handoff is None
    assert capture_host.iqfeed_depth_bridge._capture_handoff is None


def test_unified_host_rolls_back_both_bindings_if_receipt_creation_fails(
    tmp_path,
) -> None:
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    host = capture_host.IqfeedCaptureHost(
        composition,
        wall_clock=lambda: NOW.replace(tzinfo=None),
    )

    with pytest.raises(CaptureContractError, match="failed atomically"):
        host.bind()

    assert host.state is capture_host.IqfeedCaptureHostState.FAILED
    assert composition.state is bootstrap.IqfeedIngressCompositionState.CLOSED
    assert composition.l1_handoff.health()["accepting"] is False
    assert composition.l1_handoff.health()["unfinished_tasks"] == 0
    assert composition.l2_handoff.health()["accepting"] is False
    assert composition.l2_handoff.health()["unfinished_tasks"] == 0
    assert capture_host.iqfeed_trade_bridge._capture_handoff is None
    assert capture_host.iqfeed_depth_bridge._capture_handoff is None


def test_unified_host_routes_hot_admission_to_depth_lifecycle(tmp_path, monkeypatch):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    host = capture_host.IqfeedCaptureHost(composition, wall_clock=lambda: NOW)
    host.bind()
    observed: dict[str, Any] = {}

    def admit(symbol: str, *, required_stream):
        observed["required_stream"] = required_stream
        return type(
            "Admission",
            (),
            {"symbol": symbol, "capture_ready": True},
        )()

    def activate(symbol: str, *, available_at):
        observed["symbol"] = symbol
        observed["available_at"] = available_at
        return False

    monkeypatch.setattr(composition.service, "admit_hot_symbol", admit)
    monkeypatch.setattr(
        capture_host.iqfeed_depth_bridge,
        "activate_capture_symbol",
        activate,
    )
    result = host.admit_hot_symbol("VEEE")
    assert observed["required_stream"] is CaptureStream.NBBO_QUOTE
    assert observed["symbol"] == "VEEE"
    assert observed["available_at"] == NOW
    assert result.capture_ready is True
    assert result.l2_checkpoint_queued is False
    assert result.rejected_reason == "iqfeed_l2_checkpoint_coverage_unavailable"
    host.close()


def test_unified_host_installs_typed_first_dip_bridge_around_real_fsm_tick(
    tmp_path, monkeypatch
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    host = capture_host.IqfeedCaptureHost(composition, wall_clock=lambda: NOW)
    host.bind()
    observed: dict[str, Any] = {
        "inside": False,
        "scanner_inside": False,
        "ohlcv_inside": False,
        "microstructure_inside": False,
        "tick_calls": 0,
    }
    coordinator = type("Coordinator", (), {"certification_symbol": "VEEE"})()
    monkeypatch.setattr(
        composition.service,
        "coordinator_for",
        lambda symbol: coordinator if symbol == "VEEE" else None,
    )

    class FakeBridge:
        def __init__(self, **kwargs):
            observed["bridge"] = kwargs
            self.final_capture_frontier = None

        def install(self):
            class Scope:
                def __enter__(self):
                    observed["inside"] = True
                    return self

                def __exit__(self, *_exc):
                    observed["inside"] = False
                    return False

            return Scope()

    class FakeScannerBridge:
        def __init__(self, **kwargs):
            observed["scanner_bridge"] = kwargs
            self.captured_reads = ()

        def install(self):
            class Scope:
                def __enter__(self):
                    observed["scanner_inside"] = True
                    return self

                def __exit__(self, *_exc):
                    observed["scanner_inside"] = False
                    return False

            return Scope()

    class FakeOhlcvBridge:
        def __init__(self, **kwargs):
            observed["ohlcv_bridge"] = kwargs
            self.captured_reads = ()

        def install(self):
            class Scope:
                def __enter__(self):
                    observed["ohlcv_inside"] = True
                    return self

                def __exit__(self, *_exc):
                    observed["ohlcv_inside"] = False
                    return False

            return Scope()

    class FakeMicrostructureBridge:
        def __init__(self, **kwargs):
            observed["microstructure_bridge"] = kwargs
            self.captured_reads = ()

        def install(self):
            class Scope:
                def __enter__(self):
                    observed["microstructure_inside"] = True
                    return self

                def __exit__(self, *_exc):
                    observed["microstructure_inside"] = False
                    return False

            return Scope()

    def tick(db, session_id, *, adapter_factory):
        assert observed["inside"] is True
        assert observed["scanner_inside"] is True
        assert observed["ohlcv_inside"] is True
        assert observed["microstructure_inside"] is True
        observed["tick_calls"] += 1
        observed["tick"] = (db, session_id, adapter_factory)
        observed["tick_decision_now"] = capture_host.momentum_live_runner._utcnow()
        return {"ok": True, "state": "watching_live"}

    monkeypatch.setattr(
        capture_host,
        "LiveFirstDipAdaptiveCaptureBridge",
        FakeBridge,
    )
    monkeypatch.setattr(
        capture_host,
        "LiveScannerSnapshotCaptureBridge",
        FakeScannerBridge,
    )
    monkeypatch.setattr(
        capture_host,
        "LiveOhlcvCaptureBridge",
        FakeOhlcvBridge,
    )
    monkeypatch.setattr(
        capture_host,
        "LiveMicrostructureCaptureBridge",
        FakeMicrostructureBridge,
    )
    monkeypatch.setattr(
        capture_host.momentum_live_runner,
        "tick_live_session",
        tick,
    )
    source = type(
        "Source",
        (),
        {"inputs": type("Inputs", (), {"symbol": "VEEE"})()},
    )()
    adapter_factory = lambda _session: object()
    result = host.tick_captured_alpaca_paper_session(
        object(),
        17,
        symbol="veee",
        detector_attestation=type(
            "DetectorAttestation",
            (),
            {"decision_id": "captured-paper-test-decision"},
        )(),
        detector_policy=object(),
        adaptive_source=source,
        final_read_provider=lambda **_kwargs: object(),
        adapter_factory=adapter_factory,
    )

    assert dict(result.fsm_result) == {"ok": True, "state": "watching_live"}
    assert result.decision_at == NOW
    assert result.first_dip_final_capture_frontier is None
    assert result.scanner_snapshot_read_ids == ()
    assert result.ohlcv_read_ids == ()
    assert result.microstructure_read_ids == ()
    assert observed["tick_calls"] == 1
    assert observed["tick_decision_now"] == NOW.replace(tzinfo=None)
    assert observed["bridge"]["coordinator"] is coordinator
    assert observed["bridge"]["adaptive_source"] is source
    assert observed["scanner_bridge"]["coordinator"] is coordinator
    assert observed["ohlcv_bridge"]["coordinator"] is coordinator
    assert observed["ohlcv_bridge"]["macro_cache"] == {}
    assert observed["microstructure_bridge"]["coordinator"] is coordinator
    assert observed["tick"][1:] == (17, adapter_factory)
    assert observed["inside"] is False
    assert observed["scanner_inside"] is False
    assert observed["ohlcv_inside"] is False
    assert observed["microstructure_inside"] is False
    assert capture_host.momentum_live_runner._SIM_NOW.get() is None
    assert host.health()["captured_paper_runner_invocations_in_flight"] == ()
    host.close()


def test_unified_host_rejects_foreign_adaptive_source_before_fsm_tick(
    tmp_path, monkeypatch
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    host = capture_host.IqfeedCaptureHost(composition, wall_clock=lambda: NOW)
    host.bind()
    tick_calls: list[int] = []
    monkeypatch.setattr(
        capture_host.momentum_live_runner,
        "tick_live_session",
        lambda *_args, **_kwargs: tick_calls.append(1),
    )
    foreign = type(
        "Source",
        (),
        {"inputs": type("Inputs", (), {"symbol": "PLSM"})()},
    )()

    with pytest.raises(CaptureContractError, match="source symbol mismatch"):
        host.tick_captured_alpaca_paper_session(
            object(),
            17,
            symbol="VEEE",
            detector_attestation=object(),
            detector_policy=object(),
            adaptive_source=foreign,
            final_read_provider=lambda **_kwargs: object(),
            adapter_factory=lambda _session: object(),
        )
    assert tick_calls == []
    host.close()


def test_hot_admission_remains_explicitly_blocked_until_external_lifecycle_exists(
    tmp_path,
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )

    with pytest.raises(CaptureContractError, match="run factory is not installed"):
        composition.service.admit_hot_symbol("VEEE")
    assert composition.service.health()["pending_symbols"] == ()
    assert composition.service.health()["running_symbols"] == ()
    composition.close()


def test_generation_bound_factory_logs_stable_l1_l2_roster_without_activation(
    tmp_path,
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    composition.start_ingress()
    l1, l2 = _open_external_generations(composition)
    runtime = _shared_store_runtime(composition)
    coordinator = None
    try:
        composition.install_hot_run_factory(
            shared_store_runtime=runtime,
            startup_input_provider=_base_startup_provider(composition),
            settings_projection_sha256=SETTINGS_PROJECTION_SHA256,
        )
        health = composition.health()
        assert health["activation_authorized"] is False
        assert health["provider_socket_started"] is False
        assert health["database_or_broker_started"] is False
        assert health["hot_run_factory_installed"] is True
        assert health["external_producer_generation_roster_status"] == "valid"
        assert health["generation_bound_run_factory_ready"] is True
        assert health["hot_admission_available"] is False

        coordinator, evidence = composition.service.run_factory(
            "VEEE",
            resource_binding=composition.binding,
            pressure_controller=composition.pressure_controller,
            shared_admission_budget=composition.shared_admission_budget,
            wall_clock=lambda: NOW,
        )
        roster = evidence.config["iqfeed_external_producer_generation_roster"]
        assert evidence.config["captured_paper_settings_projection_sha256"] == (
            SETTINGS_PROJECTION_SHA256
        )
        assert evidence.config["capture_resource_binding"] == (
            composition.binding.to_record()
        )
        assert (
            sha256_json(roster)
            == evidence.config[
                "iqfeed_external_producer_generation_roster_sha256"
            ]
            == health["external_producer_generation_roster_sha256"]
        )
        assert evidence.config["iqfeed_ingress_composition_sha256"] == (
            composition.provenance.composition_sha256
        )
        assert evidence.config["iqfeed_bootstrap_manifest_sha256"] == (
            composition.preflight.manifest_sha256
        )
        assert coordinator.identity.config_sha256 == sha256_json(evidence.config)
        producers = coordinator._producer_lifecycle.producers
        assert set(producers) == {"live_fsm", "iqfeed_l1", "iqfeed_l2"}
        assert producers["iqfeed_l1"].instance_id == l1.provider_instance_id
        assert producers["iqfeed_l1"].generation == 7
        assert producers["iqfeed_l2"].instance_id == l2.provider_instance_id
        assert producers["iqfeed_l2"].generation == 11
        assert coordinator.identity.generation == 1
        assert runtime.health()["lease_count"] == 1
    finally:
        if coordinator is not None and coordinator.state.value == "created":
            coordinator.discard_unstarted(reason="bootstrap_test_cleanup")
        composition.close()
        runtime.close()
    assert runtime.health()["closed"] is True


def test_generation_bound_factory_rejects_a_different_settings_projection(
    tmp_path,
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    composition.start_ingress()
    _open_external_generations(composition)
    base = _base_startup_provider(composition)

    def drifted_provider(symbol: str, **kwargs) -> LiveCaptureRunInputs:
        inputs = base(symbol, **kwargs)
        config = dict(inputs.evidence.config)
        config["captured_paper_settings_projection_sha256"] = "8" * 64
        config_sha = sha256_json(config)
        return replace(
            inputs,
            identity=replace(inputs.identity, config_sha256=config_sha),
            evidence=replace(inputs.evidence, config=config),
            producers=tuple(
                replace(row, config_sha256=config_sha)
                for row in inputs.producers
            ),
        )

    runtime = _shared_store_runtime(composition)
    try:
        composition.install_hot_run_factory(
            shared_store_runtime=runtime,
            startup_input_provider=drifted_provider,
            settings_projection_sha256=SETTINGS_PROJECTION_SHA256,
        )
        with pytest.raises(CaptureContractError, match="escaped settings/run/resource"):
            composition.service.run_factory(
                "VEEE",
                resource_binding=composition.binding,
                pressure_controller=composition.pressure_controller,
                shared_admission_budget=composition.shared_admission_budget,
                wall_clock=lambda: NOW,
            )
        assert runtime.health()["lease_count"] == 0
    finally:
        composition.close()
        runtime.close()


def test_generation_bound_factory_missing_lane_fails_before_provider_or_writer_lease(
    tmp_path,
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    composition.start_ingress()
    composition.l1_handoff.record_connection_boundary(
        at=NOW,
        bridge_run_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "bootstrap-only-l1")),
        connection_generation=3,
        active=True,
    )
    calls: list[str] = []
    runtime = _shared_store_runtime(composition)
    try:
        composition.install_hot_run_factory(
            shared_store_runtime=runtime,
            startup_input_provider=_base_startup_provider(composition, calls=calls),
            settings_projection_sha256=SETTINGS_PROJECTION_SHA256,
        )
        health = composition.health()
        assert health["hot_admission_available"] is False
        assert health["external_producer_generation_roster_status"] == (
            "unavailable_or_invalid"
        )
        with pytest.raises(CaptureContractError, match="incomplete: iqfeed_l2"):
            composition.service.run_factory(
                "VEEE",
                resource_binding=composition.binding,
                pressure_controller=composition.pressure_controller,
                shared_admission_budget=composition.shared_admission_budget,
                wall_clock=lambda: NOW,
            )
        assert calls == []
        assert runtime.health()["lease_count"] == 0
    finally:
        composition.close()
        runtime.close()


def test_generation_change_during_packaging_fails_before_writer_lease(
    tmp_path, monkeypatch
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    composition.start_ingress()
    l1, _l2 = _open_external_generations(composition)
    changed = replace(
        l1,
        provider_generation=l1.provider_generation + 1,
        observed_at=NOW + timedelta(milliseconds=1),
    )
    reads = 0

    def changing_generation():
        nonlocal reads
        reads += 1
        return l1 if reads == 1 else changed

    monkeypatch.setattr(
        composition.l1_handoff,
        "active_producer_generation",
        changing_generation,
    )
    calls: list[str] = []
    runtime = _shared_store_runtime(composition)
    try:
        composition.install_hot_run_factory(
            shared_store_runtime=runtime,
            startup_input_provider=_base_startup_provider(composition, calls=calls),
            settings_projection_sha256=SETTINGS_PROJECTION_SHA256,
        )
        with pytest.raises(CaptureContractError, match="changed while packaging"):
            composition.service.run_factory(
                "VEEE",
                resource_binding=composition.binding,
                pressure_controller=composition.pressure_controller,
                shared_admission_budget=composition.shared_admission_budget,
                wall_clock=lambda: NOW,
            )
        assert calls == ["VEEE"]
        assert runtime.health()["lease_count"] == 0
    finally:
        composition.close()
        runtime.close()


def test_generation_change_during_run_construction_discards_writer_lease(
    tmp_path, monkeypatch
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    composition.start_ingress()
    l1, _l2 = _open_external_generations(composition)
    changed = replace(
        l1,
        provider_generation=l1.provider_generation + 1,
        observed_at=NOW + timedelta(milliseconds=1),
    )
    reads = 0

    def changing_after_construction():
        nonlocal reads
        reads += 1
        return l1 if reads <= 2 else changed

    monkeypatch.setattr(
        composition.l1_handoff,
        "active_producer_generation",
        changing_after_construction,
    )
    runtime = _shared_store_runtime(composition)
    try:
        composition.install_hot_run_factory(
            shared_store_runtime=runtime,
            startup_input_provider=_base_startup_provider(composition),
            settings_projection_sha256=SETTINGS_PROJECTION_SHA256,
        )
        with pytest.raises(CaptureContractError, match="during run construction"):
            composition.service.run_factory(
                "VEEE",
                resource_binding=composition.binding,
                pressure_controller=composition.pressure_controller,
                shared_admission_budget=composition.shared_admission_budget,
                wall_clock=lambda: NOW,
            )
        assert runtime.health()["lease_count"] == 0
    finally:
        composition.close()
        runtime.close()


def test_generation_provenance_mismatch_fails_before_base_provider(
    tmp_path, monkeypatch
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    composition.start_ingress()
    _l1, l2 = _open_external_generations(composition)
    monkeypatch.setattr(
        composition.l2_handoff,
        "active_producer_generation",
        lambda: replace(l2, bridge_source_sha256="f" * 64),
    )
    calls: list[str] = []
    runtime = _shared_store_runtime(composition)
    try:
        composition.install_hot_run_factory(
            shared_store_runtime=runtime,
            startup_input_provider=_base_startup_provider(composition, calls=calls),
            settings_projection_sha256=SETTINGS_PROJECTION_SHA256,
        )
        with pytest.raises(CaptureContractError, match="escaped preflight provenance"):
            composition.service.run_factory(
                "VEEE",
                resource_binding=composition.binding,
                pressure_controller=composition.pressure_controller,
                shared_admission_budget=composition.shared_admission_budget,
                wall_clock=lambda: NOW,
            )
        assert calls == []
        assert runtime.health()["lease_count"] == 0
    finally:
        composition.close()
        runtime.close()


def test_base_startup_roster_cannot_claim_external_iqfeed_streams(tmp_path):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    composition.start_ingress()
    _open_external_generations(composition)
    base = _base_startup_provider(composition)

    def conflicting_provider(symbol: str, **kwargs) -> LiveCaptureRunInputs:
        inputs = base(symbol, **kwargs)
        return replace(
            inputs,
            producers=(
                replace(
                    inputs.producers[0],
                    streams=(CaptureStream.IQFEED_PRINT,),
                ),
            ),
        )

    runtime = _shared_store_runtime(composition)
    try:
        composition.install_hot_run_factory(
            shared_store_runtime=runtime,
            startup_input_provider=conflicting_provider,
            settings_projection_sha256=SETTINGS_PROJECTION_SHA256,
        )
        with pytest.raises(CaptureContractError, match="conflicts with IQFeed ownership"):
            composition.service.run_factory(
                "VEEE",
                resource_binding=composition.binding,
                pressure_controller=composition.pressure_controller,
                shared_admission_budget=composition.shared_admission_budget,
                wall_clock=lambda: NOW,
            )
        assert runtime.health()["lease_count"] == 0
    finally:
        composition.close()
        runtime.close()


def test_base_startup_roster_requires_one_provider_ohlcv_owner(tmp_path):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    composition.start_ingress()
    _open_external_generations(composition)
    base = _base_startup_provider(composition)

    def missing_ohlcv_provider(symbol: str, **kwargs) -> LiveCaptureRunInputs:
        inputs = base(symbol, **kwargs)
        producer = inputs.producers[0]
        return replace(
            inputs,
            producers=(
                replace(
                    producer,
                    streams=tuple(
                        stream
                        for stream in producer.streams
                        if stream is not CaptureStream.PROVIDER_OHLCV
                    ),
                ),
            ),
        )

    runtime = _shared_store_runtime(composition)
    try:
        composition.install_hot_run_factory(
            shared_store_runtime=runtime,
            startup_input_provider=missing_ohlcv_provider,
            settings_projection_sha256=SETTINGS_PROJECTION_SHA256,
        )
        with pytest.raises(
            CaptureContractError,
            match="one provider OHLCV capture owner",
        ):
            composition.service.run_factory(
                "VEEE",
                resource_binding=composition.binding,
                pressure_controller=composition.pressure_controller,
                shared_admission_budget=composition.shared_admission_budget,
                wall_clock=lambda: NOW,
            )
        assert runtime.health()["lease_count"] == 0
    finally:
        composition.close()
        runtime.close()


def test_composition_rehash_rejects_post_preflight_artifact_drift(tmp_path):
    bundle = _make_bundle(tmp_path)
    verified = _load(bundle)
    startup_path = Path(bundle.manifest["startup_evidence"]["path"])
    startup_path.write_bytes(startup_path.read_bytes() + b" ")

    with pytest.raises(CaptureContractError, match="drifted after preflight"):
        bootstrap.prepare_iqfeed_capture_ingress(
            verified,
            pressure_sample=_pressure_sample(bundle),
            wall_clock=lambda: NOW,
        )


def test_composition_reparses_hash_bound_inputs_instead_of_trusting_mutable_preflight(
    tmp_path,
):
    bundle = _make_bundle(tmp_path)
    verified = _load(bundle)
    verified.run_configuration["writer_batch_events"] += 1

    with pytest.raises(CaptureContractError, match="drifted from manifest"):
        bootstrap.prepare_iqfeed_capture_ingress(
            verified,
            pressure_sample=_pressure_sample(bundle),
            wall_clock=lambda: NOW,
        )


def test_pressure_freshness_is_checked_after_hash_reverification(tmp_path):
    bundle = _make_bundle(tmp_path)
    wall_times = iter((NOW, NOW + timedelta(seconds=6)))

    with pytest.raises(CaptureContractError, match="stale or future-dated"):
        bootstrap.prepare_iqfeed_capture_ingress(
            _load(bundle),
            pressure_sample=_pressure_sample(bundle),
            wall_clock=lambda: next(wall_times),
        )


@pytest.mark.parametrize(
    "sample",
    [
        lambda bundle: _pressure_sample(
            bundle, observed_at=NOW - timedelta(minutes=1)
        ),
        lambda bundle: _pressure_sample(bundle, cpu_percent=85.0),
    ],
)
def test_composition_rejects_stale_or_already_pressured_host_sample(
    tmp_path, sample
):
    bundle = _make_bundle(tmp_path)
    verified = _load(bundle)

    with pytest.raises(CaptureContractError, match="pressure"):
        bootstrap.prepare_iqfeed_capture_ingress(
            verified,
            pressure_sample=sample(bundle),
            wall_clock=lambda: NOW,
        )


def test_composition_start_rolls_back_first_handoff_if_second_cannot_start(
    tmp_path, monkeypatch
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )

    def _fail_start():
        raise RuntimeError("fixture L2 start failure")

    monkeypatch.setattr(composition.l2_handoff, "start", _fail_start)
    with pytest.raises(CaptureContractError, match="failed to start atomically"):
        composition.start_ingress()
    assert composition.state is bootstrap.IqfeedIngressCompositionState.FAILED
    assert composition.l1_handoff.health()["accepting"] is False


def test_composition_start_rolls_back_both_handoffs_after_partial_second_start(
    tmp_path, monkeypatch
):
    bundle = _make_bundle(tmp_path)
    composition = bootstrap.prepare_iqfeed_capture_ingress(
        _load(bundle),
        pressure_sample=_pressure_sample(bundle),
        wall_clock=lambda: NOW,
    )
    original_start = composition.l2_handoff.start

    def _start_then_fail():
        original_start()
        raise RuntimeError("fixture failure after L2 thread start")

    monkeypatch.setattr(composition.l2_handoff, "start", _start_then_fail)
    with pytest.raises(CaptureContractError, match="failed to start atomically"):
        composition.start_ingress()
    assert composition.state is bootstrap.IqfeedIngressCompositionState.FAILED
    assert composition.l1_handoff.health()["accepting"] is False
    assert composition.l2_handoff.health()["accepting"] is False


def test_manifest_hash_is_an_external_mandatory_pin(tmp_path):
    bundle = _make_bundle(tmp_path)
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        preflight.load_iqfeed_capture_bootstrap_preflight(
            bundle.manifest_path,
            expected_manifest_sha256="f" * 64,
            allowed_read_roots=(bundle.read_root, REPO),
            allowed_write_roots=(bundle.write_root,),
            wall_clock=lambda: NOW,
            host_fingerprint_provider=lambda: HOST_FINGERPRINT,
            local_drive_check=lambda _path: True,
        )
    assert caught.value.code == "HASH_MISMATCH"


def test_live_runner_flag_must_be_explicitly_off(tmp_path):
    bundle = _make_bundle(
        tmp_path,
        mutate_startup=lambda row: row["feature_flags"].__setitem__(
            "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED", True
        ),
    )
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "BROKER_EXECUTION_FLAG_NOT_OFF"


def test_account_provider_must_match_alpaca_paper_identity(tmp_path):
    bundle = _make_bundle(
        tmp_path,
        mutate_startup=lambda row: row.__setitem__("account_provider", "other"),
    )
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "ACCOUNT_PROVIDER_MISMATCH"


@pytest.mark.parametrize(
    ("lane", "field", "value"),
    [
        ("exact_print", "provider_event_at_available", False),
        ("nbbo_quote", "provider_event_at_available", True),
    ],
)
def test_l1_exact_print_and_quote_clock_authority_cannot_be_conflated(
    tmp_path, lane, field, value
):
    bundle = _make_bundle(
        tmp_path,
        mutate_startup=lambda row: row["iqfeed_l1_clock_contract"][lane].__setitem__(
            field, value
        ),
    )
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "IQFEED_CLOCK_CONTRACT_INVALID"


def test_l2_checkpoint_cannot_claim_initial_snapshot_completion(tmp_path):
    bundle = _make_bundle(
        tmp_path,
        mutate_startup=lambda row: row["iqfeed_l2_clock_contract"][
            "checkpoint"
        ].__setitem__("initial_snapshot_complete", True),
    )
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "IQFEED_L2_CLOCK_CONTRACT_INVALID"


def test_l2_and_migration_sources_are_mandatory_hash_bound_inputs(tmp_path):
    def _remove_l2_capture(row):
        row["code_build"]["artifacts"] = [
            artifact
            for artifact in row["code_build"]["artifacts"]
            if artifact["role"] != "iqfeed_l2_capture"
        ]

    bundle = _make_bundle(tmp_path, mutate_startup=_remove_l2_capture)
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "INVALID_SOURCE_ROSTER"


def test_benchmark_must_bind_the_current_capture_sources(tmp_path):
    bundle = _make_bundle(
        tmp_path,
        mutate_resource=lambda row: row["capture_runtime_source"].__setitem__(
            "runtime_sha256", "b" * 64
        ),
    )
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "BENCHMARK_SOURCE_MISMATCH"


def test_forged_resolved_budget_is_recomputed_and_rejected(tmp_path):
    bundle = _make_bundle(
        tmp_path,
        mutate_resource=lambda row: row["resolved_resource_binding"][
            "budget"
        ].__setitem__("max_queue_events", 999_999),
    )
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "RESOURCE_BINDING_MISMATCH"


def test_stale_startup_account_evidence_fails_closed(tmp_path):
    bundle = _make_bundle(
        tmp_path,
        mutate_startup=lambda row: row.__setitem__(
            "captured_at", _iso(NOW - timedelta(hours=1))
        ),
    )
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "STALE_EVIDENCE"


def test_handoff_cannot_exceed_measured_queue_budget(tmp_path):
    bundle = _make_bundle(
        tmp_path,
        mutate_manifest=lambda row: row["handoff_configuration"]["l1"].__setitem__(
            "max_pending_events", 1_000_000
        ),
    )
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "HANDOFF_EXCEEDS_RESOURCE_BINDING"


@pytest.mark.parametrize(
    ("field", "l1_value", "l2_value"),
    [
        ("max_pending_events", 6_000, 5_000),
        ("max_gap_keys", 600, 500),
    ],
)
def test_individually_valid_handoffs_cannot_overbook_aggregate_binding(
    tmp_path, field, l1_value, l2_value
):
    def _overbook(row):
        row["handoff_configuration"]["l1"][field] = l1_value
        row["handoff_configuration"]["l2"][field] = l2_value

    bundle = _make_bundle(tmp_path, mutate_manifest=_overbook)
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "HANDOFF_EXCEEDS_RESOURCE_BINDING"


def test_handoff_byte_allocations_are_aggregate_not_per_lane(tmp_path):
    bundle = _make_bundle(tmp_path)
    each = bundle.binding.budget.async_queue_bytes // 2 + 1
    bundle.manifest["handoff_configuration"]["l1"]["max_pending_bytes"] = each
    bundle.manifest["handoff_configuration"]["l2"]["max_pending_bytes"] = each
    bundle.manifest_path, bundle.manifest_sha256 = _publish(
        bundle.read_root / "manifests", bundle.manifest
    )

    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "HANDOFF_EXCEEDS_RESOURCE_BINDING"


def test_writer_batch_must_fit_budget_remaining_after_both_handoffs(tmp_path):
    def _reserve_almost_every_event(row):
        row["handoff_configuration"]["l1"]["max_pending_events"] = 4_900
        row["handoff_configuration"]["l2"]["max_pending_events"] = 4_900

    bundle = _make_bundle(tmp_path, mutate_manifest=_reserve_almost_every_event)
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "RUN_CONFIG_EXCEEDS_RESOURCE_BINDING"


def test_integer_limits_do_not_accept_float_lookalikes(tmp_path):
    bundle = _make_bundle(
        tmp_path,
        mutate_manifest=lambda row: row["handoff_configuration"]["l1"].__setitem__(
            "max_pending_events", 1000.0
        ),
    )
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "INVALID_INTEGER"


def test_unc_startup_artifact_is_rejected_before_read(tmp_path):
    bundle = _make_bundle(
        tmp_path,
        mutate_manifest=lambda row: row["startup_evidence"].__setitem__(
            "path", r"\\server\share\startup.json"
        ),
    )
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        _load(bundle)
    assert caught.value.code == "NONLOCAL_PATH"


def test_artifact_outside_external_read_allowlist_is_rejected(tmp_path):
    bundle = _make_bundle(tmp_path)
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        preflight.load_iqfeed_capture_bootstrap_preflight(
            bundle.manifest_path,
            expected_manifest_sha256=bundle.manifest_sha256,
            allowed_read_roots=(bundle.manifest_path.parent, REPO),
            allowed_write_roots=(bundle.write_root,),
            wall_clock=lambda: NOW,
            host_fingerprint_provider=lambda: HOST_FINGERPRINT,
            local_drive_check=lambda _path: True,
        )
    assert caught.value.code == "PATH_OUTSIDE_ALLOWLIST"


def test_reparse_startup_artifact_is_rejected(tmp_path):
    bundle = _make_bundle(tmp_path)
    target = Path(bundle.manifest["startup_evidence"]["path"])
    link_dir = bundle.read_root / "links"
    link_dir.mkdir()
    link = link_dir / target.name
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("local policy does not permit creating a symlink")
    manifest = dict(bundle.manifest)
    manifest["startup_evidence"] = {
        **manifest["startup_evidence"],
        "path": str(link),
    }
    manifest_path, manifest_sha = _publish(
        bundle.read_root / "linked-manifest", manifest
    )
    with pytest.raises(preflight.BootstrapPreflightError) as caught:
        preflight.load_iqfeed_capture_bootstrap_preflight(
            manifest_path,
            expected_manifest_sha256=manifest_sha,
            allowed_read_roots=(bundle.read_root, REPO),
            allowed_write_roots=(bundle.write_root,),
            wall_clock=lambda: NOW,
            host_fingerprint_provider=lambda: HOST_FINGERPRINT,
            local_drive_check=lambda _path: True,
        )
    assert caught.value.code == "REPARSE_PATH"


def test_cli_rejection_never_claims_provider_or_database_started(tmp_path, capsys):
    bundle = _make_bundle(tmp_path)
    result = preflight.main(
        [
            "--manifest",
            str(bundle.manifest_path),
            "--manifest-sha256",
            "0" * 64,
            "--allow-read-root",
            str(bundle.read_root),
            "--allow-write-root",
            str(bundle.write_root),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["verdict"] == "BOOTSTRAP_PREFLIGHT_REJECTED"
    assert payload["provider_or_database_started"] is False
    assert payload["activation_authorized"] is False
