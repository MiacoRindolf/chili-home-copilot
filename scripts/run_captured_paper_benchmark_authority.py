"""Execute one exact captured-PAPER benchmark launch receipt and attest it.

The runner accepts only the canonical, content-addressed manifest and inert
launch receipt emitted by :mod:`build_captured_paper_benchmark_authority`.  It
revalidates every source, Python, dependency, Git, output, argv, cwd, and
environment binding immediately before invoking that exact argv once.  A
successful run produces a second, append-only execution receipt that binds the
canonical stdout bytes to the benchmark's retained content-addressed report.

This module is standard-library-only and contains no database, provider,
broker, order, activation, cutover, or Task Scheduler integration.
"""

from __future__ import annotations

if __name__ == "__main__" and (
    globals().get("__chili_held_runner_source_sha256__") is None
    or globals().get("__chili_held_builder_module__") is None
    or globals().get("__chili_held_python_executable_sha256__") is None
):
    raise SystemExit("RUNNER_HELD_BOOTSTRAP_REQUIRED")

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Any, Mapping, Sequence


_HELD_BENCHMARK_AUTHORITY_RUNNER_LOADER = r"""
import hashlib, os, stat, sys, types
from pathlib import Path
flags = sys.flags
if not (sys.implementation.name == 'cpython'
        and sys.version_info[:2] == (3, 11)
        and flags.isolated == 1 and flags.ignore_environment == 1
        and flags.no_site == 1 and flags.safe_path is True
        and flags.dont_write_bytecode == 1):
    raise SystemExit(90)
values = tuple(sys.argv[1:])
try:
    boundary = values.index('--')
except ValueError:
    raise SystemExit(91)
bootstrap = values[:boundary]
arguments = values[boundary + 1:]
if len(bootstrap) != 5 or not arguments:
    raise SystemExit(92)
builder_raw = Path(bootstrap[0])
builder_expected = bootstrap[1]
runner_raw = Path(bootstrap[2])
runner_expected = bootstrap[3]
python_expected = bootstrap[4]
if (not builder_raw.is_absolute() or not runner_raw.is_absolute()
        or any(len(value) != 64
               or any(c not in '0123456789abcdef' for c in value)
               for value in (builder_expected, runner_expected, python_expected))):
    raise SystemExit(93)
def identity(value):
    return (int(value.st_dev), int(value.st_ino), int(value.st_size),
            int(value.st_mtime_ns), int(stat.S_IFMT(value.st_mode)),
            int(getattr(value, 'st_file_attributes', 0)))
def hold(raw_path, expected, limit):
    path = raw_path.resolve(strict=True)
    before = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode)
            or int(getattr(before, 'st_file_attributes', 0)) & 0x400
            or int(before.st_size) <= 0 or int(before.st_size) > limit):
        raise SystemExit(94)
    descriptor = os.open(path, os.O_RDONLY | int(getattr(os, 'O_BINARY', 0)))
    try:
        opened = os.fstat(descriptor)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        terminal = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = os.stat(path, follow_symlinks=False)
    source = b''.join(chunks)
    if (identity(before) != identity(opened)
            or identity(opened) != identity(terminal)
            or identity(terminal) != identity(after)
            or hashlib.sha256(source).hexdigest() != expected):
        raise SystemExit(95)
    return path, source
builder_path, builder_source = hold(builder_raw, builder_expected,
                                      64 * 1024 * 1024)
runner_path, runner_source = hold(runner_raw, runner_expected,
                                  64 * 1024 * 1024)
python_path, python_source = hold(Path(sys.executable), python_expected,
                                  256 * 1024 * 1024)
if (python_path != Path(sys.executable).resolve(strict=True)
        or builder_path == runner_path):
    raise SystemExit(96)
package = types.ModuleType('scripts')
package.__path__ = []
package.__package__ = 'scripts'
builder_module = types.ModuleType(
    'scripts.build_captured_paper_benchmark_authority')
builder_module.__file__ = str(builder_path)
builder_module.__package__ = 'scripts'
builder_module.__cached__ = None
sys.modules['scripts'] = package
sys.modules[builder_module.__name__] = builder_module
setattr(package, 'build_captured_paper_benchmark_authority', builder_module)
builder_module.__dict__.update({
    '__chili_held_builder_source_sha256__': builder_expected,
    '__chili_held_python_executable_sha256__': python_expected})
exec(compile(builder_source, str(builder_path), 'exec', dont_inherit=True),
     builder_module.__dict__, builder_module.__dict__)
sys.argv[:] = [str(runner_path), *arguments]
runner_scope = {'__name__': '__main__', '__file__': str(runner_path),
                '__package__': None, '__cached__': None,
                '__builtins__': __builtins__,
                '__chili_held_builder_module__': builder_module,
                '__chili_held_builder_source_sha256__': builder_expected,
                '__chili_held_runner_source_sha256__': runner_expected,
                '__chili_held_python_executable_sha256__': python_expected}
exec(compile(runner_source, str(runner_path), 'exec', dont_inherit=True),
     runner_scope, runner_scope)
""".strip()

_injected_authority = globals().get("__chili_held_builder_module__")
if _injected_authority is not None:
    authority = _injected_authority
else:
    from scripts import build_captured_paper_benchmark_authority as authority


EXECUTION_RECEIPT_SCHEMA_VERSION = (
    "chili.captured-paper-benchmark-execution-receipt.v2"
)
EXECUTION_CLAIM_SCHEMA_VERSION = (
    "chili.captured-paper-benchmark-execution-claim.v1"
)
UTC = timezone.utc
_MAX_AUTHORITY_BYTES = 4 * 1024 * 1024
_MAX_STDOUT_BYTES = 64 * 1024 * 1024
_MAX_STDERR_BYTES = 1024 * 1024
_MAX_REPORT_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _close_windows_handle(handle: int | None) -> bool:
    if handle is None or os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return bool(kernel32.CloseHandle(ctypes.c_void_p(handle)))


def _create_kill_on_close_job() -> int | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    handle = int(job)
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
        ctypes.c_void_p(handle), 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        error = ctypes.get_last_error()
        _close_windows_handle(handle)
        raise OSError(error, "SetInformationJobObject failed")
    return handle


