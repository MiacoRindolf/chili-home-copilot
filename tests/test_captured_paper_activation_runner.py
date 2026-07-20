from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
import uuid

import pytest

from scripts import captured_paper_activation_runner as runner
from scripts import captured_paper_activation_contract as contract

_REAL_ASSERT_ISOLATED_INTERPRETER = runner._assert_isolated_interpreter

NOW = datetime(2026, 7, 18, 17, 0, tzinfo=UTC)
ACCOUNT_ID = "11111111-2222-4333-8444-555555555555"
COMMIT = "a" * 40
GENERATION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write(path: Path, raw: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return _sha(raw)


@dataclass
class RequestFixture:
    request: runner.ActivationRunnerRequest
    payload: dict[str, Any]
    request_path: Path
    files: dict[str, Path]

    def write_request(
        self,
        *,
        payload: Mapping[str, Any] | None = None,
        raw: bytes | None = None,
    ) -> tuple[Path, str]:
        request_raw = raw if raw is not None else _canonical(payload or self.payload)
        self.request_path.write_bytes(request_raw)
        return self.request_path, _sha(request_raw)


@pytest.fixture
def request_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> RequestFixture:
    candidate = tmp_path / "candidate"
    artifact = tmp_path / "artifacts"
    allowed = tmp_path / "allowed"
    candidate.mkdir()
    artifact.mkdir()
    allowed.mkdir()

    executable_names = (
        "git_executable",
        "python_executable",
        "powershell_executable",
        "schtasks_executable",
        "runtime_env_path",
    )
    files: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for index, name in enumerate(executable_names):
        suffix = ".bin"
        path = tmp_path / "pinned" / f"{name}{suffix}"
        raw = f"{index}:{name}\n".encode()
        hashes[name] = _write(path, raw)
        files[name] = path
    script_paths = {
        "bootstrap_stage0_script": (
            candidate / "scripts/captured_paper_isolated_stage0.py"
        ),
        "chain_script": candidate / "scripts/run_captured_paper_operator_chain.py",
        "finalizer_script": candidate / "scripts/finalize_captured_paper_activation.py",
        "cutover_script": candidate / "scripts/captured_paper_host_cutover.py",
    }
    for index, (name, path) in enumerate(script_paths.items(), start=20):
        hashes[name] = _write(path, f"{index}:{name}\n".encode())
        files[name] = path
    for index, relative in enumerate(
        runner._LAUNCHER_SOURCE_PATHS.values(), start=30
    ):
        source = candidate / relative
        if not source.exists():
            _write(source, f"launcher-source:{index}:{relative}\n".encode())
    dependency_root = tmp_path / "dependencies"
    dependency_root.mkdir()
    dependency_identity = contract.python_dependency_root_identity_sha256(
        dependency_root=dependency_root,
        python_executable=files["python_executable"],
        python_executable_sha256=hashes["python_executable"],
    )
    files["chain_request_path"] = tmp_path / "pinned" / "chain-request.json"
    hashes["chain_request_path"] = _write(
        files["chain_request_path"],
        _canonical(
            {
                "schema_version": "chili.captured-paper-operator-chain-request.v1",
                "account_scope": runner.ACCOUNT_SCOPE,
                "live_cash_authorized": False,
                "python_dependency_root": str(dependency_root),
                "python_dependency_root_identity_sha256": dependency_identity,
                "bootstrap_stage0_script": str(files["bootstrap_stage0_script"]),
                "bootstrap_stage0_script_sha256": hashes[
                    "bootstrap_stage0_script"
                ],
            }
        ),
    )

    payload: dict[str, Any] = {
        "schema_version": runner.REQUEST_SCHEMA_VERSION,
        "account_scope": runner.ACCOUNT_SCOPE,
        "live_cash_authorized": False,
        "paper_task_name": runner.PAPER_TASK_NAME,
        "candidate_root": str(candidate),
        "expected_git_commit": COMMIT,
        "git_executable": str(files["git_executable"]),
        "git_executable_sha256": hashes["git_executable"],
        "python_executable": str(files["python_executable"]),
        "python_executable_sha256": hashes["python_executable"],
        "powershell_executable": str(files["powershell_executable"]),
        "powershell_executable_sha256": hashes["powershell_executable"],
        "schtasks_executable": str(files["schtasks_executable"]),
        "schtasks_executable_sha256": hashes["schtasks_executable"],
        "bootstrap_stage0_script": str(files["bootstrap_stage0_script"]),
        "bootstrap_stage0_script_sha256": hashes["bootstrap_stage0_script"],
        "chain_script": str(files["chain_script"]),
        "chain_script_sha256": hashes["chain_script"],
        "chain_request_path": str(files["chain_request_path"]),
        "chain_request_sha256": hashes["chain_request_path"],
        "finalizer_script": str(files["finalizer_script"]),
        "finalizer_script_sha256": hashes["finalizer_script"],
        "cutover_script": str(files["cutover_script"]),
        "cutover_script_sha256": hashes["cutover_script"],
        "python_dependency_root": str(dependency_root),
        "python_dependency_root_identity_sha256": dependency_identity,
        "runtime_env_path": str(files["runtime_env_path"]),
        "runtime_env_sha256": hashes["runtime_env_path"],
        "artifact_root": str(artifact),
        "expected_account_id": ACCOUNT_ID,
        "test_database_name": "captured_paper_test",
        "allowed_read_roots": [str(tmp_path)],
        "timeouts": {
            "chain": 10,
            "no_order_smoke": 10,
            "finalize": 10,
            "validate_only": 10,
            "apply": 10,
            "rollback": 10,
            "task_query": 10,
        },
    }
    monkeypatch.setattr(
        runner,
        "_authoritative_executable_paths",
        lambda: {
            name: files[name].resolve(strict=True)
            for name in (
                "git_executable",
                "python_executable",
                "powershell_executable",
                "schtasks_executable",
            )
        },
    )
    request_path = tmp_path / "activation-request.json"
    request_raw = _canonical(payload)
    request_path.write_bytes(request_raw)
    loaded = runner.load_activation_runner_request(
        request_path=request_path,
        request_sha256=_sha(request_raw),
    )
    return RequestFixture(
        request=loaded,
        payload=payload,
        request_path=request_path,
        files=files,
    )


@dataclass
class Scenario:
    head: str = COMMIT
    dirty: bool = False
    wrong_git_root: bool = False
    ignored_executable_payload: bool = False
    task_exists_before: bool = False
    task_exists_after: bool = True
    apply_outcome: str = "success"
    rollback_outcome: str = "success"
    publish_started: bool = True
    recovery_outcome: str = "none"


class FakeExecutor:
    def __init__(
        self,
        request: runner.ActivationRunnerRequest,
        tmp_path: Path,
        scenario: Scenario | None = None,
    ) -> None:
        self.request = request
        self.tmp_path = tmp_path
        self.scenario = scenario or Scenario()
        self.calls: list[tuple[tuple[str, ...], int, Path, Mapping[str, str]]] = []
        self.task_queries = 0
        self.receipt_path = (
            request.artifact_root
            / "receipts"
            / f"no-order-receipt-{tmp_path.name}.json"
        )
        self.preactivation_path = request.artifact_root / "preactivation" / "pending"
        self.next_command_path = request.artifact_root / "operator" / "pending"
        self.manifest_path = request.artifact_root / "activation" / "pending"
        self.program = str(request.powershell_executable)
        self._prepare_chain_documents()

    @property
    def modes(self) -> list[str]:
        values: list[str] = []
        for argv, _timeout, _cwd, _env in self.calls:
            if "--mode" in argv:
                values.append(argv[argv.index("--mode") + 1])
        return values

    def replace_next_command(self, document: Mapping[str, Any]) -> None:
        raw = _canonical(document)
        self.next_sha = _sha(raw)
        self.next_command_path = (
            self.request.artifact_root
            / "operator"
            / GENERATION
            / "next-command"
            / self.next_sha[:2]
            / f"{self.next_sha}.json"
        )
        _write(self.next_command_path, raw)

    def _prepare_chain_documents(self) -> None:
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        dependency_root = self.request.python_dependency_root
        dependency_identity = contract.python_dependency_root_identity_sha256(
            dependency_root=dependency_root,
            python_executable=self.request.python_executable,
            python_executable_sha256=self.request.python_executable_sha256,
        )
        source_paths = {
            role: self.request.candidate_root / relative
            for role, relative in runner._LAUNCHER_SOURCE_PATHS.items()
        }
        source_hashes = {role: _sha(path.read_bytes()) for role, path in source_paths.items()}
        staged_root = self.request.artifact_root / "activation" / GENERATION
        staged_paths: dict[str, Path] = {}
        for role, suffix in (
            ("activation_launcher", ".ps1"),
            ("activation_stage0", ".py"),
            ("activation_service", ".py"),
        ):
            digest = source_hashes[role]
            staged = staged_root / digest / f"{digest}{suffix}"
            _write(staged, source_paths[role].read_bytes())
            staged_paths[role] = staged
        host_ready = staged_root / "handshake" / "captured-paper"
        host_ready.parent.mkdir(parents=True, exist_ok=True)
        invocations: dict[str, Mapping[str, Any]] = {}
        for mode in sorted(contract._LAUNCHER_MODE_BINDINGS):
            projection = contract.launcher_invocation_projection(
                mode=mode,
                candidate_root=self.request.candidate_root,
                python_executable=self.request.python_executable,
                python_executable_sha256=self.request.python_executable_sha256,
                python_dependency_root=dependency_root,
                python_dependency_root_identity_sha256=dependency_identity,
                allowed_read_roots=tuple(Path(root) for root in self.request.allowed_read_roots),
                launcher_path=source_paths["activation_launcher"],
                launcher_sha256=source_hashes["activation_launcher"],
                stage0_path=source_paths["activation_stage0"],
                stage0_sha256=source_hashes["activation_stage0"],
                service_path=source_paths["activation_service"],
                service_sha256=source_hashes["activation_service"],
                launcher_staged_path=staged_paths["activation_launcher"],
                stage0_staged_path=staged_paths["activation_stage0"],
                service_staged_path=staged_paths["activation_service"],
                host_ready_receipt=(host_ready if mode == "ActivatePaper" else None),
                no_order_receipt_output=(
                    self.receipt_path if mode == "NoOrderSmoke" else None
                ),
            )
            invocations[mode] = {
                "projection": dict(projection),
                "projection_sha256": contract.sha256_json(projection),
            }
        launcher_raw = _canonical(
            {
                "schema_version": contract.LAUNCHER_ARGUMENT_CONTRACT_SCHEMA_VERSION,
                "invocations": invocations,
            }
        )
        launcher_sha = _sha(launcher_raw)
        launcher_path = (
            self.request.artifact_root
            / "operator"
            / GENERATION
            / "launcher-contract"
            / launcher_sha[:2]
            / f"{launcher_sha}.json"
        )
        _write(launcher_path, launcher_raw)
        preactivation_document = {
            "schema_version": contract.PREACTIVATION_MANIFEST_SCHEMA_VERSION,
            "activation_generation": GENERATION,
            "authority_boundary": {
                "broker": "alpaca",
                "broker_environment": "paper",
                "account_scope": runner.ACCOUNT_SCOPE,
                "expected_account_id": ACCOUNT_ID,
                "paper_order_submission_authorized": False,
                "live_cash_authorized": False,
                "real_money_authorized": False,
            },
            "cutover": {
                "candidate_root": str(self.request.candidate_root),
                "launcher_arguments_path": str(launcher_path),
                "launcher_arguments_sha256": launcher_sha,
            },
        }
        preactivation_document["activation_manifest_sha256"] = contract.sha256_json(
            preactivation_document
        )
        preactivation_raw = _canonical(preactivation_document)
        preactivation_sha = _sha(preactivation_raw)
        self.preactivation_path = (
            self.request.artifact_root
            / "preactivation"
            / preactivation_sha[:2]
            / f"{preactivation_sha}.json"
        )
        _write(self.preactivation_path, preactivation_raw)
        projection = invocations["NoOrderSmoke"]["projection"]
        next_command = {
            "schema_version": "chili.captured-paper-operator-next-command.v1",
            "activation_generation": GENERATION,
            "account_scope": runner.ACCOUNT_SCOPE,
            "expected_account_id": ACCOUNT_ID,
            "next_step": "NO_ORDER_SMOKE_ONLY",
            "program": self.program,
            "arguments": list(
                runner._expected_no_order_powershell_arguments(
                    projection=projection,
                    preactivation_path=self.preactivation_path,
                    preactivation_sha256=preactivation_sha,
                )
            ),
            "no_order_receipt_output": str(
                projection["no_order_receipt_output_path"]
            ),
            "preactivation_manifest_path": str(self.preactivation_path),
            "preactivation_manifest_sha256": preactivation_sha,
            "host_snapshot_authority": (
                "PREACTIVATION_BASELINE_FROM_EXTERNAL_RAW_SNAPSHOT"
            ),
            "current_host_inventory_observed": False,
            "final_real_validate_only_required": True,
            "invoked": False,
            "activate_paper_command_emitted": False,
            "host_cutover_invoked": False,
            "paper_order_submission_authorized": False,
            "paper_service_started": False,
            "live_cash_authorized": False,
        }
        next_raw = _canonical(next_command)
        self.next_sha = _sha(next_raw)
        self.next_command_path = (
            self.request.artifact_root
            / "operator"
            / GENERATION
            / "next-command"
            / self.next_sha[:2]
            / f"{self.next_sha}.json"
        )
        _write(self.next_command_path, next_raw)

        snapshot_refs: dict[str, tuple[Path, str]] = {}
        for role in ("task_snapshot", "process_snapshot", "restore_plan"):
            path = (
                self.request.artifact_root
                / "snapshots"
                / self.tmp_path.name
                / f"{role.replace('_', '-')}.json"
            )
            snapshot_refs[role] = (path, _write(path, _canonical({"role": role})))
        plan = {
            "schema_version": "chili.captured-paper-operator-plan.v1",
            "activation_generation": GENERATION,
            "expected_account_id": ACCOUNT_ID,
            "candidate_root": str(self.request.candidate_root),
            "operator_output_root": str(self.request.artifact_root / "operator"),
            "preactivation_output_root": str(
                self.request.artifact_root / "preactivation"
            ),
            "activation_artifact_root": str(
                self.request.artifact_root / "activation"
            ),
            "capture_store_root": str(self.request.artifact_root / "capture-store"),
            "runtime_env_path": str(self.request.runtime_env_path),
            "runtime_env_sha256": self.request.runtime_env_sha256,
            "python_executable": str(self.request.python_executable),
            "powershell_executable": str(self.request.powershell_executable),
            "no_order_receipt_output": str(self.receipt_path),
            "allowed_read_roots": list(self.request.allowed_read_roots),
            **{
                f"{role}_path": str(reference[0])
                for role, reference in snapshot_refs.items()
            },
            **{
                f"{role}_sha256": reference[1]
                for role, reference in snapshot_refs.items()
            },
        }
        plan_raw = _canonical(plan)
        self.plan_sha = _sha(plan_raw)
        plan_path = (
            self.request.artifact_root / "operator" / f"{self.plan_sha}.plan.json"
        )
        _write(plan_path, plan_raw)

        generation_root = self.request.artifact_root / "operator" / GENERATION
        _write(
            generation_root / "candidate-task-template" / "task.xml",
            b"<Task />\n",
        )
        _write(
            generation_root / "candidate-action" / "action.json",
            _canonical({"account_scope": runner.ACCOUNT_SCOPE}),
        )
        manifest_raw = _canonical({"kind": "activation-manifest", "paper": True})
        self.manifest_sha = _sha(manifest_raw)
        self.manifest_path = (
            self.request.artifact_root
            / "activation"
            / self.manifest_sha[:2]
            / f"{self.manifest_sha}.json"
        )
        _write(self.manifest_path, manifest_raw)

    def _chain_result(self) -> runner.CommandResult:
        document = {
            "verdict": "CAPTURED_ALPACA_PAPER_BUILD_READY_WITH_EXTERNAL_HOST_BASELINE",
            "activation_generation": GENERATION,
            "next_command": {
                "path": str(self.next_command_path),
                "sha256": self.next_sha,
            },
            "preactivation_manifest": {
                "path": str(self.preactivation_path),
                "sha256": hashlib.sha256(self.preactivation_path.read_bytes()).hexdigest(),
            },
        }
        return runner.CommandResult(
            0,
            f"PLAN: {self.plan_sha}\n{_canonical(document).decode()}\n",
            "",
        )

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int,
        cwd: Path,
        env: Mapping[str, str],
    ) -> runner.CommandResult:
        args = tuple(str(value) for value in argv)
        self.calls.append((args, timeout, cwd, dict(env)))

        if args[:3] == (
            str(self.request.git_executable),
            "rev-parse",
            "--show-toplevel",
        ):
            root = self.tmp_path if self.scenario.wrong_git_root else self.request.candidate_root
            return runner.CommandResult(0, f"{root}\n", "")
        if args[:3] == (
            str(self.request.git_executable),
            "rev-parse",
            "HEAD",
        ):
            return runner.CommandResult(0, f"{self.scenario.head}\n", "")
        if args[:3] == (
            str(self.request.git_executable),
            "status",
            "--porcelain=v2",
        ):
            return runner.CommandResult(
                0,
                " M scripts/captured_paper_activation_runner.py\n"
                if self.scenario.dirty
                else "",
                "",
            )
        if args[:3] == (
            str(self.request.git_executable),
            "ls-files",
            "--error-unmatch",
        ):
            separator = args.index("--")
            return runner.CommandResult(
                0, "\0".join(args[separator + 1 :]) + "\0", ""
            )
        if args[:3] == (
            str(self.request.git_executable),
            "ls-files",
            "--others",
        ):
            return runner.CommandResult(
                0,
                "ignored-shadow.pyd\0"
                if self.scenario.ignored_executable_payload
                else "",
                "",
            )
        if args and args[0] == str(self.request.schtasks_executable):
            self.task_queries += 1
            exists = (
                self.scenario.task_exists_before
                if self.task_queries == 1
                else self.scenario.task_exists_after
            )
            return runner.CommandResult(
                0 if exists else 1,
                "task\n" if exists else "",
                "" if exists else "ERROR: The system cannot find the file specified.\n",
            )
        if str(self.request.chain_script) in args:
            return self._chain_result()
        if args and args[0] == self.program:
            _write(self.receipt_path, _canonical({"broker_posts": 0, "paper": True}))
            return runner.CommandResult(0, "NO_ORDER_SMOKE_OK\n", "")
        if str(self.request.finalizer_script) in args:
            document = {
                "verdict": "CAPTURED_ALPACA_PAPER_FINAL_MANIFEST_PUBLISHED",
                "manifest_path": str(self.manifest_path),
                "manifest_sha256": self.manifest_sha,
            }
            return runner.CommandResult(0, _canonical(document).decode() + "\n", "")
        if str(self.request.cutover_script) in args:
            mode = args[args.index("--mode") + 1]
            if mode == "RecoverOnly":
                if self.scenario.recovery_outcome == "error":
                    return runner.CommandResult(9, "", "synthetic recovery failure")
                if self.scenario.recovery_outcome == "rolled_back":
                    return runner.CommandResult(
                        0, '{"verdict":"ROLLED_BACK_EXACT"}\n', ""
                    )
                if self.scenario.recovery_outcome == "applied":
                    return runner.CommandResult(
                        0, '{"verdict":"ALREADY_APPLIED_EXACT"}\n', ""
                    )
                return runner.CommandResult(
                    0, '{"verdict":"NO_RECOVERY_REQUIRED"}\n', ""
                )
            if mode == "ValidateOnly":
                return runner.CommandResult(
                    0, '{"verdict":"VALIDATED_NO_HOST_MUTATION"}\n', ""
                )
            if mode == "Apply":
                if self.scenario.apply_outcome == "timeout":
                    raise runner.CapturedPaperActivationRunnerError(
                        "STAGE_TIMEOUT", "synthetic Apply timeout"
                    )
                if self.scenario.apply_outcome == "error":
                    return runner.CommandResult(9, "", "synthetic Apply failure")
                if self.scenario.publish_started:
                    _write(
                        self.request.artifact_root
                        / "activation"
                        / GENERATION
                        / "handshake"
                        / "only.started.json",
                        _canonical({"state": "STARTED", "paper": True}),
                    )
                return runner.CommandResult(
                    0, '{"verdict":"APPLIED_ALPACA_PAPER_ONLY"}\n', ""
                )
            if mode == "Rollback":
                if self.scenario.rollback_outcome == "timeout":
                    raise runner.CapturedPaperActivationRunnerError(
                        "STAGE_TIMEOUT", "synthetic Rollback timeout"
                    )
                if self.scenario.rollback_outcome == "error":
                    return runner.CommandResult(9, "", "synthetic Rollback failure")
                return runner.CommandResult(0, '{"verdict":"ROLLED_BACK_EXACT"}\n', "")
        raise AssertionError(f"unexpected fake command: {args!r}")


