from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from types import MappingProxyType

import pytest

from scripts import captured_paper_readiness_evidence as readiness
from scripts import captured_paper_host_cutover as host_cutover
from scripts import run_captured_paper_preactivation_probes as probes
from scripts.captured_paper_runtime_env import (
    CapturedPaperRuntimeEnvironmentReceipt,
    RUNTIME_ENV_SCHEMA_VERSION,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 18, 0, tzinfo=UTC)
ACCOUNT = "11111111-2222-4333-8444-555555555555"
GENERATION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def source_hashes() -> dict[str, str]:
    roles = set(readiness.EXPECTED_ISSUER_ROLES.values()) | {
        "captured_paper_preactivation_probes",
        "captured_paper_host_cutover",
        "iqfeed_capture_host",
        "iqfeed_trade_bridge",
        "iqfeed_depth_bridge",
        "iqfeed_l1_capture",
        "iqfeed_l2_capture",
    }
    return {role: h(role) for role in roles}


def context(tmp_path: Path) -> readiness.ReadinessValidationContext:
    return readiness.ReadinessValidationContext(
        activation_generation=GENERATION,
        expected_account_id=ACCOUNT,
        code_build_sha256=h("build"),
        effective_config_sha256=h("placeholder-config"),
        capture_receipt_sha256=h("capture"),
        runtime_environment_sha256=h("placeholder-runtime"),
        database_target_fingerprint=h("database"),
        iqfeed_bootstrap_manifest_sha256=h("bootstrap"),
        launcher_argument_contract_sha256=h("launcher"),
        capture_store_root=str((tmp_path / "capture").resolve()),
        source_hashes=source_hashes(),
        allowed_read_roots=(str(tmp_path.resolve()),),
    )


class StaticAuthority:
    def __init__(self, value: object) -> None:
        self.value = value

    def observe(self):
        return self.value

    def execute(self):
        return self.value


def runtime_authority(
    ctx: readiness.ReadinessValidationContext,
) -> tuple[StaticAuthority, readiness.ReadinessValidationContext]:
    effective = {
        "CHILI_ALPACA_ENABLED": "true",
        "CHILI_ALPACA_PAPER": "true",
    }
    receipt_body = {
        "schema_version": RUNTIME_ENV_SCHEMA_VERSION,
        "source_path": "C:/sealed/captured-paper.env",
        "source_sha256": h("env"),
        "expected_account_id": ACCOUNT,
        "first_dip_policy_mode": "candidate",
        "effective_config": effective,
        "secret_fingerprints": {
            "DATABASE_URL": h("db-secret"),
            "CHILI_ALPACA_API_KEY": h("key"),
            "CHILI_ALPACA_API_SECRET": h("secret"),
        },
    }
    runtime_sha = readiness.sha256_json(receipt_body)
    receipt = CapturedPaperRuntimeEnvironmentReceipt(
        source_path=receipt_body["source_path"],
        source_sha256=receipt_body["source_sha256"],
        expected_account_id=ACCOUNT,
        first_dip_policy_mode="candidate",
        effective_config=MappingProxyType(effective),
        secret_fingerprints=MappingProxyType(receipt_body["secret_fingerprints"]),
        removed_forbidden_keys=(),
        configuration_sha256=runtime_sha,
    )
    policy_body = {
        "schema_version": "chili.adaptive-risk-policy-settings.v1",
        "policy_field_bindings": {"risk": "chili_risk"},
        "settings": {"chili_risk": 0.01},
        "policy_snapshot": {"risk": 0.01},
        "policy_sha256": h("policy"),
    }
    policy = {
        **policy_body,
        "settings_projection_sha256": readiness.sha256_json(policy_body),
    }
    projection_body = {
        "schema_version": "chili.captured-paper-settings-projection.v1",
        "runtime_environment_sha256": runtime_sha,
        "settings": {
            "chili_alpaca_enabled": True,
            "chili_alpaca_paper": True,
            "chili_equity_execution_rail": "alpaca",
            "chili_momentum_auto_arm_equity_only": True,
            "chili_momentum_auto_arm_crypto_only": False,
            "chili_momentum_first_dip_reclaim_policy_mode": "candidate",
            "chili_momentum_short_enabled": False,
            "chili_momentum_short_lane_enabled": False,
        },
        "adaptive_risk_policy": policy,
        "captured_paper_operational_policy": {"time_in_force": "day"},
        "captured_paper_config_isolated": True,
        "paper_credentials_present": True,
        "live_cash_credentials_present": False,
        "cash_broker_environment_keys_present": False,
    }
    config_sha = readiness.sha256_json(projection_body)
    projection = {**projection_body, "settings_projection_sha256": config_sha}
    return (
        StaticAuthority(
            probes.RuntimeSettingsNativeObservation(
                receipt=receipt, settings_projection=projection
            )
        ),
        replace(
            ctx,
            runtime_environment_sha256=runtime_sha,
            effective_config_sha256=config_sha,
        ),
    )


