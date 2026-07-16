from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from scripts import captured_paper_host_cutover as cutover
from scripts import collect_captured_paper_host_snapshot as collector


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _task_xml(*, command: str, arguments: str, enabled: bool = True) -> bytes:
    state = "true" if enabled else "false"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Task version="1.4" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        f"<Settings><Enabled>{state}</Enabled></Settings>"
        "<Actions><Exec>"
        f"<Command>{escape(command)}</Command>"
        f"<Arguments>{escape(arguments)}</Arguments>"
        "</Exec></Actions></Task>"
    ).encode("utf-8")


def test_schtasks_ascii_utf16_declaration_is_reencoded_without_guessing() -> None:
    observed = _task_xml(command=r"C:\Windows\wscript.exe", arguments="x.vbs")
    observed = observed.replace(b'encoding="UTF-8"', b'encoding="UTF-16"')

    normalized = collector._normalize_schtasks_xml_output(observed)

    assert normalized.startswith(b"\xff\xfe")
    assert cutover._task_enabled_from_xml(normalized) is True
    assert collector._normalize_schtasks_xml_output(normalized) == normalized


def test_schtasks_non_ascii_misdeclared_xml_fails_closed() -> None:
    observed = (
        b'<?xml version="1.0" encoding="UTF-16"?><Task>' + b"\xe9" + b"</Task>"
    )

    with pytest.raises(
        collector.CapturedPaperHostSnapshotError,
        match="encoding differs",
    ):
        collector._normalize_schtasks_xml_output(observed)


def _identity(
    *, pid: int, role: str, executable: Path, script: Path
) -> cutover.ProcessIdentity:
    cmdline = (str(executable.resolve()), str(script.resolve()))
    return cutover.ProcessIdentity(
        pid=pid,
        create_time_ns=1_700_000_000_000_000_000 + pid,
        executable_path=cmdline[0],
        executable_sha256=cutover.sha256_bytes(executable.read_bytes()),
        cmdline=cmdline,
        cmdline_sha256=cutover.sha256_json(list(cmdline)),
        role=role,
        bridge_script_path=cmdline[1],
        bridge_script_sha256=cutover.sha256_bytes(script.read_bytes()),
    )


def _approved_starter_source(*, role: str, executable: Path, script: Path) -> str:
    basename = script.name
    common = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        "if (-not (Get-Process iqconnect -ErrorAction SilentlyContinue)) {",
        "    Start-Process -FilePath 'E:\\DTN\\IQFeed\\iqconnect.exe' -WorkingDirectory 'E:\\DTN\\IQFeed'",
        "    Start-Sleep -Seconds 20",
        "}",
        '$existing = Get-CimInstance Win32_Process -Filter "Name = \'python.exe\'" |',
        f"    Where-Object {{ $_.CommandLine -like '*{basename}*' }}",
        "if ($existing) { exit 0 }",
    ]
    if role == "iqfeed_depth_bridge":
        tail = [
            "$log = 'D:\\CHILI-Docker\\chili-data\\iqfeed_depth\\bridge.log'",
            "$err = 'D:\\CHILI-Docker\\chili-data\\iqfeed_depth\\bridge.err.log'",
        ]
    else:
        tail = [
            "$dir = 'D:\\CHILI-Docker\\chili-data\\iqfeed_trades'",
            "if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }",
            "$log = Join-Path $dir 'bridge.log'",
            "$err = Join-Path $dir 'bridge.err.log'",
        ]
    tail.extend(
        [
            f"Start-Process -FilePath '{executable.resolve()}' `",
            f"    -ArgumentList '{script.resolve()}' `",
            "    -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err",
        ]
    )
    return "\n".join([*common, *tail, ""])


class FakeReadOnlyProbe:
    def __init__(
        self,
        *,
        tasks: Mapping[str, cutover.TaskObservation],
        processes: Sequence[cutover.ProcessIdentity],
    ) -> None:
        self.tasks = dict(tasks)
        self.processes = tuple(processes)
        self.calls: list[str] = []

    def get_task(self, name: str) -> cutover.TaskObservation | None:
        self.calls.append(f"read-task:{name}")
        return self.tasks.get(name)

    def find_bridge_processes(
        self, *, legacy_root: Path
    ) -> tuple[cutover.ProcessIdentity, ...]:
        self.calls.append(f"read-processes:{legacy_root}")
        return self.processes