@pytest.fixture(autouse=True)
def _protected_runtime_without_host_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    # The focused readiness shard runs inside the real activation runner, which
    # already owns the production host-wide mutex.  Unit invocations must use
    # their own namespace; the dedicated contention test below still proves
    # that two owners of the same mutex fail closed.
    monkeypatch.setattr(
        runner,
        "_HOST_WIDE_ACTIVATION_MUTEX_NAME",
        "Local\\CHILI-Captured-PAPER-Activation-Test-" + uuid.uuid4().hex,
    )
    monkeypatch.setattr(runner, "_assert_isolated_interpreter", lambda: None)
    monkeypatch.setattr(runner, "_sanitize_python_control_environment", lambda: None)
    monkeypatch.setattr(runner, "_install_sealed_dependency_root", lambda _request: None)
    monkeypatch.setattr(runner.sys, "pycache_prefix", runner.sys.pycache_prefix)
    monkeypatch.setattr(
        runner,
        "_install_paper_environment",
        lambda _request, *, pycache_root: {
            "CHILI_ACCOUNT_SCOPE": runner.ACCOUNT_SCOPE,
            "PYTHONPYCACHEPREFIX": str(pycache_root),
        },
    )


def _run(
    request: runner.ActivationRunnerRequest,
    executor: FakeExecutor,
    *,
    mode: str = "ValidateOnly",
) -> Mapping[str, Any]:
    return runner.run_activation(
        request,
        mode=mode,
        confirmation=(
            runner.ACTIVATE_CONFIRMATION if mode == "ActivatePaper" else None
        ),
        executor=executor,
        clock=lambda: NOW,
    )