class _WindowsJobProcess:
    def __init__(self, *, process_handle: int, process_id: int, stdout: Any, stderr: Any) -> None:
        self._process_handle = process_handle
        self.pid = process_id
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        import ctypes
        from ctypes import wintypes

        code = wintypes.DWORD()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        if not kernel32.GetExitCodeProcess(
            ctypes.c_void_p(self._process_handle), ctypes.byref(code)
        ):
            raise OSError(ctypes.get_last_error(), "GetExitCodeProcess failed")
        if int(code.value) == 259:  # STILL_ACTIVE
            return None
        self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        import ctypes
        from ctypes import wintypes

        milliseconds = 0xFFFFFFFF if timeout is None else max(0, min(0xFFFFFFFE, int(timeout * 1000)))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        result = int(kernel32.WaitForSingleObject(ctypes.c_void_p(self._process_handle), milliseconds))
        if result == 0x00000102:
            raise subprocess.TimeoutExpired(self.pid, timeout)
        if result != 0:
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
        observed = self.poll()
        if observed is None:
            raise OSError("process remained active after wait")
        return observed

    def kill(self) -> None:
        if self.poll() is not None:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        if not kernel32.TerminateProcess(ctypes.c_void_p(self._process_handle), 1):
            raise OSError(ctypes.get_last_error(), "TerminateProcess failed")

    def close(self) -> bool:
        result = _close_windows_handle(self._process_handle)
        if result:
            self._process_handle = 0
        return result


