from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from scripts import captured_paper_host_cutover as cutover
from scripts import captured_paper_readiness_evidence as readiness


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _issuer_apply_cmdline(
    prepared: cutover.PreparedCutover, journal_root: Path
) -> list[str]:
    source = Path(cutover.__file__).resolve(strict=True)
    values = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        prepared.invocation.stage0_script_path,
        "--manifest", str(prepared.manifest_path),
        "--manifest-sha256", prepared.manifest_sha256,
        "--candidate-root", str(prepared.candidate_root),
        "--target-role", "captured_paper_host_cutover",
        "--target", str(source),
        "--target-sha256", hashlib.sha256(source.read_bytes()).hexdigest(),
        "--",
        "--mode", cutover.MODE_APPLY,
        "--manifest", str(prepared.manifest_path),
        "--manifest-sha256", prepared.manifest_sha256,
        "--candidate-root", str(prepared.candidate_root),
    ]
    for root in prepared.allowed_read_roots:
        values.extend(("--allow-read-root", str(root)))
    values.extend(
        (
            "--task-snapshot", str(prepared.task_snapshot.artifact_path),
            "--process-snapshot", str(prepared.process_snapshot.artifact_path),
            "--restore-plan", str(prepared.restore_plan.artifact_path),
            "--candidate-task-template", str(prepared.candidate_template_path),
            "--candidate-action", str(prepared.candidate_action_path),
            "--journal-root", str(journal_root),
            "--confirm-fake-money-paper", cutover.APPLY_CONFIRMATION,
        )
    )
    return values


@pytest.fixture(autouse=True)
def _deterministic_apply_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    def probe(
        *, prepared: cutover.PreparedCutover, journal_root: Path
    ) -> dict[str, object]:
        executable = Path(sys.executable).resolve(strict=True)
        source = Path(cutover.__file__).resolve(strict=True)
        cmdline = _issuer_apply_cmdline(prepared, journal_root)
        return {
            "issuer_pid": os.getpid(),
            "issuer_create_time_ns": 1_700_000_000_000_000_001,
            "issuer_executable_path": str(executable),
            "issuer_executable_sha256": hashlib.sha256(
                executable.read_bytes()
            ).hexdigest(),
            "issuer_cmdline": cmdline,
            "issuer_cmdline_sha256": cutover.sha256_json(cmdline),
            "issuer_source_path": str(source),
            "issuer_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }

    monkeypatch.setattr(cutover, "_issuer_provenance", probe)


def _task_xml(
    name: str,
    enabled: bool = True,
    *,
    command: str = r"C:\Windows\System32\cmd.exe",
    arguments: str = "/c exit 0",
) -> bytes:
    value = "true" if enabled else "false"
    if name.endswith("-Logon"):
        trigger = "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>"
    else:
        trigger = (
            "<CalendarTrigger><StartBoundary>2026-01-01T10:36:00</StartBoundary>"
            "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
            "</CalendarTrigger>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Task version="1.4" xmlns="{NS}">'
        f"<RegistrationInfo><Description>{name}</Description></RegistrationInfo>"
        f"<Triggers>{trigger}</Triggers>"
        '<Principals><Principal id="Author">'
        "<UserId>S-1-5-21-1111111111-2222222222-3333333333-1001</UserId>"
        "<LogonType>InteractiveToken</LogonType>"
        "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>"
        f"<Settings><Enabled>{value}</Enabled></Settings>"
        f'<Actions Context="Author"><Exec><Command>{command}</Command>'
        f"<Arguments>{arguments}</Arguments></Exec></Actions></Task>"
    ).encode()


def _set_task_enabled(raw: bytes, enabled: bool) -> bytes:
    root = ET.fromstring(raw)
    node = root.find(f".//{{{NS}}}Settings/{{{NS}}}Enabled")
    assert node is not None
    node.text = "true" if enabled else "false"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def test_task_xml_missing_enabled_uses_scheduler_schema_default_true() -> None:
    raw = _task_xml("schema-default").replace(
        b"<Enabled>true</Enabled>", b""
    )

    assert cutover._task_enabled_from_xml(raw) is True
    assert cutover._task_enabled_from_xml(
        _task_xml("explicit-disabled", enabled=False)
    ) is False


def _identity(
    *,
    pid: int,
    role: str,
    executable: Path,
    script: Path | None,
    cmdline: tuple[str, ...],
) -> cutover.ProcessIdentity:
    return cutover.ProcessIdentity(
        pid=pid,
        create_time_ns=1_700_000_000_000_000_000 + pid,
        executable_path=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        cmdline=cmdline,
        cmdline_sha256=cutover.sha256_json(list(cmdline)),
        role=role,
        bridge_script_path=str(script) if script else None,
        bridge_script_sha256=(
            hashlib.sha256(script.read_bytes()).hexdigest() if script else None
        ),
    )


def _docker_lane_inspect(
    *,
    state: str = "running",
    config_tag: str = "v1",
    restart_name: str = "unless-stopped",
    auto_remove: bool = False,
) -> dict:
    running = state == "running"
    return {
        "Id": "d" * 64,
        "Image": "sha256:" + "e" * 64,
        "Name": "/" + cutover.LEGACY_EXECUTION_LANE_NAME,
        "Created": "2026-07-16T00:00:00Z",
        "Path": "/usr/local/bin/python",
        "Args": ["-m", "app", config_tag],
        "Config": {
            "Image": "chili:test",
            "Entrypoint": ["/entrypoint"],
            "Cmd": ["python", "-m", "app"],
            "Env": [
                "SECRET_MUST_NOT_BE_HASHED=value",
                "CHILI_ALPACA_ENABLED=1",
                "CHILI_ALPACA_PAPER=1",
                "CHILI_MOMENTUM_EQUITY_EXECUTION_VIA_ALPACA_PAPER=true",
                "CHILI_MOMENTUM_PAPER_RUNNER_ENABLED=true",
                "CHILI_MOMENTUM_PAPER_RUNNER_SCHEDULER_ENABLED=true",
                "CHILI_AUTOTRADER_ENABLED=false",
                "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED=false",
                "CHILI_MOMENTUM_EXEC_AUTO_ARM_LIVE_ENABLED=false",
                "CHILI_MOMENTUM_EXEC_LIVE_RUNNER_ENABLED=false",
                "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=false",
                "CHILI_COINBASE_AUTOTRADER_LIVE=0",
                "COINBASE_AUTOTRADER_LIVE=0",
            ],
            "Labels": {
                "com.docker.compose.project": "chili-home-copilot",
                "com.docker.compose.service": "momentum-exec-worker",
                "com.docker.compose.project.config_files": (
                    r"D:\dev\chili-home-copilot\docker-compose.yml"
                ),
                "mutable": "ignored",
            },
        },
        "HostConfig": {
            "RestartPolicy": {"Name": restart_name, "MaximumRetryCount": 0},
            "AutoRemove": auto_remove,
            "Binds": ["ignored"],
        },
        "State": {
            "Status": "running" if running else "exited",
            "Running": running,
            "Paused": False,
            "Restarting": False,
            "Dead": False,
        },
    }


def _docker_backend_with_command(command):
    backend = object.__new__(cutover.WindowsHostCutoverBackend)
    backend._docker_command = command
    tasks = {
        name: cutover.TaskObservation(name, _task_xml(name), True)
        for name in cutover.EXECUTION_LANE_RECREATOR_TASKS
    }

    def probe():
        return tuple(
            cutover.ExecutionLaneRecreatorTaskObservation(
                name=name,
                definition_sha256=cutover._task_definition_sha_ignoring_enabled(
                    task.xml
                ),
                action_sha256=cutover.sha256_json(
                    {"name": name, "kind": "action"}
                ),
                source_chain_sha256=cutover.sha256_json(
                    {"name": name, "kind": "source-chain"}
                ),
                enabled=task.enabled,
            )
            for name, task in sorted(tasks.items())
        )

    def set_enabled(name, enabled):
        current = tasks[name]
        tasks[name] = cutover.TaskObservation(
            name, _set_task_enabled(current.xml, enabled), enabled
        )

    backend._execution_lane_recreator_probe = probe
    backend.get_task = lambda name: tasks.get(name)
    backend.set_task_enabled = set_enabled
    backend.stop_task = lambda _name: None
    # This unit fixture owns only the modeled Docker/task state.  Do not let
    # the production descendant inventory inspect unrelated processes on the
    # developer host during an otherwise hermetic state-transition test.
    backend.await_execution_lane_recreator_processes = (
        lambda *, timeout_seconds: ()
    )
    return backend


def test_docker_execution_lane_stop_and_restore_use_full_container_id() -> None:
    state = {"value": "running"}
    calls: list[tuple[str, ...]] = []

    def command(arguments):
        args = tuple(arguments)
        calls.append(args)
        if args[0] == "stop":
            assert args[1] == "d" * 64
            state["value"] = "stopped"
            return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)
        if args[0] == "start":
            assert args[1] == "d" * 64
            state["value"] = "running"
            return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)
        return SimpleNamespace(
            stdout=json.dumps(
                _docker_lane_inspect(state=state["value"])
            ).encode("utf-8"),
            stderr=b"",
            returncode=0,
        )

    backend = _docker_backend_with_command(command)
    baseline = backend.inspect_legacy_execution_lane()
    assert baseline.state == "running"
    assert backend.quiesce_legacy_execution_lane(expected=baseline) == (
        len(cutover.EXECUTION_LANE_RECREATOR_TASKS) * 2 + 1
    )
    assert state["value"] == "stopped"
    assert not any(
        item.enabled
        for item in backend.inspect_legacy_execution_lane().recreator_tasks
    )
    assert not any(item[0] == "pause" for item in calls)
    assert backend.restore_legacy_execution_lane(expected=baseline) == (
        len(cutover.EXECUTION_LANE_RECREATOR_TASKS) + 1
    )
    assert state["value"] == "running"
    assert ("stop", "d" * 64) in calls
    assert ("start", "d" * 64) in calls


def test_docker_execution_lane_identity_drift_blocks_before_stop() -> None:
    calls: list[tuple[str, ...]] = []

    def command(arguments):
        args = tuple(arguments)
        calls.append(args)
        document = _docker_lane_inspect(config_tag="replacement")
        return SimpleNamespace(
            stdout=json.dumps(document).encode("utf-8"),
            stderr=b"",
            returncode=0,
        )

    backend = _docker_backend_with_command(command)
    expected = cutover.LegacyExecutionLaneObservation(
        container_name=cutover.LEGACY_EXECUTION_LANE_NAME,
        container_id="d" * 64,
        image_id="sha256:" + "e" * 64,
        config_sha256="f" * 64,
        execution_scope="legacy:mixed-paper-config-live-masters-disabled",
        scope_sha256="9" * 64,
        recreator_tasks=tuple(
            cutover.ExecutionLaneRecreatorTaskObservation(
                name=name,
                definition_sha256="1" * 64,
                action_sha256="2" * 64,
                source_chain_sha256="3" * 64,
                enabled=True,
            )
            for name in sorted(cutover.EXECUTION_LANE_RECREATOR_TASKS)
        ),
        state="running",
    )
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="EXECUTION_LANE_IDENTITY_DRIFT",
    ):
        backend.quiesce_legacy_execution_lane(expected=expected)
    assert not any(item[0] == "stop" for item in calls)


def test_docker_execution_lane_config_hash_excludes_secrets_and_labels() -> None:
    first = _docker_lane_inspect()
    second = _docker_lane_inspect()
    second["Config"]["Env"] = [
        "SECRET_MUST_NOT_BE_HASHED=other"
        if item.startswith("SECRET_MUST_NOT_BE_HASHED=")
        else item
        for item in second["Config"]["Env"]
    ]
    second["Config"]["Labels"]["mutable"] = "different"
    documents = iter((first, second))

    def command(_arguments):
        return SimpleNamespace(
            stdout=json.dumps(next(documents)).encode("utf-8"),
            stderr=b"",
            returncode=0,
        )

    backend = _docker_backend_with_command(command)
    one = backend.inspect_legacy_execution_lane()
    two = backend.inspect_legacy_execution_lane()
    assert one.identity_key() == two.identity_key()


@pytest.mark.parametrize(
    ("document", "reason_code"),
    [
        (
            _docker_lane_inspect(auto_remove=True),
            "EXECUTION_LANE_ROLLBACK_UNSAFE",
        ),
        (
            _docker_lane_inspect(restart_name="always"),
            "EXECUTION_LANE_RESTART_POLICY_UNSAFE",
        ),
    ],
)
def test_docker_execution_lane_rejects_rollback_unsafe_lifecycle_policy(
    document: dict,
    reason_code: str,
) -> None:
    backend = _docker_backend_with_command(
        lambda _arguments: SimpleNamespace(
            stdout=json.dumps(document).encode("utf-8"),
            stderr=b"",
            returncode=0,
        )
    )

    with pytest.raises(cutover.CapturedPaperHostCutoverError, match=reason_code):
        backend.inspect_legacy_execution_lane()


