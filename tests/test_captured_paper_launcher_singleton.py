from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable

import pytest

from scripts import captured_paper_activation_contract as contract


LAUNCHER = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "start-captured-alpaca-paper.ps1"
)
REPO_ROOT = LAUNCHER.parents[1]
SERVICE = REPO_ROOT / "scripts" / "captured_alpaca_paper_service.py"
STAGE0 = REPO_ROOT / "scripts" / "captured_paper_isolated_stage0.py"
TEST_GENERATION = "2a64e285-a7f0-47cf-8312-7627304434e8"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _publish_content_addressed(root: Path, value: Any) -> tuple[Path, str]:
    raw = _canonical(value)
    digest = hashlib.sha256(raw).hexdigest()
    path = root / digest[:2] / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path, digest


def _argument_contract(
    *,
    tmp_path: Path,
    python_stub: Path,
    no_order_receipt_path: Path,
    allowed_roots: tuple[Path, ...],
) -> dict[str, Any]:
    launcher_sha = _sha_file(LAUNCHER)
    service_sha = _sha_file(SERVICE)
    stage0_sha = _sha_file(STAGE0)
    python_sha = _sha_file(python_stub)
    dependency_root = tmp_path / "site-packages"
    dependency_root.mkdir(exist_ok=True)
    dependency_identity_sha = contract.python_dependency_root_identity_sha256(
        dependency_root=dependency_root,
        python_executable=python_stub,
        python_executable_sha256=python_sha,
    )
    generation_root = tmp_path / "staged" / TEST_GENERATION
    staged_launcher = generation_root / launcher_sha / f"{launcher_sha}.ps1"
    staged_launcher.parent.mkdir(parents=True, exist_ok=True)
    staged_launcher.write_bytes(LAUNCHER.read_bytes())
    staged_service = generation_root / service_sha / f"{service_sha}.py"
    staged_service.parent.mkdir(parents=True, exist_ok=True)
    staged_service.write_bytes(SERVICE.read_bytes())
    staged_stage0 = generation_root / stage0_sha / f"{stage0_sha}.py"
    staged_stage0.parent.mkdir(parents=True, exist_ok=True)
    staged_stage0.write_bytes(STAGE0.read_bytes())
    host_ready = generation_root / "handshake" / "host-ready.json"
    host_ready.parent.mkdir(parents=True, exist_ok=True)
    invocations: dict[str, Any] = {}
    for mode in ("ActivatePaper", "NoOrderSmoke", "ValidateOnly"):
        projection = contract.launcher_invocation_projection(
            mode=mode,
            candidate_root=REPO_ROOT,
            python_executable=python_stub,
            python_executable_sha256=python_sha,
            python_dependency_root=dependency_root,
            python_dependency_root_identity_sha256=dependency_identity_sha,
            allowed_read_roots=allowed_roots,
            launcher_path=LAUNCHER,
            launcher_sha256=launcher_sha,
            stage0_path=STAGE0,
            stage0_sha256=stage0_sha,
            service_path=SERVICE,
            service_sha256=service_sha,
            launcher_staged_path=staged_launcher,
            stage0_staged_path=staged_stage0,
            service_staged_path=staged_service,
            host_ready_receipt=(host_ready if mode == "ActivatePaper" else None),
            no_order_receipt_output=(
                no_order_receipt_path if mode == "NoOrderSmoke" else None
            ),
        )
        invocations[mode] = {
            "projection": dict(projection),
            "projection_sha256": contract.sha256_json(projection),
        }
    return {
        "schema_version": contract.LAUNCHER_ARGUMENT_CONTRACT_SCHEMA_VERSION,
        "invocations": invocations,
    }


