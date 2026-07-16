from __future__ import annotations

import ast
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import zlib

import pytest

from scripts import alpaca_paper_operational_preflight as preflight


UTC = timezone.utc
ACCOUNT_ID = "00000000-0000-0000-0000-000000000123"


def _canonical(value: object) -> bytes:
    return preflight.canonical_json_bytes(value)


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _settings(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "chili_alpaca_enabled": True,
        "chili_alpaca_paper": True,
        "chili_alpaca_expected_account_id": ACCOUNT_ID,
        "chili_alpaca_data_feed": "iex",
        "chili_momentum_equity_execution_via_alpaca_paper": True,
        "chili_momentum_paper_runner_enabled": False,
        "chili_momentum_paper_runner_scheduler_enabled": False,
        "chili_momentum_paper_runner_dev_tick_enabled": False,
        "chili_momentum_live_runner_enabled": False,
        "chili_momentum_live_runner_scheduler_enabled": False,
        "chili_momentum_live_runner_loop_enabled": False,
        "chili_momentum_live_runner_dev_tick_enabled": False,
        "chili_momentum_auto_arm_live_enabled": False,
        # The scheduler subordinate is recorded, but the false master is the
        # effective auto-arm boundary.
        "chili_momentum_auto_arm_live_scheduler_enabled": True,
    }
    values.update(overrides)
    return values


class GetOnlyAdapter:
    def __init__(
        self,
        *,
        account_id: str = ACCOUNT_ID,
        market_open: bool = False,
        positions: object = (),
        orders: object = (),
        include_block_flags: bool = True,
    ) -> None:
        self.calls: list[object] = []
        self.account_id = account_id
        self.market_open = market_open
        self.positions = positions
        self.orders = orders
        self.include_block_flags = include_block_flags

    def get_account_snapshot(self):
        self.calls.append("account")
        row = {
            "ok": True,
            "account_id": self.account_id,
            "equity": 100_000.0,
            "buying_power": 400_000.0,
            "status": "ACTIVE",
            "paper": True,
        }
        if self.include_block_flags:
            row.update(
                {
                    "account_blocked": False,
                    "trading_blocked": False,
                    "transfers_blocked": False,
                    "trade_suspended_by_user": False,
                }
            )
        return row

    def get_market_clock_snapshot(self):
        self.calls.append("clock")
        return {
            "ok": True,
            "is_open": self.market_open,
            "timestamp": "2026-07-14T20:00:00Z",
            "next_open": "2026-07-15T08:00:00Z",
            "next_close": "2026-07-15T20:00:00Z",
            "paper": True,
        }

    def list_positions(self):
        self.calls.append("positions")
        return self.positions, object()

    def list_open_orders(self, *, strict=False):
        self.calls.append(("orders", strict))
        return self.orders, object()

    # If implementation ever reaches one of these traps, the test fails before
    # a report can be mistaken for read-only evidence.
    def place_market_order(self, **_kwargs):  # pragma: no cover - safety trap
        raise AssertionError("mutation attempted")

    def place_limit_order_gtc(self, **_kwargs):  # pragma: no cover - safety trap
        raise AssertionError("mutation attempted")

    def cancel_order(self, *_args, **_kwargs):  # pragma: no cover - safety trap
        raise AssertionError("mutation attempted")

    def close_all_positions(self):  # pragma: no cover - safety trap
        raise AssertionError("mutation attempted")


def _fake_repo(root: Path) -> Path:
    package = root / "app/services/trading/momentum_neural"
    package.mkdir(parents=True)
    (root / "app/__init__.py").write_text("", encoding="utf-8")
    (package / "replay_capture_contract.py").write_text(
        "CAPTURE_CONTRACT = 'test-generation'\n", encoding="utf-8"
    )
    (package / "replay_capture_runtime.py").write_text(
        "CAPTURE_RUNTIME = 'test-generation'\n", encoding="utf-8"
    )
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "benchmark_replay_capture_runtime.py").write_text(
        "BENCHMARK_RUNTIME = 'test-generation'\n", encoding="utf-8"
    )
    (root / "app/execution.py").write_text("READY = True\n", encoding="utf-8")
    (root / "requirements.txt").write_text("zstandard\n", encoding="utf-8")
    return root