def _direct_fixture(tmp_path: Path):
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"python")
    trade_script = tmp_path / "iqfeed_trade_bridge.py"
    trade_script.write_bytes(b"# trade")
    depth_script = tmp_path / "iqfeed_depth_bridge.py"
    depth_script.write_bytes(b"# depth")
    trade = _identity(
        pid=101,
        role="iqfeed_trade_bridge",
        executable=executable,
        script=trade_script,
    )
    depth = _identity(
        pid=102,
        role="iqfeed_depth_bridge",
        executable=executable,
        script=depth_script,
    )
    tasks: dict[str, cutover.TaskObservation] = {}
    for name in cutover.REQUIRED_LEGACY_TASKS:
        process = depth if "-Depth-" in name else trade
        raw = _task_xml(
            command=process.executable_path,
            arguments=cutover._quote_windows_arguments(process.cmdline[1:]),
        )
        tasks[name] = cutover.TaskObservation(name=name, xml=raw, enabled=True)
    return tasks, (depth, trade)


NATIVE_WSCRIPT = str(cutover._native_system32_executable("wscript.exe"))
NATIVE_POWERSHELL = str(cutover._native_system32_executable("powershell.exe"))


def _wrapper_fixture(
    tmp_path: Path,
    *,
    wrapper_token: str | None = None,
    powershell_token: str = NATIVE_POWERSHELL,
    command: str = NATIVE_WSCRIPT,
    extra_argv: tuple[str, ...] = (),
    starter_suffix: str = "",
):
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"python")
    wrapper = tmp_path / "run-hidden.vbs"
    wrapper.write_bytes(
        (Path(cutover.__file__).resolve().parent / "run-hidden.vbs").read_bytes()
    )
    role_files = {
        "iqfeed_depth_bridge": tmp_path / "iqfeed_depth_bridge.py",
        "iqfeed_trade_bridge": tmp_path / "iqfeed_trade_bridge.py",
    }
    for path in role_files.values():
        path.write_bytes(f"# {path.stem}".encode())
    processes = tuple(
        _identity(
            pid=200 + index,
            role=role,
            executable=executable,
            script=script,
        )
        for index, (role, script) in enumerate(sorted(role_files.items()))
    )
    starters: dict[str, Path] = {}
    tasks: dict[str, cutover.TaskObservation] = {}
    parsed_arguments: dict[str, tuple[str, ...]] = {}
    for name in cutover.REQUIRED_LEGACY_TASKS:
        role = "iqfeed_depth_bridge" if "-Depth-" in name else "iqfeed_trade_bridge"
        starter = starters.get(role)
        if starter is None:
            starter = tmp_path / f"start-{role}.ps1"
            starter.write_text(
                _approved_starter_source(
                    role=role, executable=executable, script=role_files[role]
                )
                + starter_suffix,
                encoding="utf-8",
            )
            starters[role] = starter
        argv = (
            wrapper_token or str(wrapper),
            powershell_token,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(starter),
            *extra_argv,
        )
        arguments = cutover._quote_windows_arguments(argv)
        parsed_arguments[arguments] = argv
        raw = _task_xml(command=command, arguments=arguments)
        tasks[name] = cutover.TaskObservation(name=name, xml=raw, enabled=True)
    return tasks, processes, parsed_arguments, wrapper, starters


