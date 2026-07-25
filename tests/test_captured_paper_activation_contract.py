from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
import pytest

from scripts import captured_paper_readiness_evidence as readiness
from scripts import captured_paper_activation_contract as contract
from scripts import build_captured_paper_preactivation as preactivation_builder

from scripts.captured_paper_activation_contract import (
    ACTIVATION_MANIFEST_SCHEMA_VERSION,
    CAPTURE_BINDING_SCHEMA_VERSION,
    CODE_BUILD_SCHEMA_VERSION,
    IQFEED_BOOTSTRAP_MANIFEST_SCHEMA_VERSION,
    LAUNCHER_ARGUMENT_CONTRACT_SCHEMA_VERSION,
    PREACTIVATION_MANIFEST_SCHEMA_VERSION,
    CapturedPaperActivationContractError,
    VerifiedCapturedPaperPreactivation,
    finalize_captured_paper_activation,
    launcher_invocation_projection,
    load_captured_paper_activation,
    load_captured_paper_preactivation,
    sha256_json,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 5, 0, 0, tzinfo=UTC)
ACCOUNT_ID = "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f"
GENERATION = "df0d0942-bbc0-4dc7-8218-ef387a8761db"

CODE_ROLES = (
    "activation_contract",
    "activation_launcher",
    "activation_service",
    "adaptive_risk_account_lock",
    "adaptive_risk_policy",
    "adaptive_risk_request_builder",
    "adaptive_risk_reservation",
    "adaptive_risk_runtime_contract",
    "alpaca_fill_activity",
    "alpaca_fill_read_capability",
    "alpaca_paper_adapter",
    "app_config",
    "app_db",
    "app_migrations",
    "auto_arm",
    "captured_adaptive_risk_source",
    "captured_alpaca_paper_adapter",
    "captured_paper_admission",
    "captured_paper_dispatcher",
    "captured_paper_entry_intent",
    "captured_paper_fill_capture",
    "captured_paper_fill_watch",
    "captured_paper_financial_breaker",
    "captured_paper_host_cutover",
    "captured_paper_outbox",
    "captured_paper_phase_one_handoff",
    "captured_paper_positive_acceptance",
    "captured_paper_post_commit_worker",
    "captured_paper_production_material",
    "captured_paper_production_provider",
    "captured_paper_restart_inventory",
    "captured_paper_selection",
    "captured_paper_service_supervisor",
    "captured_paper_transport",
    "captured_paper_transport_worker",
    "entry_gates",
    "execution_family_registry",
    "first_dip_tape_decision",
    "first_dip_tape_policy",
    "iqfeed_capture_bootstrap",
    "iqfeed_capture_bootstrap_preflight",
    "iqfeed_capture_host",
    "iqfeed_depth_bridge",
    "iqfeed_l1_capture",
    "iqfeed_l2_capture",
    "iqfeed_trade_bridge",
    "live_replay_capture",
    "live_runner",
    "live_runner_loop",
    "replay_capture_contract",
    "replay_capture_runtime",
    "readiness_evidence",
    "runtime_environment",
    "trading_models",
)
# Keep this synthetic candidate aligned with the verifier's primary entrypoint
# roster; dependency-closure behavior is tested separately against real bytes.
CODE_ROLES = tuple(sorted(contract._REQUIRED_CODE_ROLES))