def _measurement() -> dict[str, object]:
    memory = preflight.psutil.virtual_memory()
    total_memory = int(memory.total)
    return {
        "measured_at": "2026-07-14T19:00:00Z",
        "sample_seconds": 10.0,
        "total_memory_bytes": total_memory,
        "available_memory_bytes": max(1, total_memory // 2),
        "disk_free_bytes": 200_000_000_000,
        "average_cpu_percent": 12.5,
        "sustained_append_bytes_per_second": 25_000_000.0,
        "fsync_p95_milliseconds": 1.25,
        "logical_cpu_count": int(preflight.psutil.cpu_count(logical=True) or 1),
        "host_fingerprint_sha256": preflight._current_host_fingerprint(
            total_memory
        ),
    }


def _write_benchmark(
    root: Path, repo: Path
) -> tuple[Path, dict[str, object], str]:
    from app.services.trading.momentum_neural import replay_capture_contract as contract
    from app.services.trading.momentum_neural import replay_capture_runtime as runtime

    measurement = _measurement()
    typed_values = dict(measurement)
    typed_values["measured_at"] = datetime.fromisoformat(
        str(measurement["measured_at"]).replace("Z", "+00:00")
    )
    typed_measurement = runtime.CaptureResourceMeasurement(**typed_values)
    typed_policy = runtime.CaptureBudgetPolicy(**_resource_policy())
    binding = runtime.CaptureResourceBinding.resolve(typed_measurement, typed_policy)
    binding_payload = json.loads(
        contract.canonical_json_bytes(
            {
                **binding.to_record(),
                "binding_sha256": binding.binding_sha256,
                "hashes": binding.hashes,
                "max_writer_threads": binding.budget.max_writer_threads,
            }
        ).decode("utf-8")
    )
    source_root = repo / "app/services/trading/momentum_neural"
    contract_hash = hashlib.sha256(
        (source_root / "replay_capture_contract.py").read_bytes()
    ).hexdigest()
    runtime_hash = hashlib.sha256(
        (source_root / "replay_capture_runtime.py").read_bytes()
    ).hexdigest()
    benchmark_hash = hashlib.sha256(
        (repo / "scripts/benchmark_replay_capture_runtime.py").read_bytes()
    ).hexdigest()
    health = {
        "stopped_cleanly": True,
        "last_error": None,
        "events_written": 1_000,
        "ingress": {
            "dropped": 0,
            "write_bandwidth_dropped": 0,
            "reported_gap_lost": 0,
            "post_close_submissions": 0,
        },
        "resource": {
            "fail_closed": False,
            "resource_failure_reasons": [],
            "sync": {"failures": 0, "dirty_objects": 0},
        },
    }
    shared_health = {
        **health,
        "events_written": 500,
    }
    generated_at = datetime(2026, 7, 14, 19, 0, 10, tzinfo=UTC)
    payload = {
        "benchmark_schema_version": preflight.CAPTURE_BENCHMARK_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "artifact_freshness": {
            "age_seconds_at_emit": 10.0,
            "max_age_seconds": preflight.CAPTURE_BENCHMARK_MAX_AGE_SECONDS,
            "fresh_at_emit": True,
        },
        "acceptance": {"accepted": True, "reasons": []},
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
        "capture_runtime_source": {
            "benchmark_script_sha256": benchmark_hash,
            "contract_sha256": contract_hash,
            "runtime_sha256": runtime_hash,
        },
        "environment": {
            "logical_cpu_count": measurement["logical_cpu_count"],
            "measurement_host_fingerprint_sha256": measurement[
                "host_fingerprint_sha256"
            ],
            "current_host_fingerprint_sha256": measurement[
                "host_fingerprint_sha256"
            ],
            "host_fingerprint_matches": True,
        },
        "resource_measurement": {
            **measurement,
            "measurement_sha256": typed_measurement.measurement_sha256,
            "durable_publication": {"all_verified": True},
        },
        "enqueue": {"submitted": 1_000, "accepted": 1_000},
        "writer": {"health": health},
        "parameters": {"writers": 2},
        "resolved_resource_binding": binding_payload,
        "shared_store_validation": {
            "executed": True,
            "requested_identity_count": 2,
            "identity_count": 2,
            "resource_binding_sha256": binding.binding_sha256,
            "accepted_events": 1_000,
            "writers_stopped_cleanly": True,
            "survivor_store_access_after_first_release": True,
            "writer_health": [shared_health, shared_health],
            "aggregate_admission": {
                "completed": 1_000,
                "outstanding_events": 0,
                "outstanding_bytes": 0,
                "rejections": {},
            },
        },
    }
    raw = _canonical(payload)
    digest = hashlib.sha256(raw).hexdigest()
    report_root = root / "reports"
    report_root.mkdir()
    path = report_root / f"{digest}.json"
    path.write_bytes(raw)
    return path, measurement, digest


def _resource_policy() -> dict[str, object]:
    return {
        "memory_reserve_bytes": 64 * 1024**2,
        "disk_reserve_bytes": 256 * 1024**2,
        "capture_fraction_of_memory_headroom": 0.25,
        "ring_fraction_of_capture_memory": 0.2,
        "queue_fraction_of_capture_memory": 0.2,
        "capture_fraction_of_disk_headroom": 0.1,
        "capture_fraction_of_measured_write_bandwidth": 0.5,
        "max_average_cpu_percent": 95.0,
        "capture_fraction_of_cpu_headroom": 0.9,
        "calibrated_hot_symbol_bytes": 10_000_000,
        "max_queue_events": 50_000,
        "max_ring_events": 100_000,
        "max_gap_keys": 4_096,
        "raw_retention_days": 7,
        "derived_retention_days": 90,
        "pressure_cpu_enter_percent": 92.0,
        "pressure_cpu_exit_percent": 80.0,
        "pressure_memory_enter_margin_bytes": 1 * 1024**2,
        "pressure_memory_exit_margin_bytes": 2 * 1024**2,
        "pressure_disk_enter_margin_bytes": 1 * 1024**2,
        "pressure_disk_exit_margin_bytes": 2 * 1024**2,
        "pressure_write_latency_enter_milliseconds": 100.0,
        "pressure_write_latency_exit_milliseconds": 25.0,
        "pressure_enter_samples": 3,
        "pressure_recovery_samples": 3,
        "pressure_sample_max_age_seconds": 5.0,
        "store_owner_lease_seconds": 60.0,
        "store_owner_heartbeat_seconds": 10.0,
    }


def _write_resource_binding(capture_root: Path, measurement: dict[str, object]) -> None:
    policy = _resource_policy()
    budget = preflight._resolved_budget(measurement, policy)
    record = {
        "schema_version": preflight.CAPTURE_RESOURCE_SCHEMA_VERSION,
        "measurement": measurement,
        "measurement_sha256": _sha(measurement),
        "policy": policy,
        "policy_sha256": _sha(policy),
        "budget": budget,
        "budget_sha256": _sha(budget),
    }
    raw = _canonical(record)
    digest = hashlib.sha256(raw).hexdigest()
    audit = capture_root / "resource_audits" / f"{digest}.json"
    audit.parent.mkdir(parents=True)
    audit.write_bytes(raw)


def _write_seal(
    root: Path,
    *,
    repo: Path,
    settings: dict[str, object],
    measurement: dict[str, object],
) -> Path:
    provenance = preflight.provenance_payload(
        repo_root=repo, settings_values=settings
    )
    run_id = "10000000-0000-0000-0000-000000000001"
    identity = {
        "run_id": run_id,
        "generation": 1,
        "code_build_sha256": provenance["code_build_sha256"],
        "config_sha256": provenance["config_sha256"],
        "feature_flags_sha256": provenance["feature_flags_sha256"],
        "account_identity_sha256": hashlib.sha256(ACCOUNT_ID.encode()).hexdigest(),
        "broker": "alpaca",
        "broker_environment": "paper",
    }
    identity_hash = _sha(identity)
    event = {"identity": identity, "sequence": 1, "stream": "config_snapshot"}
    event_raw = _canonical(event) + b"\n"
    event_digest = hashlib.sha256(event_raw).hexdigest()
    relative = (
        f"events/date=2026-07-14/run={run_id}/generation=1/"
        f"{event_digest}.jsonl.zlib"
    )
    compressed = zlib.compress(event_raw, level=3)
    capture_root = root / "capture-store"
    event_path = capture_root / Path(*relative.split("/"))
    event_path.parent.mkdir(parents=True)
    event_path.write_bytes(compressed)

    accumulator = "a" * 64
    empty_accumulator = "b" * 64
    close_proof = {
        "identity_sha256": identity_hash,
        "writer_count": 1,
        "writers_started": 1,
        "writers_stopped_cleanly": 1,
        "writer_errors": [],
        "ingress_submitted": 1,
        "ingress_accepted": 1,
        "ingress_dropped": 0,
        "reported_gap_lost": 0,
        "accepted_event_accumulator_sha256": accumulator,
        "gap_records_emitted": 0,
        "gap_lost_emitted": 0,
        "emitted_gap_accumulator_sha256": empty_accumulator,
        "ingress_closed": True,
        "ingress_finalized": True,
        "post_close_submissions": 0,
        "queued_events": 0,
        "queued_bytes": 0,
        "pending_gap_keys": 0,
        "submission_sequence_min": 1,
        "submission_sequence_max": 1,
        "events_written": 1,
        "written_event_accumulator_sha256": accumulator,
        "gap_records_written": 0,
        "lost_events_recorded": 0,
        "written_gap_accumulator_sha256": empty_accumulator,
        "event_chunks_written": 1,
        "gap_chunks_written": 0,
        "schema_version": "chili-replay-capture-close-proof-v1",
    }
    object_ref = {
        "kind": "event_chunk",
        "relative_path": relative,
        "sha256": event_digest,
        "record_count": 1,
        "reference_count": 1,
        "raw_bytes": len(event_raw),
        "compressed_bytes": len(compressed),
        "sequence_min": 1,
        "sequence_max": 1,
    }
    root_material = {
        "identity": identity,
        "close_proof": close_proof,
        "close_proof_sha256": _sha(close_proof),
        "objects": [object_ref],
        "event_count": 1,
        "gap_count": 0,
        "gap_lost_count": 0,
        "event_accumulator_sha256": accumulator,
        "gap_accumulator_sha256": empty_accumulator,
        "sequence_min": 1,
        "sequence_max": 1,
    }
    seal = {
        "schema_version": preflight.CAPTURE_SEAL_SCHEMA_VERSION,
        **root_material,
        "content_root_sha256": _sha(root_material),
    }
    seal_raw = _canonical(seal)
    seal_digest = hashlib.sha256(seal_raw).hexdigest()
    seal_path = (
        capture_root
        / "seals"
        / f"run={run_id}"
        / "generation=1"
        / f"{seal_digest}.json"
    )
    seal_path.parent.mkdir(parents=True)
    seal_path.write_bytes(seal_raw)
    _write_resource_binding(capture_root, measurement)
    return seal_path


def _write_coverage_request(root: Path, seal: Path) -> Path:
    seal_payload = json.loads(seal.read_text(encoding="utf-8"))
    identity_sha256 = _sha(seal_payload["identity"])
    payload = {
        "schema_version": preflight.REPLAY_COVERAGE_REQUEST_SCHEMA_VERSION,
        "expected_final_seal_sha256": seal.stem,
        "expected_identity_sha256": identity_sha256,
        "warmup_start_at": "2026-07-14T18:00:00Z",
        "decision_at": "2026-07-14T18:01:00Z",
        "exit_end_at": "2026-07-14T18:02:00Z",
        "required_streams": ["nbbo_quote"],
        "decision_id": "config-only-attack",
        "decision_checkpoint_sha256": "e" * 64,
        "required_read_ids": [],
        "symbol": "VEEE",
        "network_fallback_policy": "deny",
        "replay_driver": "ReplayV3",
    }
    path = root / "capture-coverage-request.json"
    path.write_bytes(_canonical(payload))
    return path


def _write_adaptive(
    root: Path, *, repo: Path, settings: dict[str, object]
) -> tuple[Path, str]:
    from app.services.trading.momentum_neural.adaptive_risk_runtime_contract import (
        ADAPTIVE_RISK_INPUT_CONTRACT_SHA256,
        ADAPTIVE_RISK_RESOLVER_ID,
        REQUIRED_ATOMIC_RESERVATION_DIMENSIONS,
        AdaptiveRiskRuntimeBinding,
        assess_adaptive_risk_runtime_readiness,
    )
    from app.services.trading.momentum_neural.adaptive_risk_policy import (
        RISK_PACKET_SCHEMA_VERSION,
    )
    from tests.test_adaptive_risk_policy import _policy

    policy = _policy()
    code_build_sha256 = preflight.provenance_payload(
        repo_root=repo, settings_values=settings
    )["code_build_sha256"]
    bindings = [
        AdaptiveRiskRuntimeBinding(
            surface=surface,
            resolver_id=ADAPTIVE_RISK_RESOLVER_ID,
            packet_schema_version=RISK_PACKET_SCHEMA_VERSION,
            input_contract_sha256=ADAPTIVE_RISK_INPUT_CONTRACT_SHA256,
            policy_sha256=policy.policy_sha256,
            code_build_sha256=code_build_sha256,
            strict_packet_recomputed_at_last_risk_boundary=True,
            decision_packet_persisted_content_addressed=True,
            reservation_same_transaction_as_admission=True,
            atomic_reservation_dimensions=REQUIRED_ATOMIC_RESERVATION_DIMENSIONS,
            account_identity_bound=True,
            order_idempotency_and_ownership_bound=True,
            reconciliation_bound=True,
            stale_data_fail_closed=True,
            kill_switch_bound=True,
            config_and_evidence_provenance_logged=True,
        )
        for surface in sorted(preflight.REQUIRED_ADAPTIVE_SURFACES)
    ]
    serialized_bindings = []
    for binding in bindings:
        row = asdict(binding)
        row["atomic_reservation_dimensions"] = sorted(
            row["atomic_reservation_dimensions"]
        )
        row["activation_only_dollar_caps"] = list(
            row["activation_only_dollar_caps"]
        )
        serialized_bindings.append(row)
    payload = {
        "schema_version": preflight.ADAPTIVE_READINESS_SCHEMA_VERSION,
        "policy": asdict(policy),
        "bindings": serialized_bindings,
        "readiness": assess_adaptive_risk_runtime_readiness(bindings).to_payload(),
    }
    path = root / "adaptive-readiness.json"
    path.write_bytes(_canonical(payload))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _write_history(root: Path) -> Path:
    payload = {
        "schema_version": preflight.ROSS_COVERAGE_SCHEMA_VERSION,
        "read_only": True,
        "certification_eligible": False,
        "rows": [
            {
                "label_id": "plsm_first_dip",
                "coverage_status": "coverage_unavailable",
                "coverage_reasons": ["trade_stream_starts_after_labeled_phase"],
            }
        ],
    }
    path = root / "ross-coverage.json"
    path.write_bytes(_canonical(payload))
    return path


@pytest.fixture
def complete_evidence(tmp_path: Path):
    repo = _fake_repo(tmp_path / "repo")
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    settings = _settings()
    benchmark, measurement, benchmark_sha256 = _write_benchmark(evidence_root, repo)
    seal = _write_seal(
        evidence_root,
        repo=repo,
        settings=settings,
        measurement=measurement,
    )
    coverage_request = _write_coverage_request(evidence_root, seal)
    adaptive, adaptive_sha256 = _write_adaptive(
        evidence_root, repo=repo, settings=settings
    )
    history = _write_history(evidence_root)
    return {
        "repo": repo,
        "settings": settings,
        "benchmark": benchmark,
        "benchmark_sha256": benchmark_sha256,
        "seal": seal,
        "seal_sha256": seal.stem,
        "coverage_request": coverage_request,
        "adaptive": adaptive,
        "adaptive_sha256": adaptive_sha256,
        "history": history,
    }


def _evaluate(complete_evidence, *, adapter=None, settings=None, **paths):
    return preflight.evaluate_preflight(
        adapter=adapter or GetOnlyAdapter(),
        settings_values=settings or complete_evidence["settings"],
        capture_benchmark_path=paths.get("benchmark", complete_evidence["benchmark"]),
        capture_benchmark_expected_sha256=paths.get(
            "benchmark_sha256", complete_evidence["benchmark_sha256"]
        ),
        capture_seal_path=paths.get("seal", complete_evidence["seal"]),
        capture_expected_seal_sha256=paths.get(
            "seal_sha256", complete_evidence["seal_sha256"]
        ),
        capture_coverage_request_path=paths.get(
            "coverage_request", complete_evidence["coverage_request"]
        ),
        adaptive_readiness_path=paths.get("adaptive", complete_evidence["adaptive"]),
        adaptive_expected_sha256=paths.get(
            "adaptive_sha256", complete_evidence["adaptive_sha256"]
        ),
        historical_coverage_paths=paths.get("history", [complete_evidence["history"]]),
        repo_root=complete_evidence["repo"],
        mode=paths.get("mode", "staged-alpaca-soak"),
        generated_at=datetime(2026, 7, 14, 20, 0, tzinfo=UTC),
    )


def _write_mutated_benchmark(
    original: Path,
    mutate,
) -> tuple[Path, str]:
    payload = json.loads(original.read_text(encoding="utf-8"))
    mutate(payload)
    raw = _canonical(payload)
    digest = hashlib.sha256(raw).hexdigest()
    path = original.parent / f"{digest}.json"
    path.write_bytes(raw)
    return path, digest


def _validate_benchmark_fixture(
    complete_evidence,
    *,
    path: Path | None = None,
    digest: str | None = None,
    evaluated_at: datetime | None = None,
):
    return preflight.validate_capture_benchmark(
        path or complete_evidence["benchmark"],
        repo_root=complete_evidence["repo"],
        expected_artifact_sha256=digest or complete_evidence["benchmark_sha256"],
        evaluated_at=evaluated_at or datetime(2026, 7, 14, 20, 0, tzinfo=UTC),
    )


def _write_contract_request(
    path: Path,
    *,
    request,
    final_seal_sha256: str,
    overrides: dict[str, object] | None = None,
) -> Path:
    payload: dict[str, object] = {
        "schema_version": preflight.REPLAY_COVERAGE_REQUEST_SCHEMA_VERSION,
        "expected_final_seal_sha256": final_seal_sha256,
        "expected_identity_sha256": request.expected_identity_sha256,
        "warmup_start_at": request.warmup_start_at.isoformat().replace("+00:00", "Z"),
        "decision_at": request.decision_at.isoformat().replace("+00:00", "Z"),
        "exit_end_at": request.exit_end_at.isoformat().replace("+00:00", "Z"),
        "required_streams": sorted(stream.value for stream in request.required_streams),
        "decision_id": request.decision_id,
        "decision_checkpoint_sha256": request.decision_checkpoint_sha256,
        "required_read_ids": sorted(request.required_read_ids),
        "symbol": request.symbol,
        "network_fallback_policy": "deny",
        "replay_driver": "ReplayV3",
    }
    payload.update(overrides or {})
    path.write_bytes(_canonical(payload))
    return path


@pytest.fixture
def verified_capture_evidence(tmp_path: Path):
    # Reuse the capture contract's real writer/sealer fixture so this test goes
    # through ContentAddressedCaptureStore and its private VerifiedReplayCapture
    # attestation instead of manufacturing a seal-shaped JSON document.
    from tests.test_replay_capture_contract import (
        _resource_binding,
        _sealed_passing_manifest,
    )

    request, _manifest, verified = _sealed_passing_manifest(
        tmp_path, resource_bound=True
    )
    capture_root = tmp_path / "sealed-capture"
    seal_path = (
        capture_root
        / "seals"
        / f"run={verified.identity.run_id}"
        / f"generation={verified.identity.generation}"
        / f"{verified.final_seal_sha256}.json"
    )
    binding = _resource_binding()
    measurement = asdict(binding.measurement)
    measurement["measured_at"] = binding.measurement.measured_at.isoformat().replace(
        "+00:00", "Z"
    )
    request_path = _write_contract_request(
        tmp_path / "verified-request.json",
        request=request,
        final_seal_sha256=verified.final_seal_sha256,
    )
    provenance = {
        "code_build_sha256": verified.identity.code_build_sha256,
        "config_sha256": verified.identity.config_sha256,
        "feature_flags_sha256": verified.identity.feature_flags_sha256,
        "expected_account_id_sha256": verified.identity.account_identity_sha256,
    }
    return {
        "request": request,
        "request_path": request_path,
        "verified": verified,
        "seal_path": seal_path,
        "measurement": measurement,
        "measurement_sha256": _sha(measurement),
        "provenance": provenance,
    }


@pytest.fixture
def resource_bound_v4_capture(tmp_path: Path):
    from app.services.trading.momentum_neural.replay_capture_contract import (
        CaptureClocks,
        CaptureEvent,
        CaptureRunIdentity,
        CaptureStream,
        ReplayCoverageRequest,
    )
    from app.services.trading.momentum_neural.replay_capture_runtime import (
        BoundedCaptureIngress,
        CaptureWriterWorker,
        ContentAddressedCaptureStore,
    )
    from tests.test_replay_capture_resource_gate import _binding

    binding = _binding()
    identity = CaptureRunIdentity(
        run_id="10000000-0000-0000-0000-000000000004",
        generation=4,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        feature_flags_sha256="c" * 64,
        account_identity_sha256="d" * 64,
        broker="alpaca",
        broker_environment="paper",
    )
    available_at = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)
    event = CaptureEvent(
        identity=identity,
        sequence=1,
        stream=CaptureStream.CONFIG_SNAPSHOT,
        symbol=None,
        provider="fixture",
        clocks=CaptureClocks(
            received_at=available_at - timedelta(microseconds=1),
            available_at=available_at,
        ),
        payload={"config_only": True},
    )
    capture_root = tmp_path / "v4-capture"
    store = ContentAddressedCaptureStore(
        capture_root,
        compression_codec="zlib",
        resource_binding=binding,
    )
    ingress = BoundedCaptureIngress(
        max_events=10,
        max_bytes=1_000_000,
        max_gap_keys=8,
        sustained_write_budget_bytes_per_second=(
            binding.budget.sustained_write_budget_bytes_per_second
        ),
        resource_binding=binding,
    )
    assert ingress.submit(event)
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=10,
        batch_bytes=1_000_000,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = worker.seal_run(identity)
    store.close()
    seal_path = (
        capture_root
        / "seals"
        / f"run={identity.run_id}"
        / f"generation={identity.generation}"
        / f"{seal.seal_sha256}.json"
    )
    request = ReplayCoverageRequest(
        warmup_start_at=available_at,
        decision_at=available_at,
        exit_end_at=available_at,
        required_streams=frozenset({CaptureStream.NBBO_QUOTE}),
        decision_id="missing-decision",
        decision_checkpoint_sha256="e" * 64,
        symbol="VEEE",
        expected_identity_sha256=identity.identity_sha256,
    )
    request_path = _write_contract_request(
        tmp_path / "v4-request.json",
        request=request,
        final_seal_sha256=seal.seal_sha256,
    )
    measurement = asdict(binding.measurement)
    measurement["measured_at"] = binding.measurement.measured_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert _sha(measurement) == binding.measurement.measurement_sha256
    return {
        "capture_root": capture_root,
        "seal": seal,
        "seal_path": seal_path,
        "request_path": request_path,
        "measurement": measurement,
        "provenance": {
            "code_build_sha256": identity.code_build_sha256,
            "config_sha256": identity.config_sha256,
            "feature_flags_sha256": identity.feature_flags_sha256,
            "expected_account_id_sha256": identity.account_identity_sha256,
        },
    }