def test_direct_identity_collection_is_content_addressed_and_read_only(
    tmp_path: Path,
) -> None:
    tasks, processes = _direct_fixture(tmp_path)
    probe = FakeReadOnlyProbe(tasks=tasks, processes=processes)
    collection = collector.collect_host_snapshot(
        probe=probe,
        legacy_root=tmp_path,
        captured_at=NOW,
        argv_parser=lambda value: (value,),
    )

    assert collection.verdict == "VALIDATED"
    assert collection.reason_code == "CURRENT_DIRECT_IDENTITY_CONTRACT_SATISFIED"
    assert collection.task_snapshot_document["schema_version"] == cutover.TASK_SNAPSHOT_SCHEMA
    assert (
        collection.process_snapshot_document["schema_version"]
        == cutover.PROCESS_SNAPSHOT_SCHEMA
    )
    assert collection.restore_plan_document["schema_version"] == cutover.RESTORE_PLAN_SCHEMA
    assert probe.calls == [
        *(f"read-task:{name}" for name in cutover.REQUIRED_LEGACY_TASKS),
        f"read-processes:{tmp_path.resolve()}",
        *(f"read-task:{name}" for name in cutover.REQUIRED_LEGACY_TASKS),
        f"read-processes:{tmp_path.resolve()}",
    ]

    output = tmp_path / "outputs"
    output.mkdir()
    persisted = collector.persist_host_snapshot(collection, output_root=output)
    assert persisted.verdict == "VALIDATED"
    assert persisted.artifact_directory.name == persisted.manifest_sha256
    assert persisted.manifest_path.name == f"{persisted.manifest_sha256}.manifest.json"
    manifest_raw = persisted.manifest_path.read_bytes()
    assert cutover.sha256_bytes(manifest_raw) == persisted.manifest_sha256
    manifest = json.loads(manifest_raw)
    assert manifest["host_mutation_count"] == 0
    assert manifest["task_or_process_mutation_authorized"] is False
    assert manifest["provider_access_performed"] is False
    assert manifest["broker_access_performed"] is False
    assert manifest["database_access_performed"] is False
    assert manifest["live_cash_authorized"] is False
    for role, path in persisted.artifact_paths.items():
        assert cutover.sha256_bytes(path.read_bytes()) == persisted.artifact_sha256s[role]
        assert json.loads(path.read_bytes())

    replay = collector.persist_host_snapshot(collection, output_root=output)
    assert replay.manifest_sha256 == persisted.manifest_sha256
    assert replay.artifact_paths == persisted.artifact_paths


def test_wrapper_chain_becomes_typed_restore_authority_without_claiming_provenance(
    tmp_path: Path,
) -> None:
    tasks, processes, parsed_arguments, wrapper, _starters = _wrapper_fixture(tmp_path)

    collection = collector.collect_host_snapshot(
        probe=FakeReadOnlyProbe(tasks=tasks, processes=processes),
        legacy_root=tmp_path,
        captured_at=NOW,
        argv_parser=lambda value: parsed_arguments[value],
    )

    assert collection.verdict == "VALIDATED"
    assert collection.reason_code == "WRAPPER_RESTORE_AUTHORITY_CONTRACT_SATISFIED"
    contracts = collection.restore_plan_document["legacy_task_launch_contracts"]
    assert set(contracts) == set(cutover.REQUIRED_LEGACY_TASKS)
    for value in contracts.values():
        assert value["launch_kind"] == cutover.LEGACY_WRAPPER_LAUNCH_KIND
        assert value["contract_sha256"] == cutover.sha256_json(
            {key: item for key, item in value.items() if key != "contract_sha256"}
        )
    restore_raw = cutover._canonical_json_bytes(dict(collection.restore_plan_document))
    restore_path = tmp_path / "wrapper-restore-plan-v3.json"
    restore_path.write_bytes(restore_raw)
    parsed_restore = cutover._parse_restore_plan(
        path=restore_path,
        raw=restore_raw,
        digest=cutover.sha256_bytes(restore_raw),
        roots=(tmp_path.resolve(),),
    )
    cutover._assert_snapshot_plan_consistency(
        cutover.TaskSnapshot(
            captured_at=NOW,
            tasks=tasks,
            artifact_path=tmp_path / "task.json",
            artifact_sha256="1" * 64,
        ),
        cutover.ProcessSnapshot(
            captured_at=NOW,
            processes=processes,
            artifact_path=tmp_path / "process.json",
            artifact_sha256="2" * 64,
        ),
        parsed_restore,
    )
    evidence = collection.wrapper_chain_document
    assert evidence["diagnostic_only"] is True
    assert evidence["authority_granted"] is False
    for row in evidence["tasks"].values():
        assert row["wrapper_target_matches_running_process"] is True
        assert row["direct_task_process_identity"] is False
        assert row["unresolved_steps"] == [
            "starter_projection_not_execution_proof",
            "vbs_forwarding_semantics_not_authoritative",
        ]
        assert row["vbs_wrapper"]["sha256"] == cutover.sha256_bytes(wrapper.read_bytes())
        assert row["powershell"]["sha256"]
        assert row["expected_bridge"]["status"] == "STATIC_PROJECTION"

    output = tmp_path / "wrapper-output"
    output.mkdir()
    persisted = collector.persist_host_snapshot(collection, output_root=output)
    manifest = json.loads(persisted.manifest_path.read_bytes())
    assert manifest["verdict"] == "VALIDATED"
    assert manifest["reason_code"] == "WRAPPER_RESTORE_AUTHORITY_CONTRACT_SATISFIED"
    assert manifest["paper_order_submission_authorized"] is False