def test_windows_backend_allows_only_recreator_query_toggle_and_end(
    tmp_path: Path,
) -> None:
    backend = object.__new__(cutover.WindowsHostCutoverBackend)
    calls: list[tuple[str, ...]] = []

    def task_command(arguments, **_kwargs):
        args = tuple(arguments)
        calls.append(args)
        if args[0] == "/Query":
            return SimpleNamespace(
                stdout=_task_xml(str(args[2])), stderr=b"", returncode=0
            )
        return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

    backend._task_command = task_command
    forbidden_xml = tmp_path / "forbidden.xml"
    forbidden_xml.write_bytes(_task_xml("forbidden"))
    for name in cutover.EXECUTION_LANE_RECREATOR_TASKS:
        assert backend.get_task(name) is not None
        backend.set_task_enabled(name, False)
        backend.set_task_enabled(name, True)
        backend.stop_task(name)
        with pytest.raises(
            cutover.CapturedPaperHostCutoverError, match="TASK_NAME_INVALID"
        ):
            backend.register_task(
                name,
                forbidden_xml,
                cutover.sha256_bytes(forbidden_xml.read_bytes()),
            )
        with pytest.raises(
            cutover.CapturedPaperHostCutoverError, match="TASK_NAME_INVALID"
        ):
            backend.start_task(name)
        with pytest.raises(
            cutover.CapturedPaperHostCutoverError, match="TASK_NAME_INVALID"
        ):
            backend.delete_task(name)

    for name in cutover.EXECUTION_LANE_RECREATOR_TASKS:
        assert ("/Query", "/TN", name, "/XML") in calls
        assert ("/Change", "/TN", name, "/DISABLE") in calls
        assert ("/Change", "/TN", name, "/ENABLE") in calls
        assert ("/End", "/TN", name) in calls


@pytest.mark.parametrize("python_name", ["python.exe", "pythonw.exe"])
def test_recreator_inventory_detects_orphaned_python_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    python_name: str,
) -> None:
    import psutil

    orchestrator_path = (
        r"D:\CHILI-Docker\captured-paper\premarket-activation\orchestrator.py"
    )

    class OrphanedOrchestrator:
        info = {"pid": 4242, "name": python_name}

        @staticmethod
        def cmdline() -> list[str]:
            return [
                rf"C:\Users\rindo\miniconda3\envs\chili-env\{python_name}",
                orchestrator_path,
            ]

    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda *_args, **_kwargs: (OrphanedOrchestrator(),),
    )
    backend = object.__new__(cutover.WindowsHostCutoverBackend)

    assert backend.await_execution_lane_recreator_processes(
        timeout_seconds=0.0
    ) == (f"4242:{python_name}:matched",)