def test_static_broker_surface_is_exact_get_only_allowlist():
    source = Path(preflight.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "probe_alpaca_read_only"
    )
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "adapter"
    ]
    assert {node.func.attr for node in calls} == {
        "get_account_snapshot",
        "get_market_clock_snapshot",
        "list_positions",
        "list_open_orders",
    }
    open_orders = next(node for node in calls if node.func.attr == "list_open_orders")
    assert len(open_orders.keywords) == 1
    assert open_orders.keywords[0].arg == "strict"
    assert isinstance(open_orders.keywords[0].value, ast.Constant)
    assert open_orders.keywords[0].value.value is True


def test_clean_closed_market_posture_remains_no_go_without_certifying_evidence(
    complete_evidence,
):
    adapter = GetOnlyAdapter(market_open=False)
    report = _evaluate(complete_evidence, adapter=adapter)

    assert report["ready"] is False
    assert report["execution_authorized"] is False
    assert report["profitability_certified"] is False
    assert "capture_seal_not_ready" in report["blockers"]
    assert "adaptive_runtime_not_ready" in report["blockers"]
    assert "capture_capacity_calibration_unavailable" in report["blockers"]
    assert report["checks"]["capture_benchmark"]["capacity_authority"] == (
        "diagnostic_only"
    )
    assert report["checks"]["capture_benchmark"][
        "capacity_limits_authorized"
    ] is False
    assert report["checks"]["broker_get_only"]["market_open"] is False
    assert report["checks"]["broker_get_only"]["market_closed_is_nonblocking"] is True
    assert report["checks"]["historical_replay"]["coverage_unavailable_count"] == 1
    assert report["checks"]["historical_replay"]["scored_pass_count"] == 0
    assert adapter.calls == ["account", "clock", "positions", ("orders", True)]
    assert preflight.report_exit_code(report) == 2


