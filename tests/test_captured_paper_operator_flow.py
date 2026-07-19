from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from scripts import captured_paper_activation_contract as contract
from scripts import captured_paper_readiness_evidence as readiness
from scripts import captured_paper_operator_flow as operator
from scripts import run_captured_paper_preactivation_probes as probes
from scripts.captured_paper_runtime_env import (
    CapturedPaperRuntimeEnvironmentReceipt,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 22, 0, tzinfo=UTC)
GENERATION = "2307d717-48da-4bdc-8c1f-682d1d29bcf8"
ACCOUNT = "2b7ffb65-b682-4c86-af7c-0d70f44fa2c2"


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def write(path: Path, value: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def configuration(tmp_path: Path) -> operator.CapturedPaperOperatorConfiguration:
    candidate = tmp_path / "candidate"
    output = tmp_path / "operator"
    preactivation = tmp_path / "preactivation"
    activation = tmp_path / "activation"
    capture = tmp_path / "capture"
    dependencies = tmp_path / "site-packages"
    for path in (candidate, output, preactivation, activation, capture, dependencies):
        path.mkdir()
    runtime = write(tmp_path / "paper.env", b"paper-env-fixture")
    bootstrap_body = {
        "schema_version": contract.IQFEED_BOOTSTRAP_MANIFEST_SCHEMA_VERSION,
        "fixture": True,
    }
    bootstrap = write(tmp_path / "bootstrap.json", canonical(bootstrap_body))
    python = write(tmp_path / "python.exe", b"fake-python")
    powershell = write(tmp_path / "powershell.exe", b"fake-powershell")
    task = write(tmp_path / "task.json", b"{}")
    process = write(tmp_path / "process.json", b"{}")
    restore = write(tmp_path / "restore.json", b"{}")
    return operator.CapturedPaperOperatorConfiguration(
        activation_generation=GENERATION,
        expected_account_id=ACCOUNT,
        candidate_root=candidate,
        operator_output_root=output,
        preactivation_output_root=preactivation,
        activation_artifact_root=activation,
        capture_store_root=capture,
        runtime_env_path=runtime,
        runtime_env_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest(),
        iqfeed_bootstrap_manifest_path=bootstrap,
        iqfeed_bootstrap_manifest_sha256=hashlib.sha256(
            bootstrap.read_bytes()
        ).hexdigest(),
        python_executable=python,
        python_dependency_root=dependencies,
        no_order_receipt_output=output / "no-order.json",
        powershell_executable=powershell,
        host_principal_user_id="test-user",
        task_snapshot_path=task,
        task_snapshot_sha256=hashlib.sha256(task.read_bytes()).hexdigest(),
        process_snapshot_path=process,
        process_snapshot_sha256=hashlib.sha256(process.read_bytes()).hexdigest(),
        restore_plan_path=restore,
        restore_plan_sha256=hashlib.sha256(restore.read_bytes()).hexdigest(),
        capture_certification_symbol="TEST",
        allowed_read_roots=(tmp_path,),
    )


def runtime_receipt(config: operator.CapturedPaperOperatorConfiguration):
    body = {
        "schema_version": "chili.captured-paper-runtime-env.v1",
        "source_path": str(config.runtime_env_path),
        "source_sha256": config.runtime_env_sha256,
        "expected_account_id": ACCOUNT,
        "first_dip_policy_mode": "candidate",
        "effective_config": {"CHILI_ALPACA_PAPER": "true"},
        "secret_fingerprints": {
            "DATABASE_URL": h("database"),
            "CHILI_ALPACA_API_KEY": h("key"),
            "CHILI_ALPACA_API_SECRET": h("secret"),
        },
    }
    return CapturedPaperRuntimeEnvironmentReceipt(
        source_path=body["source_path"],
        source_sha256=body["source_sha256"],
        expected_account_id=ACCOUNT,
        first_dip_policy_mode="candidate",
        effective_config=MappingProxyType(body["effective_config"]),
        secret_fingerprints=MappingProxyType(body["secret_fingerprints"]),
        removed_forbidden_keys=("ALPACA_API_KEY",),
        configuration_sha256=readiness.sha256_json(body),
    )


def settings_projection(receipt: CapturedPaperRuntimeEnvironmentReceipt):
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
    body = {
        "schema_version": "chili.captured-paper-settings-projection.v1",
        "runtime_environment_sha256": receipt.configuration_sha256,
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
    return {**body, "settings_projection_sha256": readiness.sha256_json(body)}


def test_operator_flow_publishes_build_ready_and_only_no_order_next_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    receipt = runtime_receipt(config)
    projection = settings_projection(receipt)
    source_hashes = {
        "activation_launcher": h("activation-launcher"),
        "activation_stage0": h("activation-stage0"),
        "activation_service": h("activation-service"),
        "captured_paper_host_cutover": h("host-cutover"),
        "captured_paper_preactivation_probes": h("probes"),
        "iqfeed_capture_host": h("host"),
        "iqfeed_trade_bridge": h("trade"),
        "iqfeed_depth_bridge": h("depth"),
        "iqfeed_l1_capture": h("l1"),
        "iqfeed_l2_capture": h("l2"),
    }
    inventory = SimpleNamespace(
        code_build_sha256=h("code-build"),
        source_hashes=MappingProxyType(source_hashes),
    )
    monkeypatch.setattr(
        operator.builder, "inventory_captured_paper_code", lambda *a, **k: inventory
    )

    launcher = config.activation_artifact_root / h("launcher") / f"{h('launcher')}.ps1"
    service = config.activation_artifact_root / h("service") / f"{h('service')}.py"
    stage0 = config.activation_artifact_root / h("stage0") / f"{h('stage0')}.py"
    for path in (launcher, service, stage0):
        write(path, path.name.encode())
    read_roots = [str(tmp_path.resolve())]
    base_projection = {
        "allowed_read_roots": read_roots,
        "candidate_root": str(config.candidate_root),
        "launcher_path": str(launcher),
        "python_executable_path": str(config.python_executable),
        "service_staged_path": str(service),
        "stage0_path": str(stage0),
        "launcher_source_sha256": source_hashes["activation_launcher"],
        "stage0_source_sha256": source_hashes["activation_stage0"],
        "service_source_sha256": source_hashes["activation_service"],
    }
    launcher_document = {
        "schema_version": contract.LAUNCHER_ARGUMENT_CONTRACT_SCHEMA_VERSION,
        "invocations": {
            "ActivatePaper": {
                "projection": {
                    **base_projection,
                    "mode": "ActivatePaper",
                    "service_mode": "activate-paper",
                },
                "projection_sha256": h("activate"),
            },
            "NoOrderSmoke": {
                "projection": {
                    **base_projection,
                    "mode": "NoOrderSmoke",
                    "service_mode": "no-order-smoke",
                    "no_order_receipt_output_path": str(
                        config.no_order_receipt_output
                    ),
                },
                "projection_sha256": h("smoke"),
            },
            "ValidateOnly": {
                "projection": {
                    **base_projection,
                    "mode": "ValidateOnly",
                    "service_mode": "validate-only",
                },
                "projection_sha256": h("validate"),
            },
        },
    }
    monkeypatch.setattr(
        operator.builder,
        "build_launcher_argument_contract_offline",
        lambda **kwargs: launcher_document,
    )
    monkeypatch.setattr(
        operator.host_cutover,
        "build_candidate_task_xml_template",
        lambda **kwargs: b"<Task />",
    )
    monkeypatch.setattr(
        operator.host_cutover,
        "build_candidate_action_document",
        lambda **kwargs: {
            "schema_version": "candidate-action.v1",
            "launcher_argument_contract_sha256": kwargs[
                "launcher_argument_contract_sha256"
            ],
        },
    )
    empty_authority = SimpleNamespace()
    materialized = probes.TrustedProbeAuthorities(
        runtime_settings=empty_authority,
        broker_account=empty_authority,
        database_schema=empty_authority,
        capture_host_smoke=empty_authority,
        focused_regressions=empty_authority,
        lifecycle_preflight=empty_authority,
        kill_switch=empty_authority,
        rollback_snapshot=empty_authority,
    )
    monkeypatch.setattr(
        operator,
        "_materialize_probe_authorities",
        lambda **kwargs: (materialized, NOW),
    )

    def fake_probe_run(*, output_root, **kwargs):
        refs = {}
        for kind in readiness.PREACTIVATION_KINDS:
            path = write(Path(output_root) / f"{kind}.json", canonical({"kind": kind}))
            refs[kind] = {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
        manifest = {
            "schema_version": "probe.v1",
            "readiness_receipts": refs,
        }
        path = write(Path(output_root) / "manifest.json", canonical(manifest))
        return {
            "manifest": manifest,
            "manifest_path": str(path),
            "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    monkeypatch.setattr(operator.probes, "run_trusted_preactivation_probes", fake_probe_run)

    captured_request: dict[str, object] = {}

    def fake_build(**kwargs):
        request = json.loads(Path(kwargs["request_path"]).read_text(encoding="utf-8"))
        captured_request.update(request)
        assert kwargs["request_sha256"] == hashlib.sha256(
            Path(kwargs["request_path"]).read_bytes()
        ).hexdigest()
        document = {
            "schema_version": contract.PREACTIVATION_MANIFEST_SCHEMA_VERSION,
            "activation_generation": GENERATION,
        }
        raw = canonical(document)
        digest = hashlib.sha256(raw).hexdigest()
        path = write(config.preactivation_output_root / digest[:2] / f"{digest}.json", raw)
        return SimpleNamespace(manifest_path=path, manifest_sha256=digest)

    monkeypatch.setattr(
        operator.builder, "build_captured_paper_preactivation_offline", fake_build
    )
    composition = operator.CapturedPaperOperatorComposition(
        configuration=config,
        runtime_receipt=receipt,
        settings_projection=projection,
        paper_adapter=object(),
        database_engine=object(),
        migrations_module=object(),
        capture_smoke_runner=lambda: None,
        test_environment={
            "TEST_DATABASE_URL": "postgresql://test:test@localhost:5433/chili_test"
        },
        wall_clock=lambda: NOW,
    )

    result = operator.run_captured_paper_operator_flow(composition)

    assert result.paper_order_submission_authorized is False
    assert result.paper_service_started is False
    assert result.live_cash_authorized is False
    assert (
        result.host_snapshot_authority
        == "PREACTIVATION_BASELINE_FROM_EXTERNAL_RAW_SNAPSHOT"
    )
    assert result.current_host_inventory_observed is False
    assert result.final_real_validate_only_required is True
    next_command = json.loads(result.next_command_path.read_text(encoding="utf-8"))
    assert next_command["next_step"] == "NO_ORDER_SMOKE_ONLY"
    assert next_command["invoked"] is False
    assert next_command["activate_paper_command_emitted"] is False
    assert next_command["current_host_inventory_observed"] is False
    assert next_command["final_real_validate_only_required"] is True
    assert "ActivatePaper" not in next_command["arguments"]
    assert "NoOrderSmoke" in next_command["arguments"]
    assert set(captured_request["readiness_receipts"]) == set(
        readiness.PREACTIVATION_KINDS
    )
    serialized_request = canonical(captured_request)
    assert b"test:test" not in serialized_request
    assert b"CHILI_ALPACA_API_SECRET" not in serialized_request

    with pytest.raises(
        operator.CapturedPaperOperatorFlowError, match="OUTPUT_ALREADY_EXISTS"
    ):
        operator.run_captured_paper_operator_flow(composition)


def test_fixed_migration_rehearsal_owns_selection_and_rejects_forbidden_effects(
    tmp_path: Path,
) -> None:
    assert operator.MIGRATION_REHEARSAL_NODE_IDS == (
        "tests/test_captured_paper_outbox.py::"
        "test_migration_337_is_registered_idempotent_and_installs_guards",
        "tests/test_alpaca_fill_settlement_runtime_wiring.py::"
        "test_migration_336_preserves_v1_and_requires_strict_v2",
        "tests/test_captured_paper_selection_producer.py::"
        "test_migration_350_is_registered_idempotent_and_installs_guards",
        "tests/test_captured_paper_selection_producer.py::"
        "test_batch_upsert_and_frontier_cas_commit_together",
        "tests/test_captured_paper_selection_producer.py::"
        "test_migration_353_route_state_schema_and_cas_guards",
        "tests/test_captured_paper_variant_binding.py::"
        "test_migration_352_receipt_and_append_only_transition_round_trip",
    )
    python = write(tmp_path / "python.exe", b"python")
    seen: list[tuple[str, ...]] = []

    def runner(command, *, cwd, env, capture_output, check):
        del cwd, capture_output, check
        seen.append(tuple(command))
        assert env["DATABASE_URL"].endswith("chili_test")
        body = {
            "schema_version": "chili.captured-paper-pytest-side-effect-census.v1",
            "events": [
                {"event_type": "fake_transport", "count": 0},
                {"event_type": "real_network", "count": 0},
                {"event_type": "live_cash", "count": 0},
                {"event_type": "broker_post", "count": 0},
            ],
        }
        document = {**body, "report_sha256": readiness.sha256_json(body)}
        Path(env["CHILI_CAPTURED_PAPER_SIDE_EFFECT_REPORT"]).write_bytes(
            canonical(document)
        )
        junit_argument = next(
            item for item in command if str(item).startswith("--junitxml=")
        )
        junit_path = Path(str(junit_argument).split("=", 1)[1])
        cases = "".join(
            f'<testcase name="{node.rsplit("::", 1)[1]}" />'
            for node in operator.MIGRATION_REHEARSAL_NODE_IDS
        )
        junit_path.write_text(
            '<testsuites><testsuite tests="6" failures="0" errors="0" '
            f'skipped="0">{cases}</testsuite></testsuites>',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    rehearsal = operator.FixedMigrationRehearsalRunner(
        candidate_root=tmp_path,
        python_executable=python,
        environment={
            "TEST_DATABASE_URL": "postgresql://test:test@localhost:5433/chili_test",
            "CHILI_ALPACA_API_SECRET": "must-not-reach-child",
        },
        command_runner=runner,
    )
    assert rehearsal() == (0, 0, 0, 0, 0, 0)
    assert len(seen) == 1
    for node in operator.MIGRATION_REHEARSAL_NODE_IDS:
        assert node in seen[0]
    assert "tests/test_easy.py" not in seen[0]

    unsafe = operator.FixedMigrationRehearsalRunner(
        candidate_root=tmp_path,
        python_executable=python,
        environment={
            "TEST_DATABASE_URL": "postgresql://prod:prod@localhost:5432/chili"
        },
        command_runner=runner,
    )
    with pytest.raises(
        operator.CapturedPaperOperatorFlowError, match="TEST_DATABASE_UNSAFE"
    ):
        unsafe()


def test_launcher_staging_reinventories_and_rejects_any_source_drift(
    tmp_path: Path,
) -> None:
    candidate = tmp_path.resolve()
    hashes = {
        "activation_launcher": h("launcher"),
        "activation_stage0": h("stage0"),
        "activation_service": h("service"),
        "captured_paper_host_cutover": h("host"),
    }
    initial = SimpleNamespace(
        code_build_sha256=h("build"), source_hashes=MappingProxyType(hashes)
    )
    projection = {
        "candidate_root": str(candidate),
        "launcher_source_sha256": hashes["activation_launcher"],
        "stage0_source_sha256": hashes["activation_stage0"],
        "service_source_sha256": hashes["activation_service"],
    }
    launcher_document = {
        "invocations": {
            mode: {"projection": dict(projection)}
            for mode in ("ActivatePaper", "NoOrderSmoke", "ValidateOnly")
        }
    }
    operator._assert_launcher_inventory_binding(
        initial_inventory=initial,
        repeated_inventory=initial,
        launcher_document=launcher_document,
        candidate_root=candidate,
    )

    drifted_hashes = dict(hashes)
    drifted_hashes["captured_paper_host_cutover"] = h("changed")
    drifted = SimpleNamespace(
        code_build_sha256=h("changed-build"),
        source_hashes=MappingProxyType(drifted_hashes),
    )
    with pytest.raises(
        operator.CapturedPaperOperatorFlowError, match="CODE_INVENTORY_DRIFT"
    ):
        operator._assert_launcher_inventory_binding(
            initial_inventory=initial,
            repeated_inventory=drifted,
            launcher_document=launcher_document,
            candidate_root=candidate,
        )

    mismatched = json.loads(json.dumps(launcher_document))
    mismatched["invocations"]["NoOrderSmoke"]["projection"][
        "service_source_sha256"
    ] = h("foreign-service")
    with pytest.raises(
        operator.CapturedPaperOperatorFlowError,
        match="LAUNCHER_INVENTORY_MISMATCH",
    ):
        operator._assert_launcher_inventory_binding(
            initial_inventory=initial,
            repeated_inventory=initial,
            launcher_document=mismatched,
            candidate_root=candidate,
        )


def test_materialization_runs_runtime_after_long_shards_and_short_ttl_reads_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    receipt = runtime_receipt(config)
    projection = settings_projection(receipt)
    order: list[str] = []

    class Authority:
        def __init__(self, label: str) -> None:
            self.label = label

        def observe(self):
            order.append(self.label)
            return object()

        def execute(self):
            order.append(self.label)
            return object()

    monkeypatch.setattr(
        operator.probes,
        "InstalledRuntimeSettingsAuthority",
        lambda **kwargs: Authority("runtime"),
    )
    monkeypatch.setattr(
        operator.probes,
        "SubprocessFocusedRegressionAuthority",
        lambda **kwargs: Authority("focused"),
    )
    monkeypatch.setattr(
        operator.probes,
        "SubprocessLifecycleScenarioAuthority",
        lambda **kwargs: Authority("lifecycle"),
    )
    monkeypatch.setattr(
        operator.probes,
        "HostCutoverPreactivationBaselineAuthority",
        lambda **kwargs: Authority("rollback"),
    )

    class Rehearsal:
        def __call__(self):
            order.append("rehearsal")
            return (0,)

    monkeypatch.setattr(
        operator, "FixedMigrationRehearsalRunner", lambda **kwargs: Rehearsal()
    )
    monkeypatch.setattr(
        operator.probes,
        "CaptureOnlySmokeReadAuthority",
        lambda **kwargs: Authority("capture"),
    )
    monkeypatch.setattr(
        operator.probes,
        "SqlAlchemyDatabaseReadAuthority",
        lambda **kwargs: Authority("database"),
    )
    monkeypatch.setattr(
        operator.probes,
        "SqlAlchemyKillSwitchReadAuthority",
        lambda **kwargs: Authority("kill"),
    )

    class RecordedAdapter:
        pass

    def record(*args, **kwargs):
        order.append("broker")
        return RecordedAdapter()

    monkeypatch.setattr(operator, "_record_exact_broker_reads", record)
    context = readiness.ReadinessValidationContext(
        activation_generation=GENERATION,
        expected_account_id=ACCOUNT,
        code_build_sha256=h("code"),
        effective_config_sha256=h("config"),
        capture_receipt_sha256=h("capture"),
        runtime_environment_sha256=receipt.configuration_sha256,
        database_target_fingerprint=h("database"),
        iqfeed_bootstrap_manifest_sha256=h("bootstrap"),
        launcher_argument_contract_sha256=h("launcher"),
        capture_store_root=str(config.capture_store_root),
        source_hashes={},
        allowed_read_roots=(str(tmp_path),),
    )
    composition = operator.CapturedPaperOperatorComposition(
        configuration=config,
        runtime_receipt=receipt,
        settings_projection=projection,
        paper_adapter=object(),
        database_engine=object(),
        migrations_module=object(),
        capture_smoke_runner=lambda: None,
        test_environment={
            "TEST_DATABASE_URL": "postgresql://u:p@localhost:5433/chili_test"
        },
        wall_clock=lambda: NOW,
    )

    roster, observed_at = operator._materialize_probe_authorities(
        composition=composition,
        context=context,
        candidate_template_path=tmp_path / "candidate.xml",
        candidate_action_path=tmp_path / "action.json",
    )

    assert type(roster) is probes.TrustedProbeAuthorities
    assert observed_at == NOW
    assert order == [
        "focused",
        "rollback",
        "rehearsal",
        "lifecycle",
        "runtime",
        "capture",
        "database",
        "kill",
        "broker",
    ]


@pytest.mark.parametrize(
    "url",
    (
        "postgresql://u:p@db.internal:5433/chili_test",
        "postgresql://u:p@localhost:5432/chili_test",
        "postgresql://u:p@localhost/chili_test",
        "postgresql://u:p@localhost:5433/chili_test?target=other",
        "postgresql://u:p@localhost:5433/chili_test#fragment",
        "postgresql://u:p@localhost:5433/prod%5ftest",
        "mysql://u:p@localhost:3307/chili_test",
        "postgresql://u:p@localhost:5433/not_test_database",
    ),
)
def test_test_database_url_requires_unambiguous_loopback_nondefault_port(
    url: str,
) -> None:
    with pytest.raises(
        operator.CapturedPaperOperatorFlowError, match="TEST_DATABASE_UNSAFE"
    ):
        operator._sanitized_test_environment({"TEST_DATABASE_URL": url})

    safe = operator._sanitized_test_environment(
        {
            "TEST_DATABASE_URL": (
                "postgresql+psycopg://u:p@127.0.0.1:5433/chili_test"
            )
        }
    )
    assert safe["DATABASE_URL"].endswith(":5433/chili_test")


def test_test_environment_removes_ambient_pytest_and_python_controls() -> None:
    safe = operator._sanitized_test_environment(
        {
            "TEST_DATABASE_URL": "postgresql://u:p@localhost:5433/chili_test",
            "PYTEST_ADDOPTS": "--collect-only",
            "PYTEST_PLUGINS": "untrusted_plugin",
            "PYTHONPATH": "C:/untrusted",
            "PYTHONOPTIMIZE": "2",
            "COVERAGE_PROCESS_START": "C:/untrusted.coveragerc",
        }
    )
    assert "PYTEST_ADDOPTS" not in safe
    assert "PYTEST_PLUGINS" not in safe
    assert "PYTHONPATH" not in safe
    assert "PYTHONOPTIMIZE" not in safe
    assert "COVERAGE_PROCESS_START" not in safe
    assert safe["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert safe["PYTHONNOUSERSITE"] == "1"


def _audit(generation: str) -> dict[str, object]:
    body = {
        "schema_version": "chili.alpaca-paper-order-submission-audit.v1",
        "broker_environment": "paper",
        "asset_class": "us_equity",
        "provider_account_id": ACCOUNT,
        "adapter_connection_generation": generation,
        "adapter_build_sha256": h("adapter"),
        "audit_generation": "audit",
        "submission_call_count": 0,
        "submission_chain_sha256": h("chain"),
    }
    text = canonical(body).decode()
    return {
        **body,
        "snapshot_canonical_json": text,
        "snapshot_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def test_broker_read_is_recorded_in_memory_and_revalidated_without_post() -> None:
    generation = "alpaca-paper-rest:" + h("generation")

    class Adapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_paper_connection_generation_receipt(self):
            self.calls.append("connection")
            body = {
                "schema_version": "chili.alpaca-paper-connection-generation.v1",
                "broker_environment": "paper",
                "asset_class": "us_equity",
                "provider_account_id": ACCOUNT,
                "adapter_connection_generation": generation,
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
            self.calls.append("audit")
            return _audit(generation)

        def get_account_snapshot(self):
            self.calls.append("account")
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
            self.calls.append("positions")
            return {
                "readable": True,
                "pagination_complete": True,
                "broker_environment": "paper",
                "asset_class": "us_equity",
                "provider_account_id": ACCOUNT,
                "adapter_connection_generation": generation,
                "positions": [],
                "inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
            }

        def get_paper_open_order_census(self, *, read_binding):
            self.calls.append("orders")
            return {
                "readable": True,
                "pagination_complete": True,
                "broker_environment": "paper",
                "asset_class": "us_equity",
                "provider_account_id": ACCOUNT,
                "adapter_connection_generation": generation,
                "orders": [],
                "inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
            }

    context = readiness.ReadinessValidationContext(
        activation_generation=GENERATION,
        expected_account_id=ACCOUNT,
        code_build_sha256=h("code"),
        effective_config_sha256=h("config"),
        capture_receipt_sha256=h("capture"),
        runtime_environment_sha256=h("runtime"),
        database_target_fingerprint=h("database"),
        iqfeed_bootstrap_manifest_sha256=h("bootstrap"),
        launcher_argument_contract_sha256=h("launcher"),
        capture_store_root="C:/capture",
        source_hashes={},
        allowed_read_roots=("C:/",),
    )
    live = Adapter()
    recorded = operator._record_exact_broker_reads(live, context=context)
    result = probes._broker_observations(
        probes.AlpacaPaperBrokerReadAuthority(recorded),
        context=context,
        now=NOW,
    )

    assert result["paper_execution_only"] is True
    assert result["position_count"] == result["open_order_count"] == 0
    assert live.calls == [
        "account",
        "connection",
        "audit",
        "positions",
        "orders",
        "audit",
    ]
    assert not hasattr(recorded, "post_limit_buy")