def _spawn_windows_job_process(
    *, argv: Sequence[str], cwd: Path, environment: Mapping[str, str]
) -> tuple[_WindowsJobProcess, int]:
    """Create the child atomically inside one kill-on-close Job.

    ``PROC_THREAD_ATTRIBUTE_JOB_LIST`` associates the process during
    ``CreateProcessW`` itself; there is no post-create assignment window.
    ``HANDLE_LIST`` restricts inheritance to the three explicit stdio handles.
    """

    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _StartupInfoW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
            ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _StartupInfoExW(ctypes.Structure):
        _fields_ = [("StartupInfo", _StartupInfoW), ("lpAttributeList", ctypes.c_void_p)]

    class _ProcessInformation(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(_SecurityAttributes), wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD
    ]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfoExW), ctypes.POINTER(_ProcessInformation),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    job_handle = _create_kill_on_close_job()
    if job_handle is None:
        raise OSError("Windows Job handle is unavailable")
    handles: list[int] = []
    stdout_file: Any = None
    stderr_file: Any = None
    process_info = _ProcessInformation()
    attribute_list: Any = None
    try:
        security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)

        def pipe() -> tuple[int, int]:
            read = wintypes.HANDLE()
            write = wintypes.HANDLE()
            if not kernel32.CreatePipe(
                ctypes.byref(read), ctypes.byref(write), ctypes.byref(security), 0
            ):
                raise OSError(ctypes.get_last_error(), "CreatePipe failed")
            read_value, write_value = int(read.value), int(write.value)
            handles.extend((read_value, write_value))
            if not kernel32.SetHandleInformation(ctypes.c_void_p(read_value), 1, 0):
                raise OSError(ctypes.get_last_error(), "SetHandleInformation failed")
            return read_value, write_value

        stdout_read, stdout_write = pipe()
        stderr_read, stderr_write = pipe()
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(_SecurityAttributes), wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        stdin_handle_raw = kernel32.CreateFileW(
            "NUL", 0x80000000, 0x00000003, ctypes.byref(security), 3, 0x80, None
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if not stdin_handle_raw or int(stdin_handle_raw) == invalid_handle:
            raise OSError(ctypes.get_last_error(), "opening NUL failed")
        stdin_handle = int(stdin_handle_raw)
        handles.append(stdin_handle)

        size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if size.value <= 0:
            raise OSError(ctypes.get_last_error(), "attribute-list sizing failed")
        attribute_storage = ctypes.create_string_buffer(size.value)
        attribute_list = ctypes.cast(attribute_storage, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(
            attribute_list, 2, 0, ctypes.byref(size)
        ):
            raise OSError(ctypes.get_last_error(), "attribute-list initialization failed")
        inherited = (wintypes.HANDLE * 3)(stdin_handle, stdout_write, stderr_write)
        jobs = (wintypes.HANDLE * 1)(job_handle)
        if not kernel32.UpdateProcThreadAttribute(
            attribute_list, 0, ctypes.c_size_t(0x00020002),
            ctypes.byref(inherited), ctypes.sizeof(inherited), None, None
        ):
            raise OSError(ctypes.get_last_error(), "HANDLE_LIST binding failed")
        if not kernel32.UpdateProcThreadAttribute(
            attribute_list, 0, ctypes.c_size_t(0x0002000D),
            ctypes.byref(jobs), ctypes.sizeof(jobs), None, None
        ):
            raise OSError(ctypes.get_last_error(), "JOB_LIST binding failed")

        startup = _StartupInfoExW()
        startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoExW)
        startup.StartupInfo.dwFlags = 0x00000101  # USESHOWWINDOW | USESTDHANDLES
        startup.StartupInfo.wShowWindow = 0
        startup.StartupInfo.hStdInput = stdin_handle
        startup.StartupInfo.hStdOutput = stdout_write
        startup.StartupInfo.hStdError = stderr_write
        startup.lpAttributeList = attribute_list
        command = subprocess.list2cmdline(list(argv))
        if not command or len(command) >= 32767:
            raise OSError("benchmark command line is invalid or oversized")
        command_buffer = ctypes.create_unicode_buffer(command)
        environment_block = "\0".join(
            f"{key}={value}" for key, value in sorted(environment.items(), key=lambda item: item[0].casefold())
        ) + "\0\0"
        environment_buffer = ctypes.create_unicode_buffer(environment_block)
        created = kernel32.CreateProcessW(
            str(argv[0]), command_buffer, None, None, True,
            0x00080000 | 0x00000400 | 0x08000000,
            ctypes.cast(environment_buffer, ctypes.c_void_p), str(cwd),
            ctypes.byref(startup), ctypes.byref(process_info)
        )
        if not created:
            raise OSError(ctypes.get_last_error(), "CreateProcessW failed")
        _close_windows_handle(int(process_info.hThread))
        process_info.hThread = None
        for child_handle in (stdin_handle, stdout_write, stderr_write):
            _close_windows_handle(child_handle)
            handles.remove(child_handle)
        stdout_fd = msvcrt.open_osfhandle(stdout_read, os.O_RDONLY | int(getattr(os, "O_BINARY", 0)))
        handles.remove(stdout_read)
        stdout_file = os.fdopen(stdout_fd, "rb", buffering=0)
        stderr_fd = msvcrt.open_osfhandle(stderr_read, os.O_RDONLY | int(getattr(os, "O_BINARY", 0)))
        handles.remove(stderr_read)
        stderr_file = os.fdopen(stderr_fd, "rb", buffering=0)
        process = _WindowsJobProcess(
            process_handle=int(process_info.hProcess), process_id=int(process_info.dwProcessId),
            stdout=stdout_file, stderr=stderr_file,
        )
        process_info.hProcess = None
        return process, job_handle
    except BaseException:
        _close_windows_handle(job_handle)
        if process_info.hProcess:
            _close_windows_handle(int(process_info.hProcess))
        if process_info.hThread:
            _close_windows_handle(int(process_info.hThread))
        if stdout_file is not None:
            stdout_file.close()
        if stderr_file is not None:
            stderr_file.close()
        raise
    finally:
        if attribute_list is not None:
            kernel32.DeleteProcThreadAttributeList(attribute_list)
        for handle in handles:
            _close_windows_handle(handle)


class CapturedPaperBenchmarkExecutionError(RuntimeError):
    """Stable fail-closed execution error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


def _fail(code: str, message: str) -> CapturedPaperBenchmarkExecutionError:
    return CapturedPaperBenchmarkExecutionError(code, message)


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
        raise _fail("NON_CANONICAL_VALUE", "execution value is not canonical JSON") from exc


def _strict_json(raw: bytes, *, field: str) -> Mapping[str, Any]:
    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise _fail("JSON_INVALID", f"{field} is not strict JSON") from exc
    if not isinstance(value, dict) or raw != _canonical_json_bytes(value):
        raise _fail("JSON_NON_CANONICAL", f"{field} is not canonical JSON")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    if set(value) != expected:
        raise _fail("SCHEMA_INVALID", f"{field} keys are not exact")


def _sha(value: Any, *, field: str) -> str:
    candidate = str(value or "").lower()
    if _SHA256_RE.fullmatch(candidate) is None:
        raise _fail("SHA256_INVALID", f"{field} is not one lowercase SHA-256")
    return candidate


def _read_content_addressed_json(
    path_value: str | Path,
    expected_sha256: str,
    *,
    field: str,
    max_bytes: int = _MAX_AUTHORITY_BYTES,
) -> tuple[Path, bytes, Mapping[str, Any]]:
    try:
        path = authority._canonical_file(path_value, field=field)
        expected = _sha(expected_sha256, field=f"{field}.sha256")
        if (
            path.name.casefold() != f"{expected}.json"
            or path.parent.name.casefold() != expected[:2]
        ):
            raise _fail("CONTENT_ADDRESS_INVALID", f"{field} path is not content addressed")
        pin = authority._pin_file(path, field=field, max_bytes=max_bytes)
        if pin.sha256 != expected:
            raise _fail("HASH_MISMATCH", f"{field} differs from its expected hash")
        raw = authority._read_pinned_bytes(pin, field=field)
    except CapturedPaperBenchmarkExecutionError:
        raise
    except authority.CapturedPaperBenchmarkAuthorityError as exc:
        raise _fail("AUTHORITY_PATH_INVALID", f"{field} path or bytes are invalid") from exc
    return path, raw, _strict_json(raw, field=field)


def _load_authority(
    *, receipt_path: str | Path, receipt_sha256: str
) -> tuple[Path, str, Mapping[str, Any], Path, str, Mapping[str, Any]]:
    receipt_path_resolved, _receipt_raw, receipt = _read_content_addressed_json(
        receipt_path, receipt_sha256, field="launch_receipt"
    )
    _exact_keys(
        receipt,
        {
            "account_scope",
            "authority_programs",
            "argv_is_shell_string",
            "authority_mode",
            "benchmark_argv",
            "benchmark_completed",
            "benchmark_report",
            "candidate_root",
            "execution_context",
            "expected_git_commit",
            "git",
            "held_loader_sha256",
            "invoked",
            "manifest",
            "output",
            "posture",
            "python",
            "python_dependency_root",
            "schema_version",
            "source_roster",
            "source_roster_sha256",
        },
        field="launch_receipt",
    )
    if receipt.get("schema_version") != authority.RECEIPT_SCHEMA_VERSION:
        raise _fail("RECEIPT_SCHEMA_INVALID", "launch receipt schema is unsupported")
    manifest_reference = receipt.get("manifest")
    if not isinstance(manifest_reference, Mapping):
        raise _fail("RECEIPT_SCHEMA_INVALID", "manifest reference is malformed")
    _exact_keys(manifest_reference, {"path", "sha256"}, field="manifest_reference")
    manifest_path, _manifest_raw, manifest = _read_content_addressed_json(
        str(manifest_reference.get("path") or ""),
        str(manifest_reference.get("sha256") or ""),
        field="manifest",
    )
    _exact_keys(
        manifest,
        {
            "account_scope",
            "authority_programs",
            "authority_mode",
            "benchmark_arguments",
            "candidate_root",
            "execution_context",
            "expected_benchmark_schema_version",
            "expected_git_commit",
            "git",
            "held_loader",
            "output",
            "posture",
            "python",
            "python_dependency_root",
            "schema_version",
            "source_roster",
            "source_roster_sha256",
        },
        field="manifest",
    )
    if manifest.get("schema_version") != authority.MANIFEST_SCHEMA_VERSION:
        raise _fail("MANIFEST_SCHEMA_INVALID", "benchmark authority manifest is unsupported")
    return (
        receipt_path_resolved,
        _sha(receipt_sha256, field="launch_receipt.sha256"),
        receipt,
        manifest_path,
        _sha(manifest_reference.get("sha256"), field="manifest.sha256"),
        manifest,
    )


def _mapping(value: Any, *, field: str, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail("SCHEMA_INVALID", f"{field} must be an object")
    _exact_keys(value, keys, field=field)
    return value


def _validate_static_bindings(
    *,
    receipt: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_path: Path,
    manifest_sha256: str,
    require_empty_output: bool = True,
) -> tuple[Path, Path, tuple[str, ...], Mapping[str, str], int]:
    if not (
        receipt.get("account_scope") == manifest.get("account_scope") == authority.ACCOUNT_SCOPE
        and receipt.get("authority_mode")
        == manifest.get("authority_mode")
        == authority.AUTHORITY_MODE
        and receipt.get("candidate_root") == manifest.get("candidate_root")
        and receipt.get("expected_git_commit") == manifest.get("expected_git_commit")
        and receipt.get("source_roster") == manifest.get("source_roster")
        and receipt.get("source_roster_sha256") == manifest.get("source_roster_sha256")
        and receipt.get("execution_context") == manifest.get("execution_context")
        and receipt.get("git") == manifest.get("git")
        and receipt.get("authority_programs") == manifest.get("authority_programs")
        and receipt.get("argv_is_shell_string") is False
        and receipt.get("invoked") is False
        and receipt.get("benchmark_completed") is False
        and receipt.get("benchmark_report") is None
        and manifest.get("expected_benchmark_schema_version")
        == authority.EXPECTED_BENCHMARK_SCHEMA_VERSION
    ):
        raise _fail("AUTHORITY_BINDING_INVALID", "receipt and manifest bindings differ")
    expected_manifest_ref = {"path": str(manifest_path), "sha256": manifest_sha256}
    if receipt.get("manifest") != expected_manifest_ref:
        raise _fail("AUTHORITY_BINDING_INVALID", "manifest reference differs")

    programs = _mapping(
        manifest.get("authority_programs"),
        field="authority_programs",
        keys={"builder", "runner"},
    )
    for role in ("builder", "runner"):
        row = _mapping(
            programs.get(role),
            field=f"authority_programs.{role}",
            keys={"path", "sha256"},
        )
        expected_path = (
            Path(str(manifest.get("candidate_root") or ""))
            / authority.AUTHORITY_PROGRAM_PATHS[role]
        ).resolve(strict=True)
        path = authority._canonical_file(
            str(row.get("path") or ""), field=f"authority_programs.{role}"
        )
        pin = authority._pin_file(
            path, field=f"authority_programs.{role}", max_bytes=authority._MAX_SOURCE_BYTES
        )
        if path != expected_path or pin.sha256 != _sha(
            row.get("sha256"), field=f"authority_programs.{role}.sha256"
        ):
            raise _fail("AUTHORITY_PROGRAM_DRIFT", f"{role} source authority changed")
    held_runner = globals().get("__chili_held_runner_source_sha256__")
    held_builder = globals().get("__chili_held_builder_source_sha256__")
    held_python = globals().get("__chili_held_python_executable_sha256__")
    if held_runner is not None and held_runner != programs["runner"]["sha256"]:
        raise _fail("HELD_RUNNER_AUTHORITY_MISMATCH", "held runner differs from manifest")
    if held_builder is not None and held_builder != programs["builder"]["sha256"]:
        raise _fail("HELD_BUILDER_AUTHORITY_MISMATCH", "held builder differs from manifest")
    python_projection = manifest.get("python")
    if held_python is not None and (
        not isinstance(python_projection, Mapping)
        or held_python != python_projection.get("executable_sha256")
    ):
        raise _fail("HELD_PYTHON_AUTHORITY_MISMATCH", "held Python differs from manifest")

    expected_manifest_posture = {
        "benchmark_execution_authorized": True,
        "broker_contact_authorized": False,
        "database_access_authorized": False,
        "host_activation_authorized": False,
        "live_cash_authorized": False,
        "order_submission_authorized": False,
        "provider_contact_authorized": False,
    }
    expected_receipt_posture = {
        "benchmark_output_written": False,
        "broker_contacted": False,
        "cutover_performed": False,
        "database_accessed": False,
        "host_activation_performed": False,
        "live_cash_authorized": False,
        "orders_submitted": False,
        "provider_contacted": False,
        "task_scheduler_mutated": False,
    }
    if (
        manifest.get("posture") != expected_manifest_posture
        or receipt.get("posture") != expected_receipt_posture
    ):
        raise _fail("POSTURE_INVALID", "launch authority posture is not diagnostic-only")

    candidate = authority._canonical_directory(
        str(manifest.get("candidate_root") or ""), field="candidate_root"
    )
    git_authority = _mapping(
        manifest.get("git"), field="git", keys={"executable_path", "executable_sha256"}
    )
    git_path = authority._canonical_file(
        str(git_authority.get("executable_path") or ""), field="git_executable"
    )
    git_pin = authority._pin_file(
        git_path, field="git_executable", max_bytes=authority._MAX_EXECUTABLE_BYTES
    )
    if git_pin.sha256 != _sha(
        git_authority.get("executable_sha256"), field="git.executable_sha256"
    ):
        raise _fail("GIT_DRIFT", "Git executable differs from authority")
    output_manifest = _mapping(
        manifest.get("output"),
        field="manifest.output",
        keys={
            "root",
            "root_identity",
            "storage_volume_identity",
            "storage_volume_identity_sha256",
        },
    )
    output_receipt = _mapping(
        receipt.get("output"),
        field="receipt.output",
        keys={"root", "root_identity", "storage_volume_identity_sha256"},
    )
    output = authority._canonical_directory(
        str(output_manifest.get("root") or ""), field="benchmark_output_root"
    )
    current_output_identity = dict(authority._directory_identity(output))
    expected_output_identity = output_manifest.get("root_identity")
    stable_identity_keys = {"st_dev", "st_ino", "st_mode"}
    identity_matches = (
        current_output_identity == expected_output_identity
        if require_empty_output
        else isinstance(expected_output_identity, Mapping)
        and {
            key: current_output_identity.get(key) for key in stable_identity_keys
        }
        == {key: expected_output_identity.get(key) for key in stable_identity_keys}
    )
    if (
        output_receipt.get("root") != str(output)
        or output_receipt.get("root_identity") != output_manifest.get("root_identity")
        or output_receipt.get("storage_volume_identity_sha256")
        != output_manifest.get("storage_volume_identity_sha256")
        or not identity_matches
    ):
        raise _fail("OUTPUT_ROOT_DRIFT", "benchmark output root is not exact and empty")
    if require_empty_output and any(output.iterdir()):
        raise _fail("OUTPUT_ROOT_DRIFT", "benchmark output root is not exact and empty")
    _volume, volume_sha = authority._storage_volume_identity(output)
    if volume_sha != output_manifest.get("storage_volume_identity_sha256"):
        raise _fail("OUTPUT_VOLUME_DRIFT", "benchmark output storage volume changed")

    source_rows = manifest.get("source_roster")
    if not isinstance(source_rows, list) or receipt.get("source_roster") != source_rows:
        raise _fail("SOURCE_ROSTER_INVALID", "source roster is malformed")
    expected_roles = sorted(authority.SOURCE_ROLE_PATHS)
    observed_roles: list[str] = []
    source_hashes: dict[str, str] = {}
    for row in source_rows:
        if not isinstance(row, Mapping):
            raise _fail("SOURCE_ROSTER_INVALID", "source row is malformed")
        _exact_keys(row, {"path", "role", "sha256"}, field="source_row")
        role = str(row.get("role") or "")
        if role not in authority.SOURCE_ROLE_PATHS:
            raise _fail("SOURCE_ROSTER_INVALID", "source role is unknown")
        expected_path = (candidate / authority.SOURCE_ROLE_PATHS[role]).resolve(strict=True)
        path = authority._canonical_file(str(row.get("path") or ""), field=f"source.{role}")
        digest = _sha(row.get("sha256"), field=f"source.{role}.sha256")
        pin = authority._pin_file(path, field=f"source.{role}", max_bytes=authority._MAX_SOURCE_BYTES)
        if path != expected_path or pin.sha256 != digest:
            raise _fail("SOURCE_DRIFT", f"source.{role} differs from authority")
        observed_roles.append(role)
        source_hashes[role] = digest
    if observed_roles != expected_roles or authority._sha256_bytes(
        authority._canonical_json_bytes(source_rows)
    ) != manifest.get("source_roster_sha256"):
        raise _fail("SOURCE_ROSTER_INVALID", "source roster order or digest differs")

    python_manifest = _mapping(
        manifest.get("python"),
        field="manifest.python",
        keys={
            "executable_path",
            "executable_sha256",
            "implementation",
            "isolation_flags",
            "version",
        },
    )
    python_receipt = _mapping(
        receipt.get("python"),
        field="receipt.python",
        keys={"executable_path", "executable_sha256"},
    )
    executable = authority._canonical_file(
        str(python_manifest.get("executable_path") or ""), field="python_executable"
    )
    python_sha = _sha(
        python_manifest.get("executable_sha256"), field="python.executable_sha256"
    )
    python_pin = authority._pin_file(
        executable, field="python_executable", max_bytes=authority._MAX_EXECUTABLE_BYTES
    )
    if not (
        python_pin.sha256 == python_sha
        and python_receipt
        == {"executable_path": str(executable), "executable_sha256": python_sha}
        and python_manifest.get("implementation") == "cpython"
        and python_manifest.get("version") == [3, 11]
        and python_manifest.get("isolation_flags") == ["-I", "-S", "-B"]
    ):
        raise _fail("PYTHON_DRIFT", "Python executable authority changed")
    try:
        authority._probe_python311(executable)
        authority._recheck_pin(python_pin, field="python_executable")
    except authority.CapturedPaperBenchmarkAuthorityError as exc:
        raise _fail("PYTHON_DRIFT", "Python executable verification failed") from exc

    dependency_manifest = _mapping(
        manifest.get("python_dependency_root"),
        field="manifest.python_dependency_root",
        keys={
            "identity",
            "identity_sha256",
            "path",
            "required_distributions",
            "tree_sha256",
        },
    )
    dependency_receipt = _mapping(
        receipt.get("python_dependency_root"),
        field="receipt.python_dependency_root",
        keys={"identity_sha256", "path", "tree_sha256"},
    )
    dependency = authority._canonical_directory(
        str(dependency_manifest.get("path") or ""), field="python_dependency_root"
    )
    dependency_body, dependency_tree = authority._dependency_identity(
        root=dependency,
        python_executable=executable,
        python_executable_sha256=python_sha,
    )
    dependency_sha = hashlib.sha256(
        authority._canonical_json_bytes(dict(dependency_body))
    ).hexdigest()
    distributions = authority._dependency_distributions(dependency, dependency_tree)
    if not (
        dependency_manifest.get("identity") == dict(dependency_body)
        and dependency_manifest.get("identity_sha256") == dependency_sha
        and dependency_manifest.get("tree_sha256") == dependency_tree["tree_sha256"]
        and dependency_manifest.get("required_distributions") == distributions
        and dependency_receipt
        == {
            "identity_sha256": dependency_sha,
            "path": str(dependency),
            "tree_sha256": dependency_tree["tree_sha256"],
        }
    ):
        raise _fail("DEPENDENCY_DRIFT", "sealed dependency authority changed")

    context = _mapping(
        manifest.get("execution_context"),
        field="execution_context",
        keys={
            "cwd",
            "environment",
            "environment_sha256",
            "shell",
            "stderr",
            "stdin",
            "stdout",
            "timeout_seconds",
        },
    )
    environment = context.get("environment")
    if not isinstance(environment, Mapping) or any(
        type(key) is not str or type(value) is not str
        for key, value in environment.items()
    ):
        raise _fail("EXECUTION_CONTEXT_INVALID", "execution environment is malformed")
    safe_environment = dict(environment)
    expected_environment = dict(authority._sanitized_execution_environment())
    if not (
        context.get("cwd") == str(candidate)
        and safe_environment == expected_environment
        and context.get("environment_sha256")
        == hashlib.sha256(authority._canonical_json_bytes(safe_environment)).hexdigest()
        and context.get("shell") is False
        and context.get("stdin") == "devnull"
        and context.get("stdout") == "bounded_binary_pipe_64mib"
        and context.get("stderr") == "bounded_binary_pipe_1mib"
        and type(context.get("timeout_seconds")) is int
        and 1 <= int(context["timeout_seconds"]) <= authority._BENCHMARK_TIMEOUT_SECONDS
    ):
        raise _fail("EXECUTION_CONTEXT_INVALID", "execution context differs from authority")

    argv_raw = receipt.get("benchmark_argv")
    arguments_raw = manifest.get("benchmark_arguments")
    if not isinstance(argv_raw, list) or not isinstance(arguments_raw, list) or any(
        type(value) is not str for value in [*argv_raw, *arguments_raw]
    ):
        raise _fail("ARGV_INVALID", "authorized benchmark argv is malformed")
    argv = tuple(argv_raw)
    loader = str(argv[5]) if len(argv) > 5 else ""
    if hashlib.sha256(loader.encode("utf-8")).hexdigest() != receipt.get(
        "held_loader_sha256"
    ) or manifest.get("held_loader") != {
        "sha256": receipt.get("held_loader_sha256"),
        "source_role": "pressure_probe",
        "variable": authority.HELD_LOADER_VARIABLE,
    }:
        raise _fail("LOADER_DRIFT", "held loader differs from authority")
    bootstrap: list[str] = []
    for role in authority.LOADER_ROLE_ORDER:
        bootstrap.extend(
            (
                str((candidate / authority.SOURCE_ROLE_PATHS[role]).resolve(strict=True)),
                source_hashes[role],
            )
        )
    bootstrap.extend(
        (str(dependency), dependency_sha, str(manifest_path), manifest_sha256, python_sha)
    )
    expected_argv = (
        str(executable),
        "-I",
        "-S",
        "-B",
        "-c",
        loader,
        *bootstrap,
        "--",
        *map(str, arguments_raw),
    )
    if argv != expected_argv:
        raise _fail("ARGV_INVALID", "receipt argv is not derivable from manifest")

    try:
        authority._verify_git_worktree(
            git_pin=git_pin,
            candidate_root=candidate,
            expected_commit=str(manifest.get("expected_git_commit") or ""),
            expected_tracked_paths=tuple(
                authority._expected_tracked_paths()
            ),
        )
    except authority.CapturedPaperBenchmarkAuthorityError as exc:
        raise _fail("GIT_DRIFT", "candidate Git authority changed") from exc
    return candidate, output, argv, safe_environment, int(context["timeout_seconds"])


def _bounded_run(
    *,
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> tuple[int, bytes, bytes]:
    process: Any = None
    job_handle: int | None = None
    job_lock = threading.Lock()
    job_close_failed = threading.Event()
    threads: tuple[threading.Thread, ...] = ()
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    read_errors: list[BaseException] = []

    def close_job_once() -> bool:
        nonlocal job_handle
        with job_lock:
            handle = job_handle
            job_handle = None
            if handle is None:
                return not job_close_failed.is_set()
            if _close_windows_handle(handle):
                return True
            job_close_failed.set()
            return False

    def read_pipe(name: str, pipe: Any, limit: int) -> None:
        try:
            while True:
                chunk = pipe.read(64 * 1024)
                if not chunk:
                    return
                remaining = limit - len(outputs[name])
                if len(chunk) > remaining:
                    outputs[name].extend(chunk[: max(0, remaining)])
                    overflow.set()
                    close_job_once()
                    if process is not None and process.poll() is None:
                        process.kill()
                    return
                outputs[name].extend(chunk)
        except BaseException as exc:  # fail closed across pipe/platform errors
            read_errors.append(exc)
            close_job_once()
            if process is not None and process.poll() is None:
                process.kill()

    try:
        if os.name == "nt":
            process, job_handle = _spawn_windows_job_process(
                argv=argv, cwd=cwd, environment=environment
            )
        else:
            process = subprocess.Popen(
                list(argv), cwd=cwd, env=dict(environment), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                close_fds=True, bufsize=0,
            )
        if process.stdout is None or process.stderr is None:
            raise OSError("private process pipes were not created")
        threads = (
            threading.Thread(
                target=read_pipe,
                args=("stdout", process.stdout, _MAX_STDOUT_BYTES),
                name="chili-benchmark-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=read_pipe,
                args=("stderr", process.stderr, _MAX_STDERR_BYTES),
                name="chili-benchmark-stderr",
                daemon=True,
            ),
        )
        for thread in threads:
            thread.start()
        process.wait(timeout=timeout_seconds)
        # Closing the Job after the exact root exits guarantees any descendant
        # that inherited a pipe is killed before readers are joined.
        if not close_job_once():
            raise OSError("Windows Job handle could not be closed")
        for thread in threads:
            thread.join(timeout=5.0)
        if any(thread.is_alive() for thread in threads):
            process.kill()
            process.wait(timeout=5.0)
            raise OSError("private process pipe reader did not terminate")
    except subprocess.TimeoutExpired as exc:
        close_job_once()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5.0)
        raise _fail("BENCHMARK_TIMEOUT", "benchmark exceeded its authorized timeout") from exc
    except OSError as exc:
        close_job_once()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5.0)
        raise _fail("BENCHMARK_LAUNCH_FAILED", "exact benchmark process could not start") from exc
    finally:
        close_job_once()
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=5.0)
        if isinstance(process, _WindowsJobProcess) and not process.close():
            job_close_failed.set()
    if overflow.is_set():
        raise _fail("BENCHMARK_OUTPUT_OVERSIZED", "benchmark output exceeded its bound")
    if read_errors:
        raise _fail("BENCHMARK_PIPE_FAILED", "benchmark pipe read failed")
    if job_close_failed.is_set():
        raise _fail("BENCHMARK_JOB_CLEANUP_FAILED", "benchmark Job cleanup did not complete")
    return int(process.returncode), bytes(outputs["stdout"]), bytes(outputs["stderr"])


def _report_path(output_root: Path, report: Mapping[str, Any], report_sha: str) -> Path:
    output = report.get("output")
    if not isinstance(output, Mapping) or set(output) != {
        "directory",
        "report_artifact_layout",
        "retained",
        "safe_cleanup_verified",
    }:
        raise _fail("BENCHMARK_REPORT_INVALID", "benchmark output projection is malformed")
    if not (
        output.get("retained") is True
        and output.get("safe_cleanup_verified") is False
        and output.get("report_artifact_layout")
        == "reports/<canonical-sha256>.json_when_retained"
    ):
        raise _fail("BENCHMARK_REPORT_INVALID", "benchmark report was not retained exactly")
    try:
        directory = authority._canonical_directory(
            str(output.get("directory") or ""), field="benchmark_owned_directory"
        )
    except authority.CapturedPaperBenchmarkAuthorityError as exc:
        raise _fail(
            "BENCHMARK_REPORT_PATH_INVALID", "owned report directory is unavailable"
        ) from exc
    if directory.parent != output_root or not directory.name.startswith(
        "chili-replay-capture-benchmark-"
    ):
        raise _fail("BENCHMARK_REPORT_PATH_INVALID", "owned report directory escaped output root")
    suffix = directory.name.removeprefix("chili-replay-capture-benchmark-")
    owner_token, separator, _random_tail = suffix.partition("-")
    if (
        not separator
        or re.fullmatch(r"[0-9a-f]{32}", owner_token) is None
        or not _random_tail
    ):
        raise _fail("BENCHMARK_REPORT_PATH_INVALID", "owned directory token is invalid")
    try:
        marker_path = authority._canonical_file(
            directory / ".chili-replay-capture-benchmark-owner.json",
            field="benchmark_ownership_marker",
        )
    except authority.CapturedPaperBenchmarkAuthorityError as exc:
        raise _fail(
            "BENCHMARK_REPORT_PATH_INVALID", "ownership marker is unavailable"
        ) from exc
    marker_raw = marker_path.read_bytes()
    if not marker_raw.endswith(b"\n") or marker_raw.count(b"\n") != 1:
        raise _fail("BENCHMARK_OWNERSHIP_INVALID", "ownership marker is not one JSON line")
    marker = _strict_json(marker_raw[:-1], field="benchmark_ownership_marker")
    if marker != {
        "benchmark_schema_version": report.get("benchmark_schema_version"),
        "directory": str(directory),
        "output_root": str(output_root),
        "owner_token": owner_token,
    }:
        raise _fail("BENCHMARK_OWNERSHIP_INVALID", "ownership marker binding differs")
    try:
        return authority._canonical_file(
            directory / "reports" / f"{report_sha}.json", field="benchmark_report"
        )
    except authority.CapturedPaperBenchmarkAuthorityError as exc:
        raise _fail(
            "BENCHMARK_REPORT_PATH_INVALID", "retained benchmark report is unavailable"
        ) from exc


def _publish_execution_claim(
    *,
    authority_root: Path,
    launch_receipt_path: Path,
    launch_receipt_sha256: str,
    manifest_path: Path,
    manifest_sha256: str,
    started_at: datetime,
) -> tuple[Path, str]:
    claim = {
        "launch_receipt": {
            "path": str(launch_receipt_path),
            "sha256": launch_receipt_sha256,
        },
        "manifest": {"path": str(manifest_path), "sha256": manifest_sha256},
        "schema_version": EXECUTION_CLAIM_SCHEMA_VERSION,
        "started_at_utc": started_at.isoformat().replace("+00:00", "Z"),
    }
    raw = _canonical_json_bytes(claim)
    claim_sha = hashlib.sha256(raw).hexdigest()
    parent = authority_root / "authority" / "execution-claim"
    try:
        authority._ensure_private_directory(parent, root=authority_root)
    except authority.CapturedPaperBenchmarkAuthorityError as exc:
        raise _fail("EXECUTION_CLAIM_FAILED", "execution claim directory is invalid") from exc
    target = parent / f"{launch_receipt_sha256}.json"
    try:
        with target.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        authority._fsync_directory(parent)
    except FileExistsError as exc:
        raise _fail("LAUNCH_RECEIPT_ALREADY_INVOKED", "launch receipt is one-shot") from exc
    except (OSError, authority.CapturedPaperBenchmarkAuthorityError) as exc:
        if os.path.lexists(target):
            try:
                target.unlink()
                authority._fsync_directory(parent)
            except (OSError, authority.CapturedPaperBenchmarkAuthorityError):
                pass
        raise _fail("EXECUTION_CLAIM_FAILED", "execution claim could not be published") from exc
    if target.read_bytes() != raw:
        raise _fail("EXECUTION_CLAIM_FAILED", "execution claim did not reread exactly")
    return target.resolve(strict=True), claim_sha


def _run_captured_paper_benchmark_authority(
    *,
    receipt_path: str | Path,
    receipt_sha256: str,
    executor: Any = _bounded_run,
    clock: Any = lambda: datetime.now(UTC),
) -> tuple[Path, str]:
    """Execute exactly one launch receipt and publish its terminal attestation."""

    (
        launch_receipt_path,
        launch_receipt_sha,
        launch_receipt,
        manifest_path,
        manifest_sha,
        manifest,
    ) = _load_authority(receipt_path=receipt_path, receipt_sha256=receipt_sha256)
    candidate, output_root, argv, environment, timeout_seconds = _validate_static_bindings(
        receipt=launch_receipt,
        manifest=manifest,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
    )
    started = clock()
    if not isinstance(started, datetime) or started.tzinfo is None:
        raise _fail("CLOCK_INVALID", "execution clock must return aware datetime")
    started = started.astimezone(UTC)
    authority_root = launch_receipt_path.parents[3]
    claim_path, claim_sha = _publish_execution_claim(
        authority_root=authority_root,
        launch_receipt_path=launch_receipt_path,
        launch_receipt_sha256=launch_receipt_sha,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        started_at=started,
    )
    # Close the validation/claim race: verify all immutable launch inputs and
    # the still-empty output root again after the one-shot claim, immediately
    # before passing the exact receipt argv to the process API.
    candidate, output_root, argv, environment, timeout_seconds = _validate_static_bindings(
        receipt=launch_receipt,
        manifest=manifest,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
    )
    exit_code, stdout, stderr = executor(
        argv=argv,
        cwd=candidate,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )
    completed = clock()
    if not isinstance(completed, datetime) or completed.tzinfo is None:
        raise _fail("CLOCK_INVALID", "execution clock must return aware datetime")
    completed = completed.astimezone(UTC)
    if completed < started:
        raise _fail("CLOCK_INVALID", "execution clocks are causally inconsistent")
    if exit_code != 0 or stderr:
        raise _fail("BENCHMARK_REJECTED", "benchmark did not complete cleanly")
    if not stdout.endswith(b"\n") or stdout.count(b"\n") != 1:
        raise _fail("BENCHMARK_STDOUT_INVALID", "benchmark stdout is not one canonical JSON line")
    report_raw = stdout[:-1]
    if not report_raw or len(report_raw) > _MAX_REPORT_BYTES:
        raise _fail("BENCHMARK_STDOUT_INVALID", "benchmark report is empty or oversized")
    report = _strict_json(report_raw, field="benchmark_report")
    report_sha = hashlib.sha256(report_raw).hexdigest()
    report_path = _report_path(output_root, report, report_sha)
    if report_path.read_bytes() != report_raw:
        raise _fail("BENCHMARK_REPORT_MISMATCH", "stdout differs from retained report bytes")
    if not (
        report.get("benchmark_schema_version")
        == manifest.get("expected_benchmark_schema_version")
        and report.get("acceptance") == {"accepted": True, "reasons": []}
        and report.get("capture_runtime_source")
        == {
            "benchmark_script_sha256": next(
                row["sha256"]
                for row in manifest["source_roster"]
                if row["role"] == "benchmark"
            ),
            "contract_sha256": next(
                row["sha256"]
                for row in manifest["source_roster"]
                if row["role"] == "contract"
            ),
            "first_dip_tape_policy_sha256": next(
                row["sha256"]
                for row in manifest["source_roster"]
                if row["role"] == "first_dip_tape_policy"
            ),
            "pressure_probe_sha256": next(
                row["sha256"]
                for row in manifest["source_roster"]
                if row["role"] == "pressure_probe"
            ),
            "replay_errors_sha256": next(
                row["sha256"]
                for row in manifest["source_roster"]
                if row["role"] == "replay_errors"
            ),
            "runtime_sha256": next(
                row["sha256"]
                for row in manifest["source_roster"]
                if row["role"] == "runtime"
            ),
            "stage0_sha256": next(
                row["sha256"]
                for row in manifest["source_roster"]
                if row["role"] == "stage0"
            ),
        }
    ):
        raise _fail("BENCHMARK_REPORT_BINDING_INVALID", "benchmark report source binding differs")
    environment_projection = report.get("environment")
    if not isinstance(environment_projection, Mapping) or not (
        environment_projection.get("benchmark_authority_manifest_sha256") == manifest_sha
        and environment_projection.get("dependency_root_identity_sha256")
        == manifest["python_dependency_root"]["identity_sha256"]
        and environment_projection.get("python_executable_sha256")
        == manifest["python"]["executable_sha256"]
    ):
        raise _fail("BENCHMARK_REPORT_BINDING_INVALID", "benchmark environment binding differs")

    # The output root was empty before launch and exact report containment was
    # verified above.  Re-check immutable authority after the child exits.
    _validate_static_bindings(
        receipt=launch_receipt,
        manifest=manifest,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        require_empty_output=False,
    )
    children = tuple(output_root.iterdir())
    if children != (report_path.parent.parent,):
        raise _fail(
            "BENCHMARK_OUTPUT_INVENTORY_INVALID",
            "benchmark produced objects outside its one owned directory",
        )
    if launch_receipt_path.read_bytes() == b"" or manifest_path.read_bytes() == b"":
        raise _fail("AUTHORITY_DRIFT", "launch authority disappeared after execution")

    execution_document = {
        "account_scope": authority.ACCOUNT_SCOPE,
        "authority_programs": manifest["authority_programs"],
        "argv_is_shell_string": False,
        "authority_mode": authority.AUTHORITY_MODE,
        "benchmark": {
            "acceptance": {"accepted": True, "reasons": []},
            "exit_code": 0,
            "report": {"path": str(report_path), "sha256": report_sha},
            "schema_version": report["benchmark_schema_version"],
            "stderr_bytes": 0,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stdout_without_newline_sha256": report_sha,
        },
        "benchmark_completed": True,
        "completed_at_utc": completed.isoformat().replace("+00:00", "Z"),
        "duration_seconds": (completed - started).total_seconds(),
        "execution_context": launch_receipt["execution_context"],
        "execution_claim": {"path": str(claim_path), "sha256": claim_sha},
        "expected_git_commit": manifest["expected_git_commit"],
        "git": manifest["git"],
        "invoked": True,
        "launch_receipt": {
            "path": str(launch_receipt_path),
            "sha256": launch_receipt_sha,
        },
        "manifest": {"path": str(manifest_path), "sha256": manifest_sha},
        "posture": {
            "benchmark_output_written": True,
            "broker_contacted": False,
            "cutover_performed": False,
            "database_accessed": False,
            "host_activation_performed": False,
            "live_cash_authorized": False,
            "orders_submitted": False,
            "provider_contacted": False,
            "task_scheduler_mutated": False,
        },
        "schema_version": EXECUTION_RECEIPT_SCHEMA_VERSION,
        "started_at_utc": started.isoformat().replace("+00:00", "Z"),
    }
    execution_raw = _canonical_json_bytes(execution_document)

    # The execution receipt is the terminal capability consumed downstream.
    # Revalidate every byte it binds before making that receipt visible; once
    # publication succeeds, returning the immutable address is the only work
    # left in this function.
    expected_claim_raw = _canonical_json_bytes(
        {
            "launch_receipt": {
                "path": str(launch_receipt_path),
                "sha256": launch_receipt_sha,
            },
            "manifest": {"path": str(manifest_path), "sha256": manifest_sha},
            "schema_version": EXECUTION_CLAIM_SCHEMA_VERSION,
            "started_at_utc": started.isoformat().replace("+00:00", "Z"),
        }
    )
    if (
        report_path.read_bytes() != report_raw
        or launch_receipt_path.read_bytes()
        != authority._canonical_json_bytes(dict(launch_receipt))
        or manifest_path.read_bytes() != authority._canonical_json_bytes(dict(manifest))
        or claim_path.read_bytes() != expected_claim_raw
        or hashlib.sha256(expected_claim_raw).hexdigest() != claim_sha
    ):
        raise _fail("TERMINAL_AUTHORITY_DRIFT", "terminal authority bytes changed")
    try:
        execution_path, execution_sha = authority._publish_new_json(
            authority_root, kind="execution-receipt", raw=execution_raw
        )
    except authority.CapturedPaperBenchmarkAuthorityError as exc:
        raise _fail(
            "EXECUTION_RECEIPT_PUBLISH_FAILED",
            "terminal execution receipt could not be published",
        ) from exc
    return execution_path, execution_sha


def run_captured_paper_benchmark_authority(
    *,
    receipt_path: str | Path,
    receipt_sha256: str,
    executor: Any = _bounded_run,
    clock: Any = lambda: datetime.now(UTC),
) -> tuple[Path, str]:
    """Execute one exact authority while sanitizing shared-validator failures."""

    try:
        return _run_captured_paper_benchmark_authority(
            receipt_path=receipt_path,
            receipt_sha256=receipt_sha256,
            executor=executor,
            clock=clock,
        )
    except CapturedPaperBenchmarkExecutionError:
        raise
    except authority.CapturedPaperBenchmarkAuthorityError as exc:
        raise _fail(
            "AUTHORITY_VALIDATION_FAILED", "benchmark authority validation failed"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--receipt-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        path, digest = run_captured_paper_benchmark_authority(
            receipt_path=arguments.receipt,
            receipt_sha256=arguments.receipt_sha256,
        )
    except CapturedPaperBenchmarkExecutionError as exc:
        sys.stderr.write(exc.code + "\n")
        return 2
    sys.stdout.buffer.write(
        _canonical_json_bytes(
            {
                "execution_receipt": {"path": str(path), "sha256": digest},
                "schema_version": EXECUTION_RECEIPT_SCHEMA_VERSION,
            }
        )
        + b"\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