def _active_start_authority_fixture(
    prepared: cutover.PreparedCutover,
    *,
    permit_sha256: str,
    quiet_horizon_event_sha256: str,
) -> dict[str, object]:
    snapshot = {
        "position_count": 0,
        "open_order_count": 0,
        "order_submission_call_count": 0,
    }
    order_census = {"exact_order_count": 0}
    fill_census = {"exact_activity_count": 0}
    broker = {
        "schema_version": "chili.captured-paper-broker-fixed-point.v1",
        "verdict": "PAPER_BROKER_QUIET_FIXED_POINT",
        "account_scope": "alpaca:paper",
        "expected_account_id": prepared.expected_account_id,
        "activation_generation": prepared.activation_generation,
        "activation_manifest_sha256": prepared.manifest_sha256,
        "assumption_bound": True,
        "live_cash_certification": False,
        "baseline_snapshot": snapshot,
        "first_snapshot": snapshot,
        "first_order_census": order_census,
        "first_fill_activity_census": fill_census,
        "second_snapshot": snapshot,
        "second_order_census": order_census,
        "second_fill_activity_census": fill_census,
    }
    kill_body = {
        "schema_version": "chili.captured-paper-kill-switch-query.v1",
        "activation_generation": prepared.activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": prepared.expected_account_id,
        "active": False,
    }
    final_kill = {
        **kill_body,
        "query_receipt_sha256": cutover.sha256_json(kill_body),
    }
    body = {
        "schema_version": cutover.ACTIVE_START_AUTHORITY_SCHEMA,
        "verdict": "CAPTURED_ALPACA_PAPER_ACTIVE_START_AUTHORIZED",
        "account_scope": "alpaca:paper",
        "expected_account_id": prepared.expected_account_id,
        "runtime_generation": prepared.activation_generation,
        "activation_manifest_sha256": prepared.manifest_sha256,
        "kill_switch_receipt_sha256": "7" * 64,
        "launcher_attestation_sha256": "8" * 64,
        "launcher_attestation_consumed": True,
        "host_activation_permit_sha256": permit_sha256,
        "host_activation_permit_consumed": True,
        "host_quiet_horizon_event_sha256": quiet_horizon_event_sha256,
        "broker_fixed_point": broker,
        "broker_fixed_point_sha256": cutover.sha256_json(broker),
        "post_permit_broker_snapshot_sha256": cutover.sha256_json(snapshot),
        "order_transition_fence_sha256": cutover.sha256_json(order_census),
        "fill_activity_fence_sha256": cutover.sha256_json(fill_census),
        "final_kill_switch_query": final_kill,
        "final_kill_switch_query_sha256": cutover.sha256_json(final_kill),
        "paper_order_submission_authorized": True,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    body["authority_sha256"] = cutover.sha256_json(body)
    return body


class FakeHost:
    def __init__(
        self,
        prepared: cutover.PreparedCutover,
        *,
        fail_operation: str | None = None,
        fail_after_effect: bool = False,
        execution_lane_state: str = "running",
    ) -> None:
        self.prepared = prepared
        self.tasks = {
            name: cutover.TaskObservation(name, item.xml, item.enabled)
            for name, item in prepared.task_snapshot.tasks.items()
        }
        self.processes = {
            item.pid: item for item in prepared.process_snapshot.processes
        }
        self.fail_operation = fail_operation
        self.fail_after_effect = fail_after_effect
        self.failed = False
        self.mutations: list[str] = []
        self.next_pid = 9000
        self.startup_receipt_overrides: dict[str, dict[str, object]] = {}
        self.startup_challenge = "c" * 64
        self.dispatch_lock_identity: dict[str, object] | None = None
        self.initial_execution_lane_state = execution_lane_state
        self.execution_lane_state = execution_lane_state
        self.execution_lane_container_id = "d" * 64
        self.execution_lane_image_id = "sha256:" + "e" * 64
        self.execution_lane_config_sha256 = "f" * 64
        self.execution_lane_scope_sha256 = "9" * 64
        self.execution_lane_recreator_states = {
            name: True for name in cutover.EXECUTION_LANE_RECREATOR_TASKS
        }

    def _maybe_fail(self, operation: str, *, after: bool = False) -> None:
        if (
            not self.failed
            and self.fail_operation == operation
            and self.fail_after_effect is after
        ):
            self.failed = True
            raise RuntimeError(f"injected:{operation}:{after}")

    def get_task(self, name: str) -> cutover.TaskObservation | None:
        return self.tasks.get(name)

    def set_task_enabled(self, name: str, enabled: bool) -> None:
        operation = f"task:{name}:{'enable' if enabled else 'disable'}"
        self._maybe_fail(operation)
        current = self.tasks[name]
        self.tasks[name] = cutover.TaskObservation(
            name, _set_task_enabled(current.xml, enabled), enabled
        )
        self.mutations.append(operation)
        self._maybe_fail(operation, after=True)

    def register_task(
        self, name: str, xml_path: Path, expected_sha256: str
    ) -> None:
        operation = f"register:{name}"
        self._maybe_fail(operation)
        raw = xml_path.read_bytes()
        assert cutover.sha256_bytes(raw) == expected_sha256
        self.tasks[name] = cutover.TaskObservation(
            name, raw, cutover._task_enabled_from_xml(raw)
        )
        self.mutations.append(operation)
        self._maybe_fail(operation, after=True)

    def start_task(self, name: str) -> None:
        operation = f"start:{name}"
        self._maybe_fail(operation)
        if name == cutover.CANDIDATE_TASK_NAME:
            for kind in ("launcher", "service"):
                self.next_pid += 1
                if kind == "launcher":
                    executable = Path(
                        self.prepared.invocation.powershell_executable_path
                    )
                    arguments = self.prepared.invocation.launcher_arguments
                else:
                    executable = Path(self.prepared.invocation.python_executable_path)
                    arguments = self.prepared.invocation.service_arguments
                self.processes[self.next_pid] = _identity(
                    pid=self.next_pid,
                    role=f"candidate_{kind}",
                    executable=executable,
                    script=None,
                    cmdline=(str(executable), *arguments),
                )
        else:
            binding = next(
                item
                for item in self.prepared.restore_plan.bindings
                if item.restore_task == name
            )
            if not any(item.role == binding.role for item in self.processes.values()):
                self.next_pid += 1
                self.processes[self.next_pid] = _identity(
                    pid=self.next_pid,
                    role=binding.role,
                    executable=Path(binding.executable_path),
                    script=Path(binding.bridge_script_path),
                    cmdline=binding.expected_cmdline,
                )
        self.mutations.append(operation)
        self._maybe_fail(operation, after=True)

    def stop_task(self, name: str) -> None:
        operation = f"stop:{name}"
        self._maybe_fail(operation)
        self.mutations.append(operation)
        self._maybe_fail(operation, after=True)

    def delete_task(self, name: str) -> None:
        operation = f"delete:{name}"
        self._maybe_fail(operation)
        self.tasks.pop(name, None)
        self.mutations.append(operation)
        self._maybe_fail(operation, after=True)

    def find_candidate_tasks(
        self, invocation: cutover.CandidateInvocation
    ) -> tuple[cutover.TaskObservation, ...]:
        del invocation
        return tuple(
            item
            for item in self.tasks.values()
            if item.xml_sha256 == self.prepared.resolved_task_xml_sha256
        )

    def get_process(self, pid: int, *, role: str) -> cutover.ProcessIdentity | None:
        value = self.processes.get(pid)
        return value if value is not None and value.role == role else None

    def stop_process(self, expected: cutover.ProcessIdentity) -> None:
        operation = f"stop-process:{expected.role}"
        self._maybe_fail(operation)
        assert self.processes[expected.pid].semantic_key() == expected.semantic_key()
        self.processes.pop(expected.pid)
        self.mutations.append(operation)
        self._maybe_fail(operation, after=True)

    def find_legacy_processes(
        self, bindings: tuple[cutover.LegacyProcessBinding, ...]
    ) -> tuple[cutover.ProcessIdentity, ...]:
        roles = {item.role for item in bindings}
        return tuple(
            sorted(
                (item for item in self.processes.values() if item.role in roles),
                key=lambda item: item.role,
            )
        )

    def await_legacy_processes(
        self,
        bindings: tuple[cutover.LegacyProcessBinding, ...],
        *,
        timeout_seconds: float,
    ) -> tuple[cutover.ProcessIdentity, ...]:
        del timeout_seconds
        return self.find_legacy_processes(bindings)

    def await_candidate_processes(
        self, invocation: cutover.CandidateInvocation, *, timeout_seconds: float
    ) -> tuple[cutover.CandidateProcessObservation, ...]:
        del invocation, timeout_seconds
        values = []
        for item in self.processes.values():
            if item.role == "candidate_launcher":
                values.append(cutover.CandidateProcessObservation("launcher", item))
            elif item.role == "candidate_service":
                values.append(cutover.CandidateProcessObservation("service", item))
        return tuple(sorted(values, key=lambda item: item.kind))

    def stop_candidate_process(
        self,
        expected: cutover.CandidateProcessObservation,
        invocation: cutover.CandidateInvocation,
    ) -> None:
        del invocation
        operation = f"stop-candidate:{expected.kind}"
        self._maybe_fail(operation)
        current = self.processes[expected.identity.pid]
        assert current.semantic_key() == expected.identity.semantic_key()
        self.processes.pop(expected.identity.pid)
        self.mutations.append(operation)
        self._maybe_fail(operation, after=True)

    def read_service_startup_receipt(
        self,
        invocation: cutover.CandidateInvocation,
        expected_service: cutover.ProcessIdentity,
        *,
        phase: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        del timeout_seconds
        if phase in self.startup_receipt_overrides:
            return dict(self.startup_receipt_overrides[phase])
        if phase == "prepared":
            if self.dispatch_lock_identity is None:
                paths = cutover._startup_handshake_paths(
                    invocation, roots=self.prepared.allowed_read_roots
                )
                self.dispatch_lock_identity = dict(
                    cutover.create_startup_dispatch_lock(paths["dispatch_lock"])
                )
            return dict(cutover.build_startup_prepared_receipt(
                prepared=self.prepared,
                service=expected_service,
                challenge_sha256=self.startup_challenge,
                dispatch_lock_identity=self.dispatch_lock_identity,
                prepared_at=NOW,
                valid_until=NOW + timedelta(seconds=20),
            ))
        if phase == "started":
            permit_path = Path(f"{invocation.host_ready_receipt_base}.permit.json")
            permit = cutover._strict_json(permit_path.read_bytes(), "fake permit")
            journal_path = Path(str(permit["journal_path"]))
            journal_rows = [
                cutover._strict_json(line, "fake journal event")
                for line in journal_path.read_bytes().splitlines()
                if line
            ]
            quiet_event = next(
                row
                for row in journal_rows
                if row.get("event_type")
                == "legacy_paper_broker_quiet_horizon_completed"
            )
            authority = _active_start_authority_fixture(
                self.prepared,
                permit_sha256=str(permit["permit_sha256"]),
                quiet_horizon_event_sha256=str(quiet_event["event_sha256"]),
            )
            paths = cutover._startup_handshake_paths(
                invocation, roots=self.prepared.allowed_read_roots
            )
            evidence_raw = cutover._canonical_json_bytes(authority)
            paths["active_start_evidence"].write_bytes(evidence_raw)
            return dict(cutover.build_startup_started_receipt(
                prepared=self.prepared,
                service=expected_service,
                challenge_sha256=self.startup_challenge,
                prepared_receipt_sha256=str(permit["prepared_receipt_sha256"]),
                activation_permit_sha256=str(permit["permit_sha256"]),
                active_start_authority=authority,
                active_start_evidence_artifact_sha256=(
                    cutover.sha256_bytes(evidence_raw)
                ),
                started_at=NOW,
                valid_until=NOW + timedelta(seconds=20),
            ))
        raise AssertionError(f"unexpected startup phase {phase}")

    def inspect_legacy_execution_lane(
        self,
    ) -> cutover.LegacyExecutionLaneObservation:
        return cutover.LegacyExecutionLaneObservation(
            container_name=cutover.LEGACY_EXECUTION_LANE_NAME,
            container_id=self.execution_lane_container_id,
            image_id=self.execution_lane_image_id,
            config_sha256=self.execution_lane_config_sha256,
            execution_scope="legacy:mixed-paper-config-live-masters-disabled",
            scope_sha256=self.execution_lane_scope_sha256,
            recreator_tasks=tuple(
                cutover.ExecutionLaneRecreatorTaskObservation(
                    name=name,
                    definition_sha256=cutover.sha256_json(
                        {"name": name, "kind": "definition"}
                    ),
                    action_sha256=cutover.sha256_json(
                        {"name": name, "kind": "action"}
                    ),
                    source_chain_sha256=cutover.sha256_json(
                        {"name": name, "kind": "source-chain"}
                    ),
                    enabled=self.execution_lane_recreator_states[name],
                )
                for name in sorted(cutover.EXECUTION_LANE_RECREATOR_TASKS)
            ),
            state=self.execution_lane_state,
        )

    def await_execution_lane_recreator_processes(
        self, *, timeout_seconds: float
    ) -> tuple[str, ...]:
        del timeout_seconds
        return ()

    def quiesce_legacy_execution_lane(
        self, *, expected: cutover.LegacyExecutionLaneObservation
    ) -> int:
        assert self.inspect_legacy_execution_lane() == expected
        if expected.state == "stopped" and not any(
            item.enabled for item in expected.recreator_tasks
        ):
            return 0
        operation = (
            "execution-lane:stop"
            if expected.state == "running"
            else "execution-lane:quiesce-authorities"
        )
        self._maybe_fail(operation)
        self.execution_lane_state = "stopped"
        self.execution_lane_recreator_states = {
            name: False for name in cutover.EXECUTION_LANE_RECREATOR_TASKS
        }
        self.mutations.append(operation)
        self._maybe_fail(operation, after=True)
        return 1

    def restore_legacy_execution_lane(
        self, *, expected: cutover.LegacyExecutionLaneObservation
    ) -> int:
        assert self.inspect_legacy_execution_lane().identity_key() == expected.identity_key()
        current = self.inspect_legacy_execution_lane()
        if current == expected:
            return 0
        operation = f"execution-lane:restore-{expected.state}"
        self._maybe_fail(operation)
        self.execution_lane_state = expected.state
        self.execution_lane_recreator_states = {
            item.name: item.enabled for item in expected.recreator_tasks
        }
        self.mutations.append(operation)
        self._maybe_fail(operation, after=True)
        return 1


@pytest.fixture
def prepared(tmp_path: Path) -> cutover.PreparedCutover:
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"fake-python")
    powershell = tmp_path / "powershell.exe"
    powershell.write_bytes(b"fake-powershell")
    launcher_raw = b"# sealed fake launcher"
    launcher_sha = hashlib.sha256(launcher_raw).hexdigest()
    launcher_source = tmp_path / "start-captured-alpaca-paper.ps1"
    launcher_source.write_bytes(launcher_raw)
    launcher = tmp_path / "staged" / launcher_sha / f"{launcher_sha}.ps1"
    stage0_source = tmp_path / "captured_paper_isolated_stage0.py"
    stage0_source.write_text("# fake stage0")
    stage0_sha = hashlib.sha256(stage0_source.read_bytes()).hexdigest()
    stage0 = tmp_path / "staged" / stage0_sha / f"{stage0_sha}.py"
    service_source = tmp_path / "captured_alpaca_paper_service.py"
    service_source.write_text("# fake service")
    service_sha = hashlib.sha256(service_source.read_bytes()).hexdigest()
    service = tmp_path / "staged" / f"{service_sha}.py"
    ready_receipt = tmp_path / "service-ready.json"
    trade = tmp_path / "iqfeed_trade_bridge.py"
    trade.write_text("# trade")
    depth = tmp_path / "iqfeed_depth_bridge.py"
    depth.write_text("# depth")
    task_artifact = tmp_path / "task-snapshot.json"
    process_artifact = tmp_path / "process-snapshot.json"
    restore_artifact = tmp_path / "restore-plan.json"
    action_artifact = tmp_path / "candidate-action.json"
    template_artifact = tmp_path / "candidate-task.xml"
    manifest = tmp_path / ("a" * 64 + ".json")
    dependency_root = tmp_path / "site-packages"
    dependency_root.mkdir()
    dependency_identity_sha = (
        cutover.activation_contract.python_dependency_root_identity_sha256(
            dependency_root=dependency_root,
            python_executable=executable,
            python_executable_sha256=hashlib.sha256(
                executable.read_bytes()
            ).hexdigest(),
        )
    )
    for path in (
        task_artifact,
        process_artifact,
        restore_artifact,
        action_artifact,
        template_artifact,
        manifest,
    ):
        path.write_bytes(path.name.encode())
    trade_identity = _identity(
        pid=101,
        role="iqfeed_trade_bridge",
        executable=executable,
        script=trade,
        cmdline=(str(executable), str(trade)),
    )
    depth_identity = _identity(
        pid=102,
        role="iqfeed_depth_bridge",
        executable=executable,
        script=depth,
        cmdline=(str(executable), str(depth)),
    )
    tasks = {}
    for name in cutover.REQUIRED_LEGACY_TASKS:
        identity = depth_identity if "Depth" in name else trade_identity
        raw = _task_xml(
            name,
            command=identity.executable_path,
            arguments=cutover._quote_windows_arguments(identity.cmdline[1:]),
        )
        tasks[name] = cutover.TaskObservation(name, raw, True)
    bindings = (
        cutover.LegacyProcessBinding(
            role="iqfeed_depth_bridge",
            executable_path=str(executable),
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            bridge_script_path=str(depth),
            bridge_script_sha256=hashlib.sha256(depth.read_bytes()).hexdigest(),
            restore_task="CHILI-IQFeed-Depth-Bridge-Daily",
            restore_task_xml_sha256=tasks[
                "CHILI-IQFeed-Depth-Bridge-Daily"
            ].xml_sha256,
            restore_task_action_sha256=cutover._task_action_sha256(
                tasks["CHILI-IQFeed-Depth-Bridge-Daily"].xml
            ),
            expected_cmdline=depth_identity.cmdline,
            expected_cmdline_sha256=depth_identity.cmdline_sha256,
        ),
        cutover.LegacyProcessBinding(
            role="iqfeed_trade_bridge",
            executable_path=str(executable),
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            bridge_script_path=str(trade),
            bridge_script_sha256=hashlib.sha256(trade.read_bytes()).hexdigest(),
            restore_task="CHILI-IQFeed-Trade-Bridge-Daily",
            restore_task_xml_sha256=tasks[
                "CHILI-IQFeed-Trade-Bridge-Daily"
            ].xml_sha256,
            restore_task_action_sha256=cutover._task_action_sha256(
                tasks["CHILI-IQFeed-Trade-Bridge-Daily"].xml
            ),
            expected_cmdline=trade_identity.cmdline,
            expected_cmdline_sha256=trade_identity.cmdline_sha256,
        ),
    )
    projection = {
        "mode": "ActivatePaper",
        "service_mode": "activate-paper",
        "foreground": True,
        "singleton_name": "Global\\CHILI-Captured-Alpaca-PAPER-SINGLETON",
        "candidate_root": str(tmp_path),
        "launcher_source_path": str(launcher_source),
        "launcher_source_sha256": launcher_sha,
        "launcher_path": str(launcher),
        "launcher_sha256": launcher_sha,
        "stage0_source_path": str(stage0_source),
        "stage0_source_sha256": stage0_sha,
        "stage0_path": str(stage0),
        "stage0_sha256": stage0_sha,
        "service_source_path": str(service_source),
        "service_source_sha256": service_sha,
        "service_staged_path": str(service),
        "service_sha256": service_sha,
        "python_executable_path": str(executable),
        "python_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "python_dependency_root": str(dependency_root),
        "python_dependency_root_identity_sha256": dependency_identity_sha,
        "allowed_read_roots": [str(tmp_path)],
        "service_arguments": [
            "-I", "-S", "-B", str(stage0),
            "--manifest", cutover.MANIFEST_PATH_TOKEN,
            "--manifest-sha256", cutover.MANIFEST_SHA256_TOKEN,
            "--candidate-root", str(tmp_path),
            "--target-role", "activation_service",
            "--target", str(service),
            "--target-sha256", service_sha,
            "--",
            "--mode", "activate-paper",
            "--manifest", cutover.MANIFEST_PATH_TOKEN,
            "--manifest-sha256", cutover.MANIFEST_SHA256_TOKEN,
            "--candidate-root", str(tmp_path),
            "--launcher-path", str(launcher),
            "--launcher-sha256", launcher_sha,
            "--host-ready-receipt", str(ready_receipt),
        ],
    }
    template = cutover.build_candidate_task_xml_template(
        principal_user_id="TEST\\paper-user",
        powershell_executable_path=str(powershell),
        activate_paper_projection=projection,
    )
    resolved_xml, invocation = cutover._validate_candidate_template(
        template=template,
        projection=projection,
        manifest_path=manifest,
        manifest_sha256="a" * 64,
    )
    return cutover.PreparedCutover(
        activation_generation="11111111-1111-4111-8111-111111111111",
        expected_account_id="22222222-2222-4222-8222-222222222222",
        manifest_path=manifest,
        manifest_sha256="a" * 64,
        candidate_root=tmp_path,
        allowed_read_roots=(tmp_path,),
        task_snapshot=cutover.TaskSnapshot(
            captured_at=NOW,
            tasks=tasks,
            artifact_path=task_artifact,
            artifact_sha256="1" * 64,
        ),
        process_snapshot=cutover.ProcessSnapshot(
            captured_at=NOW,
            processes=(depth_identity, trade_identity),
            artifact_path=process_artifact,
            artifact_sha256="2" * 64,
        ),
        restore_plan=cutover.RestorePlan(
            task_enabled_states={name: True for name in tasks},
            restart_tasks=(
                "CHILI-IQFeed-Depth-Bridge-Daily",
                "CHILI-IQFeed-Trade-Bridge-Daily",
            ),
            bindings=bindings,
            candidate_task_name=cutover.CANDIDATE_TASK_NAME,
            artifact_path=restore_artifact,
            artifact_sha256="3" * 64,
        ),
        candidate_action_path=action_artifact,
        candidate_action_sha256="4" * 64,
        candidate_template_path=template_artifact,
        candidate_template_sha256="5" * 64,
        resolved_task_xml=resolved_xml,
        resolved_task_xml_sha256=cutover.sha256_bytes(resolved_xml),
        invocation=invocation,
        rollback_receipt_sha256="6" * 64,
    )


def _executor(
    prepared: cutover.PreparedCutover, backend: FakeHost
) -> cutover.CapturedPaperHostCutoverExecutor:
    journal = prepared.candidate_root / "journal"
    journal.mkdir(exist_ok=True)
    monotonic = [0.0]

    def wait(seconds: float) -> None:
        monotonic[0] += float(seconds)

    return cutover.CapturedPaperHostCutoverExecutor(
        prepared=prepared,
        backend=backend,
        journal_root=journal,
        clock=lambda: NOW,
        monotonic_clock=lambda: monotonic[0],
        wait=wait,
    )


def test_preactivation_rollback_baseline_validates_local_bytes_without_final_authority(
    prepared: cutover.PreparedCutover, tmp_path: Path,
) -> None:
    repo = Path(cutover.__file__).resolve().parents[1]
    roots = (repo, tmp_path.resolve())
    task_path = tmp_path / "baseline-task.json"
    process_path = tmp_path / "baseline-process.json"
    restore_path = tmp_path / "baseline-restore.json"
    template_path = tmp_path / "baseline-candidate.xml"
    action_path = tmp_path / "baseline-action.json"
    task_path.write_bytes(
        cutover._canonical_json_bytes(
            cutover.build_task_snapshot_document(
                captured_at=NOW, tasks=prepared.task_snapshot.tasks
            )
        )
    )
    process_path.write_bytes(
        cutover._canonical_json_bytes(
            cutover.build_process_snapshot_document(
                captured_at=NOW, processes=prepared.process_snapshot.processes
            )
        )
    )
    restore_path.write_bytes(
        cutover._canonical_json_bytes(
            cutover.build_restore_plan_document(
                tasks=prepared.task_snapshot.tasks,
                bindings=prepared.restore_plan.bindings,
            )
        )
    )
    projection = {
        "mode": "ActivatePaper",
        "service_mode": "activate-paper",
        "candidate_root": str(repo),
        "launcher_path": prepared.invocation.launcher_script_path,
        "python_executable_path": prepared.invocation.python_executable_path,
        "service_staged_path": prepared.invocation.service_script_path,
        "allowed_read_roots": [str(repo), str(tmp_path.resolve())],
    }
    template = cutover.build_candidate_task_xml_template(
        principal_user_id="TEST\\paper-user",
        powershell_executable_path=prepared.invocation.powershell_executable_path,
        activate_paper_projection=projection,
    )
    template_path.write_bytes(template)
    host_sha = cutover.sha256_bytes(Path(cutover.__file__).read_bytes())
    launcher_contract_sha = "7" * 64
    action_path.write_bytes(
        cutover._canonical_json_bytes(
            cutover.build_candidate_action_document(
                host_cutover_source_sha256=host_sha,
                launcher_argument_contract_sha256=launcher_contract_sha,
                candidate_task_xml_sha256=cutover.sha256_bytes(template),
            )
        )
    )
    baseline = cutover.prepare_preactivation_rollback_baseline(
        cutover.PreActivationRollbackContext(
            activation_generation=prepared.activation_generation,
            expected_account_id=prepared.expected_account_id,
            candidate_root=repo,
            allowed_read_roots=roots,
            host_cutover_source_sha256=host_sha,
            launcher_argument_contract_sha256=launcher_contract_sha,
        ),
        task_snapshot_path=task_path,
        process_snapshot_path=process_path,
        restore_plan_path=restore_path,
        candidate_task_template_path=template_path,
        candidate_action_path=action_path,
        validated_at=NOW,
    )
    document = cutover.build_preactivation_rollback_baseline_document(baseline)
    assert document["validation_mode"] == "PREACTIVATION_ROLLBACK_BASELINE"
    assert document["final_validate_only_performed"] is False
    assert document["host_mutation_count"] == 0
    assert document["paper_order_submission_authorized"] is False
    assert baseline.baseline_sha256 == cutover.sha256_json(document)

    action_path.write_bytes(action_path.read_bytes() + b"\n")
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="ARTIFACT_NOT_CANONICAL",
    ):
        cutover.prepare_preactivation_rollback_baseline(
            baseline.context,
            task_snapshot_path=task_path,
            process_snapshot_path=process_path,
            restore_plan_path=restore_path,
            candidate_task_template_path=template_path,
            candidate_action_path=action_path,
            validated_at=NOW,
        )