def _error_code(exc_info: pytest.ExceptionInfo[runner.CapturedPaperActivationRunnerError]) -> str:
    return exc_info.value.code


def test_loader_accepts_only_exact_canonical_hash_bound_request(
    request_fixture: RequestFixture,
) -> None:
    path, digest = request_fixture.write_request()

    loaded = runner.load_activation_runner_request(
        request_path=path,
        request_sha256=digest,
    )

    assert loaded.request_sha256 == digest
    assert loaded.expected_account_id == ACCOUNT_ID
    assert loaded.candidate_root == Path(request_fixture.payload["candidate_root"])


def test_loader_rejects_noncanonical_request_bytes(
    request_fixture: RequestFixture,
) -> None:
    raw = json.dumps(request_fixture.payload, indent=2, sort_keys=False).encode()
    path, digest = request_fixture.write_request(raw=raw)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.load_activation_runner_request(
            request_path=path,
            request_sha256=digest,
        )

    assert _error_code(exc_info) == "REQUEST_NOT_CANONICAL"


def test_loader_rejects_duplicate_json_keys(request_fixture: RequestFixture) -> None:
    raw = (
        b'{"schema_version":"one","schema_version":"two",'
        b'"account_scope":"alpaca:paper"}'
    )
    path, digest = request_fixture.write_request(raw=raw)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.load_activation_runner_request(
            request_path=path,
            request_sha256=digest,
        )

    assert _error_code(exc_info) == "JSON_DUPLICATE_KEY"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_scope", "alpaca:live"),
        ("live_cash_authorized", True),
        ("paper_task_name", "CHILI-Live-Cash"),
    ],
)
def test_loader_rejects_any_non_paper_scope(
    request_fixture: RequestFixture,
    field: str,
    value: object,
) -> None:
    payload = {**request_fixture.payload, field: value}
    path, digest = request_fixture.write_request(payload=payload)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.load_activation_runner_request(
            request_path=path,
            request_sha256=digest,
        )

    assert _error_code(exc_info) == "PAPER_SCOPE_INVALID"