CHECKS = {
    "runtime_settings": {
        "alpaca_paper",
        "paper_credentials_present",
        "live_credentials_absent",
        "equity_only",
        "short_disabled",
        "crypto_disabled",
        "adaptive_policy_parity",
        "first_dip_candidate",
        "magic_activation_caps_absent",
    },
    "broker_account": {
        "paper",
        "status_active",
        "identity_match",
        "flat",
        "no_open_orders",
        "trading_blocked_false",
        "transfers_blocked_false",
        "account_read_fresh",
    },
    "database_schema": {
        "migration_exact",
        "idempotent_rehearsal_passed",
        "outbox_schema_present",
        "fill_settlement_schema_present",
        "post_settlement_contradiction_schema_present",
        "production_db_target_match",
    },
    "capture_host_smoke": {
        "launcher_hash_match",
        "host_hash_match",
        "trade_bridge_hash_match",
        "depth_bridge_hash_match",
        "l1_bound",
        "l2_lane_fail_closed",
        "capture_store_writable",
        "zero_silent_drops",
        "provider_health_fresh",
    },
    "focused_regressions": {
        "compile_passed",
        "targeted_tests_passed",
        "failures_zero",
        "network_calls_zero",
        "live_cash_paths_not_exercised",
    },
    "lifecycle_preflight": {
        "ownership_idempotency",
        "indeterminate_submit_retain",
        "late_fill_quarantine",
        "append_only_fill_settlement",
        "same_cid_reconciliation",
        "no_blind_repost",
    },
    "kill_switch": {"readable", "inactive", "same_account", "fresh"},
    "no_order_smoke": {
        "service_started",
        "runtime_registered",
        "paper_account_pinned",
        "provider_capture_healthy",
        "transport_disabled",
        "broker_order_count_unchanged",
        "broker_post_calls_zero",
        "live_cash_authority_absent",
    },
    "rollback_snapshot": {
        "four_tasks_captured",
        "task_xml_hashes_bound",
        "legacy_processes_captured",
        "restore_commands_validated",
        "candidate_action_hash_bound",
        "singleton_policy_bound",
    },
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _publish(path: Path, value: Any) -> tuple[Path, str]:
    raw = _canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _with_self_digest(value: dict[str, Any], field: str) -> dict[str, Any]:
    body = dict(value)
    body[f"{field}_sha256"] = sha256_json(body)
    return body


def _typed_evidence(
    kind: str,
    *,
    context: readiness.ReadinessValidationContext,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    h = lambda char: char * 64
    observed = observed_at or (NOW - timedelta(seconds=6))
    schema_version = f"{readiness.READINESS_EVIDENCE_SCHEMA_PREFIX}{kind}.v2"
    if kind == "runtime_settings":
        return {
            "schema_version": schema_version,
            "source_receipts": {
                "runtime_environment": context.runtime_environment_sha256,
                "settings_projection": context.effective_config_sha256,
                "adaptive_policy": h("d"),
            },
            "runtime_environment_sha256": context.runtime_environment_sha256,
            "settings_projection_sha256": context.effective_config_sha256,
            "execution_broker": "alpaca",
            "broker_environment": "paper",
            "execution_rail": "alpaca",
            "paper_credentials_present": True,
            "live_cash_credentials_present": False,
            "cash_broker_environment_keys_present": False,
            "equity_only": True,
            "short_authorized": False,
            "crypto_authorized": False,
            "first_dip_policy_mode": "candidate",
            "adaptive_policy_sha256": h("d"),
            "policy_surfaces": ["captured_paper", "replay_v3"],
            "activation_only_dollar_caps": [],
            "activation_only_symbol_caps": [],
        }
    if kind == "broker_account":
        return {
            "schema_version": schema_version,
            "source_receipts": {
                "paper_connection": h("1"),
                "account_read": h("2"),
                "position_census": h("3"),
                "order_census": h("4"),
            },
            "account_identity_sha256": readiness.sha256_json(
                {
                    "account_id": context.expected_account_id,
                    "broker": "alpaca",
                    "environment": "paper",
                }
            ),
            "connection_generation": f"alpaca-paper-rest:{h('1')}",
            "connection_receipt_sha256": h("1"),
            "account_status": "ACTIVE",
            "account_blocked": False,
            "trading_blocked": False,
            "transfers_blocked": False,
            "trade_suspended_by_user": False,
            "position_count": 0,
            "open_order_count": 0,
            "position_inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
            "open_order_inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
            "observed_at": observed.isoformat(),
            "paper_execution_only": True,
        }
    if kind == "database_schema":
        return {
            "schema_version": schema_version,
            "source_receipts": {
                "schema_probe": h("5"),
                "idempotent_rehearsal": h("6"),
            },
            "database_target_fingerprint": context.database_target_fingerprint,
            "migration_roster_sha256": h("5"),
            "applied_migrations_sha256": h("5"),
            "latest_migration": "348_captured_paper_executed_read_inventory",
            "migration_count": 348,
            "required_tables": list(readiness.REQUIRED_DATABASE_TABLES),
            "idempotent_rehearsal_pass_count": 2,
            "idempotent_rehearsal_failure_count": 0,
            "observed_at": observed.isoformat(),
        }
    if kind == "capture_host_smoke":
        roles = (
            "iqfeed_capture_host",
            "iqfeed_trade_bridge",
            "iqfeed_depth_bridge",
            "iqfeed_l1_capture",
            "iqfeed_l2_capture",
        )
        return {
            "schema_version": schema_version,
            "source_receipts": {
                "bootstrap_preflight": context.iqfeed_bootstrap_manifest_sha256,
                "capture_writer_health": h("7"),
                "provider_health": h("8"),
            },
            "iqfeed_bootstrap_manifest_sha256": (
                context.iqfeed_bootstrap_manifest_sha256
            ),
            "capture_store_root": context.capture_store_root,
            "source_hashes": {role: context.source_hashes[role] for role in roles},
            "l1_bound": True,
            "l2_policy": "decision_local_fail_closed",
            "capture_store_writable": True,
            "dropped_event_count": 0,
            "overflow_count": 0,
            "unreported_gap_count": 0,
            "provider_health_observed_at": observed.isoformat(),
        }
    if kind == "focused_regressions":
        return {
            "schema_version": schema_version,
            "source_receipts": {
                "compile_report": h("9"),
                "targeted_test_report": h("a"),
                "side_effect_census": h("b"),
            },
            "code_build_sha256": context.code_build_sha256,
            "compile_file_count": 51,
            "compile_failure_count": 0,
            "selected_test_count": 57,
            "passed_test_count": 57,
            "failed_test_count": 0,
            "error_test_count": 0,
            "real_network_call_count": 0,
            "live_cash_call_count": 0,
            "real_broker_post_call_count": 0,
            "completed_at": observed.isoformat(),
        }
    if kind == "lifecycle_preflight":
        return {
            "schema_version": schema_version,
            "source_receipts": {
                name: hashlib.sha256(name.encode()).hexdigest()
                for name in sorted(readiness.EXPECTED_SOURCE_RECEIPTS[kind])
            },
            "runtime_scenario_count": 6,
            "passed_scenario_count": 6,
            "failed_scenario_count": 0,
            "fake_transport_call_count": 3,
            "real_network_call_count": 0,
            "live_cash_call_count": 0,
            "indeterminate_resources_retained": True,
            "late_fill_recorded_and_quarantined": True,
            "append_only_settlement_verified": True,
            "same_cid_only": True,
            "blind_repost_count": 0,
            "completed_at": observed.isoformat(),
        }
    if kind == "kill_switch":
        return {
            "schema_version": schema_version,
            "source_receipts": {"kill_switch_query": h("c")},
            "database_target_fingerprint": context.database_target_fingerprint,
            "state_readable": True,
            "active": False,
            "state_version": 9,
            "observed_at": observed.isoformat(),
        }
    if kind == "rollback_snapshot":
        host_cutover_sha = context.source_hashes["captured_paper_host_cutover"]
        candidate_task_xml_sha = h("0")
        candidate_action_sha = readiness.sha256_json(
            {
                "schema_version": "chili.captured-paper-host-cutover-action.v1",
                "host_cutover_source_sha256": host_cutover_sha,
                "launcher_argument_contract_sha256": (
                    context.launcher_argument_contract_sha256
                ),
                "candidate_task_xml_sha256": candidate_task_xml_sha,
                "singleton_policy": "one_unified_candidate_host",
            }
        )
        return {
            "schema_version": schema_version,
            "source_receipts": {
                "task_snapshot": h("d"),
                "process_snapshot": h("e"),
                "restore_plan": h("f"),
                "candidate_action": candidate_action_sha,
            },
            "task_snapshot_sha256": h("d"),
            "scheduled_task_xml_sha256s": {
                task: hashlib.sha256(task.encode()).hexdigest()
                for task in readiness.REQUIRED_TASKS
            },
            "legacy_process_snapshot_sha256": h("e"),
            "restore_plan_sha256": h("f"),
            "host_cutover_source_sha256": host_cutover_sha,
            "launcher_argument_contract_sha256": (
                context.launcher_argument_contract_sha256
            ),
            "candidate_task_xml_sha256": candidate_task_xml_sha,
            "candidate_action_sha256": candidate_action_sha,
            "preactivation_baseline_sha256": hashlib.sha256(
                b"baseline"
            ).hexdigest(),
            "validation_mode": "PREACTIVATION_ROLLBACK_BASELINE",
            "singleton_policy": "one_unified_candidate_host",
            "host_mutation_count": 0,
            "final_validate_only_performed": False,
            "captured_at": observed.isoformat(),
        }
    raise AssertionError(kind)


def _probe_artifact_refs(
    root: Path,
    *,
    kind: str,
    context: readiness.ReadinessValidationContext,
    evidence: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    for source_name, fields in readiness._PROBE_FIELD_OWNERS[kind].items():
        artifact = readiness.build_readiness_probe_artifact(
            kind=kind,
            source_name=source_name,
            context=context,
            observations={field: evidence[field] for field in fields},
            observed_at=NOW - timedelta(seconds=6),
        )
        path, digest = _publish(
            root / "raw-probes" / kind / f"{source_name}.json",
            dict(artifact),
        )
        refs[source_name] = {
            "path": str(path.resolve()),
            "sha256": digest,
            "size_bytes": path.stat().st_size,
        }
    return refs


def _phase_one_evidence() -> dict[str, Any]:
    body = {
        "schema_version": (
            "chili.captured-paper-phase-one-restart-reconciliation.v1"
        ),
        "activation_generation": GENERATION,
        "initial_pending_count": 0,
        "remaining_pending_count": 0,
        "reconciliation_complete": True,
        "outbox_committed_count": 0,
        "decision_handoff_unavailable_count": 0,
        "outbox_committed_completion_sha256s": [],
        "decision_handoff_unavailable_completion_sha256s": [],
        "phase_two_side_effects_inferred": False,
    }
    return {**body, "receipt_sha256": sha256_json(body)}


def _restart_gate_evidence(
    *,
    code_build_sha256: str,
    config_sha256: str,
    capture_sha256: str,
    preactivation_manifest_sha256: str,
    phase_one_receipt_sha256: str,
) -> dict[str, Any]:
    connection_generation = "alpaca-paper-rest:" + "a" * 64
    binding = {
        "schema_version": "chili.captured-paper-restart-read-binding.v1",
        "purpose": "captured_paper_restart_inventory",
        "activation_generation": GENERATION,
        "activation_manifest_sha256": preactivation_manifest_sha256,
        "code_build_sha256": code_build_sha256,
        "settings_projection_sha256": config_sha256,
        "capture_receipt_sha256": capture_sha256,
        "expected_account_id": ACCOUNT_ID,
        "connection_receipt_sha256": "1" * 64,
        "adapter_connection_generation": connection_generation,
        "adapter_build_sha256": "2" * 64,
        "phase_one_reconciliation_receipt_sha256": phase_one_receipt_sha256,
    }
    binding_json = _canonical(binding).decode("utf-8")
    body = {
        "schema_version": "chili.captured-paper-restart-gate.v1",
        "account_scope": "alpaca:paper",
        "expected_account_id": ACCOUNT_ID,
        "runtime_generation": GENERATION,
        "broker_connection_generation": connection_generation,
        "broker_adapter_build_sha256": "2" * 64,
        "broker_read_binding_canonical_json": binding_json,
        "broker_read_binding_sha256": hashlib.sha256(
            binding_json.encode()
        ).hexdigest(),
        "phase_one_reconciliation_receipt_sha256": phase_one_receipt_sha256,
        "opening_open_order_census_sha256": "3" * 64,
        "opening_position_census_sha256": "4" * 64,
        "closing_position_census_sha256": "5" * 64,
        "closing_open_order_census_sha256": "6" * 64,
        "opening_restart_receipt_sha256": "7" * 64,
        "closing_restart_receipt_sha256": "8" * 64,
        "stable_inventory_projection_sha256": "9" * 64,
        "durable_inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
        "open_order_inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
        "position_inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
        "disposition": "strict_flat_first_cutover",
        "recovery_required": False,
        "new_admissions_quarantined": False,
        "exposure_decreasing_only": False,
        "broker_inventory_flat": True,
        "observed_at": (NOW - timedelta(seconds=6)).isoformat(),
        "paper_execution_only": True,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    canonical = _canonical(body).decode("utf-8")
    return {
        **body,
        "receipt_canonical_json": canonical,
        "receipt_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }


@dataclass
class Bundle:
    root: Path
    candidate: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, Any]
    preactivation_path: Path
    preactivation_sha256: str
    preactivation: dict[str, Any]
    receipts: dict[str, dict[str, Any]]
    receipt_paths: dict[str, Path]
    role_paths: dict[str, Path]
    readiness_context: readiness.ReadinessValidationContext

    def republish_manifest(self) -> None:
        self.manifest.pop("activation_manifest_sha256", None)
        self.manifest = _with_self_digest(self.manifest, "activation_manifest")
        self.manifest_path, self.manifest_sha256 = _publish(
            self.manifest_path, self.manifest
        )

    def republish_receipt(self, kind: str) -> None:
        receipt = dict(self.receipts[kind])
        receipt.pop("receipt_sha256", None)
        receipt = _with_self_digest(receipt, "receipt")
        path, digest = _publish(self.receipt_paths[kind], receipt)
        self.receipts[kind] = receipt
        self.manifest["readiness_receipts"][kind] = {
            "path": str(path),
            "sha256": digest,
        }
        self.republish_manifest()


def _bundle(tmp_path: Path) -> Bundle:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    capture_store = tmp_path / "capture-store"
    capture_store.mkdir()
    source_env = tmp_path / "legacy.env"
    source_env.write_text("DATABASE_URL=redacted-for-test\n", encoding="utf-8")
    source_env_sha = hashlib.sha256(source_env.read_bytes()).hexdigest()

    role_paths: dict[str, Path] = {}
    artifacts = []
    for role in CODE_ROLES:
        path = candidate / preactivation_builder._SOURCE_RELATIVE_PATHS[role]
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".py":
            parent = path.parent
            while parent != candidate:
                (parent / "__init__.py").touch(exist_ok=True)
                parent = parent.parent
        path.write_text(f"# {role}\n", encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        role_paths[role] = path
        artifacts.append({"role": role, "path": str(path), "sha256": digest})
    closure = contract.discover_captured_paper_local_dependency_closure(
        candidate_root=candidate,
        seed_paths=tuple(role_paths.values()),
    )
    primary_paths = set(role_paths.values())
    for module_name, path in closure.items():
        if path in primary_paths:
            continue
        artifacts.append(
            {
                "role": contract.dependency_role(module_name),
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    artifacts.sort(key=lambda row: row["role"])
    code_body = {
        "schema_version": CODE_BUILD_SCHEMA_VERSION,
        "artifacts": artifacts,
    }
    code_build_sha = sha256_json(code_body)
    code_build = {**code_body, "code_build_sha256": code_build_sha}
    config_sha = "b" * 64

    python_executable = tmp_path / "python.exe"
    python_executable.write_bytes(b"test-only-python-executable")
    python_sha = hashlib.sha256(python_executable.read_bytes()).hexdigest()
    python_dependency_root = tmp_path / "site-packages"
    python_dependency_root.mkdir()
    dependency_identity_sha = contract.python_dependency_root_identity_sha256(
        dependency_root=python_dependency_root,
        python_executable=python_executable,
        python_executable_sha256=python_sha,
    )
    no_order_output = tmp_path / "receipts" / "no-order-output.json"
    invocation_rows: dict[str, dict[str, Any]] = {}
    launcher_sha = next(
        row["sha256"] for row in artifacts if row["role"] == "activation_launcher"
    )
    service_sha = next(
        row["sha256"] for row in artifacts if row["role"] == "activation_service"
    )
    stage0_sha = next(
        row["sha256"] for row in artifacts if row["role"] == "activation_stage0"
    )
    artifact_root = tmp_path / "activation-artifacts" / GENERATION
    staged_launcher = artifact_root / launcher_sha / f"{launcher_sha}.ps1"
    staged_launcher.parent.mkdir(parents=True)
    staged_launcher.write_bytes(role_paths["activation_launcher"].read_bytes())
    staged_service = artifact_root / service_sha / f"{service_sha}.py"
    staged_service.parent.mkdir(parents=True)
    staged_service.write_bytes(role_paths["activation_service"].read_bytes())
    staged_stage0 = artifact_root / stage0_sha / f"{stage0_sha}.py"
    staged_stage0.parent.mkdir(parents=True)
    staged_stage0.write_bytes(role_paths["activation_stage0"].read_bytes())
    host_ready_receipt = artifact_root / "handshake" / "host-ready.json"
    host_ready_receipt.parent.mkdir(parents=True)
    for mode in ("ActivatePaper", "NoOrderSmoke", "ValidateOnly"):
        projection = launcher_invocation_projection(
            mode=mode,
            candidate_root=candidate,
            python_executable=python_executable,
            python_executable_sha256=python_sha,
            python_dependency_root=python_dependency_root,
            python_dependency_root_identity_sha256=dependency_identity_sha,
            allowed_read_roots=(tmp_path,),
            launcher_path=role_paths["activation_launcher"],
            launcher_sha256=launcher_sha,
            stage0_path=role_paths["activation_stage0"],
            stage0_sha256=stage0_sha,
            service_path=role_paths["activation_service"],
            service_sha256=service_sha,
            launcher_staged_path=staged_launcher,
            stage0_staged_path=staged_stage0,
            service_staged_path=staged_service,
            host_ready_receipt=(
                host_ready_receipt if mode == "ActivatePaper" else None
            ),
            no_order_receipt_output=(
                no_order_output if mode == "NoOrderSmoke" else None
            ),
        )
        invocation_rows[mode] = {
            "projection": dict(projection),
            "projection_sha256": sha256_json(projection),
        }
    launcher_arguments_path, launcher_arguments_sha = _publish(
        tmp_path / "receipts" / "launcher-arguments.json",
        {
            "schema_version": LAUNCHER_ARGUMENT_CONTRACT_SCHEMA_VERSION,
            "invocations": invocation_rows,
        },
    )

    capture_doc = {
        "schema_version": CAPTURE_BINDING_SCHEMA_VERSION,
        "verdict": "PASS",
        "activation_generation": GENERATION,
        "account_scope": "alpaca:paper",
        "expected_account_id": ACCOUNT_ID,
        "code_build_sha256": code_build_sha,
        "effective_config_sha256": config_sha,
        "live_cash_authorized": False,
        "network_fallback_allowed": False,
        "current_database_fallback_allowed": False,
    }
    capture_path, capture_sha = _publish(
        tmp_path / "receipts" / "capture.json", capture_doc
    )
    bootstrap_path, bootstrap_sha = _publish(
        tmp_path / "receipts" / "iqfeed-bootstrap.json",
        {
            "schema_version": IQFEED_BOOTSTRAP_MANIFEST_SCHEMA_VERSION,
            "test_fixture": True,
        },
    )

    source_hashes = {row["role"]: row["sha256"] for row in artifacts}
    readiness_context = readiness.ReadinessValidationContext(
        activation_generation=GENERATION,
        expected_account_id=ACCOUNT_ID,
        code_build_sha256=code_build_sha,
        effective_config_sha256=config_sha,
        capture_receipt_sha256=capture_sha,
        runtime_environment_sha256="a" * 64,
        database_target_fingerprint="c" * 64,
        iqfeed_bootstrap_manifest_sha256=bootstrap_sha,
        launcher_argument_contract_sha256=launcher_arguments_sha,
        capture_store_root=str(capture_store),
        source_hashes=source_hashes,
        allowed_read_roots=(str(tmp_path.resolve()),),
    )

    receipts: dict[str, dict[str, Any]] = {}
    receipt_paths: dict[str, Path] = {}
    receipt_refs: dict[str, dict[str, str]] = {}
    for kind, check_names in CHECKS.items():
        if kind == "no_order_smoke":
            continue
        del check_names
        receipt_captured_at = NOW - timedelta(seconds=5)
        typed_evidence = _typed_evidence(kind, context=readiness_context)
        artifact_refs = _probe_artifact_refs(
            tmp_path,
            kind=kind,
            context=readiness_context,
            evidence=typed_evidence,
        )
        receipt = dict(
            readiness.issue_readiness_receipt_v3_from_artifacts(
                kind=kind,
                context=readiness_context,
                artifact_bindings=artifact_refs,
                captured_at=receipt_captured_at,
                expires_at=receipt_captured_at
                + timedelta(seconds=contract._RECEIPT_MAX_AGE_SECONDS[kind]),
                now=NOW,
                max_age_seconds=contract._RECEIPT_MAX_AGE_SECONDS[kind],
            )
        )
        path, digest = _publish(tmp_path / "receipts" / f"{kind}.json", receipt)
        receipts[kind] = receipt
        receipt_paths[kind] = path
        receipt_refs[kind] = {"path": str(path), "sha256": digest}

    shared_manifest = {
        "activation_generation": GENERATION,
        "runtime_environment": {
            "source_env_path": str(source_env),
            "source_env_sha256": source_env_sha,
            "runtime_environment_sha256": "a" * 64,
            "effective_config_sha256": config_sha,
            "database_target_fingerprint": "c" * 64,
        },
        "code_build": code_build,
        "capture_binding": {"path": str(capture_path), "sha256": capture_sha},
        "iqfeed_bootstrap": {
            "path": str(bootstrap_path),
            "sha256": bootstrap_sha,
        },
        "cutover": {
            "candidate_root": str(candidate),
            "activation_artifact_root": str(artifact_root.parent),
            "host_ready_receipt_base": str(host_ready_receipt),
            "python_import_root": str(candidate),
            "python_executable_path": str(python_executable),
            "python_executable_sha256": python_sha,
            "python_dependency_root": str(python_dependency_root),
            "python_dependency_root_identity_sha256": dependency_identity_sha,
            "launcher_source_path": str(role_paths["activation_launcher"]),
            "launcher_source_sha256": launcher_sha,
            "launcher_path": str(staged_launcher),
            "launcher_sha256": next(
                row["sha256"] for row in artifacts if row["role"] == "activation_launcher"
            ),
            "stage0_source_path": str(role_paths["activation_stage0"]),
            "stage0_source_sha256": stage0_sha,
            "stage0_path": str(staged_stage0),
            "stage0_sha256": stage0_sha,
            "service_source_path": str(role_paths["activation_service"]),
            "service_source_sha256": service_sha,
            "service_path": str(staged_service),
            "service_sha256": service_sha,
            "launcher_arguments_path": str(launcher_arguments_path),
            "launcher_arguments_sha256": launcher_arguments_sha,
            "scheduled_tasks": [
                "CHILI-IQFeed-Depth-Bridge-Daily",
                "CHILI-IQFeed-Depth-Bridge-Logon",
                "CHILI-IQFeed-Trade-Bridge-Daily",
                "CHILI-IQFeed-Trade-Bridge-Logon",
            ],
            "singleton_policy": "one_unified_candidate_host",
            "rollback_required": True,
        },
        "capture_store_root": str(capture_store),
    }
    authority = {
        "broker": "alpaca",
        "broker_environment": "paper",
        "account_scope": "alpaca:paper",
        "expected_account_id": ACCOUNT_ID,
        "equity_long_only": True,
        "first_dip_policy_mode": "candidate",
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "short_authorized": False,
        "crypto_authorized": False,
        "real_money_authorized": False,
    }
    preactivation = {
        "schema_version": PREACTIVATION_MANIFEST_SCHEMA_VERSION,
        "generated_at": (NOW - timedelta(seconds=4)).isoformat(),
        # Mirrors the builder's bounded activation window, long enough for
        # the measured sealed-service startup. The
        # slow-smoke test below depends on receipts expiring before the
        # envelope does.
        # generated_at is NOW-4s in this fixture, so keep the synthetic
        # interval strictly inside the 20-minute production cap.
        "expires_at": (NOW + timedelta(minutes=19)).isoformat(),
        **shared_manifest,
        "authority_boundary": authority,
        "readiness_receipts": {
            kind: ref for kind, ref in receipt_refs.items() if kind != "no_order_smoke"
        },
    }
    preactivation = _with_self_digest(preactivation, "activation_manifest")
    preactivation_path, preactivation_sha = _publish(
        tmp_path / "preactivation.json", preactivation
    )

    refreshed_readiness: dict[str, dict[str, Any]] = {}
    for kind in ("broker_account", "kill_switch"):
        refreshed = dict(
            readiness.issue_readiness_receipt_v2(
                kind=kind,
                context=readiness_context,
                evidence=_typed_evidence(kind, context=readiness_context),
                captured_at=NOW - timedelta(seconds=3),
                expires_at=NOW + timedelta(seconds=20),
                now=NOW,
                max_age_seconds=30,
            )
        )
        refreshed_sha = hashlib.sha256(_canonical(refreshed)).hexdigest()
        refreshed_path, published_sha = _publish(
            tmp_path / "receipts" / "post-smoke" / f"{refreshed_sha}.json",
            refreshed,
        )
        assert published_sha == refreshed_sha
        refreshed_readiness[kind] = refreshed
        receipts[kind] = refreshed
        receipt_paths[kind] = refreshed_path
        receipt_refs[kind] = {
            "path": str(refreshed_path),
            "sha256": refreshed_sha,
        }

    phase_one_evidence = _phase_one_evidence()
    restart_gate_evidence = _restart_gate_evidence(
        code_build_sha256=code_build_sha,
        config_sha256=config_sha,
        capture_sha256=capture_sha,
        preactivation_manifest_sha256=preactivation_sha,
        phase_one_receipt_sha256=phase_one_evidence["receipt_sha256"],
    )
    no_order_receipt = _with_self_digest(
        {
            "schema_version": "chili.captured-paper-readiness.no_order_smoke.v4",
            "receipt_kind": "no_order_smoke",
            "verdict": "PASS",
            "captured_at": (NOW - timedelta(seconds=2)).isoformat(),
            "expires_at": (NOW + timedelta(seconds=20)).isoformat(),
            "activation_generation": GENERATION,
            "account_scope": "alpaca:paper",
            "expected_account_id": ACCOUNT_ID,
            "code_build_sha256": code_build_sha,
            "effective_config_sha256": config_sha,
            "capture_receipt_sha256": capture_sha,
            "preactivation_manifest_sha256": preactivation_sha,
            "phase_one_reconciliation": phase_one_evidence,
            "restart_inventory_gate": restart_gate_evidence,
            "refreshed_readiness": refreshed_readiness,
            "live_cash_authorized": False,
            "orders_submitted": False,
            "order_submission_audit": {
                "audit_generation": (
                    "9cf6d0c5-614d-449c-a1e9-c21e3643d69c"
                ),
                "before_call_count": 0,
                "after_call_count": 0,
                "call_count_delta": 0,
                "before_chain_sha256": "8" * 64,
                "after_chain_sha256": "8" * 64,
                "before_snapshot_sha256": "9" * 64,
                "after_snapshot_sha256": "9" * 64,
            },
            "checks": {
                name: True for name in sorted(CHECKS["no_order_smoke"])
            },
        },
        "receipt",
    )
    no_order_path, no_order_sha = _publish(
        tmp_path / "receipts" / "no_order_smoke.json", no_order_receipt
    )
    receipts["no_order_smoke"] = no_order_receipt
    receipt_paths["no_order_smoke"] = no_order_path
    receipt_refs["no_order_smoke"] = {
        "path": str(no_order_path),
        "sha256": no_order_sha,
    }

    manifest = json.loads(_canonical(preactivation).decode("utf-8"))
    manifest["schema_version"] = ACTIVATION_MANIFEST_SCHEMA_VERSION
    manifest["generated_at"] = (NOW - timedelta(seconds=1)).isoformat()
    manifest["expires_at"] = (NOW + timedelta(minutes=4)).isoformat()
    manifest["authority_boundary"]["paper_order_submission_authorized"] = True
    for kind in ("broker_account", "kill_switch"):
        manifest["readiness_receipts"][kind] = receipt_refs[kind]
    manifest["readiness_receipts"]["no_order_smoke"] = receipt_refs["no_order_smoke"]
    manifest["preactivation_binding"] = {
        "path": str(preactivation_path),
        "sha256": preactivation_sha,
    }
    manifest.pop("activation_manifest_sha256", None)
    manifest = _with_self_digest(manifest, "activation_manifest")
    manifest_path, manifest_sha = _publish(tmp_path / "activation.json", manifest)
    return Bundle(
        root=tmp_path,
        candidate=candidate,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        manifest=manifest,
        preactivation_path=preactivation_path,
        preactivation_sha256=preactivation_sha,
        preactivation=preactivation,
        receipts=receipts,
        receipt_paths=receipt_paths,
        role_paths=role_paths,
        readiness_context=readiness_context,
    )


def _load(bundle: Bundle):
    return load_captured_paper_activation(
        bundle.manifest_path,
        expected_manifest_sha256=bundle.manifest_sha256,
        candidate_root=bundle.candidate,
        allowed_read_roots=(bundle.root,),
        wall_clock=lambda: NOW,
    )


def _load_preactivation(bundle: Bundle) -> VerifiedCapturedPaperPreactivation:
    return load_captured_paper_preactivation(
        bundle.preactivation_path,
        expected_manifest_sha256=bundle.preactivation_sha256,
        candidate_root=bundle.candidate,
        allowed_read_roots=(bundle.root,),
        wall_clock=lambda: NOW,
    )


def test_valid_envelope_authorizes_only_fake_money_equity_paper(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)

    verified = _load(bundle)

    assert verified.expected_account_id == ACCOUNT_ID
    assert verified.report["paper_order_submission_authorized"] is True
    assert verified.report["live_cash_authorized"] is False
    assert verified.report["short_authorized"] is False
    assert verified.report["crypto_authorized"] is False
    assert verified.envelope_stage == "activation"
    assert verified.iqfeed_bootstrap_manifest_sha256 == (
        bundle.manifest["iqfeed_bootstrap"]["sha256"]
    )
    assert {
        "captured_paper_fill_capture",
        "captured_paper_production_provider",
        "captured_paper_service_supervisor",
        "captured_paper_transport_worker",
    } <= set(verified.source_paths)
    # L2 completeness is decision-local; activation requires only a loud,
    # fail-closed lane, not a fabricated global snapshot-completion claim.
    assert bundle.receipts["capture_host_smoke"]["evidence"]["l1_bound"] is True
    assert (
        bundle.receipts["capture_host_smoke"]["evidence"]["l2_policy"]
        == "decision_local_fail_closed"
    )


def test_code_byte_drift_fails_before_any_runtime_import(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle.role_paths["captured_paper_transport"].write_text(
        "# drifted after receipt\n", encoding="utf-8"
    )

    with pytest.raises(CapturedPaperActivationContractError, match="content hash mismatch"):
        _load(bundle)


def test_staged_entrypoint_drift_fails_before_runtime_import(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    staged_service = Path(bundle.manifest["cutover"]["service_path"])
    staged_service.write_text("# staged drift\n", encoding="utf-8")

    with pytest.raises(CapturedPaperActivationContractError, match="content hash mismatch"):
        _load(bundle)


def test_cutover_artifact_root_must_be_exact_generation_ancestor(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    # The staged bytes are still within this broad allowed root.  Authority
    # nevertheless requires the declared artifact root to be their exact
    # generation ancestor, not merely an ancestor that happens to contain them.
    bundle.manifest["cutover"]["activation_artifact_root"] = str(bundle.root)
    bundle.republish_manifest()

    with pytest.raises(
        CapturedPaperActivationContractError,
        match="cutover launcher/singleton/rollback binding mismatch",
    ):
        _load(bundle)


def test_live_cash_or_real_money_authority_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle.manifest["authority_boundary"]["live_cash_authorized"] = True
    bundle.republish_manifest()

    with pytest.raises(CapturedPaperActivationContractError, match="escaped Alpaca PAPER"):
        _load(bundle)


def test_stale_broker_account_receipt_is_rejected_even_if_hashes_match(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    bundle.receipts["broker_account"]["captured_at"] = (
        NOW - timedelta(seconds=31)
    ).isoformat()
    bundle.republish_receipt("broker_account")

    with pytest.raises(CapturedPaperActivationContractError, match="stale"):
        _load(bundle)


def test_generic_v1_checks_cannot_authorize_preactivation(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    bundle.receipts["broker_account"] = _with_self_digest(
        {
            "schema_version": "chili.captured-paper-readiness.broker_account.v1",
            "receipt_kind": "broker_account",
            "verdict": "PASS",
            "captured_at": (NOW - timedelta(seconds=5)).isoformat(),
            "expires_at": (NOW + timedelta(seconds=20)).isoformat(),
            "activation_generation": GENERATION,
            "account_scope": "alpaca:paper",
            "expected_account_id": ACCOUNT_ID,
            "code_build_sha256": bundle.manifest["code_build"][
                "code_build_sha256"
            ],
            "effective_config_sha256": bundle.manifest["runtime_environment"][
                "effective_config_sha256"
            ],
            "capture_receipt_sha256": bundle.manifest["capture_binding"]["sha256"],
            "live_cash_authorized": False,
            "orders_submitted": False,
            "checks": {name: True for name in sorted(CHECKS["broker_account"])},
        },
        "receipt",
    )
    bundle.republish_receipt("broker_account")

    with pytest.raises(
        CapturedPaperActivationContractError,
        match="typed readiness evidence is invalid",
    ):
        _load(bundle)


def test_rehashed_typed_runtime_evidence_cannot_restore_robinhood_rail(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    receipt = bundle.receipts["runtime_settings"]
    receipt["evidence"]["execution_rail"] = "robinhood_spot"
    receipt["evidence_sha256"] = readiness.sha256_json(receipt["evidence"])
    bundle.republish_receipt("runtime_settings")

    with pytest.raises(
        CapturedPaperActivationContractError,
        match="not derived from its raw artifacts",
    ):
        _load(bundle)


def test_rehashed_rollback_must_bind_exact_host_cutover_action_and_issuer(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    receipt = bundle.receipts["rollback_snapshot"]
    receipt["evidence"]["candidate_task_xml_sha256"] = "9" * 64
    receipt["evidence_sha256"] = readiness.sha256_json(receipt["evidence"])
    bundle.republish_receipt("rollback_snapshot")
    with pytest.raises(
        CapturedPaperActivationContractError,
        match="not derived from its raw artifacts",
    ):
        _load(bundle)

    other_root = tmp_path / "wrong-issuer"
    other_root.mkdir()
    bundle = _bundle(other_root)
    receipt = bundle.receipts["rollback_snapshot"]
    receipt["issuer_source_role"] = "activation_launcher"
    receipt["issuer_source_sha256"] = bundle.manifest["cutover"][
        "launcher_sha256"
    ]
    bundle.republish_receipt("rollback_snapshot")
    with pytest.raises(
        CapturedPaperActivationContractError,
        match="binding or authority is invalid",
    ):
        _load(bundle)


def test_false_no_order_smoke_check_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle.receipts["no_order_smoke"]["checks"]["broker_post_calls_zero"] = False
    bundle.republish_receipt("no_order_smoke")

    with pytest.raises(CapturedPaperActivationContractError, match="failed check"):
        _load(bundle)


def test_no_order_smoke_rejects_missing_stale_or_mismatched_refresh(
    tmp_path: Path,
) -> None:
    for name in ("missing", "stale", "mismatched"):
        (tmp_path / name).mkdir()
    missing = _bundle(tmp_path / "missing")
    missing.receipts["no_order_smoke"].pop("refreshed_readiness")
    missing.republish_receipt("no_order_smoke")
    with pytest.raises(CapturedPaperActivationContractError, match="keys differ"):
        _load(missing)

    stale = _bundle(tmp_path / "stale")
    stale_at = NOW - timedelta(seconds=20)
    stale_broker = dict(
        readiness.issue_readiness_receipt_v2(
            kind="broker_account",
            context=stale.readiness_context,
            evidence=_typed_evidence(
                "broker_account",
                context=stale.readiness_context,
                observed_at=stale_at,
            ),
            captured_at=stale_at,
            expires_at=NOW + timedelta(seconds=10),
            now=NOW,
            max_age_seconds=30,
        )
    )
    stale.receipts["no_order_smoke"]["expires_at"] = (
        NOW + timedelta(seconds=5)
    ).isoformat()
    stale.receipts["no_order_smoke"]["refreshed_readiness"][
        "broker_account"
    ] = stale_broker
    stale.republish_receipt("no_order_smoke")
    with pytest.raises(
        CapturedPaperActivationContractError, match="stale or misordered"
    ):
        _load(stale)

    mismatched = _bundle(tmp_path / "mismatched")
    forged = dict(
        mismatched.receipts["no_order_smoke"]["refreshed_readiness"][
            "kill_switch"
        ]
    )
    forged["expected_account_id"] = "95272674-963c-45da-8df8-822ec13fc6f0"
    forged.pop("receipt_sha256")
    forged["receipt_sha256"] = readiness.sha256_json(forged)
    mismatched.receipts["no_order_smoke"]["refreshed_readiness"][
        "kill_switch"
    ] = forged
    mismatched.republish_receipt("no_order_smoke")
    with pytest.raises(
        CapturedPaperActivationContractError, match="refresh is invalid"
    ):
        _load(mismatched)


def test_no_order_smoke_rejects_changed_adapter_submission_census(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    audit = bundle.receipts["no_order_smoke"]["order_submission_audit"]
    audit["after_call_count"] = 1
    audit["call_count_delta"] = 1
    audit["after_chain_sha256"] = "7" * 64
    audit["after_snapshot_sha256"] = "6" * 64
    bundle.republish_receipt("no_order_smoke")

    with pytest.raises(
        CapturedPaperActivationContractError,
        match="unchanged exact adapter submission census",
    ):
        _load(bundle)


def test_no_order_smoke_rejects_content_addressed_owned_recovery_gate(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    gate = dict(bundle.receipts["no_order_smoke"]["restart_inventory_gate"])
    gate.pop("receipt_canonical_json")
    gate.pop("receipt_sha256")
    gate.update(
        {
            "disposition": "owned_restart_recovery",
            "recovery_required": True,
            "new_admissions_quarantined": True,
            "exposure_decreasing_only": True,
        }
    )
    canonical = _canonical(gate).decode("utf-8")
    bundle.receipts["no_order_smoke"]["restart_inventory_gate"] = {
        **gate,
        "receipt_canonical_json": canonical,
        "receipt_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }
    bundle.republish_receipt("no_order_smoke")

    with pytest.raises(
        CapturedPaperActivationContractError,
        match="not strict-flat or activation-bound",
    ):
        _load(bundle)


def test_preactivation_is_typed_no_order_authority_and_final_loader_rejects_it(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)

    preactivation = _load_preactivation(bundle)

    assert isinstance(preactivation, VerifiedCapturedPaperPreactivation)
    assert preactivation.paper_order_submission_authorized is False
    assert preactivation.report["paper_order_submission_authorized"] is False
    assert preactivation.envelope_stage == "preactivation"
    assert "no_order_smoke" not in preactivation.receipt_paths
    assert preactivation.iqfeed_bootstrap_manifest_path == Path(
        bundle.preactivation["iqfeed_bootstrap"]["path"]
    )
    with pytest.raises(
        CapturedPaperActivationContractError, match="schema is unsupported"
    ):
        load_captured_paper_activation(
            bundle.preactivation_path,
            expected_manifest_sha256=bundle.preactivation_sha256,
            candidate_root=bundle.candidate,
            allowed_read_roots=(bundle.root,),
            wall_clock=lambda: NOW,
        )


def test_preactivation_rejects_order_authority_and_no_order_receipt_injection(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    document = json.loads(_canonical(bundle.preactivation).decode("utf-8"))
    document["authority_boundary"]["paper_order_submission_authorized"] = True
    document.pop("activation_manifest_sha256")
    document = _with_self_digest(document, "activation_manifest")
    path, digest = _publish(tmp_path / "bad-preactivation.json", document)

    with pytest.raises(CapturedPaperActivationContractError, match="authority escaped"):
        load_captured_paper_preactivation(
            path,
            expected_manifest_sha256=digest,
            candidate_root=bundle.candidate,
            allowed_read_roots=(bundle.root,),
            wall_clock=lambda: NOW,
        )

    document = json.loads(_canonical(bundle.preactivation).decode("utf-8"))
    document["readiness_receipts"]["no_order_smoke"] = bundle.manifest[
        "readiness_receipts"
    ]["no_order_smoke"]
    document.pop("activation_manifest_sha256")
    document = _with_self_digest(document, "activation_manifest")
    path, digest = _publish(tmp_path / "injected-preactivation.json", document)
    with pytest.raises(CapturedPaperActivationContractError, match="keys differ"):
        load_captured_paper_preactivation(
            path,
            expected_manifest_sha256=digest,
            candidate_root=bundle.candidate,
            allowed_read_roots=(bundle.root,),
            wall_clock=lambda: NOW,
        )


def test_local_finalizer_hash_binds_no_order_receipt_and_preactivation(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    output_root = tmp_path / "final-objects"
    output_root.mkdir()
    preactivation = _load_preactivation(bundle)
    no_order_ref = bundle.manifest["readiness_receipts"]["no_order_smoke"]

    built = finalize_captured_paper_activation(
        preactivation,
        no_order_smoke_path=no_order_ref["path"],
        no_order_smoke_sha256=no_order_ref["sha256"],
        output_root=output_root,
        allowed_read_roots=(bundle.root,),
        generated_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=15),
        wall_clock=lambda: NOW,
    )

    assert built.manifest_path.name == f"{built.manifest_sha256}.json"
    assert built.preactivation_manifest_sha256 == bundle.preactivation_sha256
    assert built.no_order_smoke_sha256 == no_order_ref["sha256"]
    assert built.verified.paper_order_submission_authorized is True
    assert built.verified.manifest["preactivation_binding"] == {
        "path": str(bundle.preactivation_path),
        "sha256": bundle.preactivation_sha256,
    }
    assert built.verified.capture_store_root == preactivation.capture_store_root
    assert (
        built.verified.iqfeed_bootstrap_manifest_sha256
        == preactivation.iqfeed_bootstrap_manifest_sha256
    )


def test_finalizer_rejects_preexisting_generation_handshake_artifact(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    output_root = tmp_path / "final-objects"
    output_root.mkdir()
    preactivation = _load_preactivation(bundle)
    host_ready = Path(
        preactivation.manifest["cutover"]["host_ready_receipt_base"]
    )
    Path(f"{host_ready}.permit.json").write_text("{}", encoding="utf-8")
    no_order_ref = bundle.manifest["readiness_receipts"]["no_order_smoke"]

    with pytest.raises(
        CapturedPaperActivationContractError,
        match="requires a new generation-owned host handshake",
    ):
        finalize_captured_paper_activation(
            preactivation,
            no_order_smoke_path=no_order_ref["path"],
            no_order_smoke_sha256=no_order_ref["sha256"],
            output_root=output_root,
            allowed_read_roots=(bundle.root,),
            generated_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=15),
            wall_clock=lambda: NOW,
        )


def test_slow_no_order_smoke_requires_and_accepts_post_shutdown_refresh(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    output_root = tmp_path / "slow-final-objects"
    output_root.mkdir()
    preactivation = _load_preactivation(bundle)
    # 2026-07-17: the mid-flow receipt class is uniform (10 minutes), so a
    # smoke that outlives broker/kill authority also outlives every other
    # probe receipt — past the class boundary the whole envelope is dead
    # (fail-closed), and within it a slow smoke still finalizes via the
    # mandatory post-shutdown refresh.  Exercise both sides.
    stale_probe_horizon = NOW + timedelta(seconds=615)
    smoke_completed_at = NOW + timedelta(seconds=320)

    # Past the class boundary: the original receipts (captured NOW-5s,
    # bounded receipt window) are no longer current activation authority.
    assert all(
        datetime.fromisoformat(
            json.loads(Path(bundle.preactivation["readiness_receipts"][kind]["path"]).read_text())["expires_at"]
        )
        < stale_probe_horizon
        for kind in ("broker_account", "kill_switch")
    )
    with pytest.raises(CapturedPaperActivationContractError, match="typed readiness"):
        load_captured_paper_preactivation(
            bundle.preactivation_path,
            expected_manifest_sha256=bundle.preactivation_sha256,
            candidate_root=bundle.candidate,
            allowed_read_roots=(bundle.root,),
            wall_clock=lambda: stale_probe_horizon,
        )

    refreshed: dict[str, dict[str, Any]] = {}
    refreshed_at = smoke_completed_at - timedelta(seconds=1)
    for kind in ("broker_account", "kill_switch"):
        refreshed[kind] = dict(
            readiness.issue_readiness_receipt_v2(
                kind=kind,
                context=bundle.readiness_context,
                evidence=_typed_evidence(
                    kind,
                    context=bundle.readiness_context,
                    observed_at=refreshed_at,
                ),
                captured_at=refreshed_at,
                expires_at=refreshed_at + timedelta(seconds=30),
                now=smoke_completed_at,
                max_age_seconds=30,
            )
        )
    no_order = bundle.receipts["no_order_smoke"]
    no_order["captured_at"] = smoke_completed_at.isoformat()
    no_order["expires_at"] = (
        smoke_completed_at + timedelta(seconds=20)
    ).isoformat()
    no_order["refreshed_readiness"] = refreshed
    gate = dict(no_order["restart_inventory_gate"])
    gate.pop("receipt_canonical_json")
    gate.pop("receipt_sha256")
    gate["observed_at"] = (
        smoke_completed_at - timedelta(seconds=2)
    ).isoformat()
    gate_canonical = _canonical(gate).decode("utf-8")
    no_order["restart_inventory_gate"] = {
        **gate,
        "receipt_canonical_json": gate_canonical,
        "receipt_sha256": hashlib.sha256(gate_canonical.encode()).hexdigest(),
    }
    bundle.republish_receipt("no_order_smoke")
    no_order_ref = bundle.manifest["readiness_receipts"]["no_order_smoke"]

    built = finalize_captured_paper_activation(
        preactivation,
        no_order_smoke_path=no_order_ref["path"],
        no_order_smoke_sha256=no_order_ref["sha256"],
        output_root=output_root,
        allowed_read_roots=(bundle.root,),
        generated_at=smoke_completed_at + timedelta(seconds=1),
        expires_at=smoke_completed_at + timedelta(seconds=15),
        wall_clock=lambda: smoke_completed_at + timedelta(seconds=1),
    )

    assert built.verified.paper_order_submission_authorized is True
    assert all(
        built.verified.receipt_paths[kind].name
        == f"{built.verified.receipt_hashes[kind]}.json"
        for kind in ("broker_account", "kill_switch")
    )


def test_final_rejects_receipt_substitution_and_capture_store_drift(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    substitute = tmp_path / "receipts" / "broker-account-copy.json"
    substitute.write_bytes(bundle.receipt_paths["broker_account"].read_bytes())
    document = json.loads(_canonical(bundle.manifest).decode("utf-8"))
    document["readiness_receipts"]["broker_account"]["path"] = str(substitute)
    document.pop("activation_manifest_sha256")
    document = _with_self_digest(document, "activation_manifest")
    path, digest = _publish(tmp_path / "receipt-substitution.json", document)
    with pytest.raises(
        CapturedPaperActivationContractError,
        match="content-addressed|HASH_MISMATCH",
    ):
        load_captured_paper_activation(
            path,
            expected_manifest_sha256=digest,
            candidate_root=bundle.candidate,
            allowed_read_roots=(bundle.root,),
            wall_clock=lambda: NOW,
        )

    other_capture_store = tmp_path / "other-capture-store"
    other_capture_store.mkdir()
    document = json.loads(_canonical(bundle.manifest).decode("utf-8"))
    document["capture_store_root"] = str(other_capture_store)
    document.pop("activation_manifest_sha256")
    document = _with_self_digest(document, "activation_manifest")
    path, digest = _publish(tmp_path / "capture-store-drift.json", document)
    with pytest.raises(
        CapturedPaperActivationContractError,
        match="changed material|typed readiness evidence",
    ):
        load_captured_paper_activation(
            path,
            expected_manifest_sha256=digest,
            candidate_root=bundle.candidate,
            allowed_read_roots=(bundle.root,),
            wall_clock=lambda: NOW,
        )


def test_final_rejects_no_order_smoke_captured_before_preactivation(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    captured_at = NOW - timedelta(seconds=11)
    bundle.receipts["no_order_smoke"]["captured_at"] = captured_at.isoformat()
    gate = dict(bundle.receipts["no_order_smoke"]["restart_inventory_gate"])
    gate.pop("receipt_canonical_json")
    gate.pop("receipt_sha256")
    gate["observed_at"] = (captured_at - timedelta(seconds=1)).isoformat()
    canonical = _canonical(gate).decode("utf-8")
    bundle.receipts["no_order_smoke"]["restart_inventory_gate"] = {
        **gate,
        "receipt_canonical_json": canonical,
        "receipt_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }
    bundle.republish_receipt("no_order_smoke")

    with pytest.raises(
        CapturedPaperActivationContractError,
        match="chronology escaped|stale or misordered",
    ):
        _load(bundle)


def test_final_rejects_no_order_smoke_bound_to_another_preactivation(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    bundle.receipts["no_order_smoke"]["preactivation_manifest_sha256"] = "e" * 64
    bundle.republish_receipt("no_order_smoke")

    with pytest.raises(
        CapturedPaperActivationContractError, match="readiness binding mismatch"
    ):
        _load(bundle)


def test_final_rejects_iqfeed_bootstrap_substitution_after_no_order_smoke(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    replacement_path, replacement_sha = _publish(
        tmp_path / "receipts" / "replacement-bootstrap.json",
        {
            "schema_version": IQFEED_BOOTSTRAP_MANIFEST_SCHEMA_VERSION,
            "test_fixture": "different-valid-bootstrap",
        },
    )
    bundle.manifest["iqfeed_bootstrap"] = {
        "path": str(replacement_path),
        "sha256": replacement_sha,
    }
    bundle.republish_manifest()

    with pytest.raises(
        CapturedPaperActivationContractError,
        match="changed material|typed readiness evidence",
    ):
        _load(bundle)