def test_identity_mismatch_fails_closed(complete_evidence):
    report = _evaluate(
        complete_evidence,
        adapter=GetOnlyAdapter(
            account_id="00000000-0000-0000-0000-000000000999"
        ),
    )

    assert report["ready"] is False
    assert "alpaca_account_identity_mismatch" in report["blockers"]
    assert preflight.report_exit_code(report) != 0


def test_missing_unblocked_fields_fail_closed(complete_evidence):
    report = _evaluate(
        complete_evidence,
        adapter=GetOnlyAdapter(include_block_flags=False),
    )

    assert report["ready"] is False
    assert "alpaca_account_blocked_unreadable" in report["blockers"]
    assert "alpaca_trading_blocked_unreadable" in report["blockers"]
    assert "alpaca_trade_suspended_by_user_unreadable" in report["blockers"]


def test_legacy_db_paper_runner_conflicts_with_staged_alpaca_soak(
    complete_evidence,
):
    settings = {
        **complete_evidence["settings"],
        "chili_momentum_paper_runner_enabled": True,
    }
    report = _evaluate(complete_evidence, settings=settings)

    assert report["ready"] is False
    assert "legacy_db_paper_runner_conflict" in report["blockers"]


def test_absent_required_evidence_fails_closed(complete_evidence):
    report = _evaluate(
        complete_evidence,
        benchmark=None,
        seal=None,
        adaptive=None,
    )

    assert report["ready"] is False
    assert "capture_benchmark_not_ready" in report["blockers"]
    assert "capture_seal_not_ready" in report["blockers"]
    assert "adaptive_runtime_not_ready" in report["blockers"]
    assert preflight.report_exit_code(report) == 2