@pytest.mark.parametrize(
    "account_id",
    ["not-a-uuid", "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"],
)
def test_loader_rejects_invalid_or_noncanonical_paper_account_uuid(
    request_fixture: RequestFixture,
    account_id: str,
) -> None:
    payload = {**request_fixture.payload, "expected_account_id": account_id}
    path, digest = request_fixture.write_request(payload=payload)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.load_activation_runner_request(
            request_path=path,
            request_sha256=digest,
        )

    assert _error_code(exc_info) == "ACCOUNT_ID_INVALID"


def test_loader_rejects_outer_request_hash_drift(
    request_fixture: RequestFixture,
) -> None:
    path, digest = request_fixture.write_request()
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.load_activation_runner_request(
            request_path=path,
            request_sha256=digest,
        )

    assert _error_code(exc_info) == "REQUEST_HASH_MISMATCH"


def test_loader_rejects_pinned_file_drift(request_fixture: RequestFixture) -> None:
    request_fixture.files["cutover_script"].write_bytes(b"changed after signing\n")
    path, digest = request_fixture.write_request()

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.load_activation_runner_request(
            request_path=path,
            request_sha256=digest,
        )

    assert _error_code(exc_info) == "FILE_HASH_MISMATCH"


@pytest.mark.parametrize(
    "field",
    [
        "bootstrap_stage0_script",
        "chain_script",
        "finalizer_script",
        "cutover_script",
    ],
)
def test_loader_rejects_hash_valid_script_outside_canonical_candidate_location(
    request_fixture: RequestFixture,
    field: str,
) -> None:
    original = request_fixture.files[field]
    alternate = request_fixture.request_path.parent / "alternate" / original.name
    digest = _write(alternate, original.read_bytes())
    payload = {
        **request_fixture.payload,
        field: str(alternate),
        f"{field}_sha256": digest,
    }
    path, request_digest = request_fixture.write_request(payload=payload)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.load_activation_runner_request(
            request_path=path,
            request_sha256=request_digest,
        )

    assert _error_code(exc_info) == "PATH_AUTHORITY_MISMATCH"