@pytest.mark.parametrize(
    ("fixture_kwargs", "reason"),
    [
        ({"extra_argv": ("--unexpected",)}, "extra or unsupported token"),
        ({"wrapper_token": r"relative\\run-hidden.vbs"}, "absolute local-drive"),
        ({"wrapper_token": r"\\server\share\run-hidden.vbs"}, "absolute local-drive"),
        ({"wrapper_token": r"D:\\dev\\run-hidden.vbs:authority"}, "absolute local-drive"),
    ],
)
def test_wrapper_authority_rejects_extra_argv_and_nonlocal_or_alternate_paths(
    tmp_path: Path, fixture_kwargs: Mapping[str, object], reason: str
) -> None:
    tasks, processes, parsed, _wrapper, _starters = _wrapper_fixture(
        tmp_path, **fixture_kwargs
    )
    with pytest.raises(
        collector.CapturedPaperHostSnapshotError, match=reason
    ):
        collector.collect_host_snapshot(
            probe=FakeReadOnlyProbe(tasks=tasks, processes=processes),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: parsed[value],
        )


@pytest.mark.parametrize(
    "fixture_kwargs",
    [
        {"command": "wscript.exe"},
        {"powershell_token": "powershell.exe"},
    ],
)
def test_wrapper_contract_requires_absolute_native_wscript_and_powershell(
    tmp_path: Path, fixture_kwargs: Mapping[str, object]
) -> None:
    # A bare token is resolved through current-directory/PATH search at
    # launch time, so the sealed native hash would not be runtime-bound.
    tasks, processes, parsed, _wrapper, _starters = _wrapper_fixture(
        tmp_path, **fixture_kwargs
    )
    with pytest.raises(
        collector.CapturedPaperHostSnapshotError,
        match="exact absolute native",
    ):
        collector.collect_host_snapshot(
            probe=FakeReadOnlyProbe(tasks=tasks, processes=processes),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: parsed[value],
        )


def test_wrapper_contract_rejects_shadow_system32_paths(tmp_path: Path) -> None:
    # An existing copy of the binary at any other absolute path must be
    # rejected by identity, not merely by nonexistence.
    shadow_dir = tmp_path / "System32"
    shadow_dir.mkdir()
    shadow = shadow_dir / "wscript.exe"
    shadow.write_bytes(b"shadow wscript")
    tasks, processes, parsed, _wrapper, _starters = _wrapper_fixture(
        tmp_path, command=str(shadow)
    )
    with pytest.raises(
        collector.CapturedPaperHostSnapshotError,
        match="exact absolute native",
    ):
        collector.collect_host_snapshot(
            probe=FakeReadOnlyProbe(tasks=tasks, processes=processes),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: parsed[value],
        )


def test_probe_ignores_forged_systemroot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged = tmp_path / "ForgedRoot"
    (forged / "System32").mkdir(parents=True)
    (forged / "System32" / "schTasks.exe").write_bytes(b"forged schtasks")
    monkeypatch.setenv("SystemRoot", str(forged))

    probe = collector.WindowsReadOnlyHostProbe()

    resolved = Path(probe._schtasks)
    assert os.path.normcase(str(resolved)) == os.path.normcase(
        str(cutover._native_system32_directory() / "schtasks.exe")
    )
    assert os.path.normcase(str(forged)) not in os.path.normcase(str(resolved))


def test_backend_ignores_forged_systemroot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged = tmp_path / "ForgedRoot"
    (forged / "System32").mkdir(parents=True)
    (forged / "System32" / "schTasks.exe").write_bytes(b"forged schtasks")
    monkeypatch.setenv("SystemRoot", str(forged))

    backend = cutover.WindowsHostCutoverBackend(bindings=())

    resolved = Path(backend._schtasks)
    assert os.path.normcase(str(resolved)) == os.path.normcase(
        str(cutover._native_system32_directory() / "schtasks.exe")
    )
    assert os.path.normcase(str(forged)) not in os.path.normcase(str(resolved))