def test_benchmark_requires_external_content_digest(complete_evidence):
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_benchmark_external_digest_mismatch",
    ):
        _validate_benchmark_fixture(complete_evidence, digest="f" * 64)


def test_benchmark_freshness_is_rechecked_at_consumption(complete_evidence):
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_benchmark_measurement_stale",
    ):
        _validate_benchmark_fixture(
            complete_evidence,
            evaluated_at=datetime(2026, 7, 14, 20, 0, 1, tzinfo=UTC),
        )


def test_benchmark_honors_stricter_artifact_lifetime_at_consumption(
    complete_evidence,
):
    def shorten_lifetime(payload):
        payload["artifact_freshness"]["max_age_seconds"] = 1_200.0

    path, digest = _write_mutated_benchmark(
        complete_evidence["benchmark"], shorten_lifetime
    )
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_benchmark_measurement_stale",
    ):
        _validate_benchmark_fixture(
            complete_evidence,
            path=path,
            digest=digest,
            evaluated_at=datetime(2026, 7, 14, 19, 30, 1, tzinfo=UTC),
        )


def test_benchmark_cannot_self_promote_capacity_authority(complete_evidence):
    def promote(payload):
        payload["authority"]["capacity_authority"] = "authoritative"
        payload["authority"]["hot_symbol_limit_authorized"] = True

    path, digest = _write_mutated_benchmark(
        complete_evidence["benchmark"], promote
    )
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_benchmark_capacity_authority_invalid",
    ):
        _validate_benchmark_fixture(
            complete_evidence, path=path, digest=digest
        )


