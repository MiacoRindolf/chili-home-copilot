from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
import uuid

import pytest

from scripts import build_captured_paper_preactivation as builder
from scripts import captured_paper_activation_contract as contract
from scripts import captured_paper_readiness_evidence as readiness


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 13, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[1]
ACCOUNT_ID = "2b7ffb65-b682-4c86-af7c-0d70f44fa2c2"
GENERATION = "2307d717-48da-4bdc-8c1f-682d1d29bcf8"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _publish(path: Path, value: Any) -> tuple[Path, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _canonical(value)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _ref(path: Path, digest: str) -> dict[str, str]:
    return {"path": str(path), "sha256": digest}


def _self_digest(value: dict[str, Any], field: str) -> dict[str, Any]:
    result = dict(value)
    result[f"{field}_sha256"] = contract.sha256_json(result)
    return result


def _typed_evidence(
    kind: str,
    *,
    context: readiness.ReadinessValidationContext,
) -> dict[str, Any]:
    h = lambda char: char * 64
    schema = f"{readiness.READINESS_EVIDENCE_SCHEMA_PREFIX}{kind}.v2"
    observed = (NOW - timedelta(seconds=3)).isoformat()
    sources = {
        name: hashlib.sha256(f"{kind}:{name}".encode()).hexdigest()
        for name in readiness.EXPECTED_SOURCE_RECEIPTS[kind]
    }
    if kind == "runtime_settings":
        sources.update(
            runtime_environment=context.runtime_environment_sha256,
            settings_projection=context.effective_config_sha256,
            adaptive_policy=h("4"),
        )
        return {
            "schema_version": schema,
            "source_receipts": sources,
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
            "adaptive_policy_sha256": h("4"),
            "policy_surfaces": ["captured_paper", "replay_v3"],
            "activation_only_dollar_caps": [],
            "activation_only_symbol_caps": [],
        }
    if kind == "broker_account":
        sources["paper_connection"] = h("5")
        empty = hashlib.sha256(b"[]").hexdigest()
        return {
            "schema_version": schema,
            "source_receipts": sources,
            "account_identity_sha256": readiness.sha256_json(
                {
                    "account_id": context.expected_account_id,
                    "broker": "alpaca",
                    "environment": "paper",
                }
            ),
            "connection_generation": f"alpaca-paper-rest:{h('5')}",
            "connection_receipt_sha256": h("5"),
            "account_status": "ACTIVE",
            "account_blocked": False,
            "trading_blocked": False,
            "transfers_blocked": False,
            "trade_suspended_by_user": False,
            "position_count": 0,
            "open_order_count": 0,
            "position_inventory_sha256": empty,
            "open_order_inventory_sha256": empty,
            "observed_at": observed,
            "paper_execution_only": True,
        }
    if kind == "database_schema":
        sources["schema_probe"] = h("6")
        return {
            "schema_version": schema,
            "source_receipts": sources,
            "database_target_fingerprint": context.database_target_fingerprint,
            "migration_roster_sha256": h("6"),
            "applied_migrations_sha256": h("6"),
            "latest_migration": "348_captured_paper_executed_read_inventory",
            "migration_count": 348,
            "required_tables": list(readiness.REQUIRED_DATABASE_TABLES),
            "idempotent_rehearsal_pass_count": 2,
            "idempotent_rehearsal_failure_count": 0,
            "observed_at": observed,
        }
    if kind == "capture_host_smoke":
        sources["bootstrap_preflight"] = context.iqfeed_bootstrap_manifest_sha256
        roles = (
            "iqfeed_capture_host",
            "iqfeed_trade_bridge",
            "iqfeed_depth_bridge",
            "iqfeed_l1_capture",
            "iqfeed_l2_capture",
        )
        return {
            "schema_version": schema,
            "source_receipts": sources,
            "iqfeed_bootstrap_manifest_sha256": context.iqfeed_bootstrap_manifest_sha256,
            "capture_store_root": context.capture_store_root,
            "source_hashes": {role: context.source_hashes[role] for role in roles},
            "l1_bound": True,
            "l2_policy": "decision_local_fail_closed",
            "capture_store_writable": True,
            "dropped_event_count": 0,
            "overflow_count": 0,
            "unreported_gap_count": 0,
            "provider_health_observed_at": observed,
        }
    if kind == "focused_regressions":
        return {
            "schema_version": schema,
            "source_receipts": sources,
            "code_build_sha256": context.code_build_sha256,
            "compile_file_count": 52,
            "compile_failure_count": 0,
            "selected_test_count": 57,
            "passed_test_count": 57,
            "failed_test_count": 0,
            "error_test_count": 0,
            "real_network_call_count": 0,
            "live_cash_call_count": 0,
            "real_broker_post_call_count": 0,
            "completed_at": observed,
        }
    if kind == "lifecycle_preflight":
        return {
            "schema_version": schema,
            "source_receipts": sources,
            "runtime_scenario_count": 6,
            "passed_scenario_count": 6,
            "failed_scenario_count": 0,
            "fake_transport_call_count": 2,
            "real_network_call_count": 0,
            "live_cash_call_count": 0,
            "indeterminate_resources_retained": True,
            "late_fill_recorded_and_quarantined": True,
            "append_only_settlement_verified": True,
            "same_cid_only": True,
            "blind_repost_count": 0,
            "completed_at": observed,
        }
    if kind == "kill_switch":
        return {
            "schema_version": schema,
            "source_receipts": sources,
            "database_target_fingerprint": context.database_target_fingerprint,
            "state_readable": True,
            "active": False,
            "state_version": 1,
            "observed_at": observed,
        }
    if kind == "rollback_snapshot":
        host_cutover_sha = context.source_hashes["captured_paper_host_cutover"]
        candidate_task_xml_sha = h("9")
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
        sources.update(
            process_snapshot=h("7"),
            restore_plan=h("8"),
            candidate_action=candidate_action_sha,
        )
        return {
            "schema_version": schema,
            "source_receipts": sources,
            "task_snapshot_sha256": h("6"),
            "scheduled_task_xml_sha256s": {
                task: hashlib.sha256(task.encode()).hexdigest()
                for task in readiness.REQUIRED_TASKS
            },
            "legacy_process_snapshot_sha256": h("7"),
            "restore_plan_sha256": h("8"),
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
            "captured_at": observed,
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
            observed_at=NOW - timedelta(seconds=3),
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


def _bundle(tmp_path: Path) -> dict[str, Any]:
    evidence = tmp_path / "evidence"
    output = tmp_path / "output"
    capture_store = tmp_path / "capture-store"
    output.mkdir()
    capture_store.mkdir()
    activation_artifacts = tmp_path / "activation-artifacts"
    activation_artifacts.mkdir()

    inventory = builder.inventory_captured_paper_code(
        REPO, allowed_read_roots=(REPO, tmp_path)
    )
    source_env = evidence / "captured-paper.env"
    source_env.parent.mkdir(parents=True)
    source_env.write_text(
        "CHILI_CAPTURED_PAPER_CONFIG_ISOLATED=true\n", encoding="utf-8"
    )
    source_sha = hashlib.sha256(source_env.read_bytes()).hexdigest()
    fingerprints = {
        "DATABASE_URL": "1" * 64,
        "CHILI_ALPACA_API_KEY": "2" * 64,
        "CHILI_ALPACA_API_SECRET": "3" * 64,
    }
    runtime_body = {
        "schema_version": builder.RUNTIME_ENV_RECEIPT_SCHEMA_VERSION,
        "source_path": str(source_env.resolve()),
        "source_sha256": source_sha,
        "expected_account_id": ACCOUNT_ID,
        "first_dip_policy_mode": "candidate",
        "effective_config": {
            "CHILI_CAPTURED_PAPER_CONFIG_ISOLATED": "true",
            "CHILI_ALPACA_ENABLED": "true",
            "CHILI_ALPACA_PAPER": "true",
            "CHILI_ALPACA_EXPECTED_ACCOUNT_ID": ACCOUNT_ID,
            "CHILI_AUTOTRADER_USER_ID": "7",
            "CHILI_EQUITY_EXECUTION_RAIL": "alpaca",
            "CHILI_MOMENTUM_EQUITY_EXECUTION_VIA_ALPACA_PAPER": "true",
            "CHILI_MOMENTUM_CRYPTO_EXECUTION_VIA_ALPACA_PAPER": "false",
            "CHILI_MOMENTUM_FIRST_DIP_RECLAIM_POLICY_MODE": "candidate",
            "CHILI_MOMENTUM_SHORT_ENABLED": "false",
            "CHILI_MOMENTUM_SHORT_LANE_ENABLED": "false",
        },
        "secret_fingerprints": fingerprints,
    }
    runtime = {
        **runtime_body,
        "removed_forbidden_keys": ["ALPACA_API_KEY"],
        "configuration_sha256": contract.sha256_json(runtime_body),
    }
    runtime_path, runtime_sha = _publish(evidence / "runtime.json", runtime)

    projection_body = {
        "schema_version": builder.SETTINGS_PROJECTION_SCHEMA_VERSION,
        "runtime_environment_sha256": runtime["configuration_sha256"],
        "settings": {
            "chili_alpaca_enabled": True,
            "chili_alpaca_paper": True,
            "chili_alpaca_expected_account_id": ACCOUNT_ID,
            "chili_autotrader_user_id": 7,
            "chili_equity_execution_rail": "alpaca",
            "chili_momentum_auto_arm_equity_only": True,
            "chili_momentum_auto_arm_crypto_only": False,
            "chili_momentum_first_dip_reclaim_policy_mode": "candidate",
            "chili_momentum_short_enabled": False,
            "chili_momentum_short_lane_enabled": False,
        },
        "adaptive_risk_policy": {"policy_sha256": "4" * 64},
        "captured_paper_operational_policy": {"time_in_force": "day"},
        "captured_paper_config_isolated": True,
        "paper_credentials_present": True,
        "live_cash_credentials_present": False,
        "cash_broker_environment_keys_present": False,
    }
    projection = {
        **projection_body,
        "settings_projection_sha256": contract.sha256_json(projection_body),
    }
    projection_path, projection_sha = _publish(
        evidence / "settings-projection.json", projection
    )

    capture = {
        "schema_version": contract.CAPTURE_BINDING_SCHEMA_VERSION,
        "verdict": "PASS",
        "activation_generation": GENERATION,
        "account_scope": "alpaca:paper",
        "expected_account_id": ACCOUNT_ID,
        "code_build_sha256": inventory.code_build_sha256,
        "effective_config_sha256": projection["settings_projection_sha256"],
        "live_cash_authorized": False,
        "network_fallback_allowed": False,
        "current_database_fallback_allowed": False,
    }
    capture_path, capture_sha = _publish(evidence / "capture.json", capture)
    bootstrap_path, bootstrap_sha = _publish(
        evidence / "iqfeed-bootstrap.json",
        {
            "schema_version": contract.IQFEED_BOOTSTRAP_MANIFEST_SCHEMA_VERSION,
            "fixture": True,
        },
    )
    python_executable = evidence / "python.exe"
    python_executable.write_bytes(b"test-only-python-executable")
    python_dependency_root = evidence / "site-packages"
    python_dependency_root.mkdir()
    launcher_arguments_document = builder.build_launcher_argument_contract_offline(
        activation_generation=GENERATION,
        activation_artifact_root=activation_artifacts,
        candidate_root=REPO,
        python_executable=python_executable,
        python_dependency_root=python_dependency_root,
        allowed_read_roots=(REPO, tmp_path),
        no_order_receipt_output=output / "no-order-smoke.json",
    )
    launcher_arguments, launcher_arguments_sha = _publish(
        evidence / "launcher-arguments.json", launcher_arguments_document
    )

    readiness_context = readiness.ReadinessValidationContext(
        activation_generation=GENERATION,
        expected_account_id=ACCOUNT_ID,
        code_build_sha256=inventory.code_build_sha256,
        effective_config_sha256=projection["settings_projection_sha256"],
        capture_receipt_sha256=capture_sha,
        runtime_environment_sha256=runtime["configuration_sha256"],
        database_target_fingerprint=fingerprints["DATABASE_URL"],
        iqfeed_bootstrap_manifest_sha256=bootstrap_sha,
        launcher_argument_contract_sha256=launcher_arguments_sha,
        capture_store_root=str(capture_store),
        source_hashes=inventory.source_hashes,
        allowed_read_roots=(str(REPO), str(tmp_path.resolve())),
    )

    readiness_refs: dict[str, dict[str, str]] = {}
    readiness_paths: dict[str, Path] = {}
    for kind in sorted(builder._PREACTIVATION_RECEIPT_KINDS):
        typed_evidence = _typed_evidence(kind, context=readiness_context)
        artifact_refs = _probe_artifact_refs(
            evidence,
            kind=kind,
            context=readiness_context,
            evidence=typed_evidence,
        )
        receipt = dict(
            readiness.issue_readiness_receipt_v3_from_artifacts(
                kind=kind,
                context=readiness_context,
                artifact_bindings=artifact_refs,
                captured_at=NOW - timedelta(seconds=2),
                expires_at=NOW + timedelta(seconds=20),
                now=NOW,
                max_age_seconds=contract._RECEIPT_MAX_AGE_SECONDS[kind],
            )
        )
        path, digest = _publish(evidence / f"{kind}.json", receipt)
        readiness_refs[kind] = _ref(path, digest)
        readiness_paths[kind] = path

    request = {
        "schema_version": builder.BUILD_REQUEST_SCHEMA_VERSION,
        "activation_generation": GENERATION,
        "expected_account_id": ACCOUNT_ID,
        "candidate_root": str(REPO),
        "capture_store_root": str(capture_store),
        "runtime_environment_receipt": _ref(runtime_path, runtime_sha),
        "settings_projection": _ref(projection_path, projection_sha),
        "capture_binding": _ref(capture_path, capture_sha),
        "iqfeed_bootstrap": _ref(bootstrap_path, bootstrap_sha),
        "launcher_arguments": _ref(
            launcher_arguments, launcher_arguments_sha
        ),
        "readiness_receipts": readiness_refs,
        "cutover": {
            "scheduled_tasks": sorted(contract._REQUIRED_TASKS),
            "singleton_policy": "one_unified_candidate_host",
            "rollback_required": True,
        },
    }
    request_path, request_sha = _publish(evidence / "request.json", request)
    return {
        "output": output,
        "request": request,
        "request_path": request_path,
        "request_sha": request_sha,
        "readiness_paths": readiness_paths,
        "input_hashes": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in evidence.iterdir()
            if path.is_file()
        },
    }


def test_code_inventory_exactly_matches_activation_contract_and_local_dependency_closure() -> None:
    inventory = builder.inventory_captured_paper_code(
        REPO, allowed_read_roots=(REPO,)
    )

    assert set(contract._REQUIRED_CODE_ROLES).issubset(inventory.source_paths)
    dependency_roles = {
        role
        for role in inventory.source_paths
        if role.startswith(contract._DEPENDENCY_ROLE_PREFIX)
    }
    assert dependency_roles
    closure = contract.discover_captured_paper_local_dependency_closure(
        candidate_root=REPO,
        seed_paths=(
            inventory.source_paths[role] for role in contract._REQUIRED_CODE_ROLES
        ),
    )
    primary_paths = {
        inventory.source_paths[role] for role in contract._REQUIRED_CODE_ROLES
    }
    assert dependency_roles == {
        contract.dependency_role(module_name)
        for module_name, path in closure.items()
        if path not in primary_paths
    }
    assert list(inventory.source_paths) == sorted(inventory.source_paths)
    assert inventory.source_hashes["activation_contract"] == hashlib.sha256(
        (REPO / "scripts" / "captured_paper_activation_contract.py").read_bytes()
    ).hexdigest()
    assert contract.sha256_json(
        {
            "schema_version": contract.CODE_BUILD_SCHEMA_VERSION,
            "artifacts": [dict(row) for row in inventory.artifacts],
        }
    ) == inventory.code_build_sha256


def test_builder_binds_existing_artifacts_and_publishes_verified_no_order_envelope(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)

    built = builder.build_captured_paper_preactivation_offline(
        request_path=bundle["request_path"],
        request_sha256=bundle["request_sha"],
        candidate_root=REPO,
        output_root=bundle["output"],
        allowed_read_roots=(REPO, tmp_path),
        wall_clock=lambda: NOW,
    )

    assert built.verified.paper_order_submission_authorized is False
    assert built.verified.envelope_stage == "preactivation"
    assert built.manifest_path == (
        bundle["output"]
        / built.manifest_sha256[:2]
        / f"{built.manifest_sha256}.json"
    )
    assert built.verified.code_build_sha256 == built.code_inventory.code_build_sha256
    assert built.verified.expected_account_id == ACCOUNT_ID
    assert built.verified.manifest["authority_boundary"]["live_cash_authorized"] is False
    cutover = built.verified.manifest["cutover"]
    launcher_source = Path(cutover["launcher_source_path"])
    launcher_staged = Path(cutover["launcher_path"])
    service_source = Path(cutover["service_source_path"])
    service_staged = Path(cutover["service_path"])
    assert launcher_staged != launcher_source
    assert service_staged != service_source
    assert launcher_staged.name == f"{cutover['launcher_sha256']}.ps1"
    assert service_staged.name == f"{cutover['service_sha256']}.py"
    assert launcher_staged.read_bytes() == launcher_source.read_bytes()
    assert service_staged.read_bytes() == service_source.read_bytes()
    assert built.verified.launcher_path == launcher_staged
    assert Path(cutover["python_import_root"]) == REPO
    host_ready = Path(cutover["host_ready_receipt_base"])
    assert not host_ready.exists()
    assert not Path(f"{host_ready}.permit.json").exists()
    assert not Path(f"{host_ready}.started.json").exists()
    assert not Path(f"{host_ready}.revoked.json").exists()
    launcher_contract = json.loads(
        Path(cutover["launcher_arguments_path"]).read_text(encoding="utf-8")
    )
    invocations = launcher_contract["invocations"]
    activate_args = invocations["ActivatePaper"]["projection"]["service_arguments"]
    assert activate_args[-2:] == ["--host-ready-receipt", str(host_ready)]
    assert "--host-ready-receipt" not in invocations["ValidateOnly"]["projection"]["service_arguments"]
    assert "--host-ready-receipt" not in invocations["NoOrderSmoke"]["projection"]["service_arguments"]
    assert not list(bundle["output"].glob(".pending-*"))
    assert bundle["input_hashes"] == {
        path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for path in bundle["input_hashes"]
    }


def test_missing_operational_evidence_fails_closed_without_publishing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    request = {
        "schema_version": builder.BUILD_REQUEST_SCHEMA_VERSION,
        "activation_generation": GENERATION,
        "expected_account_id": ACCOUNT_ID,
        "candidate_root": str(REPO),
        "capture_store_root": str(tmp_path),
        "runtime_environment_receipt": None,
        "settings_projection": None,
        "capture_binding": None,
        "iqfeed_bootstrap": None,
        "launcher_arguments": None,
        "readiness_receipts": {},
        "cutover": {
            "scheduled_tasks": sorted(contract._REQUIRED_TASKS),
            "singleton_policy": "one_unified_candidate_host",
            "rollback_required": True,
        },
    }
    request_path, request_sha = _publish(tmp_path / "request.json", request)

    result = builder.main(
        [
            "--request",
            str(request_path),
            "--request-sha256",
            request_sha,
            "--candidate-root",
            str(REPO),
            "--output-root",
            str(output),
            "--allow-read-root",
            str(REPO),
            "--allow-read-root",
            str(tmp_path),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 2
    assert report["error_code"] == "MISSING_EVIDENCE"
    assert set(report["missing_evidence"]) == {
        *builder._REFERENCE_FIELDS,
        *{
            f"readiness_receipts.{kind}"
            for kind in builder._PREACTIVATION_RECEIPT_KINDS
        },
    }
    assert report["preactivation_published"] is False
    assert report["paper_order_submission_authorized"] is False
    assert report["orders_submitted"] is False
    assert report["live_cash_authorized"] is False
    assert list(output.rglob("*.json")) == []


def test_changed_receipt_is_rejected_before_any_manifest_is_published(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    path = bundle["readiness_paths"]["broker_account"]
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(builder.CapturedPaperPreactivationBuildError) as caught:
        builder.build_captured_paper_preactivation_offline(
            request_path=bundle["request_path"],
            request_sha256=bundle["request_sha"],
            candidate_root=REPO,
            output_root=bundle["output"],
            allowed_read_roots=(REPO, tmp_path),
            wall_clock=lambda: NOW,
        )

    assert caught.value.code == "HASH_MISMATCH"
    assert list(bundle["output"].rglob("*.json")) == []


def test_arbitrary_launcher_argument_text_is_not_an_activation_binding(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    request = bundle["request"]
    path = Path(request["launcher_arguments"]["path"])
    raw = b"NoOrderSmoke\nmanifest=builder-output\n"
    path.write_bytes(raw)
    request["launcher_arguments"] = {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    request_path, request_sha = _publish(bundle["request_path"], request)

    with pytest.raises(builder.CapturedPaperPreactivationBuildError) as caught:
        builder.build_captured_paper_preactivation_offline(
            request_path=request_path,
            request_sha256=request_sha,
            candidate_root=REPO,
            output_root=bundle["output"],
            allowed_read_roots=(REPO, tmp_path),
            wall_clock=lambda: NOW,
        )

    assert caught.value.code == "INVALID_JSON"
    assert list(bundle["output"].rglob("*.json")) == []


def test_runtime_projection_cannot_claim_live_or_unisolated_settings(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    request = bundle["request"]
    projection_path = Path(request["settings_projection"]["path"])
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    projection["live_cash_credentials_present"] = True
    projection.pop("settings_projection_sha256")
    projection["settings_projection_sha256"] = contract.sha256_json(projection)
    projection_path, projection_sha = _publish(projection_path, projection)
    request["settings_projection"] = _ref(projection_path, projection_sha)
    request_path, request_sha = _publish(bundle["request_path"], request)

    with pytest.raises(builder.CapturedPaperPreactivationBuildError) as caught:
        builder.build_captured_paper_preactivation_offline(
            request_path=request_path,
            request_sha256=request_sha,
            candidate_root=REPO,
            output_root=bundle["output"],
            allowed_read_roots=(REPO, tmp_path),
            wall_clock=lambda: NOW,
        )

    assert caught.value.code == "SETTINGS_PROJECTION_INVALID"
    assert list(bundle["output"].rglob("*.json")) == []


def test_post_publish_contract_rejection_removes_new_content_addressed_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    real_load = contract.load_captured_paper_preactivation
    calls = 0

    def fail_second_load(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise contract.CapturedPaperActivationContractError(
                "TEST_POST_PUBLISH_REJECTION", "test-only final read rejection"
            )
        return real_load(*args, **kwargs)

    monkeypatch.setattr(
        contract, "load_captured_paper_preactivation", fail_second_load
    )

    with pytest.raises(builder.CapturedPaperPreactivationBuildError) as caught:
        builder.build_captured_paper_preactivation_offline(
            request_path=bundle["request_path"],
            request_sha256=bundle["request_sha"],
            candidate_root=REPO,
            output_root=bundle["output"],
            allowed_read_roots=(REPO, tmp_path),
            wall_clock=lambda: NOW,
        )

    assert caught.value.code == "TEST_POST_PUBLISH_REJECTION"
    assert calls == 2
    assert list(bundle["output"].rglob("*.json")) == []


def test_builder_has_no_application_or_external_io_imports() -> None:
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
        "requests",
        "sqlalchemy",
        "socket",
        "subprocess",
        "psutil",
    }.isdisjoint(imports)


def test_builder_hash_inventory_covers_every_service_runtime_module_role() -> None:
    """A runtime import may never escape the sealed source inventory."""

    service_path = REPO / "scripts" / "captured_alpaca_paper_service.py"
    tree = ast.parse(service_path.read_text(encoding="utf-8"))
    roster_node = next(
        node.value
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name)
            and target.id == "_RUNTIME_MODULE_ROSTER"
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        )
    )
    runtime_roles = set(ast.literal_eval(roster_node))

    assert runtime_roles <= set(builder._SOURCE_RELATIVE_PATHS)