@pytest.mark.parametrize(
    "field",
    [
        "git_executable",
        "python_executable",
        "powershell_executable",
        "schtasks_executable",
    ],
)
def test_loader_rejects_hash_valid_substitute_executable(
    request_fixture: RequestFixture,
    field: str,
) -> None:
    alternate = request_fixture.request_path.parent / "alternate" / f"{field}.bin"
    digest = _write(alternate, request_fixture.files[field].read_bytes())
    payload = {
        **request_fixture.payload,
        field: str(alternate),
        f"{field}_sha256": digest,
    }
    path, request_digest = request_fixture.write_request(payload=payload)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.load_activation_runner_request(
            request_path=path,
            request_sha256=request_digest,
        )

    assert _error_code(exc_info) == "PATH_AUTHORITY_MISMATCH"


def test_loader_rejects_noncanonical_dotdot_alias_for_exact_candidate_script(
    request_fixture: RequestFixture,
) -> None:
    original = request_fixture.files["chain_script"]
    alias = original.parent / ".." / original.parent.name / original.name
    payload = {**request_fixture.payload, "chain_script": str(alias)}
    path, request_digest = request_fixture.write_request(payload=payload)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.load_activation_runner_request(
            request_path=path,
            request_sha256=request_digest,
        )

    assert _error_code(exc_info) == "PATH_NOT_CANONICAL"


def test_loader_rejects_reparse_alias_even_when_target_and_hash_are_exact(
    request_fixture: RequestFixture,
) -> None:
    original = request_fixture.files["chain_script"]
    alias = request_fixture.request_path.parent / "chain-script-alias.py"
    try:
        alias.symlink_to(original)
    except OSError:
        pytest.skip("host does not permit symlink creation")
    payload = {**request_fixture.payload, "chain_script": str(alias)}
    path, request_digest = request_fixture.write_request(payload=payload)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.load_activation_runner_request(
            request_path=path,
            request_sha256=request_digest,
        )

    assert _error_code(exc_info) == "REPARSE_PATH"


def test_loader_rejects_unc_root_before_any_network_filesystem_probe() -> None:
    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner._strict_directory(
            r"\\untrusted-host\captured-paper", field="candidate_root"
        )

    assert _error_code(exc_info) == "NETWORK_PATH_FORBIDDEN"


def test_loader_binds_dependency_root_to_canonical_chain_request(
    request_fixture: RequestFixture,
    tmp_path: Path,
) -> None:
    alternate = tmp_path / "alternate-dependencies"
    alternate.mkdir()
    chain_document = json.loads(
        request_fixture.files["chain_request_path"].read_text(encoding="utf-8")
    )
    chain_document["python_dependency_root"] = str(alternate)
    chain_raw = _canonical(chain_document)
    chain_sha = _sha(chain_raw)
    request_fixture.files["chain_request_path"].write_bytes(chain_raw)
    payload = {
        **request_fixture.payload,
        "chain_request_sha256": chain_sha,
    }
    path, request_digest = request_fixture.write_request(payload=payload)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.load_activation_runner_request(
            request_path=path,
            request_sha256=request_digest,
        )

    assert _error_code(exc_info) == "CHAIN_REQUEST_AUTHORITY_MISMATCH"


def test_production_isolation_gate_requires_i_s_b_and_no_site_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner.sys,
        "flags",
        SimpleNamespace(isolated=0, no_site=1, dont_write_bytecode=1, safe_path=1),
    )
    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        _REAL_ASSERT_ISOLATED_INTERPRETER()
    assert _error_code(exc_info) == "PYTHON_ISOLATION_REQUIRED"

    monkeypatch.setattr(
        runner.sys,
        "flags",
        SimpleNamespace(isolated=1, no_site=1, dont_write_bytecode=1, safe_path=1),
    )
    monkeypatch.delitem(runner.sys.modules, "site", raising=False)
    monkeypatch.delitem(runner.sys.modules, "sitecustomize", raising=False)
    monkeypatch.delitem(runner.sys.modules, "usercustomize", raising=False)
    _REAL_ASSERT_ISOLATED_INTERPRETER()