def test_benchmark_rejects_false_acceptance_and_shared_rejections(
    complete_evidence,
):
    def reject(payload):
        payload["acceptance"] = {
            "accepted": False,
            "reasons": ["synthetic_rejection"],
        }

    rejected_path, rejected_digest = _write_mutated_benchmark(
        complete_evidence["benchmark"], reject
    )
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_benchmark_not_accepted",
    ):
        _validate_benchmark_fixture(
            complete_evidence,
            path=rejected_path,
            digest=rejected_digest,
        )

    def inject_shared_rejection(payload):
        payload["shared_store_validation"]["aggregate_admission"]["rejections"] = {
            "synthetic": 1
        }

    shared_path, shared_digest = _write_mutated_benchmark(
        complete_evidence["benchmark"], inject_shared_rejection
    )
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_benchmark_shared_validation_not_clean",
    ):
        _validate_benchmark_fixture(
            complete_evidence,
            path=shared_path,
            digest=shared_digest,
        )


def test_benchmark_typed_binding_tamper_fails_closed(complete_evidence):
    def tamper(payload):
        payload["resolved_resource_binding"]["max_writer_threads"] += 1

    path, digest = _write_mutated_benchmark(
        complete_evidence["benchmark"], tamper
    )
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_benchmark_resource_binding_mismatch",
    ):
        _validate_benchmark_fixture(
            complete_evidence, path=path, digest=digest
        )