def _seed_apply_started(
    journal: cutover.CutoverJournal,
    prepared: cutover.PreparedCutover,
    **extra: object,
) -> None:
    for item in prepared.task_snapshot.tasks.values():
        journal.publish_object(item.xml, kind="legacy_task_xml")
    capsule_raw = cutover._canonical_json_bytes(
        cutover.build_rollback_capsule_document(prepared)
    )
    capsule_path = journal.publish_object(capsule_raw, kind="rollback_capsule")
    journal.append(
        "apply_started",
        {
            "rollback_capsule_path": str(capsule_path),
            "rollback_capsule_sha256": cutover.sha256_bytes(capsule_raw),
            "legacy_execution_lane": dict(
                cutover._legacy_execution_lane_document(
                    cutover.LegacyExecutionLaneObservation(
                        container_name=cutover.LEGACY_EXECUTION_LANE_NAME,
                        container_id="d" * 64,
                        image_id="sha256:" + "e" * 64,
                        config_sha256="f" * 64,
                        execution_scope="legacy:mixed-paper-config-live-masters-disabled",
                        scope_sha256="9" * 64,
                        recreator_tasks=tuple(
                            cutover.ExecutionLaneRecreatorTaskObservation(
                                name=name,
                                definition_sha256=cutover.sha256_json(
                                    {"name": name, "kind": "definition"}
                                ),
                                action_sha256=cutover.sha256_json(
                                    {"name": name, "kind": "action"}
                                ),
                                source_chain_sha256=cutover.sha256_json(
                                    {"name": name, "kind": "source-chain"}
                                ),
                                enabled=True,
                            )
                            for name in sorted(
                                cutover.EXECUTION_LANE_RECREATOR_TASKS
                            )
                        ),
                        state="running",
                    )
                )
            ),
            **extra,
        },
    )


def _assert_restored(prepared: cutover.PreparedCutover, backend: FakeHost) -> None:
    assert cutover.CANDIDATE_TASK_NAME not in backend.tasks
    for name, expected in prepared.task_snapshot.tasks.items():
        assert backend.tasks[name] == expected
    assert sorted(
        item.role
        for item in backend.find_legacy_processes(prepared.restore_plan.bindings)
    ) == ["iqfeed_depth_bridge", "iqfeed_trade_bridge"]
    assert backend.await_candidate_processes(prepared.invocation, timeout_seconds=0) == ()
    assert backend.execution_lane_state == backend.initial_execution_lane_state
    assert all(backend.execution_lane_recreator_states.values())