def test_wrapper_authority_rejects_unapproved_starter_semantics(tmp_path: Path) -> None:
    tasks, processes, parsed, _wrapper, _starters = _wrapper_fixture(
        tmp_path,
        starter_suffix="Start-Process -FilePath 'C:\\malicious.exe'\n",
    )
    with pytest.raises(
        collector.CapturedPaperHostSnapshotError,
        match="LEGACY_STARTER_SEMANTICS_INVALID",
    ):
        collector.collect_host_snapshot(
            probe=FakeReadOnlyProbe(tasks=tasks, processes=processes),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: parsed[value],
        )


def test_wrapper_authority_rejects_single_quoted_executable_line(
    tmp_path: Path,
) -> None:
    # A leading apostrophe is a string literal in PowerShell, not a comment;
    # piped into Invoke-Expression it executes. It must never be stripped
    # from the semantic profile.
    tasks, processes, parsed, _wrapper, _starters = _wrapper_fixture(
        tmp_path,
        starter_suffix="'Start-Process -FilePath C:\\malicious.exe' | Invoke-Expression\n",
    )
    with pytest.raises(
        collector.CapturedPaperHostSnapshotError,
        match="LEGACY_STARTER_SEMANTICS_INVALID",
    ):
        collector.collect_host_snapshot(
            probe=FakeReadOnlyProbe(tasks=tasks, processes=processes),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: parsed[value],
        )


def test_wrapper_authority_rejects_requires_module_directive(
    tmp_path: Path,
) -> None:
    # `#Requires` is an executable PowerShell engine directive (e.g. module
    # auto-load), not a comment; ignoring it would admit unapproved code.
    tasks, processes, parsed, _wrapper, _starters = _wrapper_fixture(
        tmp_path,
        starter_suffix="#Requires -Modules MaliciousModule\n",
    )
    with pytest.raises(
        collector.CapturedPaperHostSnapshotError,
        match="LEGACY_SOURCE_SEMANTICS_INVALID",
    ):
        collector.collect_host_snapshot(
            probe=FakeReadOnlyProbe(tasks=tasks, processes=processes),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: parsed[value],
        )


def test_wrapper_authority_rejects_daily_logon_semantic_divergence(
    tmp_path: Path,
) -> None:
    tasks, processes, parsed, wrapper, starters = _wrapper_fixture(tmp_path)
    name = "CHILI-IQFeed-Trade-Bridge-Logon"
    alternate = tmp_path / "alternate-trade-starter.ps1"
    alternate.write_bytes(starters["iqfeed_trade_bridge"].read_bytes())
    argv = (
        str(wrapper),
        NATIVE_POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(alternate),
    )
    arguments = cutover._quote_windows_arguments(argv)
    parsed[arguments] = argv
    tasks[name] = cutover.TaskObservation(
        name=name,
        xml=_task_xml(command=NATIVE_WSCRIPT, arguments=arguments),
        enabled=True,
    )
    with pytest.raises(
        collector.CapturedPaperHostSnapshotError,
        match="LEGACY_LAUNCH_PAIR_MISMATCH",
    ):
        collector.collect_host_snapshot(
            probe=FakeReadOnlyProbe(tasks=tasks, processes=processes),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: parsed[value],
        )


def test_wrapper_authority_rejects_reparse_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks, processes, parsed, wrapper, _starters = _wrapper_fixture(tmp_path)
    original = cutover._reject_reparse_chain

    def reject_wrapper(path: Path) -> None:
        if os.path.normcase(str(path)) == os.path.normcase(str(wrapper.absolute())):
            raise cutover.CapturedPaperHostCutoverError(
                "REPARSE_PATH", "synthetic wrapper reparse"
            )
        original(path)

    monkeypatch.setattr(cutover, "_reject_reparse_chain", reject_wrapper)
    with pytest.raises(
        collector.CapturedPaperHostSnapshotError, match="REPARSE_PATH"
    ):
        collector.collect_host_snapshot(
            probe=FakeReadOnlyProbe(tasks=tasks, processes=processes),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: parsed[value],
        )