def test_benchmark_current_host_and_source_hash_are_recomputed(complete_evidence):
    def foreign_host(payload):
        payload["environment"]["current_host_fingerprint_sha256"] = "e" * 64

    host_path, host_digest = _write_mutated_benchmark(
        complete_evidence["benchmark"], foreign_host
    )
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_benchmark_current_host_mismatch",
    ):
        _validate_benchmark_fixture(
            complete_evidence, path=host_path, digest=host_digest
        )

    def stale_source(payload):
        payload["capture_runtime_source"]["benchmark_script_sha256"] = "e" * 64

    source_path, source_digest = _write_mutated_benchmark(
        complete_evidence["benchmark"], stale_source
    )
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_benchmark_code_generation_mismatch",
    ):
        _validate_benchmark_fixture(
            complete_evidence, path=source_path, digest=source_digest
        )


def test_benchmark_noncanonical_or_reparse_path_fails_closed(
    complete_evidence,
    monkeypatch,
):
    canonical = complete_evidence["benchmark"].read_bytes()
    payload = json.loads(canonical.decode("utf-8"))
    noncanonical = json.dumps(payload, indent=2).encode("utf-8")
    digest = hashlib.sha256(noncanonical).hexdigest()
    path = complete_evidence["benchmark"].parent / f"{digest}.json"
    path.write_bytes(noncanonical)
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_benchmark_not_canonical",
    ):
        _validate_benchmark_fixture(
            complete_evidence, path=path, digest=digest
        )

    monkeypatch.setattr(preflight, "_path_has_reparse_component", lambda _path: True)
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_benchmark_missing_or_unsafe",
    ):
        _validate_benchmark_fixture(complete_evidence)


def test_unknown_broker_state_blocks_and_never_uses_mutation(complete_evidence):
    adapter = GetOnlyAdapter(positions=[{"symbol": "XYZ"}], orders=[object()])
    report = _evaluate(complete_evidence, adapter=adapter)

    assert report["ready"] is False
    assert "alpaca_unknown_positions_present" in report["blockers"]
    assert "alpaca_unknown_open_orders_present" in report["blockers"]
    assert adapter.calls == ["account", "clock", "positions", ("orders", True)]


def test_tampered_adaptive_hash_is_rejected(complete_evidence):
    adaptive = complete_evidence["adaptive"]
    payload = json.loads(adaptive.read_text(encoding="utf-8"))
    payload["binding_manifest_sha256"] = "not-a-hash"
    adaptive.write_bytes(_canonical(payload))

    report = _evaluate(complete_evidence)
    assert report["ready"] is False
    assert "adaptive_runtime_not_ready" in report["blockers"]


def test_config_only_hand_built_seal_cannot_pass_preflight(complete_evidence):
    report = _evaluate(complete_evidence)

    assert report["ready"] is False
    assert report["checks"]["capture_seal"] == {
        "ready": False,
        "reason": "capture_seal_not_ready",
    }
    assert "capture_seal_not_ready" in report["blockers"]


def test_wrong_external_expected_seal_sha_is_rejected(
    verified_capture_evidence,
):
    evidence = verified_capture_evidence
    with pytest.raises(
        preflight.EvidenceError,
        match="capture_seal_does_not_match_external_expected_sha",
    ):
        preflight.validate_capture_seal(
            evidence["seal_path"],
            expected_final_seal_sha256="f" * 64,
            coverage_request_path=evidence["request_path"],
            provenance=evidence["provenance"],
            benchmark_measurement=evidence["measurement"],
            benchmark_measurement_sha256=evidence["measurement_sha256"],
        )


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_real_v4_sealed_artifact_is_store_verified_without_mutating_store(
    verified_capture_evidence,
):
    evidence = verified_capture_evidence
    capture_root = evidence["seal_path"].parents[3]
    before = _tree_fingerprint(capture_root)
    summary = preflight.validate_capture_seal(
        evidence["seal_path"],
        expected_final_seal_sha256=evidence["verified"].final_seal_sha256,
        coverage_request_path=evidence["request_path"],
        provenance=evidence["provenance"],
        benchmark_measurement=evidence["measurement"],
        benchmark_measurement_sha256=evidence["measurement_sha256"],
    )

    assert _tree_fingerprint(capture_root) == before
    assert summary["verified_store_load"] is True
    assert summary["private_attestation_verified"] is True
    assert summary["read_only_certifying_loader_available"] is True
    assert summary["replay_network_fallback_count"] == 0
    assert summary["coverage_replayable"] is False
    assert "capture_run_open_or_producer_roster_unverified" in summary["coverage_reasons"]
    assert summary["hermetic_replay_v3_proven"] is False
    assert summary["ready"] is False