def test_validate_only_is_default_and_performs_no_mutation(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    report = _executor(prepared, backend).validate_only()
    assert report.verdict == "VALIDATED_NO_HOST_MUTATION"
    assert report.mutation_count == 0
    assert backend.mutations == []
    assert cutover._parser().parse_args(
        [
            "--manifest", "x", "--manifest-sha256", "a" * 64,
            "--candidate-root", "x", "--allow-read-root", "x",
            "--task-snapshot", "x", "--process-snapshot", "x",
            "--restore-plan", "x", "--candidate-task-template", "x",
            "--candidate-action", "x", "--journal-root", "x",
        ]
    ).mode == cutover.MODE_VALIDATE_ONLY


def test_apply_has_exact_postconditions_and_is_idempotent(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    report = executor.apply()
    assert report.verdict == "APPLIED_ALPACA_PAPER_ONLY"
    assert all(not backend.tasks[name].enabled for name in cutover.REQUIRED_LEGACY_TASKS)
    assert backend.tasks[cutover.CANDIDATE_TASK_NAME].xml_sha256 == (
        prepared.resolved_task_xml_sha256
    )
    assert backend.find_legacy_processes(prepared.restore_plan.bindings) == ()
    assert [
        item.kind
        for item in backend.await_candidate_processes(
            prepared.invocation, timeout_seconds=0
        )
    ] == ["launcher", "service"]
    assert backend.execution_lane_state == "stopped"
    assert backend.mutations.index("execution-lane:stop") < backend.mutations.index(
        f"start:{cutover.CANDIDATE_TASK_NAME}"
    )
    before = list(backend.mutations)
    second = executor.apply()
    assert second.verdict == "ALREADY_APPLIED_EXACT"
    assert backend.mutations == before


def test_recover_only_inventory_finds_one_active_capsule_and_ignores_baseline(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)

    executor.apply()
    discovered = cutover._discover_single_active_rollback_capsule(
        journal_root=executor.journal_root,
        caller_roots=prepared.allowed_read_roots,
        clock=lambda: NOW,
    )
    assert discovered is not None
    recovered, state = discovered
    assert state == "applied"
    assert recovered.activation_generation == prepared.activation_generation
    assert recovered.manifest_sha256 == prepared.manifest_sha256

    executor.rollback()
    assert cutover._discover_single_active_rollback_capsule(
        journal_root=executor.journal_root,
        caller_roots=prepared.allowed_read_roots,
        clock=lambda: NOW,
    ) is None


def test_recover_only_missing_journal_root_is_noop(
    prepared: cutover.PreparedCutover,
) -> None:
    missing = prepared.candidate_root / "never-created-recovery-journal"

    assert cutover._discover_single_active_rollback_capsule(
        journal_root=missing,
        caller_roots=prepared.allowed_read_roots,
        clock=lambda: NOW,
    ) is None
    assert not missing.exists()


def _seed_pre_identity_apply_started(
    journal: cutover.CutoverJournal,
    prepared: cutover.PreparedCutover,
) -> str:
    for item in prepared.task_snapshot.tasks.values():
        journal.publish_object(item.xml, kind="legacy_task_xml")
    capsule_raw = cutover._canonical_json_bytes(
        cutover.build_rollback_capsule_document(prepared)
    )
    capsule_path = journal.publish_object(capsule_raw, kind="rollback_capsule")
    journal.append(
        "apply_started",
        {
            "rollback_capsule_path": str(capsule_path),
            "rollback_capsule_sha256": cutover.sha256_bytes(capsule_raw),
            "legacy_schema_predates_docker_identity": True,
        },
    )
    journal.append(
        "immutable_runtime_staged",
        {
            "launcher_path": "sealed-launcher",
            "launcher_sha256": "1" * 64,
            "service_path": "sealed-service",
            "service_sha256": "2" * 64,
            "stage0_path": "sealed-stage0",
            "stage0_sha256": "3" * 64,
        },
    )
    journal.append(
        "apply_failed",
        {
            "error_code": "TASK_XML_INVALID",
            "error_type": "CapturedPaperHostCutoverError",
        },
    )
    return cutover.sha256_bytes(journal.path.read_bytes())


def test_recover_only_adopts_pre_identity_journal_only_at_exact_baseline(
    prepared: cutover.PreparedCutover,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    journal = cutover.CutoverJournal(
        root=executor.journal_root, prepared=prepared, clock=lambda: NOW
    )
    journal_sha = _seed_pre_identity_apply_started(journal, prepared)
    monkeypatch.setattr(
        cutover, "_PRE_IDENTITY_JOURNAL_RECOVERY_ALLOWLIST", frozenset({journal_sha})
    )

    report = executor.adopt_pre_identity_journal_at_exact_baseline()

    assert report is not None
    assert report.verdict == "ROLLED_BACK_EXACT"
    assert report.mutation_count == 0
    assert backend.mutations == []
    journal._events = journal._read_events()
    assert cutover._journal_state(journal.events) == "baseline"
    assert [item["event_type"] for item in journal.events][-3:] == [
        "legacy_execution_lane_identity_adopted",
        "rollback_started",
        "rollback_completed",
    ]
    adopted = cutover._legacy_execution_lane_baseline(journal.events)
    assert adopted == backend.inspect_legacy_execution_lane()


def test_recover_only_refuses_pre_identity_adoption_on_host_drift(
    prepared: cutover.PreparedCutover,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    journal = cutover.CutoverJournal(
        root=executor.journal_root, prepared=prepared, clock=lambda: NOW
    )
    journal_sha = _seed_pre_identity_apply_started(journal, prepared)
    monkeypatch.setattr(
        cutover, "_PRE_IDENTITY_JOURNAL_RECOVERY_ALLOWLIST", frozenset({journal_sha})
    )
    name = cutover.REQUIRED_LEGACY_TASKS[0]
    backend.set_task_enabled(name, False)
    before = list(backend.mutations)

    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="ROLLBACK_POSTCONDITION_FAILED",
    ):
        executor.adopt_pre_identity_journal_at_exact_baseline()

    assert backend.mutations == before
    journal._events = journal._read_events()
    assert not any(
        item["event_type"] == "legacy_execution_lane_identity_adopted"
        for item in journal.events
    )


def test_recover_only_refuses_allowlisted_pre_identity_mutation_history(
    prepared: cutover.PreparedCutover,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    journal = cutover.CutoverJournal(
        root=executor.journal_root, prepared=prepared, clock=lambda: NOW
    )
    _seed_pre_identity_apply_started(journal, prepared)
    journal.append(
        "legacy_task_disabled",
        {
            "task_name": cutover.REQUIRED_LEGACY_TASKS[0],
            "readback_xml_sha256": "4" * 64,
        },
    )
    journal_sha = cutover.sha256_bytes(journal.path.read_bytes())
    monkeypatch.setattr(
        cutover, "_PRE_IDENTITY_JOURNAL_RECOVERY_ALLOWLIST", frozenset({journal_sha})
    )

    assert executor.adopt_pre_identity_journal_at_exact_baseline() is None
    assert backend.mutations == []
    journal._events = journal._read_events()
    assert not any(
        item["event_type"] == "legacy_execution_lane_identity_adopted"
        for item in journal.events
    )


def test_cli_holds_one_host_global_lock_across_generation_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_root = tmp_path / "journal"
    journal_root.mkdir()
    fixed_lock = tmp_path / "fixed-host-cutover.lock"
    monkeypatch.setattr(cutover, "_HOST_WIDE_CUTOVER_LOCK_PATH", fixed_lock)
    mutex_name = "Local\\CHILI-Captured-PAPER-Cutover-Test-" + str(time.time_ns())
    monkeypatch.setattr(cutover, "_HOST_WIDE_CUTOVER_MUTEX_NAME", mutex_name)
    observed = []

    def inner(_argv) -> int:
        contender_result: list[str] = []

        def contend() -> None:
            try:
                with cutover._JournalLock(fixed_lock, mutex_name=mutex_name):
                    contender_result.append("acquired")
            except cutover.CapturedPaperHostCutoverError as exc:
                contender_result.append(exc.code)

        contender = threading.Thread(target=contend)
        contender.start()
        contender.join(timeout=10)
        assert not contender.is_alive()
        assert contender_result == ["CUTOVER_ALREADY_RUNNING"]
        observed.append("locked")
        return 0

    monkeypatch.setattr(cutover, "_main_with_host_lock_held", inner)
    assert cutover.main(
        [
            "--mode",
            cutover.MODE_VALIDATE_ONLY,
            "--allow-read-root",
            str(tmp_path),
            "--journal-root",
            str(journal_root),
        ]
    ) == 0
    assert observed == ["locked"]


def test_stopped_execution_lane_is_preserved_across_apply_and_rollback(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared, execution_lane_state="stopped")
    executor = _executor(prepared, backend)

    assert executor.apply().verdict == "APPLIED_ALPACA_PAPER_ONLY"
    assert backend.execution_lane_state == "stopped"
    assert "execution-lane:stop" not in backend.mutations

    assert executor.rollback().verdict == "ROLLED_BACK_EXACT"
    _assert_restored(prepared, backend)


def test_already_applied_service_crash_compensates_to_exact_legacy_state(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    crashed = next(
        pid for pid, item in backend.processes.items()
        if item.role == "candidate_service"
    )
    backend.processes.pop(crashed)

    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="APPLIED_POSTCONDITION_RECOVERED",
    ):
        executor.apply()

    _assert_restored(prepared, backend)


def test_explicit_rollback_restores_exact_tasks_and_provenance_roles(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    report = executor.rollback()
    assert report.verdict == "ROLLED_BACK_EXACT"
    _assert_restored(prepared, backend)
    before = list(backend.mutations)
    assert executor.rollback().verdict == "ALREADY_ROLLED_BACK_EXACT"
    assert backend.mutations == before


@pytest.mark.parametrize(
    "operation",
    [
        *(f"task:{name}:disable" for name in cutover.REQUIRED_LEGACY_TASKS),
        "stop-process:iqfeed_depth_bridge",
        "stop-process:iqfeed_trade_bridge",
        "execution-lane:stop",
        f"register:{cutover.CANDIDATE_TASK_NAME}",
        f"start:{cutover.CANDIDATE_TASK_NAME}",
    ],
)
@pytest.mark.parametrize("after_effect", [False, True])
def test_every_apply_mutation_failure_compensates_to_restored_host(
    prepared: cutover.PreparedCutover, operation: str, after_effect: bool
) -> None:
    backend = FakeHost(
        prepared, fail_operation=operation, fail_after_effect=after_effect
    )
    with pytest.raises(cutover.CapturedPaperHostCutoverError):
        _executor(prepared, backend).apply()
    _assert_restored(prepared, backend)


def test_process_snapshot_drift_fails_before_any_mutation(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    original = prepared.process_snapshot.processes[0]
    backend.processes[original.pid] = replace(
        original,
        create_time_ns=original.create_time_ns + 1,
    )
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="LEGACY_PROCESS_SNAPSHOT_DRIFT",
    ):
        _executor(prepared, backend).apply()
    assert backend.mutations == []


def test_alias_task_with_exact_candidate_action_blocks_before_mutation(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    backend.tasks["CHILI-foreign-alias"] = cutover.TaskObservation(
        "CHILI-foreign-alias", prepared.resolved_task_xml, True
    )
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError, match="CANDIDATE_TASK_COLLISION"
    ):
        _executor(prepared, backend).apply()
    assert backend.mutations == []


def test_foreign_candidate_task_is_never_stopped_or_deleted(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    backend.tasks[cutover.CANDIDATE_TASK_NAME] = cutover.TaskObservation(
        cutover.CANDIDATE_TASK_NAME, _task_xml("foreign"), True
    )
    before = list(backend.mutations)
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="FOREIGN_CANDIDATE_EXECUTION_QUARANTINED",
    ):
        executor.rollback()
    assert not any(item.startswith("stop:") for item in backend.mutations[len(before):])
    assert not any(item.startswith("delete:") for item in backend.mutations[len(before):])
    assert all(
        not backend.tasks[name].enabled for name in cutover.REQUIRED_LEGACY_TASKS
    )
    assert backend.find_legacy_processes(prepared.restore_plan.bindings) == ()
    assert backend.execution_lane_state == "stopped"


def test_interrupted_journal_is_rolled_back_before_a_new_apply(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    journal = cutover.CutoverJournal(
        root=executor.journal_root, prepared=prepared, clock=lambda: NOW
    )
    _seed_apply_started(journal, prepared, simulated_power_loss=True)
    name = cutover.REQUIRED_LEGACY_TASKS[0]
    backend.set_task_enabled(name, False)
    first_process = prepared.process_snapshot.processes[0]
    backend.processes.pop(first_process.pid)
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="INCOMPLETE_TRANSACTION_RECOVERED",
    ):
        executor.apply()
    _assert_restored(prepared, backend)


def test_candidate_action_digest_is_exact_and_has_no_checks_map() -> None:
    document = cutover.build_candidate_action_document(
        host_cutover_source_sha256="1" * 64,
        launcher_argument_contract_sha256="2" * 64,
        candidate_task_xml_sha256="3" * 64,
    )
    assert set(document) == {
        "schema_version",
        "host_cutover_source_sha256",
        "launcher_argument_contract_sha256",
        "candidate_task_xml_sha256",
        "singleton_policy",
    }
    assert cutover.candidate_action_sha256(
        host_cutover_source_sha256="1" * 64,
        launcher_argument_contract_sha256="2" * 64,
        candidate_task_xml_sha256="3" * 64,
    ) == cutover.sha256_json(document)


def test_tokenized_task_template_resolves_only_verified_manifest_tokens(
    tmp_path: Path,
) -> None:
    powershell = tmp_path / "powershell.exe"
    python = tmp_path / "python.exe"
    launcher_raw = b"sealed launcher"
    launcher_sha = hashlib.sha256(launcher_raw).hexdigest()
    launcher_source = tmp_path / "launcher-source.ps1"
    launcher = tmp_path / "staged" / launcher_sha / f"{launcher_sha}.ps1"
    stage0_source = tmp_path / "captured_paper_isolated_stage0.py"
    service_source = tmp_path / "captured_alpaca_paper_service.py"
    for path in (powershell, python, stage0_source, service_source):
        path.write_bytes(path.name.encode())
    launcher_source.write_bytes(launcher_raw)
    stage0_sha = hashlib.sha256(stage0_source.read_bytes()).hexdigest()
    stage0 = tmp_path / "staged" / stage0_sha / f"{stage0_sha}.py"
    service_sha = hashlib.sha256(service_source.read_bytes()).hexdigest()
    service = tmp_path / "staged" / f"{service_sha}.py"
    ready = tmp_path / "startup.json"
    dependency_root = tmp_path / "site-packages"
    dependency_root.mkdir()
    python_sha = hashlib.sha256(python.read_bytes()).hexdigest()
    dependency_identity_sha = (
        cutover.activation_contract.python_dependency_root_identity_sha256(
            dependency_root=dependency_root,
            python_executable=python,
            python_executable_sha256=python_sha,
        )
    )
    projection = {
        "mode": "ActivatePaper",
        "service_mode": "activate-paper",
        "foreground": True,
        "singleton_name": "Global\\CHILI-Captured-Alpaca-PAPER-SINGLETON",
        "candidate_root": str(tmp_path),
        "launcher_source_path": str(launcher_source),
        "launcher_source_sha256": launcher_sha,
        "launcher_path": str(launcher),
        "launcher_sha256": launcher_sha,
        "stage0_source_path": str(stage0_source),
        "stage0_source_sha256": stage0_sha,
        "stage0_path": str(stage0),
        "stage0_sha256": stage0_sha,
        "service_source_path": str(service_source),
        "service_source_sha256": service_sha,
        "service_staged_path": str(service),
        "service_sha256": service_sha,
        "python_executable_path": str(python),
        "python_executable_sha256": python_sha,
        "python_dependency_root": str(dependency_root),
        "python_dependency_root_identity_sha256": dependency_identity_sha,
        "allowed_read_roots": [str(tmp_path)],
        "service_arguments": [
            "-I", "-S", "-B", str(stage0),
            "--manifest", cutover.MANIFEST_PATH_TOKEN,
            "--manifest-sha256", cutover.MANIFEST_SHA256_TOKEN,
            "--candidate-root", str(tmp_path),
            "--target-role", "activation_service",
            "--target", str(service),
            "--target-sha256", service_sha,
            "--",
            "--mode", "activate-paper",
            "--manifest", cutover.MANIFEST_PATH_TOKEN,
            "--manifest-sha256", cutover.MANIFEST_SHA256_TOKEN,
            "--candidate-root", str(tmp_path),
            "--launcher-path", str(launcher),
            "--launcher-sha256", launcher_sha,
            "--host-ready-receipt", str(ready),
        ],
    }
    template = cutover.build_candidate_task_xml_template(
        principal_user_id="TEST\\paper-user",
        powershell_executable_path=str(powershell),
        activate_paper_projection=projection,
    )
    # The template is UTF-16 on disk (schtasks rejects UTF-8-declared XML),
    # so token assertions operate on the decoded text.
    template_text = template.decode("utf-16")
    assert template_text.count(cutover.MANIFEST_PATH_TOKEN) == 1
    assert template_text.count(cutover.MANIFEST_SHA256_TOKEN) == 1
    manifest = tmp_path / ("a" * 64 + ".json")
    manifest.write_text("{}")
    resolved, invocation = cutover._validate_candidate_template(
        template=template,
        projection=projection,
        manifest_path=manifest,
        manifest_sha256="a" * 64,
    )
    resolved_text = resolved.decode("utf-16")
    assert cutover.MANIFEST_PATH_TOKEN not in resolved_text
    assert cutover.MANIFEST_SHA256_TOKEN not in resolved_text
    assert str(manifest) in invocation.launcher_arguments
    assert "a" * 64 in invocation.launcher_arguments


def test_candidate_action_formula_is_accepted_by_typed_rollback_v2() -> None:
    host_sha = "1" * 64
    launcher_contract_sha = "2" * 64
    template_sha = "3" * 64
    action_sha = cutover.candidate_action_sha256(
        host_cutover_source_sha256=host_sha,
        launcher_argument_contract_sha256=launcher_contract_sha,
        candidate_task_xml_sha256=template_sha,
    )
    context = readiness.ReadinessValidationContext(
        activation_generation="11111111-1111-4111-8111-111111111111",
        expected_account_id="22222222-2222-4222-8222-222222222222",
        code_build_sha256="4" * 64,
        effective_config_sha256="5" * 64,
        capture_receipt_sha256="6" * 64,
        runtime_environment_sha256="7" * 64,
        database_target_fingerprint="8" * 64,
        iqfeed_bootstrap_manifest_sha256="9" * 64,
        launcher_argument_contract_sha256=launcher_contract_sha,
        capture_store_root=r"D:\capture",
        source_hashes={"captured_paper_host_cutover": host_sha},
    )
    evidence = {
        "schema_version": (
            "chili.captured-paper-readiness-evidence.rollback_snapshot.v2"
        ),
        "source_receipts": {
            "task_snapshot": "a" * 64,
            "process_snapshot": "b" * 64,
            "restore_plan": "c" * 64,
            "candidate_action": action_sha,
        },
        "task_snapshot_sha256": "d" * 64,
        "scheduled_task_xml_sha256s": {
            name: hashlib.sha256(name.encode()).hexdigest()
            for name in cutover.REQUIRED_LEGACY_TASKS
        },
        "legacy_process_snapshot_sha256": "b" * 64,
        "restore_plan_sha256": "c" * 64,
        "host_cutover_source_sha256": host_sha,
        "launcher_argument_contract_sha256": launcher_contract_sha,
        "candidate_task_xml_sha256": template_sha,
        "candidate_action_sha256": action_sha,
        "preactivation_baseline_sha256": "e" * 64,
        "validation_mode": cutover.PREACTIVATION_ROLLBACK_BASELINE_MODE,
        "singleton_policy": cutover.SINGLETON_POLICY,
        "host_mutation_count": 0,
        "final_validate_only_performed": False,
        "captured_at": NOW.isoformat(),
    }
    receipt = readiness.issue_readiness_receipt_v2(
        kind="rollback_snapshot",
        context=context,
        evidence=evidence,
        captured_at=NOW,
        expires_at=NOW.replace(hour=13),
        now=NOW,
        max_age_seconds=3600,
    )
    assert receipt["verdict"] == "PASS"
    assert receipt["issuer_source_role"] == "captured_paper_host_cutover"


def test_snapshot_builders_round_trip_exact_restore_material(
    prepared: cutover.PreparedCutover,
) -> None:
    task_document = cutover.build_task_snapshot_document(
        captured_at=NOW, tasks=prepared.task_snapshot.tasks
    )
    process_document = cutover.build_process_snapshot_document(
        captured_at=NOW, processes=prepared.process_snapshot.processes
    )
    restore_document = cutover.build_restore_plan_document(
        tasks=prepared.task_snapshot.tasks,
        bindings=prepared.restore_plan.bindings,
    )
    task_raw = cutover._canonical_json_bytes(task_document)
    process_raw = cutover._canonical_json_bytes(process_document)
    restore_raw = cutover._canonical_json_bytes(restore_document)
    task_path = prepared.candidate_root / "roundtrip-task.json"
    process_path = prepared.candidate_root / "roundtrip-process.json"
    restore_path = prepared.candidate_root / "roundtrip-restore.json"
    task_path.write_bytes(task_raw)
    process_path.write_bytes(process_raw)
    restore_path.write_bytes(restore_raw)
    evidence = {
        "scheduled_task_xml_sha256s": {
            name: item.xml_sha256
            for name, item in prepared.task_snapshot.tasks.items()
        }
    }
    task = cutover._parse_task_snapshot(
        path=task_path,
        raw=task_raw,
        digest=cutover.sha256_bytes(task_raw),
        receipt_evidence=evidence,
    )
    process = cutover._parse_process_snapshot(
        path=process_path,
        raw=process_raw,
        digest=cutover.sha256_bytes(process_raw),
        roots=prepared.allowed_read_roots,
    )
    restore = cutover._parse_restore_plan(
        path=restore_path,
        raw=restore_raw,
        digest=cutover.sha256_bytes(restore_raw),
        roots=prepared.allowed_read_roots,
    )
    cutover._assert_snapshot_plan_consistency(task, process, restore)


def test_apply_requires_explicit_fake_money_confirmation() -> None:
    assert cutover.main(
        [
            "--mode", "Apply",
            "--manifest", "x", "--manifest-sha256", "a" * 64,
            "--candidate-root", "x", "--allow-read-root", "x",
            "--task-snapshot", "x", "--process-snapshot", "x",
            "--restore-plan", "x", "--candidate-task-template", "x",
            "--candidate-action", "x", "--journal-root", "x",
        ]
    ) == 2


def test_taskless_candidate_processes_are_still_inventoried_and_stopped(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    # Simulate external task-row loss while both exact candidate processes
    # remain alive.  stop_task cannot mask this test because FakeHost.stop_task
    # intentionally does not terminate processes.
    backend.tasks.pop(cutover.CANDIDATE_TASK_NAME)
    report = executor.rollback()
    assert report.verdict == "ROLLED_BACK_EXACT"
    assert "stop-candidate:launcher" in backend.mutations
    assert "stop-candidate:service" in backend.mutations
    _assert_restored(prepared, backend)


def test_foreign_alias_collision_restores_legacy_before_failing_closed(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    alias = "CHILI-foreign-exact-invocation"
    backend.tasks[alias] = cutover.TaskObservation(
        alias, prepared.resolved_task_xml, True
    )
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="FOREIGN_CANDIDATE_EXECUTION_QUARANTINED",
    ):
        executor.rollback()
    assert alias in backend.tasks
    assert cutover.CANDIDATE_TASK_NAME not in backend.tasks
    assert all(
        not backend.tasks[name].enabled for name in cutover.REQUIRED_LEGACY_TASKS
    )
    assert backend.find_legacy_processes(prepared.restore_plan.bindings) == ()
    assert backend.execution_lane_state == "stopped"
    journal = cutover.CutoverJournal(
        root=executor.journal_root, prepared=prepared, clock=lambda: NOW
    )
    assert journal.events[-1]["event_type"] == "rollback_blocked_foreign_candidate"


def test_foreign_candidate_after_docker_restore_requiesces_all_legacy_lanes(
    prepared: cutover.PreparedCutover,
) -> None:
    alias = "CHILI-foreign-after-docker-restore"

    class LateCandidateHost(FakeHost):
        def restore_legacy_execution_lane(
            self, *, expected: cutover.LegacyExecutionLaneObservation
        ) -> int:
            mutations = super().restore_legacy_execution_lane(expected=expected)
            self.tasks[alias] = cutover.TaskObservation(
                alias, prepared.resolved_task_xml, True
            )
            return mutations

    backend = LateCandidateHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()

    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="FOREIGN_CANDIDATE_EXECUTION_QUARANTINED",
    ):
        executor.rollback()

    assert alias in backend.tasks
    assert cutover.CANDIDATE_TASK_NAME not in backend.tasks
    assert backend.execution_lane_state == "stopped"
    assert backend.find_legacy_processes(prepared.restore_plan.bindings) == ()
    assert all(
        not backend.tasks[name].enabled for name in cutover.REQUIRED_LEGACY_TASKS
    )
    journal = cutover.CutoverJournal(
        root=executor.journal_root, prepared=prepared, clock=lambda: NOW
    )
    assert journal.events[-1]["event_type"] == "rollback_blocked_foreign_candidate"
    assert journal.events[-1]["payload"][
        "legacy_execution_remains_quiesced"
    ] is True


def test_torn_final_journal_record_recovers_from_valid_prefix(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    applied = executor.apply()
    assert applied.journal_path is not None
    with applied.journal_path.open("ab") as handle:
        handle.write(b'{"schema_version":"torn')
    report = executor.rollback()
    assert report.verdict == "ROLLED_BACK_EXACT"
    raw = applied.journal_path.read_bytes()
    assert raw.endswith(b"\n")
    assert b'"schema_version":"torn' not in raw
    for line in raw.splitlines():
        cutover._strict_json(line, "repaired journal")


def test_rollback_uses_capsule_after_mutable_activation_artifacts_drift(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    for path in (
        prepared.manifest_path,
        prepared.task_snapshot.artifact_path,
        prepared.process_snapshot.artifact_path,
        prepared.restore_plan.artifact_path,
        prepared.candidate_action_path,
        prepared.candidate_template_path,
        Path(prepared.invocation.launcher_source_path),
    ):
        path.write_bytes(b"drifted after apply")
    discovered = cutover._discover_rollback_capsule(
        journal_root=executor.journal_root,
        manifest_sha256=prepared.manifest_sha256,
        caller_roots=prepared.allowed_read_roots,
    )
    assert discovered.activation_generation == prepared.activation_generation
    assert discovered.resolved_task_xml == prepared.resolved_task_xml
    report = executor.rollback()
    assert report.verdict == "ROLLED_BACK_EXACT"
    _assert_restored(prepared, backend)


def test_content_addressed_capsule_tamper_is_rejected(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    journal = cutover.CutoverJournal(
        root=executor.journal_root, prepared=prepared, clock=lambda: NOW
    )
    started = next(
        item for item in journal.events if item["event_type"] == "apply_started"
    )
    capsule = Path(started["payload"]["rollback_capsule_path"])
    capsule.write_bytes(capsule.read_bytes() + b"tamper")
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError, match="JOURNAL_OBJECT_DRIFT"
    ):
        executor.rollback()


def test_capsule_never_registers_or_starts_drifted_legacy_source(
    prepared: cutover.PreparedCutover,
) -> None:
    # A drifted restore source must fail rollback BEFORE any host mutation:
    # registering the enabled Daily/Logon task XML would hand the scheduler
    # a trigger that can execute the drifted wrapper chain on its own.
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    baseline = list(backend.mutations)
    drifted = Path(prepared.restore_plan.bindings[0].bridge_script_path)
    drifted.unlink()
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="LEGACY_RESTORE_SOURCE_DRIFT",
    ):
        executor.rollback()
    assert backend.mutations == baseline
    for name, expected in prepared.task_snapshot.tasks.items():
        observed = backend.tasks[name]
        assert observed.enabled is False
        assert observed != expected
    assert not any(
        item.startswith(("register:", "start:")) or item.endswith(":enable")
        for item in backend.mutations[len(baseline):]
    )


def test_rollback_revalidates_all_wrapper_sources_before_any_task_restore(
    prepared: cutover.PreparedCutover,
) -> None:
    # Both bindings and every launch contract revalidate up front; the second
    # role's drift must block rollback even though the first role's sources
    # are intact and would otherwise restore first.
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    baseline = list(backend.mutations)
    second = prepared.restore_plan.bindings[1]
    Path(second.bridge_script_path).write_bytes(b"# drifted after apply")
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="LEGACY_RESTORE_SOURCE_DRIFT",
    ):
        executor.rollback()
    assert backend.mutations == baseline
    assert not any(
        item.startswith("register:") for item in backend.mutations[len(baseline):]
    )
    for name in prepared.task_snapshot.tasks:
        assert backend.tasks[name].enabled is False


def test_existing_exact_role_does_not_skip_wrapper_chain_revalidation(
    prepared: cutover.PreparedCutover,
) -> None:
    # An already-running exact legacy process previously bypassed source
    # revalidation for its role; drift behind a live process must still fail
    # rollback closed before any restore mutation.
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    trade = next(
        item
        for item in prepared.process_snapshot.processes
        if item.role == "iqfeed_trade_bridge"
    )
    backend.processes[7777] = replace(
        trade, pid=7777, create_time_ns=trade.create_time_ns + 1
    )
    baseline = list(backend.mutations)
    Path(
        next(
            item
            for item in prepared.restore_plan.bindings
            if item.role == "iqfeed_trade_bridge"
        ).bridge_script_path
    ).write_bytes(b"# drifted behind a live process")
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="LEGACY_RESTORE_SOURCE_DRIFT",
    ):
        executor.rollback()
    assert backend.mutations == baseline


def test_restore_plan_binds_task_action_and_full_process_argv(
    prepared: cutover.PreparedCutover,
) -> None:
    binding = prepared.restore_plan.bindings[0]
    wrong_action = replace(binding, restore_task_action_sha256="f" * 64)
    wrong_argv = replace(
        binding,
        expected_cmdline=(*binding.expected_cmdline, "--foreign"),
        expected_cmdline_sha256=cutover.sha256_json(
            [*binding.expected_cmdline, "--foreign"]
        ),
    )
    for changed in (wrong_action, wrong_argv):
        bindings = tuple(
            changed if item.role == binding.role else item
            for item in prepared.restore_plan.bindings
        )
        with pytest.raises(
            cutover.CapturedPaperHostCutoverError, match="RESTORE_PLAN_MISMATCH"
        ):
            cutover._assert_snapshot_plan_consistency(
                prepared.task_snapshot,
                prepared.process_snapshot,
                replace(prepared.restore_plan, bindings=bindings),
            )


def test_one_pid_cannot_satisfy_two_restored_roles(
    prepared: cutover.PreparedCutover,
) -> None:
    class DuplicatePidHost(FakeHost):
        def await_legacy_processes(
            self,
            bindings: tuple[cutover.LegacyProcessBinding, ...],
            *,
            timeout_seconds: float,
        ) -> tuple[cutover.ProcessIdentity, ...]:
            values = super().await_legacy_processes(
                bindings, timeout_seconds=timeout_seconds
            )
            if len(values) == 2:
                return (values[0], replace(values[1], pid=values[0].pid))
            return values

    backend = DuplicatePidHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="LEGACY_PROCESS_RESTORE_FAILED",
    ):
        executor.rollback()


def test_uninspectable_process_identity_is_not_treated_as_absent() -> None:
    class AccessDenied(Exception):
        pass

    class NoSuchProcess(Exception):
        pass

    class ZombieProcess(Exception):
        pass

    class FakePsutil:
        @staticmethod
        def Process(_pid: int) -> object:
            raise AccessDenied()

    FakePsutil.AccessDenied = AccessDenied
    FakePsutil.NoSuchProcess = NoSuchProcess
    FakePsutil.ZombieProcess = ZombieProcess

    backend = object.__new__(cutover.WindowsHostCutoverBackend)
    backend._psutil = FakePsutil
    backend._bindings = {}
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="PROCESS_INVENTORY_UNINSPECTABLE",
    ):
        backend._identity_for_pid(42, role="candidate_service")


def test_relevant_candidate_inventory_access_denied_fails_closed(
    prepared: cutover.PreparedCutover,
) -> None:
    class AccessDenied(Exception):
        pass

    class NoSuchProcess(Exception):
        pass

    class ZombieProcess(Exception):
        pass

    class Row:
        info = {
            "pid": 42,
            "name": Path(
                prepared.invocation.python_executable_path
            ).name,
            "exe": prepared.invocation.python_executable_path,
            "cmdline": None,
        }

    class Uninspectable:
        @staticmethod
        def create_time() -> float:
            raise AccessDenied()

    class FakePsutil:
        @staticmethod
        def process_iter(*_args: object, **_kwargs: object) -> list[Row]:
            return [Row()]

        @staticmethod
        def Process(_pid: int) -> Uninspectable:
            return Uninspectable()

    FakePsutil.AccessDenied = AccessDenied
    FakePsutil.NoSuchProcess = NoSuchProcess
    FakePsutil.ZombieProcess = ZombieProcess

    backend = object.__new__(cutover.WindowsHostCutoverBackend)
    backend._psutil = FakePsutil
    backend._bindings = {}
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="PROCESS_INVENTORY_UNINSPECTABLE",
    ):
        backend._candidate_processes(prepared.invocation)


def test_candidate_token_process_with_wrong_full_argv_is_a_collision(
    prepared: cutover.PreparedCutover,
) -> None:
    class AccessDenied(Exception):
        pass

    class NoSuchProcess(Exception):
        pass

    class ZombieProcess(Exception):
        pass

    executable = prepared.invocation.python_executable_path
    cmdline = (
        executable,
        *prepared.invocation.service_arguments,
        "--foreign-extra-argument",
    )

    class Row:
        info = {
            "pid": 43,
            "name": Path(executable).name,
            "exe": executable,
            "cmdline": cmdline,
        }

    class Inspectable:
        @staticmethod
        def create_time() -> float:
            return 1700000000.0

        @staticmethod
        def exe() -> str:
            return executable

        @staticmethod
        def cmdline() -> list[str]:
            return list(cmdline)

    class FakePsutil:
        @staticmethod
        def process_iter(*_args: object, **_kwargs: object) -> list[Row]:
            return [Row()]

        @staticmethod
        def Process(_pid: int) -> Inspectable:
            return Inspectable()

    FakePsutil.AccessDenied = AccessDenied
    FakePsutil.NoSuchProcess = NoSuchProcess
    FakePsutil.ZombieProcess = ZombieProcess
    backend = object.__new__(cutover.WindowsHostCutoverBackend)
    backend._psutil = FakePsutil
    backend._bindings = {}
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="CANDIDATE_PROCESS_IDENTITY_MISMATCH",
    ):
        backend._candidate_processes(prepared.invocation)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("<LogonType>InteractiveToken</LogonType>", "<LogonType>Password</LogonType>"),
        ("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", "<MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>"),
        ("<RunLevel>HighestAvailable</RunLevel>", "<RunLevel>LeastPrivilege</RunLevel>"),
        ("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", "<ExecutionTimeLimit>PT1H</ExecutionTimeLimit>"),
    ],
)
def test_candidate_task_semantic_weakening_is_rejected(
    prepared: cutover.PreparedCutover, old: str, new: str
) -> None:
    # UTF-16 template: mutate on decoded text, re-encode for validation.
    resolved_text = prepared.resolved_task_xml.decode("utf-16")
    assert old in resolved_text
    weakened = resolved_text.replace(old, new).encode("utf-16")
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="TASK_TEMPLATE_SEMANTICS_INVALID",
    ):
        cutover._validate_candidate_task_semantics(
            weakened, candidate_root=str(prepared.candidate_root)
        )