@pytest.mark.parametrize(
    ("mode", "confirmation", "code"),
    [
        ("ActivatePaper", None, "ACTIVATION_CONFIRMATION_REQUIRED"),
        ("ActivatePaper", "wrong", "ACTIVATION_CONFIRMATION_REQUIRED"),
        (
            "ValidateOnly",
            runner.ACTIVATE_CONFIRMATION,
            "ACTIVATION_CONFIRMATION_FORBIDDEN",
        ),
    ],
)
def test_confirmation_boundary_rejects_before_any_command(
    request_fixture: RequestFixture,
    tmp_path: Path,
    mode: str,
    confirmation: str | None,
    code: str,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.run_activation(
            request_fixture.request,
            mode=mode,
            confirmation=confirmation,
            executor=executor,
        )

    assert _error_code(exc_info) == code
    assert executor.calls == []


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        (Scenario(wrong_git_root=True), "GIT_ROOT_MISMATCH"),
        (Scenario(head="b" * 40), "GIT_COMMIT_MISMATCH"),
        (Scenario(dirty=True), "WORKTREE_DIRTY"),
        (
            Scenario(ignored_executable_payload=True),
            "GIT_IGNORED_EXECUTABLE_PAYLOAD",
        ),
    ],
)
def test_pinned_commit_and_complete_worktree_authority_fail_closed(
    request_fixture: RequestFixture,
    tmp_path: Path,
    scenario: Scenario,
    code: str,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path, scenario)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        _run(request_fixture.request, executor)

    assert _error_code(exc_info) == code
    assert executor.modes == []


def test_git_authority_runs_before_secret_install_with_minimal_sanitized_env(
    request_fixture: RequestFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path)
    for key in (
        "DATABASE_URL",
        "CHILI_ALPACA_API_KEY",
        "CHILI_ALPACA_API_SECRET",
        "MASSIVE_API_KEY",
    ):
        monkeypatch.setenv(key, f"secret-{key}")

    def install_after_git(
        _request: runner.ActivationRunnerRequest, *, pycache_root: Path
    ) -> dict[str, str]:
        git_calls = [
            call
            for call in executor.calls
            if call[0] and call[0][0] == str(request_fixture.request.git_executable)
        ]
        assert len(git_calls) == 5
        return {
            "CHILI_ACCOUNT_SCOPE": runner.ACCOUNT_SCOPE,
            "PYTHONPYCACHEPREFIX": str(pycache_root),
        }

    monkeypatch.setattr(runner, "_install_paper_environment", install_after_git)
    _run(request_fixture.request, executor)

    git_calls = [
        call
        for call in executor.calls
        if call[0] and call[0][0] == str(request_fixture.request.git_executable)
    ]
    assert len(git_calls) == 5
    for _argv, _timeout, _cwd, git_env in git_calls:
        assert not {
            "DATABASE_URL",
            "CHILI_ALPACA_API_KEY",
            "CHILI_ALPACA_API_SECRET",
            "MASSIVE_API_KEY",
        }.intersection(git_env)
        assert git_env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert git_env["GIT_OPTIONAL_LOCKS"] == "0"
        assert git_env["GIT_CONFIG_VALUE_0"] == "false"


def test_existing_paper_task_blocks_a_fresh_generation(
    request_fixture: RequestFixture,
    tmp_path: Path,
) -> None:
    executor = FakeExecutor(
        request_fixture.request,
        tmp_path,
        Scenario(task_exists_before=True),
    )

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        _run(request_fixture.request, executor)

    assert _error_code(exc_info) == "EXISTING_PAPER_TASK_REQUIRES_RECONCILIATION"
    assert executor.task_queries == 1
    assert executor.modes == ["RecoverOnly"]


@pytest.mark.parametrize(
    ("recovery_outcome", "expected_code"),
    [
        ("rolled_back", "RECOVERY_COMPLETED_RERUN_REQUIRED"),
        ("applied", "PAPER_ALREADY_ACTIVE"),
        ("error", "RECOVERY_REJECTED"),
    ],
)
def test_recovery_result_stops_before_fresh_generation_work(
    request_fixture: RequestFixture,
    tmp_path: Path,
    recovery_outcome: str,
    expected_code: str,
) -> None:
    executor = FakeExecutor(
        request_fixture.request,
        tmp_path,
        Scenario(recovery_outcome=recovery_outcome),
    )

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        _run(request_fixture.request, executor)

    assert _error_code(exc_info) == expected_code
    assert executor.modes == ["RecoverOnly"]
    assert executor.task_queries == 0


def test_host_wide_lock_rejects_a_concurrent_runner_before_commands(
    request_fixture: RequestFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path)
    lock_path = tmp_path / "fixed-host-activation.lock"
    alternate_root = tmp_path / "alternate-artifacts"
    alternate_root.mkdir()
    alternate_request = replace(
        request_fixture.request,
        artifact_root=alternate_root.resolve(strict=True),
    )
    monkeypatch.setattr(runner, "_HOST_WIDE_ACTIVATION_LOCK_PATH", lock_path)
    mutex_name = "Local\\CHILI-Captured-PAPER-Activation-Test-" + uuid.uuid4().hex
    monkeypatch.setattr(runner, "_HOST_WIDE_ACTIVATION_MUTEX_NAME", mutex_name)
    acquired = threading.Event()
    release = threading.Event()
    holder_errors: list[BaseException] = []

    def _hold() -> None:
        try:
            with runner._HostWideActivationLock(
                lock_path,
                mutex_name=mutex_name,
            ):
                acquired.set()
                if not release.wait(timeout=10):
                    raise AssertionError("test did not release the host activation mutex")
        except BaseException as exc:
            holder_errors.append(exc)
            acquired.set()

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert acquired.wait(timeout=10)

    try:
        with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
            _run(alternate_request, executor)
    finally:
        release.set()
        holder.join(timeout=10)

    assert not holder.is_alive()
    assert holder_errors == []
    assert _error_code(exc_info) == "ACTIVATION_ALREADY_RUNNING"
    assert executor.calls == []