def test_resource_bound_v4_seal_is_read_without_mutation_but_config_only_cannot_pass(
    resource_bound_v4_capture,
):
    evidence = resource_bound_v4_capture
    before = _tree_fingerprint(evidence["capture_root"])
    summary = preflight.validate_capture_seal(
        evidence["seal_path"],
        expected_final_seal_sha256=evidence["seal"].seal_sha256,
        coverage_request_path=evidence["request_path"],
        provenance=evidence["provenance"],
        benchmark_measurement=evidence["measurement"],
        benchmark_measurement_sha256=_sha(evidence["measurement"]),
    )

    assert _tree_fingerprint(evidence["capture_root"]) == before
    assert summary["schema_version"] == preflight.CAPTURE_SEAL_SCHEMA_VERSION
    assert summary["current_resource_bound_seal_schema"] is True
    assert summary["resource_binding"]["typed_runtime_recomputed"] is True
    assert summary["verified_store_load"] is True
    assert summary["private_attestation_verified"] is True
    assert summary["coverage_replayable"] is False
    assert summary["coverage_reasons"] == [
        "capture_decision_checkpoint_missing_or_ambiguous"
    ]
    assert summary["ready"] is False


def test_missing_required_stream_is_explicitly_coverage_unavailable(
    verified_capture_evidence,
):
    evidence = verified_capture_evidence
    request = evidence["request"]
    required_streams = sorted(
        {stream.value for stream in request.required_streams} | {"ortex_snapshot"}
    )
    request_path = _write_contract_request(
        evidence["seal_path"].parents[3] / "missing-stream-request.json",
        request=request,
        final_seal_sha256=evidence["verified"].final_seal_sha256,
        overrides={"required_streams": required_streams},
    )
    summary = preflight.validate_capture_seal(
        evidence["seal_path"],
        expected_final_seal_sha256=evidence["verified"].final_seal_sha256,
        coverage_request_path=request_path,
        provenance=evidence["provenance"],
        benchmark_measurement=evidence["measurement"],
        benchmark_measurement_sha256=evidence["measurement_sha256"],
    )

    assert summary["coverage_replayable"] is False
    # A coverage request may require postdecision/session evidence beyond the
    # predecision FSM dependency profile.  That superset is valid; the absent
    # retained stream itself is the exact fail-closed reason.
    assert "fsm_dependency_profile_stream_set_mismatch" not in summary[
        "coverage_reasons"
    ]
    assert "stream_missing:ortex_snapshot" in summary["coverage_reasons"]


def test_missing_required_read_receipt_is_explicitly_coverage_unavailable(
    verified_capture_evidence,
):
    evidence = verified_capture_evidence
    request = evidence["request"]
    missing_read_id = "00000000-0000-0000-0000-000000000999"
    request_path = _write_contract_request(
        evidence["seal_path"].parents[3] / "missing-receipt-request.json",
        request=request,
        final_seal_sha256=evidence["verified"].final_seal_sha256,
        overrides={
            "required_read_ids": sorted(set(request.required_read_ids) | {missing_read_id})
        },
    )
    summary = preflight.validate_capture_seal(
        evidence["seal_path"],
        expected_final_seal_sha256=evidence["verified"].final_seal_sha256,
        coverage_request_path=request_path,
        provenance=evidence["provenance"],
        benchmark_measurement=evidence["measurement"],
        benchmark_measurement_sha256=evidence["measurement_sha256"],
    )

    assert summary["coverage_replayable"] is False
    assert "decision_checkpoint_read_set_mismatch" in summary["coverage_reasons"]
    assert f"read_receipt_missing:{missing_read_id}" in summary["coverage_reasons"]


def test_hash_shaped_adaptive_json_is_not_runtime_parity_evidence(
    complete_evidence, tmp_path: Path
):
    canned = {
        "schema_version": preflight.ADAPTIVE_READINESS_SCHEMA_VERSION,
        "ready": True,
        "reasons": [],
        "surface_reasons": {
            surface: [] for surface in sorted(preflight.REQUIRED_ADAPTIVE_SURFACES)
        },
        "common_policy_sha256": "c" * 64,
        "binding_manifest_sha256": "d" * 64,
    }
    path = tmp_path / "canned-adaptive.json"
    path.write_bytes(_canonical(canned))
    with pytest.raises(preflight.EvidenceError, match="adaptive_readiness_schema_invalid"):
        preflight.validate_adaptive_readiness(
            path,
            expected_artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            provenance=preflight.provenance_payload(
                repo_root=complete_evidence["repo"],
                settings_values=complete_evidence["settings"],
            ),
        )


def test_typed_adaptive_claims_are_recomputed_but_not_self_attesting(
    complete_evidence,
):
    summary = preflight.validate_adaptive_readiness(
        complete_evidence["adaptive"],
        expected_artifact_sha256=complete_evidence["adaptive_sha256"],
        provenance=preflight.provenance_payload(
            repo_root=complete_evidence["repo"],
            settings_values=complete_evidence["settings"],
        ),
    )

    assert summary["typed_binding_claims_recomputed"] is True
    assert summary["claimed_runtime_parity_ready"] is True
    assert summary["attested_current_runtime_bindings"] is False
    assert summary["ready"] is False
    assert "adaptive_runtime_binding_attestation_unavailable" in summary[
        "readiness_reasons"
    ]


def test_report_is_canonical_and_contains_no_account_uuid_or_credentials(
    complete_evidence,
):
    report = _evaluate(complete_evidence)
    encoded = _canonical(report)

    assert ACCOUNT_ID.encode() not in encoded
    assert b"api_key" not in encoded.lower()
    assert b"secret" not in encoded.lower()
    assert json.loads(encoded) == report