def _reserialized_candidate_xml(raw: bytes) -> bytes:
    """Model benign /Create -> /Query encoding/order/metadata changes."""

    root = ET.fromstring(raw)
    registration = root.find(f"{{{NS}}}RegistrationInfo")
    assert registration is not None
    uri = ET.SubElement(registration, f"{{{NS}}}URI")
    uri.text = f"\\{cutover.CANDIDATE_TASK_NAME}"
    principal = root.find(f"{{{NS}}}Principals/{{{NS}}}Principal")
    settings = root.find(f"{{{NS}}}Settings")
    assert principal is not None and settings is not None
    ET.SubElement(principal, f"{{{NS}}}ProcessTokenSidType").text = "Default"
    ET.SubElement(
        settings, f"{{{NS}}}DisallowStartOnRemoteAppSession"
    ).text = "false"
    ET.SubElement(settings, f"{{{NS}}}UseUnifiedSchedulingEngine").text = "true"
    idle = settings.find(f"{{{NS}}}IdleSettings")
    assert idle is not None
    ET.SubElement(idle, f"{{{NS}}}Duration").text = "PT10M"
    ET.SubElement(idle, f"{{{NS}}}WaitTimeout").text = "PT1H"
    principal[:] = list(reversed(list(principal)))
    settings[:] = list(reversed(list(settings)))
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