def test_validate_only_reaches_real_validate_boundary_but_never_apply(
    request_fixture: RequestFixture,
    tmp_path: Path,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path)

    result = _run(request_fixture.request, executor)

    assert result == {
        "schema_version": runner.RESULT_SCHEMA_VERSION,
        "account_scope": runner.ACCOUNT_SCOPE,
        "activation_generation": GENERATION,
        "manifest_sha256": executor.manifest_sha,
        "request_sha256": request_fixture.request.request_sha256,
        "expected_git_commit": COMMIT,
        "live_cash_authorized": False,
        "generated_at": "2026-07-18T17:00:00Z",
        "verdict": "VALIDATED_NO_HOST_MUTATION",
        "paper_started": False,
    }
    assert executor.modes == ["RecoverOnly", "ValidateOnly"]
    assert executor.task_queries == 1
    chain_argv = next(
        argv
        for argv, _timeout, _cwd, _env in executor.calls
        if str(request_fixture.request.chain_script) in argv
    )
    assert chain_argv == (
        str(request_fixture.request.python_executable),
        "-S",
        "-B",
        str(request_fixture.request.chain_script),
        "--request",
        str(request_fixture.request.chain_request_path),
        "--request-sha256",
        request_fixture.request.chain_request_sha256,
        "--activation-request",
        str(request_fixture.request.request_path),
        "--activation-request-sha256",
        request_fixture.request.request_sha256,
    )


def test_hash_valid_next_command_with_extra_launcher_argument_is_rejected_before_launch(
    request_fixture: RequestFixture,
    tmp_path: Path,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path)
    document = json.loads(executor.next_command_path.read_text(encoding="utf-8"))
    document["arguments"] = [*document["arguments"], "-InjectedArgument"]
    executor.replace_next_command(document)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        _run(request_fixture.request, executor)

    assert _error_code(exc_info) == "NEXT_COMMAND_AUTHORITY_MISMATCH"
    assert not any(call[0][0] == executor.program for call in executor.calls)


def test_staged_no_order_service_drift_is_rehashed_at_immediate_prelaunch(
    request_fixture: RequestFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path)
    original = runner._revalidate_staged_no_order_paths
    drifted = False

    def drift_then_verify(projection: Mapping[str, Any]) -> None:
        nonlocal drifted
        if not drifted:
            Path(str(projection["service_staged_path"])).write_bytes(b"drifted\n")
            drifted = True
        original(projection)

    monkeypatch.setattr(
        runner, "_revalidate_staged_no_order_paths", drift_then_verify
    )
    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        _run(request_fixture.request, executor)

    assert _error_code(exc_info) == "FILE_HASH_MISMATCH"
    assert not any(call[0][0] == executor.program for call in executor.calls)


def test_hash_valid_next_command_at_noncanonical_content_address_is_rejected(
    request_fixture: RequestFixture,
    tmp_path: Path,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path)
    alias = request_fixture.request.artifact_root / "operator" / "next-command-alias.json"
    _write(alias, executor.next_command_path.read_bytes())
    executor.next_command_path = alias

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        _run(request_fixture.request, executor)

    assert _error_code(exc_info) == "CONTENT_ADDRESS_PATH_MISMATCH"
    assert not any(call[0][0] == executor.program for call in executor.calls)


def test_hash_valid_activation_manifest_alias_is_rejected_before_cutover_validation(
    request_fixture: RequestFixture,
    tmp_path: Path,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path)
    alias = request_fixture.request.artifact_root / "activation" / "manifest-alias.json"
    _write(alias, executor.manifest_path.read_bytes())
    executor.manifest_path = alias

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        _run(request_fixture.request, executor)

    assert _error_code(exc_info) == "CONTENT_ADDRESS_PATH_MISMATCH"
    assert executor.modes == ["RecoverOnly"]


@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        (Scenario(apply_outcome="error"), "APPLY_REJECTED"),
        (Scenario(apply_outcome="timeout"), "STAGE_TIMEOUT"),
        (Scenario(publish_started=False), "STARTED_RECEIPT_UNAVAILABLE"),
        (Scenario(task_exists_after=False), "PAPER_TASK_UNAVAILABLE"),
    ],
)
def test_every_post_apply_failure_runs_exactly_one_exact_rollback(
    request_fixture: RequestFixture,
    tmp_path: Path,
    scenario: Scenario,
    expected_code: str,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path, scenario)

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        _run(request_fixture.request, executor, mode="ActivatePaper")

    assert _error_code(exc_info) == expected_code
    assert executor.modes == ["RecoverOnly", "ValidateOnly", "Apply", "Rollback"]
    assert executor.modes.count("Apply") == 1
    assert executor.modes.count("Rollback") == 1
    apply_argv = next(
        argv
        for argv, _timeout, _cwd, _env in executor.calls
        if "--mode" in argv and argv[argv.index("--mode") + 1] == "Apply"
    )
    rollback_argv = next(
        argv
        for argv, _timeout, _cwd, _env in executor.calls
        if "--mode" in argv and argv[argv.index("--mode") + 1] == "Rollback"
    )
    assert apply_argv[: apply_argv.index("--mode")] == rollback_argv[
        : rollback_argv.index("--mode")
    ]
    assert rollback_argv[rollback_argv.index("--mode") :] == (
        "--mode",
        "Rollback",
    )


def test_success_result_fsync_failure_remains_inside_compensated_apply_boundary(
    request_fixture: RequestFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path)
    original = runner._write_once

    def fail_success_receipt(path: Path, raw: bytes) -> None:
        if path.name == "result.json":
            raise OSError("synthetic result fsync failure")
        original(path, raw)

    monkeypatch.setattr(runner, "_write_once", fail_success_receipt)
    with pytest.raises(OSError, match="synthetic result fsync failure"):
        _run(request_fixture.request, executor, mode="ActivatePaper")

    assert executor.modes == ["RecoverOnly", "ValidateOnly", "Apply", "Rollback"]
    assert executor.modes.count("Rollback") == 1


@pytest.mark.parametrize("rollback_outcome", ["error", "timeout"])
def test_apply_and_rollback_failure_preserves_combined_failure(
    request_fixture: RequestFixture,
    tmp_path: Path,
    rollback_outcome: str,
) -> None:
    executor = FakeExecutor(
        request_fixture.request,
        tmp_path,
        Scenario(apply_outcome="error", rollback_outcome=rollback_outcome),
    )

    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        _run(request_fixture.request, executor, mode="ActivatePaper")

    assert _error_code(exc_info) == "APPLY_AND_ROLLBACK_FAILED"
    assert "primary=CapturedPaperActivationRunnerError" in str(exc_info.value)
    assert "rollback=CapturedPaperActivationRunnerError" in str(exc_info.value)
    assert executor.modes == ["RecoverOnly", "ValidateOnly", "Apply", "Rollback"]


