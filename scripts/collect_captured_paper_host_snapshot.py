"""Read-only, content-addressed legacy-host snapshot collector.

This command has deliberately less authority than the host-cutover executor.
It can query the exact four allowlisted Task Scheduler definitions and inspect
the two legacy IQFeed Python bridge processes.  It cannot enable, disable,
create, run, stop, or delete a task or process, and it has no provider, broker,
or database imports.

The three authority inputs are serialized with the existing
``captured_paper_host_cutover`` schemas.  Restore-plan v3 carries an
independently revalidated, typed launch contract for each wrapper task.  A
fourth diagnostic-only document records the human-readable projection, but it
never grants authority or claims that a task historically created an observed
process.  Authority comes only from the strict v3 source semantics, hashes,
pair invariants, and mandatory post-start process readback.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from scripts import captured_paper_host_cutover as host_cutover


UTC = timezone.utc
COLLECTION_MANIFEST_SCHEMA = "chili.captured-paper-host-read-only-collection.v1"
WRAPPER_CHAIN_SCHEMA = "chili.captured-paper-host-wrapper-chain-evidence.v1"

_ROLE_TO_SCRIPT = MappingProxyType(
    {
        "iqfeed_depth_bridge": "iqfeed_depth_bridge.py",
        "iqfeed_trade_bridge": "iqfeed_trade_bridge.py",
    }
)
_ROLE_TO_DAILY_TASK = MappingProxyType(
    {
        "iqfeed_depth_bridge": "CHILI-IQFeed-Depth-Bridge-Daily",
        "iqfeed_trade_bridge": "CHILI-IQFeed-Trade-Bridge-Daily",
    }
)
_TASK_TO_ROLE = MappingProxyType(
    {
        name: (
            "iqfeed_depth_bridge" if "-Depth-" in name else "iqfeed_trade_bridge"
        )
        for name in host_cutover.REQUIRED_LEGACY_TASKS
    }
)
_SECRET_ARGUMENT_RE = re.compile(
    r"(?i)(?:api[-_]?key|access[-_]?key|private[-_]?key|authorization|bearer|"
    r"credential|password|passwd|secret|token)"
)
_PS_BRIDGE_START_RE = re.compile(
    r"Start-Process\s+-FilePath\s+(?P<eq>['\"])(?P<exe>[A-Za-z]:\\[^'\"]+)"
    r"(?P=eq)\s*`?\s*(?:\r?\n\s*)?-ArgumentList\s+"
    r"(?P<sq>['\"])(?P<script>[A-Za-z]:\\[^'\"]+\.py)(?P=sq)",
    flags=re.IGNORECASE,
)


class CapturedPaperHostSnapshotError(RuntimeError):
    """Stable fail-closed collector error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


class ReadOnlyHostProbe(Protocol):
    """Only the two observations the collector is permitted to perform."""

    def get_task(self, name: str) -> host_cutover.TaskObservation | None: ...

    def find_bridge_processes(
        self, *, legacy_root: Path
    ) -> tuple[host_cutover.ProcessIdentity, ...]: ...


@dataclass(frozen=True, slots=True)
class HostSnapshotCollection:
    captured_at: datetime
    tasks: Mapping[str, host_cutover.TaskObservation]
    processes: tuple[host_cutover.ProcessIdentity, ...]
    bindings: tuple[host_cutover.LegacyProcessBinding, ...]
    task_snapshot_document: Mapping[str, Any]
    process_snapshot_document: Mapping[str, Any]
    restore_plan_document: Mapping[str, Any]
    wrapper_chain_document: Mapping[str, Any]
    verdict: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class PersistedHostSnapshotCollection:
    verdict: str
    reason_code: str
    artifact_directory: Path
    manifest_path: Path
    manifest_sha256: str
    artifact_paths: Mapping[str, Path]
    artifact_sha256s: Mapping[str, str]


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapturedPaperHostSnapshotError(
            "NON_CANONICAL_JSON", "snapshot material is not canonical JSON"
        ) from exc


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperHostSnapshotError(
            "INVALID_CLOCK", "captured_at must be timezone-aware"
        )
    return value.astimezone(UTC).isoformat()


def _assert_secret_free(values: Sequence[str], *, field: str) -> None:
    for value in values:
        if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
            raise CapturedPaperHostSnapshotError(
                "UNSAFE_COMMAND_LINE", f"{field} contains a malformed argument"
            )
        if _SECRET_ARGUMENT_RE.search(value):
            # Exact process identity cannot be retained after redaction.  The
            # only safe response is to emit no authority artifact at all.
            raise CapturedPaperHostSnapshotError(
                "SENSITIVE_COMMAND_LINE",
                f"{field} contains prohibited credential-like material",
            )


def _stable_hash_unrooted(value: str | Path, *, field: str) -> tuple[Path, str]:
    try:
        return host_cutover._stable_local_file_unrooted(value, field=field)
    except host_cutover.CapturedPaperHostCutoverError as exc:
        raise CapturedPaperHostSnapshotError(exc.code, exc.message) from exc


def _stable_hash_rooted(
    value: str | Path, *, legacy_root: Path, field: str
) -> tuple[Path, bytes, str]:
    try:
        return host_cutover._stable_read(
            value, roots=(legacy_root,), field=field, max_bytes=16 * 1024 * 1024
        )
    except host_cutover.CapturedPaperHostCutoverError as exc:
        raise CapturedPaperHostSnapshotError(exc.code, exc.message) from exc


def _strict_legacy_root(value: str | Path) -> Path:
    try:
        return host_cutover._strict_roots((value,))[0]
    except host_cutover.CapturedPaperHostCutoverError as exc:
        raise CapturedPaperHostSnapshotError(exc.code, exc.message) from exc