def _invoke_launcher(
    tmp_path: Path,
    *,
    mode: str,
    no_order_receipt_path: Path | str | None,
    planned_no_order_receipt_path: Path | None = None,
    allowed_root_order: tuple[Path, ...] | None = None,
    mutate_contract: Callable[[dict[str, Any]], None] | None = None,
    mutate_python_after_binding: bool = False,
    caller_cwd: Path | None = None,
    manifest_alias: bool = False,
    mutate_manifest_after_hash: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    captured = tmp_path / "captured-launch.json"
    python_stub = tmp_path / "python-stub.ps1"
    python_stub.write_text(
        "$payload = [ordered]@{arguments = @($args); cwd = (Get-Location).Path}\n"
        "[IO.File]::WriteAllText($env:CHILI_TEST_CAPTURED_ARGS, "
        "($payload | ConvertTo-Json -Compress -Depth 5))\n"
        "exit 0\n",
        encoding="utf-8",
    )
    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(exist_ok=True)
    planned_receipt = planned_no_order_receipt_path or receipt_parent / "no-order.json"
    roots = (REPO_ROOT, tmp_path)
    launch_roots = allowed_root_order or roots
    document = _argument_contract(
        tmp_path=tmp_path,
        python_stub=python_stub,
        no_order_receipt_path=planned_receipt,
        allowed_roots=roots,
    )
    if mutate_contract is not None:
        mutate_contract(document)
    selected_projection = document["invocations"][mode]["projection"]
    activate_projection = document["invocations"]["ActivatePaper"]["projection"]
    staged_launcher = Path(selected_projection["launcher_path"])
    staged_service = Path(selected_projection["service_staged_path"])
    staged_stage0 = Path(selected_projection["stage0_path"])
    artifact_root = staged_launcher.parent.parent.parent
    argument_raw = _canonical(document)
    argument_path = tmp_path / "launcher-arguments.json"
    argument_path.write_bytes(argument_raw)
    argument_sha = hashlib.sha256(argument_raw).hexdigest()

    schema = (
        contract.PREACTIVATION_MANIFEST_SCHEMA_VERSION
        if mode == "NoOrderSmoke"
        else contract.ACTIVATION_MANIFEST_SCHEMA_VERSION
    )
    manifest_document = {
        "schema_version": schema,
        "activation_generation": TEST_GENERATION,
        "cutover": {
            "activation_artifact_root": str(artifact_root),
            "candidate_root": str(REPO_ROOT),
            "host_ready_receipt_base": activate_projection[
                "host_ready_receipt_base"
            ],
            "launcher_source_path": str(LAUNCHER),
            "launcher_source_sha256": _sha_file(LAUNCHER),
            "launcher_path": str(staged_launcher),
            "launcher_sha256": _sha_file(staged_launcher),
            "launcher_arguments_path": str(argument_path),
            "launcher_arguments_sha256": argument_sha,
            "python_executable_path": selected_projection["python_executable_path"],
            "python_executable_sha256": selected_projection[
                "python_executable_sha256"
            ],
            "python_dependency_root": selected_projection["python_dependency_root"],
            "python_dependency_root_identity_sha256": selected_projection[
                "python_dependency_root_identity_sha256"
            ],
            "python_import_root": str(REPO_ROOT),
            "scheduled_tasks": sorted(contract._REQUIRED_TASKS),
            "service_source_path": str(SERVICE),
            "service_source_sha256": _sha_file(SERVICE),
            "service_path": str(staged_service),
            "service_sha256": _sha_file(staged_service),
            "stage0_source_path": str(STAGE0),
            "stage0_source_sha256": _sha_file(STAGE0),
            "stage0_path": str(staged_stage0),
            "stage0_sha256": _sha_file(staged_stage0),
            "singleton_policy": "one_unified_candidate_host",
            "rollback_required": True,
        },
    }
    manifest, manifest_sha = _publish_content_addressed(
        tmp_path / "manifests", manifest_document
    )
    if manifest_alias:
        alias = tmp_path / "manifest-alias.json"
        alias.write_bytes(manifest.read_bytes())
        manifest = alias
    if mutate_manifest_after_hash:
        manifest.write_bytes(manifest.read_bytes() + b"\n")
    if mutate_python_after_binding:
        python_stub.write_text("throw 'drifted test interpreter'\n", encoding="utf-8")

    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(staged_launcher),
        "-Mode",
        mode,
        "-PythonExecutable",
        str(python_stub),
        "-CandidateRoot",
        str(REPO_ROOT),
        "-ServiceScriptPath",
        str(staged_service),
        "-Stage0ScriptPath",
        str(staged_stage0),
        "-ManifestPath",
        str(manifest),
        "-ManifestSha256",
        manifest_sha,
        "-AllowedReadRootsBase64",
        base64.b64encode(
            json.dumps(
                [str(root) for root in launch_roots], separators=(",", ":")
            ).encode("utf-8")
        ).decode("ascii"),
    ]
    if no_order_receipt_path is not None:
        command.extend(["-NoOrderReceiptPath", str(no_order_receipt_path)])
    env = dict(os.environ)
    env["CHILI_TEST_CAPTURED_ARGS"] = str(captured)
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=caller_cwd,
        timeout=30,
    )
    payload = None
    if captured.exists():
        payload = json.loads(captured.read_text(encoding="utf-8-sig"))
    return result, payload


def test_launcher_uses_one_stable_cross_session_paper_mutex() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "$mutexName = 'Global\\CHILI-Captured-Alpaca-PAPER-SINGLETON'" in source
    mutex_assignment = next(
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("$mutexName =")
    )
    assert "ManifestSha256" not in mutex_assignment
    assert source.count("[Threading.Mutex]::new") == 1
    assert "$mutex.WaitOne(0)" in source
    assert "$mutex.ReleaseMutex()" in source