def test_success_can_only_report_fake_money_alpaca_paper_started(
    request_fixture: RequestFixture,
    tmp_path: Path,
) -> None:
    executor = FakeExecutor(request_fixture.request, tmp_path)

    result = _run(request_fixture.request, executor, mode="ActivatePaper")

    assert result["verdict"] == "ACTIVATED_ALPACA_PAPER_ONLY"
    assert result["account_scope"] == "alpaca:paper"
    assert result["live_cash_authorized"] is False
    assert result["paper_started"] is True
    assert result["activation_generation"] == GENERATION
    assert executor.modes == ["RecoverOnly", "ValidateOnly", "Apply"]
    assert executor.task_queries == 2


def test_run_artifacts_are_append_only_and_isolated_per_run(
    request_fixture: RequestFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_ids = iter(
        (
            uuid.UUID("00000000-0000-4000-8000-000000000001"),
            uuid.UUID("00000000-0000-4000-8000-000000000002"),
        )
    )
    monkeypatch.setattr(runner.uuid, "uuid4", lambda: next(run_ids))
    first = FakeExecutor(request_fixture.request, tmp_path / "first")
    second = FakeExecutor(request_fixture.request, tmp_path / "second")

    _run(request_fixture.request, first)
    first_root = (
        request_fixture.request.artifact_root
        / "operator-runs"
        / "00000000-0000-4000-8000-000000000001"
    )
    before = {path.name: path.read_bytes() for path in first_root.iterdir()}
    _run(request_fixture.request, second)
    second_root = (
        request_fixture.request.artifact_root
        / "operator-runs"
        / "00000000-0000-4000-8000-000000000002"
    )

    assert first_root.is_dir()
    assert second_root.is_dir()
    assert first_root != second_root
    assert before == {path.name: path.read_bytes() for path in first_root.iterdir()}
    assert "result.json" in before
    assert (second_root / "result.json").is_file()
    for stage in (
        "recover-only",
        "chain",
        "no-order-smoke",
        "finalize",
        "validate-only",
    ):
        assert list(first_root.glob(f"{stage}.*.stdout"))
        assert list(second_root.glob(f"{stage}.*.stdout"))


def test_write_once_is_idempotent_only_for_identical_bytes(tmp_path: Path) -> None:
    path = tmp_path / "append-only.json"

    runner._write_once(path, b"first")
    runner._write_once(path, b"first")
    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner._write_once(path, b"second")

    assert _error_code(exc_info) == "APPEND_ONLY_CONFLICT"
    assert path.read_bytes() == b"first"


def test_real_git_cleanliness_allows_isolated_pycache_but_rejects_ignored_payload(
    tmp_path: Path,
) -> None:
    git_text = shutil.which("git.exe" if os.name == "nt" else "git")
    if not git_text:
        pytest.skip("Git is unavailable")
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run([git_text, "init", "-q", str(repository)], check=True)
    tracked = sorted(
        {
            *(path.as_posix() for path in runner._CANDIDATE_ENTRYPOINTS.values()),
            *(path.as_posix() for path in runner._LAUNCHER_SOURCE_PATHS.values()),
            "scripts/captured_paper_activation_runner.py",
            "scripts/captured_paper_runtime_env.py",
        }
    )
    for relative in tracked:
        _write(repository / relative, f"tracked:{relative}\n".encode())
    _write(repository / "app" / ".gitkeep", b"")
    (repository / ".gitignore").write_text(
        "/app/__pycache__/\n/app/__pycache__/**\n*.pyd\n", encoding="utf-8"
    )
    subprocess.run([git_text, "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        [
            git_text,
            "-C",
            str(repository),
            "-c",
            "user.name=CHILI Test",
            "-c",
            "user.email=chili-test@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        check=True,
    )
    head = subprocess.run(
        [git_text, "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    cache = repository / "app" / "__pycache__"
    cache.mkdir(parents=True)
    for index in range(2048):
        (cache / f"ignored_{index}.cpython-313.pyc").write_bytes(b"pyc")
    sandbox = tmp_path / "git-sandbox"
    sandbox.mkdir()
    git_env = runner._minimal_git_environment(sandbox=sandbox)
    request = SimpleNamespace(
        git_executable=Path(git_text).resolve(strict=True),
        candidate_root=repository.resolve(strict=True),
        expected_git_commit=head,
        timeouts=runner.RunnerTimeouts(30, 30, 30, 30, 30, 30, 30),
    )

    runner._verify_repo(request, runner.SubprocessExecutor(), git_env)

    (repository / "ignored_payload.pyd").write_bytes(b"payload")
    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner._verify_repo(request, runner.SubprocessExecutor(), git_env)
    assert _error_code(exc_info) == "GIT_IGNORED_EXECUTABLE_PAYLOAD"


def _pid_is_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == 259
    finally:
        kernel32.CloseHandle(handle)


def test_subprocess_timeout_kills_exact_owned_child_and_grandchild_tree(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "owned-tree-pids.json"
    child_code = (
        "import json,os,pathlib,subprocess,sys,time;"
        "grand=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"pathlib.Path({str(pid_path)!r}).write_text(json.dumps([os.getpid(),grand.pid]));"
        "time.sleep(60)"
    )
    with pytest.raises(runner.CapturedPaperActivationRunnerError) as exc_info:
        runner.SubprocessExecutor().run(
            [sys.executable, "-c", child_code],
            timeout=2,
            cwd=tmp_path,
            env=dict(os.environ),
        )
    assert _error_code(exc_info) == "STAGE_TIMEOUT"
    assert pid_path.is_file()
    pids = json.loads(pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while any(_pid_is_alive(int(pid)) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert all(not _pid_is_alive(int(pid)) for pid in pids)


def test_main_sanitizes_unexpected_activation_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "paper-api-secret-must-not-escape"

    def _explode(**_kwargs: object) -> runner.ActivationRunnerRequest:
        raise RuntimeError(secret)

    monkeypatch.setattr(runner, "load_activation_runner_request", _explode)

    rc = runner.main(
        [
            "--request",
            r"D:\protected\activation-request.json",
            "--request-sha256",
            "a" * 64,
            "--mode",
            "ValidateOnly",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert secret not in captured.err
    assert json.loads(captured.err) == {
        "schema_version": runner.RESULT_SCHEMA_VERSION,
        "verdict": "REJECTED",
        "reason_code": "UNEXPECTED_ACTIVATION_FAILURE",
        "account_scope": runner.ACCOUNT_SCOPE,
        "live_cash_authorized": False,
    }