def audit_snapshot(generation: str, count: int = 0) -> dict[str, object]:
    body = {
        "schema_version": "chili.alpaca-paper-order-submission-audit.v1",
        "broker_environment": "paper",
        "asset_class": "us_equity",
        "provider_account_id": ACCOUNT,
        "adapter_connection_generation": generation,
        "adapter_build_sha256": h("adapter"),
        "audit_generation": "audit-1",
        "submission_call_count": count,
        "submission_chain_sha256": h(f"chain-{count}"),
    }
    text = canonical(body).decode()
    return {
        **body,
        "snapshot_canonical_json": text,
        "snapshot_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


class FakePaperAdapter:
    def __init__(self, *, mutate_audit: bool = False) -> None:
        self.generation = "alpaca-paper-rest:" + h("generation")
        self.audit_calls = 0
        self.mutate_audit = mutate_audit

    def get_paper_connection_generation_receipt(self):
        body = {
            "schema_version": "chili.alpaca-paper-connection-generation.v1",
            "broker_environment": "paper",
            "asset_class": "us_equity",
            "provider_account_id": ACCOUNT,
            "adapter_connection_generation": self.generation,
            "adapter_build_sha256": h("adapter"),
            "available_at": NOW.isoformat(),
        }
        text = canonical(body).decode()
        return {
            **body,
            "receipt_canonical_json": text,
            "receipt_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }

    def get_order_submission_audit_snapshot(self):
        count = int(self.mutate_audit and self.audit_calls > 0)
        self.audit_calls += 1
        return audit_snapshot(self.generation, count)

    def get_account_snapshot(self):
        return {
            "ok": True,
            "paper": True,
            "account_id": ACCOUNT,
            "status": "ACTIVE",
            "account_blocked": False,
            "trading_blocked": False,
            "transfers_blocked": False,
            "trade_suspended_by_user": False,
            "retrieved_at_utc": NOW.isoformat(),
        }

    def get_paper_position_census(self, *, read_binding):
        assert read_binding["account_scope"] == "alpaca:paper"
        return {
            "readable": True,
            "pagination_complete": True,
            "broker_environment": "paper",
            "asset_class": "us_equity",
            "provider_account_id": ACCOUNT,
            "adapter_connection_generation": self.generation,
            "positions": [],
            "inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
        }

    def get_paper_open_order_census(self, *, read_binding):
        assert read_binding["account_scope"] == "alpaca:paper"
        return {
            "readable": True,
            "pagination_complete": True,
            "broker_environment": "paper",
            "asset_class": "us_equity",
            "provider_account_id": ACCOUNT,
            "adapter_connection_generation": self.generation,
            "orders": [],
            "inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
        }


class BrokerAuthority:
    def __init__(self, adapter: FakePaperAdapter) -> None:
        self._adapter = adapter

    def adapter(self):
        return self._adapter


def database_authority() -> StaticAuthority:
    roster = ("001", "002")
    return StaticAuthority(
        probes.DatabaseNativeObservation(
            migration_roster=roster,
            applied_migrations=roster,
            table_names=readiness.REQUIRED_DATABASE_TABLES,
            rehearsal_case_exit_codes=(0, 0),
            observed_at=NOW,
        )
    )


def capture_authority(ctx: readiness.ReadinessValidationContext) -> StaticAuthority:
    roles = (
        "iqfeed_capture_host",
        "iqfeed_trade_bridge",
        "iqfeed_depth_bridge",
        "iqfeed_l1_capture",
        "iqfeed_l2_capture",
    )
    return StaticAuthority(
        probes.CaptureHostNativeObservation(
            bootstrap_manifest_sha256=ctx.iqfeed_bootstrap_manifest_sha256,
            capture_store_root=ctx.capture_store_root,
            source_hashes={role: ctx.source_hashes[role] for role in roles},
            host_binding={"trade_bridge_bound": True, "depth_bridge_bound": True},
            capture_health={
                "capture_store_writable": True,
                "dropped_event_count": 0,
                "overflow_count": 0,
                "unreported_gap_count": 0,
            },
            provider_health={
                "observed_at": NOW.isoformat(),
                "socket_readable": True,
                "exact_print_clock_observed": True,
            },
        )
    )


def focused_authority() -> StaticAuthority:
    compile_runs = tuple(
        probes.CommandExecution(
            argv=("python", "-B", "-m", "py_compile", path),
            exit_code=0,
            completed_at=NOW,
        )
        for path in probes.FOCUSED_COMPILE_RELATIVE_PATHS
    )
    pytest_argv = (
        "python",
        "-B",
        "-m",
        "pytest",
        "-q",
        *probes.FOCUSED_PYTEST_NODE_IDS,
        "-p",
        "scripts.captured_paper_pytest_side_effect_guard",
        "--junitxml=@producer-owned",
    )
    case_names = [node.rsplit("::", 1)[1] for node in probes.FOCUSED_PYTEST_NODE_IDS]
    junit = (
        f'<testsuite tests="{len(case_names)}" failures="0" errors="0" skipped="0">'
        + "".join(f'<testcase name="{name}" />' for name in case_names)
        + "</testsuite>"
    ).encode()
    return StaticAuthority(
        probes.FocusedRegressionNativeObservation(
            compile_runs=compile_runs,
            pytest_run=probes.CommandExecution(
                argv=pytest_argv, exit_code=0, completed_at=NOW
            ),
            junit_xml=junit,
            side_effect_events=(
                {"event_type": "real_network", "count": 0},
                {"event_type": "live_cash", "count": 0},
                {"event_type": "broker_post", "count": 0},
            ),
        )
    )


def test_focused_regression_roster_owns_complete_captured_paper_critical_path() -> None:
    required_compile_paths = {
        "app/migrations.py",
        "app/models/captured_paper_selection_frontier.py",
        "app/services/yf_session.py",
        "app/services/trading/momentum_neural/variants.py",
        "scripts/build_captured_paper_runtime_env.py",
        "scripts/build_captured_paper_preactivation.py",
        "scripts/captured_alpaca_paper_service.py",
        "scripts/captured_paper_host_cutover.py",
        "scripts/captured_paper_operator_flow.py",
        "scripts/captured_paper_runtime_env.py",
        "scripts/captured_paper_activation_contract.py",
        "scripts/run_captured_paper_operator_chain.py",
        "app/services/trading/momentum_neural/captured_paper_initial_candidate_reader.py",
        "app/services/trading/momentum_neural/captured_paper_selection_producer.py",
        "app/services/trading/momentum_neural/captured_paper_selection_queue.py",
        "app/services/trading/momentum_neural/captured_paper_selection_runtime.py",
        "app/services/trading/momentum_neural/captured_paper_selection_source.py",
        "app/services/trading/momentum_neural/captured_paper_service_supervisor.py",
        "app/services/trading/momentum_neural/captured_paper_variant_binding.py",
    }
    assert required_compile_paths <= set(probes.FOCUSED_COMPILE_RELATIVE_PATHS)
    assert "scripts/captured_paper_activation_runner.py" not in (
        probes.FOCUSED_COMPILE_RELATIVE_PATHS
    )

    required_test_nodes = {
        "tests/test_captured_paper_selection_source.py::test_source_captures_full_four_stream_envelope_and_scores_without_fallback",
        "tests/test_captured_paper_selection_source.py::test_missing_typed_fundamentals_receipt_fails_only_that_decision",
        "tests/test_captured_paper_selection_queue.py::test_visible_commit_is_ignored_until_post_fsync_gate_acknowledges_it",
        "tests/test_captured_paper_selection_queue.py::test_coverage_unavailable_event_emits_route_tombstone_not_empty_advance",
        "tests/test_captured_paper_selection_producer.py::test_batch_upsert_and_frontier_cas_commit_together",
        "tests/test_captured_paper_selection_producer.py::test_crash_rollback_then_restart_is_atomic_and_idempotent",
        "tests/test_captured_paper_selection_producer.py::test_migration_353_route_state_schema_and_cas_guards",
        "tests/test_captured_paper_selection_runtime.py::test_constructor_is_fully_inert_then_prime_precedes_reader_install",
        "tests/test_captured_paper_selection_runtime.py::test_hash_bound_not_applied_outcome_never_builds_runtime_or_calls_rollback",
        "tests/test_captured_paper_initial_candidate_reader.py::test_real_db_reader_returns_only_exact_current_captured_row_without_mutation",
        "tests/test_captured_paper_variant_binding.py::test_migration_352_receipt_and_append_only_transition_round_trip",
        "tests/test_captured_paper_variant_binding.py::test_reserved_clone_is_invisible_to_generic_readers_and_mutators",
        "tests/test_captured_paper_service_supervisor.py::test_selection_prime_precedes_fresh_authority_and_order_workers",
        "tests/test_captured_paper_service_supervisor.py::test_post_quiesce_deactivation_runs_after_every_owner_and_before_fence_release",
        "tests/test_captured_alpaca_paper_service.py::test_composition_uses_measured_capacity_and_one_exact_adapter_generation",
        "tests/test_captured_paper_service_selection_integration.py::test_real_service_selection_lifecycle_primes_reads_and_rolls_back",
        "tests/test_yf_session_fundamentals_receipt.py::test_authoritative_empty_is_distinct_from_provider_error",
        "tests/test_yf_session_fundamentals_receipt.py::test_stale_cache_is_not_reclassified_as_fresh_when_circuit_is_open",
        "tests/test_adaptive_risk_policy_settings.py::test_replay_and_captured_paper_use_identical_policy_projection",
        "tests/test_adaptive_risk_policy_settings.py::test_builder_cannot_bind_magic_dollar_or_one_symbol_activation_caps",
        "tests/test_adaptive_risk_policy.py::test_concurrency_emerges_from_aggregate_risk_not_one_symbol_cap",
        "tests/test_adaptive_risk_runtime_contract.py::test_atomic_three_dimension_reservation_and_no_magic_activation_caps_are_required",
        "tests/test_run_captured_paper_operator_chain.py::test_import_is_inert_and_does_not_touch_network_db_broker_or_host",
        "tests/test_run_captured_paper_operator_chain.py::test_chain_request_is_canonical_hash_bound_and_pinned_by_outer_request",
        "tests/test_run_captured_paper_operator_chain.py::test_full_operator_chain_bootstraps_exact_print_before_selection_and_is_hash_bound",
        "tests/test_captured_paper_operator_flow.py::test_operator_flow_publishes_build_ready_and_only_no_order_next_command",
        "tests/test_captured_paper_operator_flow.py::test_materialization_runs_runtime_after_long_shards_and_short_ttl_reads_last",
        "tests/test_captured_paper_runtime_env.py::test_installs_equity_only_candidate_and_excludes_every_live_credential",
        "tests/test_captured_paper_host_cutover.py::test_validate_only_is_default_and_performs_no_mutation",
        "tests/test_build_captured_paper_preactivation.py::test_code_inventory_exactly_matches_activation_contract_and_local_dependency_closure",
    }
    assert required_test_nodes <= set(probes.FOCUSED_PYTEST_NODE_IDS)
    assert not any(
        node.startswith("tests/test_captured_paper_activation_runner.py::")
        for node in probes.FOCUSED_PYTEST_NODE_IDS
    )


def lifecycle_authority(*, omit_event: bool = False) -> StaticAuthority:
    events = {
        "ownership_idempotency": ["claim_acquired", "duplicate_claim_refused"],
        "indeterminate_submit_retain": ["submit_indeterminate", "resources_retained"],
        "late_fill_quarantine": ["late_fill_observed", "exposure_quarantined"],
        "append_only_fill_settlement": ["fill_appended", "settlement_appended"],
        "same_cid_reconciliation": ["same_cid_lookup", "same_cid_reconciled"],
        "no_blind_repost": ["indeterminate_observed", "reconciliation_only"],
    }
    if omit_event:
        events["no_blind_repost"] = ["indeterminate_observed"]
    report = {
        "schema_version": "chili.captured-paper-lifecycle-preflight.v1",
        "scenarios": [
            {"name": name, "events": values} for name, values in events.items()
        ],
        "transport_events": [
            {"event_type": "fake_post", "count": 2},
            {"event_type": "real_network", "count": 0},
            {"event_type": "live_cash", "count": 0},
            {"event_type": "blind_repost", "count": 0},
        ],
        "completed_at": NOW.isoformat(),
    }
    return StaticAuthority(
        probes.LifecycleNativeObservation(
            scenario_run=probes.CommandExecution(
                argv=(
                    "python",
                    "-B",
                    "scripts/run_captured_paper_lifecycle_preflight.py",
                    "--fake-transport-only",
                    "--output=@producer-owned",
                ),
                exit_code=0,
                completed_at=NOW,
            ),
            event_report=canonical(report),
        )
    )


def kill_authority(active: bool = False) -> StaticAuthority:
    return StaticAuthority(
        probes.KillSwitchNativeObservation(
            row_id=9, active=active, regime="kill_switch", observed_at=NOW
        )
    )


def rollback_authority(ctx: readiness.ReadinessValidationContext) -> StaticAuthority:
    task_rows = {}
    for name in readiness.REQUIRED_TASKS:
        xml = f"<Task><Name>{name}</Name></Task>".encode()
        task_rows[name] = {
            "xml_base64": base64.b64encode(xml).decode(),
            "xml_sha256": hashlib.sha256(xml).hexdigest(),
        }
    candidate_xml = b"<Task><Candidate /></Task>"
    action = {
        "schema_version": "chili.captured-paper-host-cutover-action.v1",
        "host_cutover_source_sha256": ctx.source_hashes["captured_paper_host_cutover"],
        "launcher_argument_contract_sha256": ctx.launcher_argument_contract_sha256,
        "candidate_task_xml_sha256": hashlib.sha256(candidate_xml).hexdigest(),
        "singleton_policy": "one_unified_candidate_host",
    }
    task_raw = canonical({"schema_version": "task.v1", "tasks": task_rows})
    process_raw = canonical({"schema_version": "process.v1", "processes": []})
    restore_raw = canonical(
        {"schema_version": "restore.v1", "legacy_process_bindings": []}
    )
    action_raw = canonical(action)
    root = Path(ctx.allowed_read_roots[0])
    baseline_context = host_cutover.PreActivationRollbackContext(
        activation_generation=GENERATION,
        expected_account_id=ACCOUNT,
        candidate_root=root,
        allowed_read_roots=(root,),
        host_cutover_source_sha256=ctx.source_hashes[
            "captured_paper_host_cutover"
        ],
        launcher_argument_contract_sha256=ctx.launcher_argument_contract_sha256,
    )
    provisional = host_cutover.PreActivationRollbackBaseline(
        context=baseline_context,
        task_snapshot=host_cutover.TaskSnapshot(
            captured_at=NOW,
            tasks=MappingProxyType({}),
            artifact_path=root / "task.json",
            artifact_sha256=hashlib.sha256(task_raw).hexdigest(),
        ),
        process_snapshot=host_cutover.ProcessSnapshot(
            captured_at=NOW,
            processes=(),
            artifact_path=root / "process.json",
            artifact_sha256=hashlib.sha256(process_raw).hexdigest(),
        ),
        restore_plan=host_cutover.RestorePlan(
            task_enabled_states=MappingProxyType({}),
            restart_tasks=(),
            bindings=(),
            candidate_task_name=host_cutover.CANDIDATE_TASK_NAME,
            artifact_path=root / "restore.json",
            artifact_sha256=hashlib.sha256(restore_raw).hexdigest(),
        ),
        candidate_action_path=root / "action.json",
        candidate_action_sha256=hashlib.sha256(action_raw).hexdigest(),
        candidate_template_path=root / "candidate.xml",
        candidate_template_sha256=hashlib.sha256(candidate_xml).hexdigest(),
        validated_at=NOW,
        baseline_sha256="0" * 64,
    )
    baseline = replace(
        provisional,
        baseline_sha256=host_cutover.sha256_json(
            host_cutover.build_preactivation_rollback_baseline_document(provisional)
        ),
    )
    return StaticAuthority(
        probes.RollbackNativeObservation(
            task_snapshot=task_raw,
            process_snapshot=process_raw,
            restore_plan=restore_raw,
            candidate_task_xml=candidate_xml,
            candidate_action=action_raw,
            preactivation_baseline=baseline,
        )
    )


def authorities(
    ctx: readiness.ReadinessValidationContext,
    runtime: StaticAuthority,
) -> probes.TrustedProbeAuthorities:
    return probes.TrustedProbeAuthorities(
        runtime_settings=runtime,
        broker_account=BrokerAuthority(FakePaperAdapter()),
        database_schema=database_authority(),
        capture_host_smoke=capture_authority(ctx),
        focused_regressions=focused_authority(),
        lifecycle_preflight=lifecycle_authority(),
        kill_switch=kill_authority(),
        rollback_snapshot=rollback_authority(ctx),
    )


def test_runtime_and_broker_producers_derive_authority_and_reject_self_attestation(
    tmp_path: Path,
) -> None:
    (tmp_path / "capture").mkdir()
    ctx = context(tmp_path)
    runtime, ctx = runtime_authority(ctx)
    runtime_result = probes._runtime_observations(runtime, context=ctx, now=NOW)
    assert runtime_result["broker_environment"] == "paper"
    assert runtime_result["activation_only_dollar_caps"] == []

    broker = probes._broker_observations(
        BrokerAuthority(FakePaperAdapter()), context=ctx, now=NOW
    )
    assert broker["position_count"] == broker["open_order_count"] == 0
    with pytest.raises(probes.CapturedPaperPreactivationProbeError):
        probes._runtime_observations(
            StaticAuthority({"verdict": "PASS"}), context=ctx, now=NOW
        )
    with pytest.raises(
        probes.CapturedPaperPreactivationProbeError,
        match="read-only broker probe advanced POST audit",
    ):
        probes._broker_observations(
            BrokerAuthority(FakePaperAdapter(mutate_audit=True)),
            context=ctx,
            now=NOW,
        )


def test_database_probe_requires_captured_selection_route_state_table(
    tmp_path: Path,
) -> None:
    (tmp_path / "capture").mkdir()
    ctx = context(tmp_path)
    assert "captured_paper_selection_route_states" in (
        readiness.REQUIRED_DATABASE_TABLES
    )
    roster = ("001", "002")
    missing_route_state = tuple(
        name
        for name in readiness.REQUIRED_DATABASE_TABLES
        if name != "captured_paper_selection_route_states"
    )
    authority = StaticAuthority(
        probes.DatabaseNativeObservation(
            migration_roster=roster,
            applied_migrations=roster,
            table_names=missing_route_state,
            rehearsal_case_exit_codes=(0, 0),
            observed_at=NOW,
        )
    )

    with pytest.raises(
        probes.CapturedPaperPreactivationProbeError,
        match="migration/table roster is incomplete",
    ):
        probes._database_observations(authority, context=ctx, now=NOW)

    valid = dict(
        probes._database_observations(
            database_authority(),
            context=ctx,
            now=NOW,
        )
    )
    with pytest.raises(
        readiness.CapturedPaperReadinessEvidenceError,
        match="database evidence is not exact, complete, and rehearsed",
    ):
        readiness._validate_database(
            {
                "schema_version": (
                    "chili.captured-paper-readiness-evidence."
                    "database_schema.v3"
                ),
                "source_receipts": {
                    "schema_probe": valid["applied_migrations_sha256"],
                    "idempotent_rehearsal": h("idempotent-rehearsal"),
                },
                **valid,
                "required_tables": list(missing_route_state),
            },
            ctx,
            captured_at=NOW,
        )


def test_lifecycle_and_kill_switch_fail_closed_on_missing_or_active_authority(
    tmp_path: Path,
) -> None:
    (tmp_path / "capture").mkdir()
    ctx = context(tmp_path)
    valid = probes._lifecycle_observations(
        lifecycle_authority(), context=ctx, now=NOW
    )
    assert valid["fake_transport_call_count"] == 2
    with pytest.raises(probes.CapturedPaperPreactivationProbeError):
        probes._lifecycle_observations(
            lifecycle_authority(omit_event=True), context=ctx, now=NOW
        )
    active = probes._kill_switch_observations(
        kill_authority(active=True), context=ctx, now=NOW
    )
    assert active["active"] is True
    with pytest.raises(readiness.CapturedPaperReadinessEvidenceError):
        refs = active  # semantic validator, not producer, rejects active state.
        readiness._validate_kill_switch(
            {
                "schema_version": "chili.captured-paper-readiness-evidence.kill_switch.v3",
                "source_receipts": {"kill_switch_query": h("q")},
                **refs,
            },
            ctx,
            captured_at=NOW,
        )


def test_full_probe_run_publishes_v3_artifacts_receipts_and_non_overwriting_manifest(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (tmp_path / "capture").mkdir()
    ctx = context(tmp_path)
    runtime, ctx = runtime_authority(ctx)
    result = probes.run_trusted_preactivation_probes(
        context=ctx,
        authorities=authorities(ctx, runtime),
        output_root=output,
        max_age_seconds_by_kind={
            kind: 3600 for kind in readiness.PREACTIVATION_KINDS
        },
        wall_clock=lambda: NOW,
    )
    manifest = result["manifest"]
    assert set(manifest["artifact_bindings"]) == set(readiness.PREACTIVATION_KINDS)
    assert set(manifest["readiness_receipts"]) == set(readiness.PREACTIVATION_KINDS)
    assert manifest["orders_submitted"] is False
    artifact_path = Path(
        manifest["artifact_bindings"]["runtime_settings"]["runtime_environment"][
            "path"
        ]
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["probe_runner_source_sha256"] == ctx.source_hashes[
        "captured_paper_preactivation_probes"
    ]
    with pytest.raises(
        probes.CapturedPaperPreactivationProbeError, match="already exists"
    ):
        probes.run_trusted_preactivation_probes(
            context=ctx,
            authorities=authorities(ctx, runtime),
            output_root=output,
            max_age_seconds_by_kind={
                kind: 3600 for kind in readiness.PREACTIVATION_KINDS
            },
            wall_clock=lambda: NOW,
        )


def test_single_operational_command_runs_all_eight_from_injected_trusted_composition(
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "command-output"
    output.mkdir()
    (tmp_path / "capture").mkdir()
    ctx = context(tmp_path)
    runtime, ctx = runtime_authority(ctx)
    composition = probes.TrustedOperationalProbeComposition(
        context=ctx,
        authorities=authorities(ctx, runtime),
    )

    assert probes.main(
        ["--output-root", str(output)],
        composition_provider=lambda: composition,
        wall_clock=lambda: NOW,
    ) == 0
    command_result = json.loads(capsys.readouterr().out)
    manifest = json.loads(
        Path(command_result["manifest_path"]).read_text(encoding="utf-8")
    )
    assert set(manifest["readiness_receipts"]) == set(
        readiness.PREACTIVATION_KINDS
    )
    assert manifest["orders_submitted"] is False
    assert manifest["live_cash_authorized"] is False


def test_standalone_operational_command_is_explicitly_unavailable_fail_closed(
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "standalone-output"
    output.mkdir()

    assert probes.main(["--output-root", str(output)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error_code"] == "OPERATIONAL_COMPOSITION_UNAVAILABLE"
    assert not any(output.rglob("*.json"))


def test_fixed_command_and_rollback_producers_reject_caller_selected_passes(
    tmp_path: Path,
) -> None:
    (tmp_path / "capture").mkdir()
    ctx = context(tmp_path)
    focused = probes._focused_regression_observations(
        focused_authority(), context=ctx, now=NOW
    )
    assert focused["compile_failure_count"] == focused["failed_test_count"] == 0
    shaped = probes.FocusedRegressionNativeObservation(
        compile_runs=(),
        pytest_run=probes.CommandExecution(
            argv=("python", "-m", "pytest", "tests/test_easy.py"),
            exit_code=0,
            completed_at=NOW,
        ),
        junit_xml=b'<testsuite tests="1" failures="0" errors="0" skipped="0" />',
        side_effect_events=(),
    )
    with pytest.raises(probes.CapturedPaperPreactivationProbeError):
        probes._focused_regression_observations(
            StaticAuthority(shaped), context=ctx, now=NOW
        )
    rollback = probes._rollback_observations(
        rollback_authority(ctx), context=ctx, now=NOW
    )
    assert rollback["validation_mode"] == "PREACTIVATION_ROLLBACK_BASELINE"
    assert rollback["host_mutation_count"] == 0
    assert rollback["final_validate_only_performed"] is False


def _write_side_effect_report(path: Path, *, fake_transport: int = 0) -> None:
    body = {
        "schema_version": "chili.captured-paper-pytest-side-effect-census.v1",
        "events": [
            {"event_type": "fake_transport", "count": fake_transport},
            {"event_type": "real_network", "count": 0},
            {"event_type": "live_cash", "count": 0},
            {"event_type": "broker_post", "count": 0},
        ],
    }
    document = {**body, "report_sha256": readiness.sha256_json(body)}
    path.write_bytes(canonical(document))


def fake_command_runner(command, *, cwd, env, capture_output, check):
    del cwd, capture_output, check
    junit_args = [item for item in command if str(item).startswith("--junitxml=")]
    if junit_args:
        names = [str(item).rsplit("::", 1)[1] for item in command if "::" in str(item)]
        count = len(names)
        cases = "".join(f'<testcase name="{name}" />' for name in names)
        Path(str(junit_args[0]).split("=", 1)[1]).write_text(
            f'<testsuite tests="{count}" failures="0" errors="0" skipped="0">{cases}</testsuite>',
            encoding="utf-8",
        )
        lifecycle = any(
            "test_positive_same_cid_reconciliation" in str(item)
            for item in command
        )
        _write_side_effect_report(
            Path(env["CHILI_CAPTURED_PAPER_SIDE_EFFECT_REPORT"]),
            fake_transport=2 if lifecycle else 0,
        )
    return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")


def test_concrete_subprocess_and_preactivation_baseline_adapters_are_runnable_with_fakes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "capture").mkdir()
    ctx = context(tmp_path)
    focused = probes.SubprocessFocusedRegressionAuthority(
        candidate_root=tmp_path,
        command_runner=fake_command_runner,
        wall_clock=lambda: NOW,
    ).execute()
    assert len(focused.compile_runs) == len(probes.FOCUSED_COMPILE_RELATIVE_PATHS)
    assert focused.pytest_run.exit_code == 0
    lifecycle = probes.SubprocessLifecycleScenarioAuthority(
        candidate_root=tmp_path,
        command_runner=fake_command_runner,
        wall_clock=lambda: NOW,
    ).execute()
    assert json.loads(lifecycle.event_report)["transport_events"][0] == {
        "count": 2,
        "event_type": "fake_post",
    }

    from scripts.iqfeed_capture_bootstrap_preflight import (
        IqfeedCaptureBootstrapPreflight,
    )

    preflight = IqfeedCaptureBootstrapPreflight(
        manifest_path=tmp_path / "bootstrap.json",
        manifest_sha256=ctx.iqfeed_bootstrap_manifest_sha256,
        startup_evidence_path=tmp_path / "startup.json",
        startup_evidence_sha256=h("startup"),
        resource_benchmark_path=tmp_path / "resource.json",
        resource_benchmark_sha256=h("resource"),
        resource_binding=object(),
        capture_store_root=tmp_path / "capture",
        run_configuration={},
        handoff_configuration={},
        source_paths={},
        source_hashes={
            role: ctx.source_hashes[role]
            for role in (
                "iqfeed_capture_host",
                "iqfeed_trade_bridge",
                "iqfeed_depth_bridge",
                "iqfeed_l1_capture",
                "iqfeed_l2_capture",
            )
        },
        startup_evidence_hashes={},
        startup_captured_at=NOW,
        startup_process_instance_id="process",
        startup_generation=1,
        broker="alpaca",
        broker_environment="paper",
        bridge_configuration={},
        benchmark_authority_reasons=(),
    )

    class Host:
        def health(self):
            return {
                "trade_bridge_bound": True,
                "depth_bridge_bound": True,
                "provider_loop_supervisor": {
                    "all_ready": True,
                    "lanes": {
                        "trade": {"socket_connected": True, "schema_verified": True},
                        "depth": {"socket_connected": True, "schema_verified": True},
                    },
                },
            }

    capture = probes.BoundCaptureHostReadAuthority(
        bootstrap_preflight=preflight,
        host=Host(),
        capture_health_provider=lambda: {
            "capture_store_writable": True,
            "dropped_event_count": 0,
            "overflow_count": 0,
            "unreported_gap_count": 0,
            "exact_print_clock_observed": True,
        },
        wall_clock=lambda: NOW,
    ).observe()
    assert capture.provider_health["socket_readable"] is True

    rollback_native = rollback_authority(ctx).observe()
    paths = []
    for index, raw in enumerate(
        (
            rollback_native.task_snapshot,
            rollback_native.process_snapshot,
            rollback_native.restore_plan,
            rollback_native.candidate_task_xml,
            rollback_native.candidate_action,
        )
    ):
        path = tmp_path / f"rollback-{index}.bin"
        path.write_bytes(raw)
        paths.append(path)

    monkeypatch.setattr(
        host_cutover,
        "prepare_preactivation_rollback_baseline",
        lambda *args, **kwargs: rollback_native.preactivation_baseline,
    )
    observed = probes.HostCutoverPreactivationBaselineAuthority(
        context=ctx,
        candidate_root=tmp_path,
        allowed_read_roots=(tmp_path,),
        task_snapshot_path=paths[0],
        process_snapshot_path=paths[1],
        restore_plan_path=paths[2],
        candidate_task_xml_path=paths[3],
        candidate_action_path=paths[4],
        wall_clock=lambda: NOW,
    ).observe()
    assert observed.preactivation_baseline.baseline_sha256 == (
        rollback_native.preactivation_baseline.baseline_sha256
    )
    assert not hasattr(observed, "validate_only_report")