def test_candidate_task_readback_normalizes_full_scheduler_policy(
    prepared: cutover.PreparedCutover,
) -> None:
    readback = _reserialized_candidate_xml(prepared.resolved_task_xml)

    assert readback != prepared.resolved_task_xml
    assert cutover._candidate_task_semantics_match(
        readback, prepared.resolved_task_xml
    )
    projection = cutover._candidate_task_scheduler_projection_from_xml(readback)
    assert projection["logon_type"] == "interactivetoken"
    assert projection["run_level"] == "highestavailable"
    assert projection["trigger_profile"] == "on_demand_only"
    assert "Enabled" not in projection["settings"]


def test_candidate_task_readback_accepts_omitted_scheduler_defaults(
    prepared: cutover.PreparedCutover,
) -> None:
    root = ET.fromstring(prepared.resolved_task_xml)
    settings = root.find(f"{{{NS}}}Settings")
    assert settings is not None
    for name in (
        "AllowHardTerminate",
        "RunOnlyIfNetworkAvailable",
        "AllowStartOnDemand",
        "Enabled",
        "Hidden",
        "RunOnlyIfIdle",
        "WakeToRun",
        "Priority",
    ):
        node = settings.find(f"{{{NS}}}{name}")
        assert node is not None
        settings.remove(node)
    idle = settings.find(f"{{{NS}}}IdleSettings")
    assert idle is not None
    restart = idle.find(f"{{{NS}}}RestartOnIdle")
    assert restart is not None
    idle.remove(restart)
    readback = ET.tostring(root, encoding="utf-16", xml_declaration=True)

    assert cutover._candidate_task_semantics_match(
        readback, prepared.resolved_task_xml
    )


@pytest.mark.parametrize(
    "required_name",
    (
        "MultipleInstancesPolicy",
        "DisallowStartIfOnBatteries",
        "StopIfGoingOnBatteries",
        "StartWhenAvailable",
        "ExecutionTimeLimit",
    ),
)
def test_candidate_task_readback_rejects_omitted_nondefault_policy(
    prepared: cutover.PreparedCutover,
    required_name: str,
) -> None:
    root = ET.fromstring(prepared.resolved_task_xml)
    settings = root.find(f"{{{NS}}}Settings")
    assert settings is not None
    node = settings.find(f"{{{NS}}}{required_name}")
    assert node is not None
    settings.remove(node)
    readback = ET.tostring(root, encoding="utf-16", xml_declaration=True)

    assert not cutover._candidate_task_semantics_match(
        readback, prepared.resolved_task_xml
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("<LogonType>InteractiveToken</LogonType>", "<LogonType>Password</LogonType>"),
        ("<RunLevel>HighestAvailable</RunLevel>", "<RunLevel>LeastPrivilege</RunLevel>"),
        ("<Triggers />", "<Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>"),
        ("<StartWhenAvailable>true</StartWhenAvailable>", "<StartWhenAvailable>false</StartWhenAvailable>"),
        ("</Settings>", "<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>"),
    ],
)
def test_candidate_task_readback_rejects_scheduler_policy_drift(
    prepared: cutover.PreparedCutover, old: str, new: str
) -> None:
    text = prepared.resolved_task_xml.decode("utf-16")
    assert old in text
    drifted = text.replace(old, new, 1).encode("utf-16")

    assert not cutover._candidate_task_semantics_match(
        drifted, prepared.resolved_task_xml
    )


def test_candidate_task_is_one_shot_without_restart_or_automatic_trigger(
    prepared: cutover.PreparedCutover,
) -> None:
    root = ET.fromstring(prepared.resolved_task_xml)
    triggers = root.find(f"{{{NS}}}Triggers")
    settings = root.find(f"{{{NS}}}Settings")

    assert triggers is not None and list(triggers) == []
    assert settings is not None
    assert settings.find(f"{{{NS}}}RestartOnFailure") is None


def test_real_default_cutover_probe_accepts_scheduler_reserialization_without_host_mutation(
    prepared: cutover.PreparedCutover,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import captured_alpaca_paper_service as service
    import psutil

    backend = FakeHost(prepared, execution_lane_state="stopped")
    backend.tasks = {
        name: cutover.TaskObservation(name, _set_task_enabled(item.xml, False), False)
        for name, item in prepared.task_snapshot.tasks.items()
    }
    candidate_xml = _reserialized_candidate_xml(prepared.resolved_task_xml)
    backend.tasks[cutover.CANDIDATE_TASK_NAME] = cutover.TaskObservation(
        cutover.CANDIDATE_TASK_NAME, candidate_xml, True
    )
    backend.processes = {}
    monkeypatch.setattr(
        cutover,
        "WindowsHostCutoverBackend",
        lambda *, bindings: backend,
    )
    monkeypatch.setattr(psutil, "process_iter", lambda *_args, **_kwargs: ())
    source = Path(cutover.__file__).resolve(strict=True)
    verified = SimpleNamespace(
        source_paths={"captured_paper_host_cutover": source},
        source_hashes={
            "captured_paper_host_cutover": hashlib.sha256(
                source.read_bytes()
            ).hexdigest()
        },
    )

    observed = service._default_cutover_probe(
        verified=verified,
        projection={},
        expected_parent_tail=prepared.invocation.launcher_arguments,
        parent_executable_path=prepared.invocation.powershell_executable_path,
    )

    assert observed["candidate_task_name"] == cutover.CANDIDATE_TASK_NAME
    assert observed["candidate_task_enabled"] is True
    assert observed["legacy_bridge_processes"] == []
    assert observed["legacy_execution_lane"]["state"] == "stopped"
    assert all(value is False for value in observed["legacy_task_enabled"].values())
    assert backend.mutations == []


def test_ads_path_alias_is_never_local_authority() -> None:
    assert not cutover._is_local_absolute(Path(r"C:\sealed\receipt.json:forged"))


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point TOCTOU")
def test_stable_read_rejects_parent_junction_swapped_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    # The genuine file the validated lexical path names.
    genuine_parent = tmp_path / "genuine"
    genuine_parent.mkdir()
    genuine = genuine_parent / "receipt.json"
    genuine.write_bytes(b"genuine sealed bytes")

    # The attacker's redirect target, and the mount point that will become a
    # junction to it between validation and open.
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "receipt.json").write_bytes(b"forged redirect bytes")
    mount = tmp_path / "mount"
    lexical = mount / "receipt.json"

    real_validate = cutover._strict_existing_file

    def swap_then_return(value, *, roots, field):
        # Validate the genuine path first so component checks pass...
        cutover._reject_reparse_chain(genuine)
        # ...then swap the parent to a junction pointing at the attacker dir,
        # exactly in the validation->open window, and return the lexical path.
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(mount), str(attacker)],
            check=True,
            capture_output=True,
        )
        return lexical

    monkeypatch.setattr(cutover, "_strict_existing_file", swap_then_return)
    try:
        with pytest.raises(
            cutover.CapturedPaperHostCutoverError, match="REPARSE_REDIRECTION"
        ):
            cutover._stable_read(lexical, roots=(tmp_path,), field="receipt")
    finally:
        monkeypatch.setattr(cutover, "_strict_existing_file", real_validate)
        if mount.exists():
            os.rmdir(mount)


def test_stale_service_owned_ready_receipt_compensates_before_apply_success(
    prepared: cutover.PreparedCutover,
) -> None:
    class StaleReadyHost(FakeHost):
        def read_service_startup_receipt(
            self,
            invocation: cutover.CandidateInvocation,
            expected_service: cutover.ProcessIdentity,
            *,
            phase: str,
            timeout_seconds: float,
        ) -> dict[str, object]:
            del timeout_seconds
            if phase != "prepared":
                return super().read_service_startup_receipt(
                    invocation,
                    expected_service,
                    phase=phase,
                    timeout_seconds=0,
                )
            value = dict(
                super().read_service_startup_receipt(
                    invocation,
                    expected_service,
                    phase="prepared",
                    timeout_seconds=0,
                )
            )
            value["prepared_at"] = cutover._iso(NOW - timedelta(seconds=31))
            value["valid_until"] = cutover._iso(NOW - timedelta(seconds=1))
            body = dict(value)
            body.pop("receipt_sha256")
            value["receipt_sha256"] = cutover.sha256_json(body)
            return value

    backend = StaleReadyHost(prepared)
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="STARTUP_RECEIPT_INVALID",
    ):
        _executor(prepared, backend).apply()
    _assert_restored(prepared, backend)


def test_content_addressed_launcher_drift_after_task_start_compensates(
    prepared: cutover.PreparedCutover,
) -> None:
    class LauncherDriftHost(FakeHost):
        def start_task(self, name: str) -> None:
            super().start_task(name)
            if name == cutover.CANDIDATE_TASK_NAME:
                staged = Path(self.prepared.invocation.launcher_script_path)
                staged.chmod(0o666)
                staged.write_bytes(b"drifted between registration and ready fence")

    backend = LauncherDriftHost(prepared)
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="APPLIED_POSTCONDITION_FAILED",
    ):
        _executor(prepared, backend).apply()
    _assert_restored(prepared, backend)


def test_two_phase_handshake_stages_exact_runtime_and_consumes_one_permit(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    report = executor.apply()
    assert report.verdict == "APPLIED_ALPACA_PAPER_ONLY"
    assert Path(prepared.invocation.launcher_script_path).read_bytes() == Path(
        prepared.invocation.launcher_source_path
    ).read_bytes()
    assert Path(prepared.invocation.service_script_path).read_bytes() == Path(
        prepared.invocation.service_source_path
    ).read_bytes()
    permit_path = Path(f"{prepared.invocation.host_ready_receipt_base}.permit.json")
    permit = cutover._strict_json(permit_path.read_bytes(), "permit")
    assert permit["schema_version"] == cutover.STARTUP_PERMIT_SCHEMA
    assert permit["challenge_sha256"] == backend.startup_challenge
    assert permit["issuer_cmdline"] == _issuer_apply_cmdline(
        prepared, executor.journal_root
    )
    assert permit["issuer_cmdline_sha256"] == cutover.sha256_json(
        permit["issuer_cmdline"]
    )
    assert permit["service_cmdline"] == list(
        next(
            item.identity.cmdline
            for item in backend.await_candidate_processes(
                prepared.invocation, timeout_seconds=0
            )
            if item.kind == "service"
        )
    )
    journal = cutover.CutoverJournal(
        root=executor.journal_root, prepared=prepared, clock=lambda: NOW
    )
    assert cutover._validate_activation_permit_against_journal(
        permit,
        journal=journal,
        prepared=prepared,
        permit_path=permit_path,
        service=next(
            item.identity
            for item in backend.await_candidate_processes(
                prepared.invocation, timeout_seconds=0
            )
            if item.kind == "service"
        ),
    ) == permit["permit_sha256"]
    kinds = [event["event_type"] for event in journal.events]
    assert kinds.index("activation_permit_issued") < kinds.index(
        "activation_permit_published"
    ) < kinds.index("apply_completed")


def test_preexisting_handshake_artifact_blocks_before_any_host_mutation(
    prepared: cutover.PreparedCutover,
) -> None:
    preexisting = Path(f"{prepared.invocation.host_ready_receipt_base}.permit.json")
    preexisting.write_text("{}")
    backend = FakeHost(prepared)
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError, match="STARTUP_HANDSHAKE_REPLAY"
    ):
        _executor(prepared, backend).apply()
    assert backend.mutations == []