def _normalize_schtasks_xml_output(raw: bytes) -> bytes:
    """Delegate to the shared cutover normalization.

    One implementation serves both the read-only collector and the cutover
    backend so a payload collected here can never be rejected there (or vice
    versa) over encoding repair differences.
    """

    try:
        return host_cutover._normalize_schtasks_xml_output(raw)
    except host_cutover.CapturedPaperHostCutoverError as exc:
        raise CapturedPaperHostSnapshotError(exc.code, exc.message) from exc


class WindowsReadOnlyHostProbe:
    """Windows host observer with no mutation methods."""

    def __init__(
        self,
        *,
        subprocess_run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        psutil_module: Any | None = None,
    ) -> None:
        if os.name != "nt":
            raise CapturedPaperHostSnapshotError(
                "WINDOWS_REQUIRED", "Task Scheduler inventory requires Windows"
            )
        # The immutable native resolver, never %SystemRoot%: a forged
        # environment variable must not point the read-only probe at a
        # staged schtasks.exe.
        try:
            system32 = host_cutover._native_system32_directory()
        except host_cutover.CapturedPaperHostCutoverError as exc:
            raise CapturedPaperHostSnapshotError(exc.code, exc.message) from exc
        self._schtasks, _digest = _stable_hash_unrooted(
            system32 / "schtasks.exe", field="schtasks.exe"
        )
        self._run = subprocess_run
        if psutil_module is None:
            try:
                import psutil as psutil_module  # type: ignore[no-redef]
            except ImportError as exc:
                raise CapturedPaperHostSnapshotError(
                    "PSUTIL_REQUIRED", "psutil is required for process identity"
                ) from exc
        self._psutil = psutil_module

    def get_task(self, name: str) -> host_cutover.TaskObservation | None:
        if name not in host_cutover.REQUIRED_LEGACY_TASKS:
            raise CapturedPaperHostSnapshotError(
                "TASK_NAME_INVALID", "task query is outside the exact legacy roster"
            )
        completed = self._run(
            [str(self._schtasks), "/Query", "/TN", name, "/XML"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=30,
        )
        if int(completed.returncode) != 0:
            raise CapturedPaperHostSnapshotError(
                "TASK_SNAPSHOT_INCOMPLETE", f"required task is not readable: {name}"
            )
        raw = _normalize_schtasks_xml_output(bytes(completed.stdout))
        try:
            enabled = host_cutover._task_enabled_from_xml(raw)
            host_cutover._task_exec_projection_from_xml(raw)
        except host_cutover.CapturedPaperHostCutoverError as exc:
            raise CapturedPaperHostSnapshotError(exc.code, exc.message) from exc
        return host_cutover.TaskObservation(name=name, xml=raw, enabled=enabled)

    def _identity_for_python_pid(
        self, pid: int, *, legacy_root: Path
    ) -> host_cutover.ProcessIdentity | None:
        try:
            process = self._psutil.Process(pid)
            before_create = int(round(float(process.create_time()) * 1_000_000_000))
            executable = str(Path(process.exe()).resolve(strict=True))
            cmdline = tuple(str(item) for item in process.cmdline())
        except (self._psutil.NoSuchProcess, self._psutil.ZombieProcess):
            return None
        except (self._psutil.AccessDenied, OSError, ValueError) as exc:
            raise CapturedPaperHostSnapshotError(
                "PROCESS_INVENTORY_UNINSPECTABLE",
                f"cannot prove Python process identity for PID {pid}",
            ) from exc
        if not cmdline:
            raise CapturedPaperHostSnapshotError(
                "PROCESS_INVENTORY_UNINSPECTABLE",
                f"Python process PID {pid} has no inspectable argv",
            )
        matches = [
            (role, index)
            for index, token in enumerate(cmdline)
            for role, basename in _ROLE_TO_SCRIPT.items()
            if Path(token).name.casefold() == basename.casefold()
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise CapturedPaperHostSnapshotError(
                "PROCESS_ROLE_AMBIGUOUS", "one process names multiple legacy bridge roles"
            )
        role, script_index = matches[0]
        # The currently supported legacy identity has no hidden argv in which
        # a secret could be serialized.  Anything else is a new launch policy
        # and must be reviewed before it can become rollback authority.
        if len(cmdline) != 2 or script_index != 1:
            raise CapturedPaperHostSnapshotError(
                "PROCESS_ARGV_UNSUPPORTED",
                f"{role} must have exactly executable + bridge-script argv",
            )
        _assert_secret_free(cmdline, field=f"process {pid} argv")
        executable_path, executable_sha = _stable_hash_unrooted(
            executable, field=f"process {pid} executable"
        )
        script_path, _raw, script_sha = _stable_hash_rooted(
            cmdline[1], legacy_root=legacy_root, field=f"process {pid} bridge script"
        )
        if (
            os.path.normcase(str(executable_path)) != os.path.normcase(cmdline[0])
            or script_path.name.casefold() != _ROLE_TO_SCRIPT[role].casefold()
        ):
            raise CapturedPaperHostSnapshotError(
                "PROCESS_PROVENANCE_MISMATCH",
                f"process {pid} argv differs from its executable/script identity",
            )
        try:
            after_create = int(round(float(process.create_time()) * 1_000_000_000))
            after_executable = str(Path(process.exe()).resolve(strict=True))
            after_cmdline = tuple(str(item) for item in process.cmdline())
        except (self._psutil.NoSuchProcess, self._psutil.ZombieProcess) as exc:
            raise CapturedPaperHostSnapshotError(
                "PROCESS_IDENTITY_DRIFT", f"legacy process {pid} exited during capture"
            ) from exc
        except (self._psutil.AccessDenied, OSError, ValueError) as exc:
            raise CapturedPaperHostSnapshotError(
                "PROCESS_INVENTORY_UNINSPECTABLE",
                f"cannot recheck legacy process PID {pid}",
            ) from exc
        if (
            before_create != after_create
            or os.path.normcase(executable) != os.path.normcase(after_executable)
            or cmdline != after_cmdline
        ):
            raise CapturedPaperHostSnapshotError(
                "PROCESS_IDENTITY_DRIFT", f"legacy process {pid} changed during capture"
            )
        return host_cutover.ProcessIdentity(
            pid=pid,
            create_time_ns=before_create,
            executable_path=str(executable_path),
            executable_sha256=executable_sha,
            cmdline=cmdline,
            cmdline_sha256=host_cutover.sha256_json(list(cmdline)),
            role=role,
            bridge_script_path=str(script_path),
            bridge_script_sha256=script_sha,
        )

    def find_bridge_processes(
        self, *, legacy_root: Path
    ) -> tuple[host_cutover.ProcessIdentity, ...]:
        found: list[host_cutover.ProcessIdentity] = []
        try:
            candidates = self._psutil.process_iter(attrs=["pid", "name"], ad_value=None)
            for item in candidates:
                name = item.info.get("name")
                if name is None:
                    raise CapturedPaperHostSnapshotError(
                        "PROCESS_INVENTORY_UNINSPECTABLE",
                        "a process name could not be inspected",
                    )
                if str(name).casefold() != "python.exe":
                    continue
                identity = self._identity_for_python_pid(
                    int(item.info["pid"]), legacy_root=legacy_root
                )
                if identity is not None:
                    found.append(identity)
        except CapturedPaperHostSnapshotError:
            raise
        except (self._psutil.AccessDenied, OSError, KeyError, TypeError, ValueError) as exc:
            raise CapturedPaperHostSnapshotError(
                "PROCESS_INVENTORY_UNINSPECTABLE",
                "legacy process inventory could not be completed",
            ) from exc
        ordered = tuple(sorted(found, key=lambda item: (item.role, item.pid)))
        if (
            len({item.pid for item in ordered}) != len(ordered)
            or len({item.role for item in ordered}) != len(ordered)
            or {item.role for item in ordered}
            != set(host_cutover.REQUIRED_LEGACY_PROCESS_ROLES)
        ):
            raise CapturedPaperHostSnapshotError(
                "PROCESS_SNAPSHOT_INCOMPLETE",
                "exactly one trade and one depth bridge process are required",
            )
        return ordered


def _windows_command_line_to_argv(value: str) -> tuple[str, ...]:
    if os.name != "nt":
        raise CapturedPaperHostSnapshotError(
            "WINDOWS_REQUIRED", "Windows command-line parsing is unavailable"
        )
    try:
        import ctypes
        from ctypes import wintypes

        argc = ctypes.c_int(0)
        command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
        command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
        result = command_line_to_argv(value, ctypes.byref(argc))
        if not result:
            raise OSError("CommandLineToArgvW failed")
        try:
            return tuple(str(result[index]) for index in range(argc.value))
        finally:
            local_free = ctypes.windll.kernel32.LocalFree
            local_free.argtypes = (ctypes.c_void_p,)
            local_free.restype = ctypes.c_void_p
            local_free(ctypes.cast(result, ctypes.c_void_p))
    except (AttributeError, OSError, ValueError) as exc:
        raise CapturedPaperHostSnapshotError(
            "TASK_ARGUMENTS_UNINSPECTABLE", "cannot parse Task Scheduler arguments"
        ) from exc


def _resolve_diagnostic_executable(value: str) -> tuple[str | None, str | None]:
    candidates: list[Path] = []
    raw = Path(value)
    if host_cutover._is_local_absolute(raw):
        candidates.append(raw)
    else:
        # Diagnostic evidence only, never authority; still resolve through
        # the immutable native directory rather than mutable %SystemRoot%.
        lowered = raw.name.casefold()
        try:
            if lowered == "wscript.exe":
                candidates.append(
                    host_cutover._native_system32_executable("wscript.exe")
                )
            elif lowered in {"powershell.exe", "powershell"}:
                candidates.append(
                    host_cutover._native_system32_executable("powershell.exe")
                )
        except host_cutover.CapturedPaperHostCutoverError:
            return None, None
    if len(candidates) != 1:
        return None, None
    try:
        path, digest = _stable_hash_unrooted(
            candidates[0], field="diagnostic wrapper executable"
        )
    except CapturedPaperHostSnapshotError:
        return None, None
    return str(path), digest


def _power_shell_bridge_projection(
    raw: bytes, *, role: str, legacy_root: Path
) -> Mapping[str, Any]:
    unresolved: list[str] = []
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return MappingProxyType(
            {
                "status": "UNRESOLVED",
                "python_executable_path": None,
                "python_executable_sha256": None,
                "bridge_script_path": None,
                "bridge_script_sha256": None,
                "unresolved_steps": ["starter_not_strict_utf8"],
            }
        )
    matches = [
        item
        for item in _PS_BRIDGE_START_RE.finditer(text)
        if Path(item.group("script")).name.casefold()
        == _ROLE_TO_SCRIPT[role].casefold()
    ]
    if len(matches) != 1:
        return MappingProxyType(
            {
                "status": "UNRESOLVED",
                "python_executable_path": None,
                "python_executable_sha256": None,
                "bridge_script_path": None,
                "bridge_script_sha256": None,
                "unresolved_steps": ["starter_bridge_launch_not_unique"],
            }
        )
    match = matches[0]
    python_path: str | None = None
    python_sha: str | None = None
    bridge_path: str | None = None
    bridge_sha: str | None = None
    try:
        resolved_python, python_sha = _stable_hash_unrooted(
            match.group("exe"), field=f"{role} starter Python executable"
        )
        python_path = str(resolved_python)
    except CapturedPaperHostSnapshotError:
        unresolved.append("starter_python_executable_unavailable")
    try:
        resolved_bridge, _bridge_raw, bridge_sha = _stable_hash_rooted(
            match.group("script"),
            legacy_root=legacy_root,
            field=f"{role} starter bridge script",
        )
        bridge_path = str(resolved_bridge)
    except CapturedPaperHostSnapshotError:
        unresolved.append("starter_bridge_script_unavailable")
    return MappingProxyType(
        {
            "status": "STATIC_PROJECTION" if not unresolved else "UNRESOLVED",
            "python_executable_path": python_path,
            "python_executable_sha256": python_sha,
            "bridge_script_path": bridge_path,
            "bridge_script_sha256": bridge_sha,
            "unresolved_steps": unresolved,
        }
    )


def build_wrapper_chain_evidence_document(
    *,
    captured_at: datetime,
    tasks: Mapping[str, host_cutover.TaskObservation],
    processes: Sequence[host_cutover.ProcessIdentity],
    legacy_root: Path,
    argv_parser: Callable[[str], tuple[str, ...]] = _windows_command_line_to_argv,
) -> Mapping[str, Any]:
    """Build diagnostic wrapper provenance without promoting it to authority."""

    process_by_role = {item.role: item for item in processes}
    rows: dict[str, Any] = {}
    for name in host_cutover.REQUIRED_LEGACY_TASKS:
        role = _TASK_TO_ROLE[name]
        task = tasks[name]
        projection = host_cutover._task_exec_projection_from_xml(task.xml)
        command = projection["command"]
        arguments = projection["arguments"]
        _assert_secret_free((command, arguments), field=f"task {name} action")
        unresolved: list[str] = []
        is_vbs_host = Path(command).name.casefold() in {"wscript.exe", "cscript.exe"}
        if is_vbs_host:
            try:
                argv = argv_parser(arguments)
                _assert_secret_free(argv, field=f"task {name} parsed argv")
            except CapturedPaperHostSnapshotError as exc:
                argv = ()
                unresolved.append(exc.code.casefold())
        else:
            argv = ()
            unresolved.append("task_is_direct_or_non_vbs_action")
        action_path, action_sha = _resolve_diagnostic_executable(command)
        if action_path is None:
            unresolved.append("task_action_executable_unresolved")

        vbs_path: str | None = None
        vbs_sha: str | None = None
        powershell_token: str | None = None
        powershell_path: str | None = None
        powershell_sha: str | None = None
        starter_path: str | None = None
        starter_sha: str | None = None
        starter_projection: Mapping[str, Any] = MappingProxyType(
            {
                "status": "UNRESOLVED",
                "python_executable_path": None,
                "python_executable_sha256": None,
                "bridge_script_path": None,
                "bridge_script_sha256": None,
                "unresolved_steps": ["starter_not_resolved"],
            }
        )
        if argv:
            if Path(argv[0]).suffix.casefold() != ".vbs":
                unresolved.append("first_wrapper_argument_is_not_vbs")
            try:
                vbs, _vbs_raw, vbs_sha = _stable_hash_rooted(
                    argv[0], legacy_root=legacy_root, field=f"task {name} VBS wrapper"
                )
                vbs_path = str(vbs)
                unresolved.append("vbs_forwarding_semantics_not_authoritative")
            except CapturedPaperHostSnapshotError:
                unresolved.append("vbs_wrapper_unresolved")
            if len(argv) >= 2:
                powershell_token = argv[1]
                if Path(powershell_token).name.casefold() not in {
                    "powershell.exe",
                    "powershell",
                }:
                    unresolved.append("powershell_token_unrecognized")
                powershell_path, powershell_sha = _resolve_diagnostic_executable(
                    powershell_token
                )
                if powershell_path is None:
                    unresolved.append("powershell_executable_unresolved")
            else:
                unresolved.append("powershell_token_missing")
            file_indexes = [
                index for index, token in enumerate(argv) if token.casefold() == "-file"
            ]
            if len(file_indexes) == 1 and file_indexes[0] + 1 < len(argv):
                try:
                    starter, starter_raw, starter_sha = _stable_hash_rooted(
                        argv[file_indexes[0] + 1],
                        legacy_root=legacy_root,
                        field=f"task {name} PowerShell starter",
                    )
                    starter_path = str(starter)
                    starter_projection = _power_shell_bridge_projection(
                        starter_raw, role=role, legacy_root=legacy_root
                    )
                    unresolved.extend(starter_projection["unresolved_steps"])
                except CapturedPaperHostSnapshotError:
                    unresolved.append("powershell_starter_unresolved")
            else:
                unresolved.append("powershell_file_argument_unresolved")

            expected_prefix = (
                "-noprofile",
                "-executionpolicy",
                "bypass",
                "-file",
            )
            if len(argv) != 7 or tuple(token.casefold() for token in argv[2:6]) != expected_prefix:
                unresolved.append("powershell_invocation_shape_unrecognized")

        process = process_by_role[role]
        expected_matches_running = bool(
            starter_projection.get("status") == "STATIC_PROJECTION"
            and os.path.normcase(str(starter_projection.get("python_executable_path")))
            == os.path.normcase(process.executable_path)
            and starter_projection.get("python_executable_sha256")
            == process.executable_sha256
            and os.path.normcase(str(starter_projection.get("bridge_script_path")))
            == os.path.normcase(process.bridge_script_path or "")
            and starter_projection.get("bridge_script_sha256")
            == process.bridge_script_sha256
        )
        if starter_projection.get("status") == "STATIC_PROJECTION":
            unresolved.append("starter_projection_not_execution_proof")
        if not expected_matches_running:
            unresolved.append("wrapper_target_differs_from_running_process")
        direct_identity = bool(
            os.path.normcase(command) == os.path.normcase(process.executable_path)
            and arguments == host_cutover._quote_windows_arguments(process.cmdline[1:])
        )
        rows[name] = {
            "role": role,
            "task_xml_sha256": task.xml_sha256,
            "task_action": {
                "command": command,
                "arguments": arguments,
                "resolved_executable_path": action_path,
                "resolved_executable_sha256": action_sha,
            },
            "vbs_wrapper": {
                "path": vbs_path,
                "sha256": vbs_sha,
                "semantic_status": (
                    "HASH_BOUND_NOT_AUTHORITY" if vbs_sha else "UNRESOLVED"
                ),
            },
            "powershell": {
                "token": powershell_token,
                "resolved_path": powershell_path,
                "sha256": powershell_sha,
            },
            "powershell_starter": {
                "path": starter_path,
                "sha256": starter_sha,
            },
            "expected_bridge": dict(starter_projection),
            "running_process": {
                "pid": process.pid,
                "create_time_ns": process.create_time_ns,
                "executable_sha256": process.executable_sha256,
                "bridge_script_sha256": process.bridge_script_sha256,
                "cmdline_sha256": process.cmdline_sha256,
            },
            "wrapper_target_matches_running_process": expected_matches_running,
            "direct_task_process_identity": direct_identity,
            "unresolved_steps": sorted(set(unresolved)),
        }
    pair_consistency: dict[str, bool] = {}
    for role in sorted(host_cutover.REQUIRED_LEGACY_PROCESS_ROLES):
        role_rows = [row for row in rows.values() if row["role"] == role]
        projections = {
            (
                row["vbs_wrapper"]["sha256"],
                row["powershell"]["sha256"],
                row["powershell_starter"]["sha256"],
                row["expected_bridge"]["python_executable_sha256"],
                row["expected_bridge"]["bridge_script_sha256"],
            )
            for row in role_rows
        }
        consistent = len(role_rows) == 2 and len(projections) == 1
        pair_consistency[role] = consistent
        if not consistent:
            for row in role_rows:
                row["unresolved_steps"] = sorted(
                    set(row["unresolved_steps"]) | {"daily_logon_wrapper_chain_differs"}
                )
    return {
        "schema_version": WRAPPER_CHAIN_SCHEMA,
        "captured_at": _iso(captured_at),
        "diagnostic_only": True,
        "authority_granted": False,
        "role_pair_consistency": pair_consistency,
        "tasks": rows,
    }


def _validate_process_files(
    processes: Sequence[host_cutover.ProcessIdentity], *, legacy_root: Path
) -> None:
    for item in processes:
        _assert_secret_free(item.cmdline, field=f"{item.role} argv")
        if len(item.cmdline) != 2:
            raise CapturedPaperHostSnapshotError(
                "PROCESS_ARGV_UNSUPPORTED",
                f"{item.role} must have exactly executable + bridge-script argv",
            )
        executable, executable_sha = _stable_hash_unrooted(
            item.executable_path, field=f"{item.role} executable"
        )
        script, _raw, script_sha = _stable_hash_rooted(
            item.bridge_script_path or "",
            legacy_root=legacy_root,
            field=f"{item.role} bridge script",
        )
        if not (
            os.path.normcase(str(executable)) == os.path.normcase(item.executable_path)
            and executable_sha == item.executable_sha256
            and os.path.normcase(str(script))
            == os.path.normcase(item.bridge_script_path or "")
            and script_sha == item.bridge_script_sha256
            and item.cmdline == (str(executable), str(script))
            and item.cmdline_sha256 == host_cutover.sha256_json(list(item.cmdline))
        ):
            raise CapturedPaperHostSnapshotError(
                "PROCESS_PROVENANCE_MISMATCH",
                f"{item.role} file/argv identity is inconsistent",
            )


def _validate_exact_process_roster(
    processes: Sequence[host_cutover.ProcessIdentity],
) -> None:
    if (
        any(type(item) is not host_cutover.ProcessIdentity for item in processes)
        or len({item.pid for item in processes}) != len(processes)
        or len({item.role for item in processes}) != len(processes)
        or {item.role for item in processes}
        != set(host_cutover.REQUIRED_LEGACY_PROCESS_ROLES)
    ):
        raise CapturedPaperHostSnapshotError(
            "PROCESS_SNAPSHOT_INCOMPLETE",
            "exactly one typed trade and one typed depth process are required",
        )


def _build_bindings(
    *,
    tasks: Mapping[str, host_cutover.TaskObservation],
    processes: Sequence[host_cutover.ProcessIdentity],
) -> tuple[host_cutover.LegacyProcessBinding, ...]:
    bindings: list[host_cutover.LegacyProcessBinding] = []
    for process in sorted(processes, key=lambda item: item.role):
        task_name = _ROLE_TO_DAILY_TASK[process.role]
        task = tasks[task_name]
        bindings.append(
            host_cutover.LegacyProcessBinding(
                role=process.role,
                executable_path=process.executable_path,
                executable_sha256=process.executable_sha256,
                bridge_script_path=str(process.bridge_script_path or ""),
                bridge_script_sha256=str(process.bridge_script_sha256 or ""),
                restore_task=task_name,
                restore_task_xml_sha256=task.xml_sha256,
                restore_task_action_sha256=host_cutover._task_action_sha256(task.xml),
                expected_cmdline=process.cmdline,
                expected_cmdline_sha256=process.cmdline_sha256,
            )
        )
    return tuple(bindings)


def _assert_wrapper_evidence_files_stable(
    document: Mapping[str, Any], *, legacy_root: Path
) -> None:
    for task_name, row_value in document["tasks"].items():
        row = dict(row_value)
        unrooted = (
            (row["task_action"].get("resolved_executable_path"),
             row["task_action"].get("resolved_executable_sha256"),
             "task action executable"),
            (row["powershell"].get("resolved_path"),
             row["powershell"].get("sha256"),
             "PowerShell executable"),
            (row["expected_bridge"].get("python_executable_path"),
             row["expected_bridge"].get("python_executable_sha256"),
             "starter Python executable"),
        )
        rooted = (
            (row["vbs_wrapper"].get("path"), row["vbs_wrapper"].get("sha256"),
             "VBS wrapper"),
            (row["powershell_starter"].get("path"),
             row["powershell_starter"].get("sha256"), "PowerShell starter"),
            (row["expected_bridge"].get("bridge_script_path"),
             row["expected_bridge"].get("bridge_script_sha256"), "bridge script"),
        )
        for path, expected, field in unrooted:
            if path is None and expected is None:
                continue
            if not isinstance(path, str) or not isinstance(expected, str):
                raise CapturedPaperHostSnapshotError(
                    "WRAPPER_EVIDENCE_INCONSISTENT",
                    f"{task_name} {field} has a partial identity",
                )
            _resolved, actual = _stable_hash_unrooted(
                path, field=f"{task_name} {field} stability"
            )
            if actual != expected:
                raise CapturedPaperHostSnapshotError(
                    "WRAPPER_EVIDENCE_DRIFT", f"{task_name} {field} changed"
                )
        for path, expected, field in rooted:
            if path is None and expected is None:
                continue
            if not isinstance(path, str) or not isinstance(expected, str):
                raise CapturedPaperHostSnapshotError(
                    "WRAPPER_EVIDENCE_INCONSISTENT",
                    f"{task_name} {field} has a partial identity",
                )
            _resolved, _raw, actual = _stable_hash_rooted(
                path,
                legacy_root=legacy_root,
                field=f"{task_name} {field} stability",
            )
            if actual != expected:
                raise CapturedPaperHostSnapshotError(
                    "WRAPPER_EVIDENCE_DRIFT", f"{task_name} {field} changed"
                )


def _assert_contract_sources_stable(
    launch_contracts: Mapping[str, host_cutover.LegacyTaskLaunchContract],
    *,
    wrapper_document: Mapping[str, Any],
    legacy_root: Path,
) -> None:
    """Final fence for the sealed contracts and their diagnostic twins.

    Diagnostic-evidence stability alone can validate a different chain than
    the sealed contracts: a source changed between contract build and
    diagnostic build reproduces the NEW bytes in the diagnostic hashes while
    the contract retains the old ones.  Every sealed source is therefore
    rehashed against its contract at the end of the capture interval, and
    wrapper diagnostic identities must equal the sealed contract identities.
    """

    for task_name, contract in launch_contracts.items():
        rooted = [
            (
                contract.expected_bridge_script_path,
                contract.expected_bridge_script_sha256,
                "bridge script",
            )
        ]
        if contract.wrapper_path is not None:
            rooted.append((contract.wrapper_path, contract.wrapper_sha256, "wrapper"))
        if contract.starter_path is not None:
            rooted.append((contract.starter_path, contract.starter_sha256, "starter"))
        for path, expected, field in rooted:
            _resolved, _raw, actual = _stable_hash_rooted(
                path,
                legacy_root=legacy_root,
                field=f"{task_name} sealed {field} stability",
            )
            if actual != expected:
                raise CapturedPaperHostSnapshotError(
                    "LAUNCH_CONTRACT_SOURCE_DRIFT",
                    f"{task_name} sealed {field} changed during capture",
                )
        _resolved, actual = _stable_hash_unrooted(
            contract.expected_executable_path,
            field=f"{task_name} sealed executable stability",
        )
        if actual != contract.expected_executable_sha256:
            raise CapturedPaperHostSnapshotError(
                "LAUNCH_CONTRACT_SOURCE_DRIFT",
                f"{task_name} sealed executable changed during capture",
            )
        if contract.launch_kind != host_cutover.LEGACY_WRAPPER_LAUNCH_KIND:
            continue
        row = wrapper_document["tasks"][task_name]
        pairs = (
            (
                row["vbs_wrapper"].get("path"),
                row["vbs_wrapper"].get("sha256"),
                contract.wrapper_path,
                contract.wrapper_sha256,
                "VBS wrapper",
            ),
            (
                row["powershell_starter"].get("path"),
                row["powershell_starter"].get("sha256"),
                contract.starter_path,
                contract.starter_sha256,
                "PowerShell starter",
            ),
            (
                row["powershell"].get("resolved_path"),
                row["powershell"].get("sha256"),
                contract.powershell_path,
                contract.powershell_sha256,
                "PowerShell executable",
            ),
        )
        for diag_path, diag_sha, contract_path, contract_sha, field in pairs:
            if not isinstance(diag_path, str) or not isinstance(diag_sha, str):
                raise CapturedPaperHostSnapshotError(
                    "WRAPPER_EVIDENCE_INCONSISTENT",
                    f"{task_name} diagnostic {field} identity is missing",
                )
            if (
                os.path.normcase(diag_path)
                != os.path.normcase(str(contract_path or ""))
                or diag_sha != contract_sha
            ):
                raise CapturedPaperHostSnapshotError(
                    "CONTRACT_DIAGNOSTIC_IDENTITY_SKEW",
                    f"{task_name} diagnostic {field} differs from its sealed contract",
                )


def _semantic_process_keys(
    values: Sequence[host_cutover.ProcessIdentity],
) -> tuple[tuple[Any, ...], ...]:
    return tuple(sorted((item.semantic_key() for item in values), key=repr))


def collect_host_snapshot(
    *,
    probe: ReadOnlyHostProbe,
    legacy_root: str | Path,
    captured_at: datetime,
    argv_parser: Callable[[str], tuple[str, ...]] = _windows_command_line_to_argv,
) -> HostSnapshotCollection:
    """Collect and validate one point-in-time host observation."""

    _iso(captured_at)
    root = _strict_legacy_root(legacy_root)
    tasks: dict[str, host_cutover.TaskObservation] = {}
    for name in host_cutover.REQUIRED_LEGACY_TASKS:
        task = probe.get_task(name)
        if task is None or type(task) is not host_cutover.TaskObservation:
            raise CapturedPaperHostSnapshotError(
                "TASK_SNAPSHOT_INCOMPLETE", f"required task is absent: {name}"
            )
        tasks[name] = task
    processes = tuple(probe.find_bridge_processes(legacy_root=root))
    _validate_exact_process_roster(processes)
    _validate_process_files(processes, legacy_root=root)
    bindings = _build_bindings(tasks=tasks, processes=processes)
    try:
        for name in host_cutover.REQUIRED_LEGACY_TASKS:
            projection = host_cutover._task_exec_projection_from_xml(tasks[name].xml)
            _assert_secret_free(
                (projection["command"], projection["arguments"]),
                field=f"task {name} action",
            )
        process_by_role = {item.role: item for item in processes}
        direct_shape = all(
            os.path.normcase(
                host_cutover._task_exec_projection_from_xml(tasks[name].xml)["command"]
            )
            == os.path.normcase(
                process_by_role[_TASK_TO_ROLE[name]].executable_path
            )
            for name in host_cutover.REQUIRED_LEGACY_TASKS
        )
        launch_contracts = (
            host_cutover._derive_direct_launch_contracts(
                tasks=tasks, bindings=bindings
            )
            if direct_shape
            else host_cutover.build_legacy_wrapper_launch_contracts(
                tasks=tasks,
                processes=processes,
                legacy_root=root,
                argv_parser=argv_parser,
            )
        )
        task_document = host_cutover.build_task_snapshot_document(
            captured_at=captured_at, tasks=tasks
        )
        process_document = host_cutover.build_process_snapshot_document(
            captured_at=captured_at, processes=processes
        )
        restore_document = host_cutover.build_restore_plan_document(
            tasks=tasks,
            bindings=bindings,
            launch_contracts=launch_contracts,
        )
        wrapper_document = build_wrapper_chain_evidence_document(
            captured_at=captured_at,
            tasks=tasks,
            processes=processes,
            legacy_root=root,
            argv_parser=argv_parser,
        )
    except host_cutover.CapturedPaperHostCutoverError as exc:
        raise CapturedPaperHostSnapshotError(exc.code, exc.message) from exc

    # The snapshot is accepted only if every external observation is byte-/
    # identity-stable across the complete evidence-build interval.
    for name in host_cutover.REQUIRED_LEGACY_TASKS:
        after = probe.get_task(name)
        before = tasks[name]
        if (
            after is None
            or type(after) is not host_cutover.TaskObservation
            or after.name != before.name
            or after.xml != before.xml
            or after.enabled is not before.enabled
        ):
            raise CapturedPaperHostSnapshotError(
                "TASK_SNAPSHOT_DRIFT", f"task changed during capture: {name}"
            )
    after_processes = tuple(probe.find_bridge_processes(legacy_root=root))
    _validate_exact_process_roster(after_processes)
    if _semantic_process_keys(after_processes) != _semantic_process_keys(processes):
        raise CapturedPaperHostSnapshotError(
            "PROCESS_IDENTITY_DRIFT", "legacy process roster changed during capture"
        )
    _validate_process_files(after_processes, legacy_root=root)
    _assert_wrapper_evidence_files_stable(wrapper_document, legacy_root=root)
    _assert_contract_sources_stable(
        launch_contracts, wrapper_document=wrapper_document, legacy_root=root
    )

    # Reuse the exact downstream predicate.  The diagnostic projection above
    # remains non-authoritative; only the typed restore-plan v4 contracts are
    # admitted here.
    task_snapshot = host_cutover.TaskSnapshot(
        captured_at=captured_at.astimezone(UTC),
        tasks=MappingProxyType(dict(tasks)),
        artifact_path=Path("task-snapshot.json"),
        artifact_sha256=host_cutover.sha256_json(task_document),
    )
    process_snapshot = host_cutover.ProcessSnapshot(
        captured_at=captured_at.astimezone(UTC),
        processes=processes,
        artifact_path=Path("process-snapshot.json"),
        artifact_sha256=host_cutover.sha256_json(process_document),
    )
    restore_plan = host_cutover.RestorePlan(
        task_enabled_states=MappingProxyType(
            {name: tasks[name].enabled for name in host_cutover.REQUIRED_LEGACY_TASKS}
        ),
        restart_tasks=tuple(restore_document["restart_tasks"]),
        bindings=bindings,
        candidate_task_name=host_cutover.CANDIDATE_TASK_NAME,
        artifact_path=Path("restore-plan.json"),
        artifact_sha256=host_cutover.sha256_json(restore_document),
        launch_contracts=launch_contracts,
    )
    verdict = "VALIDATED"
    reason_code = (
        "CURRENT_DIRECT_IDENTITY_CONTRACT_SATISFIED"
        if all(
            item.launch_kind == host_cutover.LEGACY_DIRECT_LAUNCH_KIND
            for item in launch_contracts.values()
        )
        else "WRAPPER_RESTORE_AUTHORITY_CONTRACT_SATISFIED"
    )
    try:
        host_cutover._assert_snapshot_plan_consistency(
            task_snapshot, process_snapshot, restore_plan
        )
    except host_cutover.CapturedPaperHostCutoverError as exc:
        verdict = "REJECTED"
        reason_code = exc.code
    return HostSnapshotCollection(
        captured_at=captured_at.astimezone(UTC),
        tasks=MappingProxyType(dict(tasks)),
        processes=processes,
        bindings=bindings,
        task_snapshot_document=MappingProxyType(dict(task_document)),
        process_snapshot_document=MappingProxyType(dict(process_document)),
        restore_plan_document=MappingProxyType(dict(restore_document)),
        wrapper_chain_document=MappingProxyType(dict(wrapper_document)),
        verdict=verdict,
        reason_code=reason_code,
    )


def _publish_exact(path: Path, raw: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != raw:
            raise CapturedPaperHostSnapshotError(
                "ARTIFACT_REPLAY_MISMATCH", "content-addressed artifact bytes differ"
            )
        return
    try:
        with path.open("xb") as handle:
            if handle.write(raw) != len(raw):
                raise OSError("short artifact write")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if not path.is_file() or path.read_bytes() != raw:
            raise CapturedPaperHostSnapshotError(
                "ARTIFACT_REPLAY_MISMATCH", "artifact raced with different bytes"
            )
    except OSError as exc:
        raise CapturedPaperHostSnapshotError(
            "ARTIFACT_WRITE_FAILED", "could not publish local snapshot artifact"
        ) from exc
    if path.read_bytes() != raw:
        raise CapturedPaperHostSnapshotError(
            "ARTIFACT_READBACK_FAILED", "snapshot artifact failed exact readback"
        )


def persist_host_snapshot(
    collection: HostSnapshotCollection, *, output_root: str | Path
) -> PersistedHostSnapshotCollection:
    root = _strict_legacy_root(output_root)
    documents = {
        "task_snapshot": collection.task_snapshot_document,
        "process_snapshot": collection.process_snapshot_document,
        "restore_plan": collection.restore_plan_document,
        "wrapper_chain_evidence": collection.wrapper_chain_document,
    }
    raw_documents = {
        role: _canonical_json_bytes(dict(document))
        for role, document in documents.items()
    }
    hashes = {
        role: hashlib.sha256(raw).hexdigest() for role, raw in raw_documents.items()
    }
    collector_path, collector_raw, collector_sha = host_cutover._stable_read(
        __file__, roots=(Path(__file__).resolve().parents[1],), field="host collector"
    )
    del collector_raw
    file_names = {
        role: f"{hashes[role]}.{role.replace('_', '-')}.json" for role in documents
    }
    manifest = {
        "schema_version": COLLECTION_MANIFEST_SCHEMA,
        "captured_at": _iso(collection.captured_at),
        "verdict": collection.verdict,
        "reason_code": collection.reason_code,
        "collector_source_path": str(collector_path),
        "collector_source_sha256": collector_sha,
        "artifacts": {
            role: {"file_name": file_names[role], "sha256": hashes[role]}
            for role in sorted(documents)
        },
        "host_mutation_count": 0,
        "task_or_process_mutation_authorized": False,
        "provider_access_performed": False,
        "broker_access_performed": False,
        "database_access_performed": False,
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    manifest_raw = _canonical_json_bytes(manifest)
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    artifact_directory = root / manifest_sha
    try:
        artifact_directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        host_cutover._reject_reparse_chain(artifact_directory)
    except (OSError, host_cutover.CapturedPaperHostCutoverError) as exc:
        raise CapturedPaperHostSnapshotError(
            getattr(exc, "code", "ARTIFACT_WRITE_FAILED"),
            "cannot create a sealed local artifact directory",
        ) from exc
    paths: dict[str, Path] = {}
    for role in sorted(documents):
        path = artifact_directory / file_names[role]
        _publish_exact(path, raw_documents[role])
        paths[role] = path
    manifest_path = artifact_directory / f"{manifest_sha}.manifest.json"
    _publish_exact(manifest_path, manifest_raw)
    expected_names = {path.name for path in paths.values()} | {manifest_path.name}
    actual_children = tuple(artifact_directory.iterdir())
    if (
        {path.name for path in actual_children} != expected_names
        or any(not path.is_file() for path in actual_children)
    ):
        raise CapturedPaperHostSnapshotError(
            "ARTIFACT_DIRECTORY_NOT_EXACT",
            "content-addressed artifact directory has an unexpected entry",
        )
    host_cutover._fsync_parent_directory(manifest_path)
    return PersistedHostSnapshotCollection(
        verdict=collection.verdict,
        reason_code=collection.reason_code,
        artifact_directory=artifact_directory,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        artifact_paths=MappingProxyType(paths),
        artifact_sha256s=MappingProxyType(hashes),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect read-only captured PAPER legacy-host rollback inputs."
    )
    parser.add_argument("--legacy-root", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        collection = collect_host_snapshot(
            probe=WindowsReadOnlyHostProbe(),
            legacy_root=arguments.legacy_root,
            captured_at=datetime.now(tz=UTC),
        )
        persisted = persist_host_snapshot(
            collection, output_root=arguments.output_root
        )
    except (
        CapturedPaperHostSnapshotError,
        host_cutover.CapturedPaperHostCutoverError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {
                    "verdict": "REJECTED",
                    "reason_code": getattr(exc, "code", type(exc).__name__),
                    "host_mutation_count": 0,
                    "live_cash_authorized": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "verdict": persisted.verdict,
                "reason_code": persisted.reason_code,
                "manifest_path": str(persisted.manifest_path),
                "manifest_sha256": persisted.manifest_sha256,
                "host_mutation_count": 0,
                "live_cash_authorized": False,
            },
            sort_keys=True,
        )
    )
    return 0 if persisted.verdict == "VALIDATED" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "COLLECTION_MANIFEST_SCHEMA",
    "CapturedPaperHostSnapshotError",
    "HostSnapshotCollection",
    "PersistedHostSnapshotCollection",
    "ReadOnlyHostProbe",
    "WRAPPER_CHAIN_SCHEMA",
    "WindowsReadOnlyHostProbe",
    "build_wrapper_chain_evidence_document",
    "collect_host_snapshot",
    "main",
    "persist_host_snapshot",
]