def test_launcher_remains_foreground_and_anchors_candidate_working_directory(
    tmp_path: Path,
) -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    caller = tmp_path / "unrelated-caller-cwd"
    caller.mkdir()

    result, payload = _invoke_launcher(
        tmp_path,
        mode="ValidateOnly",
        no_order_receipt_path=None,
        caller_cwd=caller,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert payload is not None
    assert Path(payload["cwd"]).resolve() == REPO_ROOT.resolve()
    assert "Start-Process" not in source
    assert "& $python @arguments" in source
    assert "Push-Location -LiteralPath $candidate" in source
    assert "$LASTEXITCODE" in source


def test_no_order_smoke_requires_and_forwards_one_strict_bound_output(
    tmp_path: Path,
) -> None:
    missing, missing_payload = _invoke_launcher(
        tmp_path,
        mode="NoOrderSmoke",
        no_order_receipt_path=None,
    )
    assert missing.returncode != 0
    assert "NoOrderReceiptPath is required" in (missing.stdout + missing.stderr)
    assert missing_payload is None

    receipt = tmp_path / "receipts" / "no-order.json"
    accepted, payload = _invoke_launcher(
        tmp_path,
        mode="NoOrderSmoke",
        no_order_receipt_path=receipt,
        planned_no_order_receipt_path=receipt,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert payload is not None
    forwarded = payload["arguments"]
    flag_index = forwarded.index("--no-order-receipt-output")
    assert Path(forwarded[flag_index + 1]) == receipt
    assert forwarded.count("--no-order-receipt-output") == 1


@pytest.mark.parametrize("mode", ["ValidateOnly", "ActivatePaper"])
def test_non_smoke_modes_reject_no_order_receipt_path(
    tmp_path: Path,
    mode: str,
) -> None:
    result, payload = _invoke_launcher(
        tmp_path,
        mode=mode,
        no_order_receipt_path=tmp_path / "not-allowed.json",
    )

    assert result.returncode != 0
    assert "accepted only for NoOrderSmoke" in (result.stdout + result.stderr)
    assert payload is None


def test_no_order_output_parent_must_exist_and_be_local(tmp_path: Path) -> None:
    result, payload = _invoke_launcher(
        tmp_path,
        mode="NoOrderSmoke",
        no_order_receipt_path=tmp_path / "missing-parent" / "no-order.json",
    )

    assert result.returncode != 0
    assert payload is None


def test_bound_python_bytes_and_no_order_path_drift_fail_before_child(
    tmp_path: Path,
) -> None:
    python_drift, python_payload = _invoke_launcher(
        tmp_path / "python-drift",
        mode="ValidateOnly",
        no_order_receipt_path=None,
        mutate_python_after_binding=True,
    )
    assert python_drift.returncode != 0
    assert "PAPER manifest cutover binding differs" in (
        python_drift.stdout + python_drift.stderr
    )
    assert python_payload is None

    path_root = tmp_path / "path-drift"
    path_root.mkdir()
    actual = path_root / "receipts" / "actual.json"
    actual.parent.mkdir()
    expected = actual.parent / "expected.json"
    path_drift, path_payload = _invoke_launcher(
        path_root,
        mode="NoOrderSmoke",
        no_order_receipt_path=actual,
        planned_no_order_receipt_path=expected,
    )
    assert path_drift.returncode != 0
    assert "sealed projection" in (path_drift.stdout + path_drift.stderr)
    assert path_payload is None


def test_allowed_root_input_order_is_canonical_but_contract_order_drift_fails(
    tmp_path: Path,
) -> None:
    accepted, payload = _invoke_launcher(
        tmp_path / "accepted",
        mode="ValidateOnly",
        no_order_receipt_path=None,
        allowed_root_order=(tmp_path / "accepted", REPO_ROOT),
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert payload is not None
    forwarded = payload["arguments"]
    forwarded_roots = [
        forwarded[index + 1]
        for index, value in enumerate(forwarded)
        if value == "--allow-read-root"
    ]
    assert [value.casefold() for value in forwarded_roots] == sorted(
        value.casefold() for value in forwarded_roots
    )

    def reverse_declared_roots(document: dict[str, Any]) -> None:
        entry = document["invocations"]["ValidateOnly"]
        entry["projection"]["allowed_read_roots"].reverse()
        entry["projection_sha256"] = contract.sha256_json(entry["projection"])

    drifted, drifted_payload = _invoke_launcher(
        tmp_path / "drifted",
        mode="ValidateOnly",
        no_order_receipt_path=None,
        mutate_contract=reverse_declared_roots,
    )
    assert drifted.returncode != 0
    assert "sealed projection" in (drifted.stdout + drifted.stderr)
    assert drifted_payload is None


def test_manifest_alias_is_rejected_even_when_bytes_and_sha_match(
    tmp_path: Path,
) -> None:
    result, payload = _invoke_launcher(
        tmp_path,
        mode="ValidateOnly",
        no_order_receipt_path=None,
        manifest_alias=True,
    )

    assert result.returncode != 0
    assert "content-addressed path" in (result.stdout + result.stderr)
    assert payload is None


def test_manifest_sha_drift_is_rejected_before_child(tmp_path: Path) -> None:
    result, payload = _invoke_launcher(
        tmp_path,
        mode="ValidateOnly",
        no_order_receipt_path=None,
        mutate_manifest_after_hash=True,
    )

    assert result.returncode != 0
    assert "manifest hash does not match" in (result.stdout + result.stderr)
    assert payload is None