def test_prepared_receipt_cannot_claim_workers_already_started(
    prepared: cutover.PreparedCutover,
) -> None:
    class PrematureWorkersHost(FakeHost):
        def read_service_startup_receipt(
            self, invocation: cutover.CandidateInvocation,
            expected_service: cutover.ProcessIdentity, *, phase: str,
            timeout_seconds: float,
        ) -> dict[str, object]:
            value = dict(super().read_service_startup_receipt(
                invocation, expected_service, phase=phase,
                timeout_seconds=timeout_seconds,
            ))
            if phase == "prepared":
                value["workers_started"] = True
                value["paper_execution_started"] = True
                body = dict(value)
                body.pop("receipt_sha256")
                value["receipt_sha256"] = cutover.sha256_json(body)
            return value

    backend = PrematureWorkersHost(prepared)
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError, match="STARTUP_PREPARED_INVALID"
    ):
        _executor(prepared, backend).apply()
    assert not Path(f"{prepared.invocation.host_ready_receipt_base}.permit.json").exists()
    _assert_restored(prepared, backend)


def test_started_ack_mismatch_revokes_permit_before_process_stop(
    prepared: cutover.PreparedCutover,
) -> None:
    class WrongStartedHost(FakeHost):
        def read_service_startup_receipt(
            self, invocation: cutover.CandidateInvocation,
            expected_service: cutover.ProcessIdentity, *, phase: str,
            timeout_seconds: float,
        ) -> dict[str, object]:
            value = dict(super().read_service_startup_receipt(
                invocation, expected_service, phase=phase,
                timeout_seconds=timeout_seconds,
            ))
            if phase == "started":
                value["activation_permit_sha256"] = "d" * 64
                body = dict(value)
                body.pop("receipt_sha256")
                value["receipt_sha256"] = cutover.sha256_json(body)
            return value

    backend = WrongStartedHost(prepared)
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError, match="STARTUP_STARTED_INVALID"
    ):
        _executor(prepared, backend).apply()
    permit = Path(f"{prepared.invocation.host_ready_receipt_base}.permit.json")
    revoked = Path(f"{prepared.invocation.host_ready_receipt_base}.revoked.json")
    assert not permit.exists()
    assert revoked.is_file()
    disable_index = backend.mutations.index(
        f"task:{cutover.CANDIDATE_TASK_NAME}:disable"
    )
    process_stop_indices = [
        index for index, value in enumerate(backend.mutations)
        if value.startswith("stop-candidate:")
    ]
    assert process_stop_indices and disable_index < min(process_stop_indices)
    _assert_restored(prepared, backend)


def test_python_c_importer_and_non_apply_argv_cannot_issue_permit(
    prepared: cutover.PreparedCutover,
) -> None:
    journal_root = prepared.candidate_root / "issuer-journal"
    journal_root.mkdir()
    executable = Path(sys.executable).resolve(strict=True)
    source = Path(cutover.__file__).resolve(strict=True)
    valid = _issuer_apply_cmdline(prepared, journal_root)
    assert cutover._validate_apply_issuer_cmdline(
        valid,
        executable_path=executable,
        source_path=source,
        prepared=prepared,
        journal_root=journal_root,
    ) == tuple(valid)

    importer = [
        str(executable),
        "-c",
        "import scripts.captured_paper_host_cutover",
        *valid[2:],
    ]
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError, match="ISSUER_CMDLINE_INVALID"
    ):
        cutover._validate_apply_issuer_cmdline(
            importer,
            executable_path=executable,
            source_path=source,
            prepared=prepared,
            journal_root=journal_root,
        )

    not_apply = list(valid)
    not_apply[not_apply.index(cutover.MODE_APPLY)] = cutover.MODE_VALIDATE_ONLY
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError, match="ISSUER_CMDLINE_INVALID"
    ):
        cutover._validate_apply_issuer_cmdline(
            not_apply,
            executable_path=executable,
            source_path=source,
            prepared=prepared,
            journal_root=journal_root,
        )


def test_fabricated_embedded_authorization_event_is_rejected(
    prepared: cutover.PreparedCutover,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    executor.apply()
    permit_path = Path(f"{prepared.invocation.host_ready_receipt_base}.permit.json")
    forged = cutover._strict_json(permit_path.read_bytes(), "permit")
    embedded = dict(forged["journal_authorization_event"])
    embedded["event_sha256"] = "f" * 64
    forged["journal_authorization_event"] = embedded
    forged["journal_authorization_event_sha256"] = "f" * 64
    forged_body = dict(forged)
    forged_body.pop("permit_sha256")
    forged["permit_sha256"] = cutover.sha256_json(forged_body)
    journal = cutover.CutoverJournal(
        root=executor.journal_root, prepared=prepared, clock=lambda: NOW
    )
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="STARTUP_PERMIT_JOURNAL_MISMATCH",
    ):
        cutover._validate_activation_permit_against_journal(
            forged,
            journal=journal,
            prepared=prepared,
            permit_path=permit_path,
        )


def test_crash_after_permit_publish_before_publication_event_is_revoked(
    prepared: cutover.PreparedCutover,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeHost(prepared)
    executor = _executor(prepared, backend)
    original = cutover.CutoverJournal.append
    injected = False

    def append(
        journal: cutover.CutoverJournal,
        event_type: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        nonlocal injected
        if event_type == "activation_permit_published" and not injected:
            injected = True
            assert Path(
                f"{prepared.invocation.host_ready_receipt_base}.permit.json"
            ).is_file()
            raise RuntimeError("simulated crash after O_EXCL permit")
        return original(journal, event_type, payload)  # type: ignore[return-value]

    monkeypatch.setattr(cutover.CutoverJournal, "append", append)
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError, match="APPLY_FAILED_ROLLED_BACK"
    ):
        executor.apply()
    assert injected
    assert not Path(
        f"{prepared.invocation.host_ready_receipt_base}.permit.json"
    ).exists()
    revoked = Path(f"{prepared.invocation.host_ready_receipt_base}.revoked.json")
    assert revoked.is_file()
    _assert_restored(prepared, backend)


def test_revocation_tombstone_precedes_failing_rollback_journal_append(
    prepared: cutover.PreparedCutover,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WrongStartedHost(FakeHost):
        def read_service_startup_receipt(
            self, invocation: cutover.CandidateInvocation,
            expected_service: cutover.ProcessIdentity, *, phase: str,
            timeout_seconds: float,
        ) -> dict[str, object]:
            value = dict(super().read_service_startup_receipt(
                invocation, expected_service, phase=phase,
                timeout_seconds=timeout_seconds,
            ))
            if phase == "started":
                value["challenge_sha256"] = "e" * 64
                body = dict(value)
                body.pop("receipt_sha256")
                value["receipt_sha256"] = cutover.sha256_json(body)
            return value

    backend = WrongStartedHost(prepared)
    executor = _executor(prepared, backend)
    original = cutover.CutoverJournal.append
    observed_tombstone = False

    def append(
        journal: cutover.CutoverJournal,
        event_type: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        nonlocal observed_tombstone
        if event_type == "rollback_started" and not observed_tombstone:
            revoked = Path(
                f"{prepared.invocation.host_ready_receipt_base}.revoked.json"
            )
            assert revoked.is_file()
            assert not Path(
                f"{prepared.invocation.host_ready_receipt_base}.permit.json"
            ).exists()
            observed_tombstone = True
            raise OSError("simulated blocked/failing evidence append")
        return original(journal, event_type, payload)  # type: ignore[return-value]

    monkeypatch.setattr(cutover.CutoverJournal, "append", append)
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="COMPENSATING_ROLLBACK_FAILED",
    ):
        executor.apply()
    assert observed_tombstone
    disable_index = backend.mutations.index(
        f"task:{cutover.CANDIDATE_TASK_NAME}:disable"
    )
    stop_indices = [
        index
        for index, operation in enumerate(backend.mutations)
        if operation.startswith("stop-candidate:")
    ]
    assert stop_indices and disable_index < min(stop_indices)
    _assert_restored(prepared, backend)


def test_exact_pre_staged_sha_runtime_is_accepted_without_overwrite(
    prepared: cutover.PreparedCutover,
) -> None:
    launcher = Path(prepared.invocation.launcher_script_path)
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(Path(prepared.invocation.launcher_source_path).read_bytes())
    service = Path(prepared.invocation.service_script_path)
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_bytes(Path(prepared.invocation.service_source_path).read_bytes())
    backend = FakeHost(prepared)
    assert _executor(prepared, backend).apply().verdict == "APPLIED_ALPACA_PAPER_ONLY"
    assert launcher.read_bytes() == Path(
        prepared.invocation.launcher_source_path
    ).read_bytes()
    assert service.read_bytes() == Path(
        prepared.invocation.service_source_path
    ).read_bytes()


def _revocation_value(
    *, identity: dict[str, object], state: str = "REVOCATION_REQUESTED"
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": (
            "chili.captured-paper-host-revocation-requested.v1"
            if state == "REVOCATION_REQUESTED"
            else cutover.STARTUP_REVOKED_SCHEMA
        ),
        "state": state,
        "activation_generation": "12aa9f2d-bda8-43d1-b0c4-397b7dbaac82",
        "manifest_sha256": "a" * 64,
        "account_scope": "alpaca:paper",
        "expected_account_id": "b19887f8-d9b5-4fa0-a622-2a8a7d70dc14",
        "journal_transaction_id": "b98b263b-574d-4dfa-97d9-4de6e38428fa",
        "journal_authorization_sequence": 2,
        "journal_authorization_event_sha256": "b" * 64,
        "permit_path": str(Path(identity["dispatch_lock_path"]).with_suffix(".permit")),
        "reason": "test",
        "workers_started": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
        **identity,
    }
    if state == "REVOCATION_REQUESTED":
        value["requested_at"] = "2026-07-16T12:00:00.000000Z"
    else:
        value["revoked_at"] = "2026-07-16T12:00:01.000000Z"
        value["revocation_requested_path"] = str(
            Path(identity["dispatch_lock_path"]).with_suffix(".requested")
        )
        value["revocation_requested_receipt_sha256"] = "c" * 64
    value["receipt_sha256"] = cutover.sha256_json(value)
    return value


def test_revocation_request_is_durable_before_dispatch_lock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = dict(cutover.create_startup_dispatch_lock(tmp_path / "dispatch.lock"))
    acquired = threading.Event()
    release = threading.Event()

    def owner() -> None:
        with cutover.hold_startup_dispatch_lock(identity, timeout_seconds=1.0):
            acquired.set()
            assert release.wait(2.0)

    thread = threading.Thread(target=owner, daemon=True)
    thread.start()
    assert acquired.wait(1.0)
    requested = tmp_path / "requested.json"
    request = _revocation_value(identity=identity)
    assert cutover._publish_revocation_requested(path=requested, value=request)
    assert requested.is_file()
    monkeypatch.setattr(cutover, "STARTUP_DISPATCH_LOCK_WAIT_SECONDS", 0.02)
    final = _revocation_value(identity=identity, state="REVOKED")
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="STARTUP_DISPATCH_LOCK_TIMEOUT",
    ):
        cutover._publish_final_revocation_under_dispatch_lock(
            path=tmp_path / "revoked.json", value=final, lock_identity=identity
        )
    assert requested.is_file()
    assert not (tmp_path / "revoked.json").exists()
    release.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_dispatch_lock_path_replacement_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dispatch.lock"
    identity = dict(cutover.create_startup_dispatch_lock(path))
    path.unlink()
    path.write_bytes(cutover.STARTUP_DISPATCH_LOCK_BYTE)

    with pytest.raises(
        cutover.CapturedPaperHostCutoverError,
        match="STARTUP_DISPATCH_LOCK_INVALID",
    ):
        cutover._validate_dispatch_lock_identity(identity, expected_path=path)


def test_revocation_retries_reject_foreign_generation_identity(tmp_path: Path) -> None:
    identity = dict(cutover.create_startup_dispatch_lock(tmp_path / "dispatch.lock"))
    requested_path = tmp_path / "requested.json"
    request = _revocation_value(identity=identity)
    cutover._publish_revocation_requested(path=requested_path, value=request)
    foreign_request = dict(request)
    foreign_request["manifest_sha256"] = "d" * 64
    foreign_request["receipt_sha256"] = cutover.sha256_json(
        {key: value for key, value in foreign_request.items() if key != "receipt_sha256"}
    )
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError, match="STARTUP_REVOCATION_REPLAY"
    ):
        cutover._publish_revocation_requested(
            path=requested_path, value=foreign_request
        )

    revoked_path = tmp_path / "revoked.json"
    final = _revocation_value(identity=identity, state="REVOKED")
    cutover._publish_final_revocation_under_dispatch_lock(
        path=revoked_path, value=final, lock_identity=identity
    )
    foreign_final = dict(final)
    foreign_final["revocation_requested_receipt_sha256"] = "e" * 64
    foreign_final["receipt_sha256"] = cutover.sha256_json(
        {key: value for key, value in foreign_final.items() if key != "receipt_sha256"}
    )
    with pytest.raises(
        cutover.CapturedPaperHostCutoverError, match="STARTUP_REVOCATION_REPLAY"
    ):
        cutover._publish_final_revocation_under_dispatch_lock(
            path=revoked_path, value=foreign_final, lock_identity=identity
        )