def test_wrapper_contract_sources_are_revalidated_after_collection(
    tmp_path: Path,
) -> None:
    tasks, processes, parsed, _wrapper, starters = _wrapper_fixture(tmp_path)
    collection = collector.collect_host_snapshot(
        probe=FakeReadOnlyProbe(tasks=tasks, processes=processes),
        legacy_root=tmp_path,
        captured_at=NOW,
        argv_parser=lambda value: parsed[value],
    )
    starters["iqfeed_trade_bridge"].write_text("# drift", encoding="utf-8")
    contract = collection.restore_plan_document["legacy_task_launch_contracts"][
        "CHILI-IQFeed-Trade-Bridge-Daily"
    ]
    parsed_contract = cutover._parse_launch_contract(
        contract,
        roots=(tmp_path.resolve(),),
        field_name="drifted contract",
        verify_bound_files=False,
    )
    with pytest.raises(cutover.CapturedPaperHostCutoverError, match="HASH_MISMATCH"):
        cutover._assert_launch_contract_sources_current(
            parsed_contract, roots=(tmp_path.resolve(),)
        )


def test_sensitive_task_arguments_are_rejected_before_any_artifact(
    tmp_path: Path,
) -> None:
    tasks, processes = _direct_fixture(tmp_path)
    name = cutover.REQUIRED_LEGACY_TASKS[0]
    process = processes[0]
    raw = _task_xml(
        command=process.executable_path,
        arguments=f'{process.bridge_script_path} --token=do-not-persist',
    )
    tasks[name] = cutover.TaskObservation(name=name, xml=raw, enabled=True)

    with pytest.raises(
        collector.CapturedPaperHostSnapshotError,
        match="SENSITIVE_COMMAND_LINE",
    ):
        collector.collect_host_snapshot(
            probe=FakeReadOnlyProbe(tasks=tasks, processes=processes),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: tuple(value.split()),
        )
    assert not (tmp_path / "outputs").exists()


def test_missing_process_role_fails_closed(tmp_path: Path) -> None:
    tasks, processes = _direct_fixture(tmp_path)
    with pytest.raises(
        collector.CapturedPaperHostSnapshotError,
        match="PROCESS_SNAPSHOT_INCOMPLETE",
    ):
        collector.collect_host_snapshot(
            probe=FakeReadOnlyProbe(tasks=tasks, processes=processes[:1]),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: (value,),
        )


def test_process_file_hash_drift_fails_closed(tmp_path: Path) -> None:
    tasks, processes = _direct_fixture(tmp_path)
    bad = processes[0]
    changed = cutover.ProcessIdentity(
        pid=bad.pid,
        create_time_ns=bad.create_time_ns,
        executable_path=bad.executable_path,
        executable_sha256="f" * 64,
        cmdline=bad.cmdline,
        cmdline_sha256=bad.cmdline_sha256,
        role=bad.role,
        bridge_script_path=bad.bridge_script_path,
        bridge_script_sha256=bad.bridge_script_sha256,
    )
    with pytest.raises(
        collector.CapturedPaperHostSnapshotError,
        match="PROCESS_PROVENANCE_MISMATCH",
    ):
        collector.collect_host_snapshot(
            probe=FakeReadOnlyProbe(tasks=tasks, processes=(changed, processes[1])),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: (value,),
        )


def test_task_drift_during_capture_fails_closed(tmp_path: Path) -> None:
    tasks, processes = _direct_fixture(tmp_path)

    class DriftingProbe(FakeReadOnlyProbe):
        def __init__(self) -> None:
            super().__init__(tasks=tasks, processes=processes)
            self.counts: dict[str, int] = {}

        def get_task(self, name: str) -> cutover.TaskObservation | None:
            self.counts[name] = self.counts.get(name, 0) + 1
            task = super().get_task(name)
            assert task is not None
            if self.counts[name] == 2 and name == cutover.REQUIRED_LEGACY_TASKS[0]:
                return cutover.TaskObservation(
                    name=name,
                    xml=_task_xml(
                        command=processes[0].executable_path,
                        arguments=cutover._quote_windows_arguments(
                            processes[0].cmdline[1:]
                        ),
                        enabled=False,
                    ),
                    enabled=False,
                )
            return task

    with pytest.raises(
        collector.CapturedPaperHostSnapshotError,
        match="TASK_SNAPSHOT_DRIFT",
    ):
        collector.collect_host_snapshot(
            probe=DriftingProbe(),
            legacy_root=tmp_path,
            captured_at=NOW,
            argv_parser=lambda value: (value,),
        )
